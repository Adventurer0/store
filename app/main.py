import asyncio
import csv
import hashlib
import io
import json
import os
import secrets
import sqlite3
import threading
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Request, Depends, HTTPException, UploadFile, File, Form, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import ValidationError
from . import db, jobs, inventory
from .models import Login, StoreInput, ProductInput, ListingInput, Batch, Note, JobAction, Resolve, Allocation, InventoryAdjustment, CommercePrepareInput
from .security import require_user, session, encrypt, cipher, password_hash, check_password, token_hash, redact
from .connectors import PLATFORMS, Blocked, mpau_available
from .commerce_browser import available as commerce_browser_available
from .publish_workbench_routes import router as publishing_router
from .commerce_readiness import describe as describe_commerce_readiness
from . import media_library

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "1024")) * 1024 * 1024
login_attempts = defaultdict(deque)
attempt_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app):
    db.init()
    cipher()
    worker = jobs.Worker()
    if os.environ.get("DISABLE_WORKER") != "1":
        worker.start()
    app.state.worker = worker
    yield
    worker.stop()


app = FastAPI(title="ShopDock 本地多店工作台", version="0.3.7", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(","))
app.include_router(publishing_router)


@app.middleware("http")
async def security_headers(request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
        limit = MAX_UPLOAD + 1024 * 1024 if request.url.path in ("/api/media", "/api/media/import") else 5 * 1024 * 1024
        length = request.headers.get("content-length")
        if length is None and request.method != "DELETE":
            return JSONResponse({"detail": "上传请求须提供 Content-Length"}, status_code=411)
        try:
            if int(length or 0) > limit or int(length or 0) < 0:
                return JSONResponse({"detail": "请求文件过大"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "无效的 Content-Length"}, status_code=400)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' ws: wss:; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'"
    if request.url.path.startswith("/api/") or request.url.path.startswith("/desktop/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path == "/" or request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(Blocked)
async def blocked(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=409)


app.include_router(media_library.router)


def must_store(store_id, secrets=False):
    store = db.get_store(store_id, secrets)
    if not store:
        raise HTTPException(404, "店铺不存在")
    return store


def must_product(product_id):
    row = db.get_product(product_id)
    if not row:
        raise HTTPException(404, "商品不存在")
    return row


@app.get("/healthz")
def health():
    return {"status": "ok", "version": "0.3.7"}


@app.get("/api/auth/status")
def auth_status(request: Request):
    user = db.one("SELECT username FROM users WHERE id=1")
    s = session(request.cookies.get("sd_session"))
    return {"initialized": bool(user), "authenticated": bool(s), "csrf": s["csrf"] if s else "", "username": user["username"] if s else ""}


def set_session():
    raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with db.connection() as c:
        c.execute("DELETE FROM sessions WHERE expires<=?", (db.now(),))
        c.execute("INSERT INTO sessions VALUES(?,?,?)", (token_hash(raw), csrf, db.now() + 12 * 3600))
    response = JSONResponse({"csrf": csrf, "ok": True})
    response.set_cookie("sd_session", raw, httponly=True, samesite="strict", secure=os.environ.get("COOKIE_SECURE") == "1", max_age=12 * 3600)
    return response


@app.post("/api/auth/setup")
def setup(body: Login):
    if len(body.password) < 12:
        raise HTTPException(422, "管理员密码至少 12 位")
    with db.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM users").fetchone():
            raise HTTPException(409, "管理员已创建，请登录")
        c.execute("INSERT INTO users VALUES(1,?,?)", (body.username, password_hash(body.password)))
    db.audit("创建管理员", {})
    return set_session()


@app.post("/api/auth/login")
def login(request: Request, body: Login):
    ip = request.client.host
    with attempt_lock:
        attempts = login_attempts[ip]
        while attempts and attempts[0] < db.now() - 600:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HTTPException(429, "尝试次数过多，请 10 分钟后重试")
        attempts.append(db.now())
    user = db.one("SELECT * FROM users WHERE id=1")
    if not user or not secrets.compare_digest(body.username, user["username"]) or not check_password(body.password, user["password"]):
        raise HTTPException(401, "账号或密码不正确")
    with attempt_lock:
        login_attempts.pop(ip, None)
    return set_session()


@app.post("/api/auth/logout")
def logout(request: Request, s=Depends(require_user)):
    with db.connection() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (s["token"],))
    response = JSONResponse({"ok": True})
    response.delete_cookie("sd_session")
    return response


@app.get("/api/state", dependencies=[Depends(require_user)])
def state():
    stores = [db.get_store(r["id"]) for r in db.all_rows("SELECT id FROM stores ORDER BY created")]
    products = [db.get_product(r["id"]) for r in db.all_rows("SELECT id FROM products ORDER BY updated DESC LIMIT 1000")]
    pending = db.one("SELECT count(*) n FROM jobs WHERE status IN ('queued','running')")["n"]
    issues = db.one("SELECT count(*) n FROM jobs WHERE status IN ('blocked','failed','uncertain')")["n"]
    orders = db.one("SELECT count(*) n FROM orders")["n"]
    return {"stores": stores, "products": products, "listings": [inventory.listing(r) for r in db.all_rows("SELECT * FROM listings")],
            "media": media_library.state_rows(),
            "stats": {"products": db.one("SELECT count(*) n FROM products")["n"], "orders": orders, "pending": pending, "issues": issues},
            "platforms": PLATFORMS, "engine_ready": mpau_available(), "desktop_ready": Path("/opt/novnc/vnc.html").exists(),
            "commerce_browser_ready": commerce_browser_available(),
            "max_upload_mb": MAX_UPLOAD // (1024 * 1024), "version": "0.3.7"}


@app.post("/api/stores", dependencies=[Depends(require_user)])
def create_store(body: StoreInput):
    sid = db.uid()
    with db.connection() as c:
        c.execute("INSERT INTO stores(id,name,platform,api_enabled,video_enabled,credentials,templates,created,commerce_enabled) VALUES(?,?,?,?,?,?,?,?,?)", (sid, body.name, body.platform, 0, body.video_enabled, encrypt(body.credentials), db.dumps(body.templates), db.now(), body.commerce_enabled))
    db.audit("添加店铺", {"store_id": sid, "name": body.name})
    return db.get_store(sid)


@app.put("/api/stores/{sid}", dependencies=[Depends(require_user)])
def update_store(sid: str, body: StoreInput):
    old = must_store(sid, secrets=True)
    if body.platform != old["platform"]:
        raise HTTPException(409, "已有店铺的平台不能更改，请新建店铺")
    credentials = old["credentials"]
    credentials.update({k: v for k, v in body.credentials.items() if v})
    with db.connection() as c:
        c.execute("UPDATE stores SET name=?,api_enabled=0,commerce_enabled=?,video_enabled=?,credentials=?,templates=? WHERE id=?", (body.name, body.commerce_enabled, body.video_enabled, encrypt(credentials), db.dumps(body.templates), sid))
    db.audit("更新店铺设置", {"store_id": sid})
    return db.get_store(sid)


@app.post("/api/stores/{sid}/account/{action}", dependencies=[Depends(require_user)])
def account(sid: str, action: str):
    store = must_store(sid)
    if action not in ("login", "check"):
        raise HTTPException(404, "未知操作")
    return jobs.account_job(store, "account_" + action)


@app.post("/api/stores/{sid}/commerce/prepare-product", dependencies=[Depends(require_user)])
def prepare_commerce_product(sid: str, body: CommercePrepareInput):
    store, product = must_store(sid), must_product(body.product_id)
    business = body.business.model_dump(exclude_unset=True) if body.business is not None else None
    return jobs.prepare_product_job(store, product, body.upload_images, business)


@app.post("/api/stores/{sid}/commerce/{action}", dependencies=[Depends(require_user)])
def commerce_account(sid: str, action: str):
    store = must_store(sid)
    if action not in ("login", "check", "logout"):
        raise HTTPException(404, "未知电商账号操作")
    return jobs.commerce_account_job(store, "commerce_" + action)


@app.post("/api/stores/{sid}/commerce/login/confirm", dependencies=[Depends(require_user)])
def commerce_login_confirm(sid: str):
    must_store(sid)
    active = db.one("SELECT id,status FROM jobs WHERE store_id=? AND kind='commerce_login' "
                    "AND status='running' ORDER BY created DESC LIMIT 1", (sid,))
    if not active:
        raise HTTPException(409, "登录浏览器尚未启动或登录任务已结束；请等待页面打开并完成扫码")
    from .commerce_browser import confirm_login
    confirm_login(sid)
    return {"ok": True, "job_id": active["id"],
            "message": "已请求结束登录任务；浏览器核对页面后会自动关闭"}


@app.get("/api/stores/{sid}/commerce/readiness", dependencies=[Depends(require_user)])
def commerce_readiness(sid: str):
    return describe_commerce_readiness(must_store(sid))


@app.post("/api/stores/{sid}/verify", dependencies=[Depends(require_user)])
def verify(sid: str, body: Resolve):
    store = must_store(sid)
    if store["platform"] != "douyin":
        raise HTTPException(422, "此验证码入口仅用于抖音")
    active = db.one("SELECT id FROM jobs WHERE store_id=? AND kind='video_publish' AND status='running'", (sid,))
    if not active or not mpau_available():
        raise HTTPException(409, "没有正在执行的抖音发布任务")
    import subprocess
    try:
        subprocess.run(["mpau", "douyin", "verify", "--account", sid, "--code", body.code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
    except (subprocess.SubprocessError, OSError):
        raise HTTPException(409, "验证码提交失败，请在扫码窗口完成验证") from None
    return {"ok": True}


def save_product(body, pid=None, connection=None):
    data = body.model_dump(exclude={"version"})
    if pid:
        if body.version is None:
            raise HTTPException(422, "编辑商品须提供 version，以避免覆盖其他修改")
        with db.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            media_library.validate_product_media(c, data)
            inventory.validate_stock(c, pid, data)
            old = c.execute('SELECT data FROM products WHERE id=?', (pid,)).fetchone()
            if old:
                previous = {s['code']: s['stock'] for s in json.loads(old['data'])['skus']}
                for s in data['skus']:
                    delta = s['stock'] - previous.get(s['code'], 0)
                    if delta:
                        c.execute('INSERT INTO inventory_ledger(created,product_id,sku_code,delta,reason) VALUES(?,?,?,?,?)', (db.now(), pid, s['code'], delta, '商品编辑调整库存'))
            result = c.execute("UPDATE products SET data=?,version=version+1,updated=? WHERE id=? AND version=?", (db.dumps(data), db.now(), pid, body.version))
            if not result.rowcount:
                raise HTTPException(409, "商品已经被修改，请刷新后重试")
    else:
        pid = db.uid()
        with db.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            media_library.validate_product_media(c, data)
            c.execute("INSERT INTO products(id,data,created,updated) VALUES(?,?,?,?)", (pid, db.dumps(data), db.now(), db.now()))
    return db.get_product(pid)


@app.post("/api/products", dependencies=[Depends(require_user)])
def create_product(body: ProductInput):
    result = save_product(body)
    db.audit("创建商品", {"product_id": result["id"]})
    return result


@app.put("/api/products/{pid}", dependencies=[Depends(require_user)])
def update_product(pid: str, body: ProductInput):
    must_product(pid)
    result = save_product(body, pid)
    db.audit("更新本地商品", {"product_id": pid})
    return result


@app.delete("/api/products/{pid}", dependencies=[Depends(require_user)])
def delete_product(pid: str):
    must_product(pid)
    with db.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        active = c.execute("SELECT 1 FROM jobs WHERE (entity_id=? OR json_extract(payload,'$.product_id')=?) AND status IN ('queued','running','uncertain','submitted')", (pid, pid)).fetchone()
        if active:
            raise HTTPException(409, "商品还有未完成或待核对的任务，不能删除")
        c.execute("DELETE FROM listings WHERE product_id=?", (pid,))
        if c.execute('SELECT 1 FROM allocations WHERE product_id=? UNION SELECT 1 FROM inventory_effects WHERE product_id=?', (pid, pid)).fetchone():
            raise HTTPException(409, '商品已有库存分配或历史扣库记录，请保留用于对账')
        c.execute("DELETE FROM products WHERE id=?", (pid,))
    db.audit("删除本地商品", {"product_id": pid})
    return {"ok": True, "message": "本地商品已删除，平台商品未受影响"}


@app.put("/api/products/{pid}/listings/{sid}", dependencies=[Depends(require_user)])
def set_listing(pid: str, sid: str, body: ListingInput):
    must_store(sid)
    product = must_product(pid)
    if set(body.sku_map) - {s['code'] for s in product['skus']} or any(not str(v) or len(str(v)) > 100 for v in body.sku_map.values()) or len(set(body.sku_map.values())) != len(body.sku_map):
        raise HTTPException(422, 'SKU 映射包含未知编码、空 ID 或重复 ID')
    with db.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        old = inventory.listing(c.execute('SELECT * FROM listings WHERE product_id=? AND store_id=?', (pid, sid)).fetchone())
        if old.get('external_id') and (old['external_id'] != body.external_id or any(k in body.sku_map and body.sku_map[k] != v for k,v in old['sku_map'].items()) or set(old['sku_map']) - set(body.sku_map)):
            if c.execute('SELECT 1 FROM allocations WHERE product_id=? AND store_id=? AND max(quantity,remote_quantity)>0 UNION SELECT 1 FROM inventory_effects WHERE product_id=? AND store_id=?', (pid, sid, pid, sid)).fetchone():
                raise HTTPException(409, '该关联已有库存 / 扣库记录，不能改绑商品或已有 SKU；可以补齐尚未映射的 SKU')
        if body.external_id and c.execute('SELECT 1 FROM listings WHERE store_id=? AND external_id=? AND product_id<>?', (sid, body.external_id, pid)).fetchone():
            raise HTTPException(409, '该平台商品已经关联另一件本地商品')
        c.execute('''INSERT INTO listings(product_id,store_id,external_id,product_link,status,updated) VALUES(?,?,?,?,?,?)
        ON CONFLICT(product_id,store_id) DO UPDATE SET external_id=excluded.external_id,product_link=excluded.product_link,status='mapped',updated=excluded.updated''',
                  (pid, sid, body.external_id, body.product_link, "mapped", db.now()))
        c.execute('UPDATE listings SET publish_config=?,sku_map=?,owned_product=? WHERE product_id=? AND store_id=?', (db.dumps(body.publish_config), db.dumps(body.sku_map), body.owned_product, pid, sid))
    db.audit("关联平台商品", {"product_id": pid, "store_id": sid})
    return {"ok": True}


@app.get('/api/inventory', dependencies=[Depends(require_user)])
def inventory_state():
    return inventory.snapshot()


@app.post('/api/products/{pid}/listings/{sid}/fetch-skus', dependencies=[Depends(require_user)])
def fetch_skus(pid: str, sid: str):
    product, store = must_product(pid), must_store(sid, secrets=True)
    from .publishing import fetch_sku_map
    from .connectors import Rejected
    mapped = inventory.listing(db.one('SELECT * FROM listings WHERE product_id=? AND store_id=?', (pid, sid)))
    try:
        found = fetch_sku_map(store, product, mapped)
    except (Rejected, ValueError) as exc:
        raise HTTPException(409, '读取平台 SKU 失败：' + str(exc)) from None
    # Preserve identity and use the same validation as manual mapping.
    set_listing(pid, sid, ListingInput(external_id=mapped['external_id'], product_link=mapped.get('product_link', ''),
                publish_config=mapped['publish_config'], sku_map={**mapped['sku_map'], **found}, owned_product=bool(mapped.get('owned_product'))))
    return {'sku_map': found}


@app.put('/api/products/{pid}/allocations', dependencies=[Depends(require_user)])
def allocations(pid: str, body: Allocation):
    must_product(pid)
    inventory.allocate(pid, body.version, body.quantities)
    return {'ok': True}


@app.post('/api/products/{pid}/inventory-adjustment', dependencies=[Depends(require_user)])
def inventory_adjustment(pid: str, body: InventoryAdjustment):
    must_product(pid)
    inventory.adjust(pid, body)
    return {'ok': True}


@app.post('/api/inventory/reconcile', dependencies=[Depends(require_user)])
def reconcile_inventory():
    count = 0
    for order in db.all_rows("SELECT * FROM orders WHERE source IN ('api','browser')"):
        db.upsert_order(order['store_id'], json.loads(order['data']), source=order['source'])
        count += 1
    return {'count': count}


@app.post('/api/jobs/{jid}/remote-check', dependencies=[Depends(require_user)])
def check_ks_video(jid: str):
    job = db.one('SELECT * FROM jobs WHERE id=?', (jid,))
    if not job or job['kind'] != 'video_publish' or job['status'] not in ('submitted', 'succeeded'):
        raise HTTPException(409, '此任务尚无可查询的快手视频回执')
    result = json.loads(job['result'])
    if not result.get('photo_id'):
        raise HTTPException(422, '任务没有快手 photo_id')
    from .ks_video import KuaishouVideo
    from .connectors import Rejected
    import httpx
    api = KuaishouVideo(must_store(job['store_id'], secrets=True))
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            response = api.request(client, 'GET', 'https://open.kuaishou.com/openapi/photo/info', params={**api.params, 'photo_id': result['photo_id']})
    except Rejected as exc:
        raise HTTPException(409, str(exc)) from None
    info = response.get('video_info', {})
    result.update({'remote_pending': info.get('pending', True), 'message': '作品仍在处理' if info.get('pending', True) else '已查询到可播放作品；官方查询未返回挂商品信息，仍需到后台确认小黄车'})
    with db.connection() as c:
        c.execute('UPDATE jobs SET result=?,updated=? WHERE id=?', (db.dumps(result), db.now(), jid))
    return result


@app.post("/api/media", dependencies=[Depends(require_user)])
async def upload(file: UploadFile = File(...), category: str = Form(''),
                 tags: str = Form('[]'), relative_path: str = Form('')):
    return await media_library.store_upload(file, category, tags, relative_path, max_bytes=MAX_UPLOAD)


@app.get("/api/media/{mid}", dependencies=[Depends(require_user)])
def get_media(mid: str):
    media = db.one("SELECT * FROM media WHERE id=?", (mid,))
    if not media:
        raise HTTPException(404, "素材不存在")
    path = media_library.media_path(media['path'])
    if not path.is_file():
        raise HTTPException(404, "素材不存在")
    return FileResponse(path, media_type=media["mime"], filename=media["name"], content_disposition_type="inline")


@app.get("/api/orders", dependencies=[Depends(require_user)])
def orders(q: str = "", store_id: str = "", offset: int = 0, limit: int = 50):
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    where, args = ["1=1"], []
    if store_id:
        where.append("store_id=?")
        args.append(store_id)
    if q:
        where.append("(external_id LIKE ? OR data LIKE ? OR note LIKE ?)")
        args += ["%" + q[:200] + "%"] * 3
    condition = " AND ".join(where)
    count = db.one("SELECT count(*) n FROM orders WHERE " + condition, args)["n"]
    rows = db.all_rows("SELECT * FROM orders WHERE " + condition + " ORDER BY updated DESC,id LIMIT ? OFFSET ?", args + [limit, offset])
    for row in rows:
        row["data"] = json.loads(row["data"])
    return {"rows": rows, "total": count, "offset": offset, "limit": limit}


@app.put("/api/orders/{oid}/note", dependencies=[Depends(require_user)])
def order_note(oid: str, body: Note):
    with db.connection() as c:
        row = c.execute("UPDATE orders SET note=? WHERE id=?", (body.note, oid))
        if not row.rowcount:
            raise HTTPException(404, "订单不存在")
    return {"ok": True}


def safe_csv_cell(value):
    value = "" if value is None else str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r")) else value


def csv_response(name, columns, rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([safe_csv_cell(v) for v in row])
    return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


@app.get("/api/export/{kind}", dependencies=[Depends(require_user)])
def export(kind: str):
    if kind == "orders":
        rows = db.all_rows("SELECT * FROM orders ORDER BY updated DESC")
        return csv_response("orders", ["store_id", "order_id", "status", "amount_cents", "title", "quantity", "note"], ([r["store_id"], r["external_id"], (d := json.loads(r["data"]))["status"], d.get("amount_cents"), " / ".join(x.get("title", "") for x in d.get("items", [])), sum(x.get("quantity", 0) for x in d.get("items", [])), r["note"]] for r in rows))
    if kind == "products":
        rows = [db.get_product(r["id"]) for r in db.all_rows("SELECT id FROM products")]
        return csv_response("products", ["product_key", "title", "category", "description", "sku_code", "sku_name", "price_cents", "stock"], ([p["id"], p["title"], p["category"], p["description"], s["code"], s["name"], s["price_cents"], s["stock"]] for p in rows for s in p["skus"]))
    if kind == "order-template":
        return csv_response("order-template", ["order_id", "status", "amount_cents", "title", "quantity"], [["EXAMPLE-001", "待发货", 12900, "示例商品（请替换）", 1]])
    if kind == "product-template":
        return csv_response("product-template", ["product_key", "title", "category", "description", "sku_code", "sku_name", "price_cents", "stock"], [["EXAMPLE-001", "示例商品（请替换）", "箱包", "商品描述", "SKU-001", "银色", 12900, 10]])
    raise HTTPException(404, "未知导出类型")


async def read_csv(file):
    raw = await file.read(5 * 1024 * 1024 + 1)
    await file.close()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, "CSV 最多 5 MB")
    try:
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    except (UnicodeError, csv.Error):
        raise HTTPException(422, "请使用 UTF-8 编码的 CSV") from None
    if not rows or len(rows) > 10000:
        raise HTTPException(422, "CSV 必须包含 1–10000 行")
    return rows


@app.post("/api/import/orders/{sid}", dependencies=[Depends(require_user)])
async def import_orders(sid: str, file: UploadFile = File(...)):
    must_store(sid)
    rows = await read_csv(file)
    valid, seen = [], set()
    try:
        for line, r in enumerate(rows, 2):
            oid = r["order_id"].strip()
            if not oid or len(oid) > 100 or oid in seen:
                raise ValueError("订单号为空、过长或重复；请先合并同单多行")
            amount, quantity = int(r["amount_cents"]), int(r["quantity"])
            if not 0 <= amount <= 10**11 or not 0 <= quantity <= 10**8:
                raise ValueError("金额或数量超出范围")
            valid.append({"external_id": oid, "status": r["status"][:100], "amount_cents": amount, "items": [{"title": r["title"][:200], "quantity": quantity}], "created": 0})
            seen.add(oid)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(422, f"第 {line} 行无效：{str(exc)[:120]}；没有导入任何记录") from None
    # Validate all rows before a single transaction; preserve API records and local notes.
    count = 0
    with db.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        for data in valid:
            row = c.execute("SELECT source FROM orders WHERE store_id=? AND external_id=?", (sid, data["external_id"])).fetchone()
            if row and row["source"] in ("api", "browser"):
                continue
            c.execute('''INSERT INTO orders(id,store_id,external_id,data,source,updated) VALUES(?,?,?,?,?,?)
            ON CONFLICT(store_id,external_id) DO UPDATE SET data=excluded.data,updated=excluded.updated''', (db.uid(), sid, data["external_id"], db.dumps(data), "csv", db.now()))
            count += 1
    db.audit("导入 CSV 订单", {"store_id": sid, "count": count})
    return {"count": count, "skipped": len(valid) - count}


@app.post("/api/import/products", dependencies=[Depends(require_user)])
async def import_products(file: UploadFile = File(...)):
    rows = await read_csv(file)
    groups = {}
    try:
        for line, r in enumerate(rows, 2):
            key = r["product_key"].strip()
            if not key:
                raise ValueError("product_key 必填")
            base = {"title": r["title"], "description": r.get("description", ""), "category": r.get("category", "")}
            if key not in groups:
                groups[key] = {**base, "skus": []}
            elif any(groups[key][k] != v for k, v in base.items()):
                raise ValueError("同一 product_key 的标题、描述、分类不一致")
            groups[key]["skus"].append({"code": r["sku_code"], "name": r["sku_name"], "price_cents": int(r["price_cents"]), "stock": int(r["stock"])})
        valid = [(key, ProductInput.model_validate(v).model_dump(exclude={"version"})) for key, v in groups.items()]
    except (KeyError, ValueError, ValidationError, TypeError) as exc:
        raise HTTPException(422, f"第 {line} 行或 SKU 组无效：{str(exc)[:180]}；没有导入任何记录") from None
    # Store import keys separately in extra; importing the same key never duplicates a product.
    with db.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        for key, data in valid:
            old = c.execute("SELECT * FROM products WHERE id=? OR json_extract(data,'$.extra.csv_key')=?", (key, key)).fetchone()
            if old:
                existing = json.loads(old["data"])
                existing.update({k: data[k] for k in ("title", "description", "category", "skus")})
                inventory.validate_stock(c, old['id'], existing)
                previous = {s['code']: s['stock'] for s in json.loads(old['data'])['skus']}
                for sku in existing['skus']:
                    delta = sku['stock'] - previous.get(sku['code'], 0)
                    if delta:
                        c.execute('INSERT INTO inventory_ledger(created,product_id,sku_code,delta,reason) VALUES(?,?,?,?,?)', (db.now(), old['id'], sku['code'], delta, 'CSV 导入调整库存'))
                c.execute("UPDATE products SET data=?,version=version+1,updated=? WHERE id=?", (db.dumps(existing), db.now(), old["id"]))
            else:
                data["extra"]["csv_key"] = key
                c.execute("INSERT INTO products(id,data,created,updated) VALUES(?,?,?,?)", (db.uid(), db.dumps(data), db.now(), db.now()))
    return {"count": len(valid)}


@app.post("/api/jobs/preflight", dependencies=[Depends(require_user)])
def preflight(body: Batch):
    try:
        plan = jobs.plans(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"items": [{k: v for k, v in x.items() if k != "dedupe_key"} for x in plan], "ready": sum(not x["problem"] for x in plan)}


@app.post("/api/jobs", dependencies=[Depends(require_user)])
def create_jobs(body: Batch):
    try:
        return jobs.enqueue(jobs.plans(body))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@app.get("/api/jobs", dependencies=[Depends(require_user)])
def list_jobs(offset: int = 0):
    rows = db.all_rows("SELECT j.*,s.name store_name,s.platform FROM jobs j JOIN stores s ON s.id=j.store_id ORDER BY j.created DESC,j.id DESC LIMIT 100 OFFSET ?", (max(0, offset),))
    for row in rows:
        row["payload"] = json.loads(row["payload"])
        row["result"] = json.loads(row["result"])
    return {"rows": rows, "total": db.one("SELECT count(*) n FROM jobs")["n"]}


@app.get("/api/jobs/{jid}/log", dependencies=[Depends(require_user)])
def job_log(jid: str):
    row = db.one("SELECT id FROM jobs WHERE id=?", (jid,))
    if not row:
        raise HTTPException(404, "任务不存在")
    path = db.DATA / "logs" / (row["id"] + ".log")
    if not path.exists():
        return {"text": "此任务没有浏览器引擎日志。"}
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - 24000))
        return {"text": redact(f.read().decode("utf-8", "replace"))}


@app.get('/api/jobs/{jid}/preview', dependencies=[Depends(require_user)])
def commerce_preview(jid: str):
    job = db.one('SELECT kind,status,result FROM jobs WHERE id=?', (jid,))
    if not job or job['kind'] != 'commerce_prepare_product':
        raise HTTPException(404, '填表预览任务不存在')
    from .commerce_preview import read_marker
    try:
        result = json.loads(job['result'])
    except (ValueError, TypeError):
        result = {}
    message = result.get('message', '') if isinstance(result, dict) else ''
    message = redact(message) if isinstance(message, str) else ''
    try:
        return read_marker(db.DATA, jid, job['status'] in ('queued', 'running'),
                           job_status=job['status'], job_message=message)
    except (ValueError, OSError):
        raise HTTPException(409, '预览记录不可用，请查看任务状态或重新打开预览') from None


@app.post('/api/jobs/{jid}/preview/close', dependencies=[Depends(require_user)])
def close_commerce_preview(jid: str):
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        job = conn.execute('SELECT kind,status FROM jobs WHERE id=?', (jid,)).fetchone()
        if not job or job['kind'] != 'commerce_prepare_product':
            raise HTTPException(404, '填表预览任务不存在')
        if job['status'] == 'queued':
            conn.execute("UPDATE jobs SET status='cancelled',result=?,updated=? WHERE id=?",
                         (db.dumps({'message': '填表预览已取消，尚未打开商家表单'}), db.now(), jid))
    from .commerce_preview import request_close
    try:
        request_close(db.DATA, jid)
    except (ValueError, OSError):
        raise HTTPException(409, '无法结束此预览，请在扫码窗口关闭本次浏览器') from None
    db.audit('结束快手填表预览', {'job_id': jid})
    return {'ok': True, 'closing': True}


@app.post("/api/jobs/{jid}/action", dependencies=[Depends(require_user)])
def job_action(jid: str, body: JobAction):
    with db.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        if row["status"] == "running":
            raise HTTPException(409, "任务正在执行；不能在远端状态未知时更改任务")
        if body.action == "cancel":
            if row["status"] not in ("queued", "blocked", "failed"):
                raise HTTPException(409, "只能取消尚未执行或明确失败的任务")
            c.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=?", (db.now(), jid))
        elif body.action in ("confirm", "mark_failed"):
            if row["status"] not in ("submitted", "uncertain") or not body.evidence.strip():
                raise HTTPException(422, "请填写平台商品 ID、作品链接或核对说明")
            if body.action == "mark_failed" and not body.checked_remote:
                raise HTTPException(409, "请确认已在平台后台核对未发布成功")
            status = "succeeded" if body.action == "confirm" else "failed"
            c.execute("UPDATE jobs SET status=?,result=?,updated=? WHERE id=?", (status, db.dumps({"message": "用户已在平台后台核对：" + ("成功" if status == "succeeded" else "未发布成功"), "evidence": body.evidence}), db.now(), jid))
        else:
            if row["status"] not in ("blocked", "failed", "uncertain"):
                raise HTTPException(409, "只有待配置、失败或结果未知的任务可以重试")
            if row["status"] == "uncertain" and not body.checked_remote:
                raise HTTPException(409, "须先核对平台后台，确认没有发布成功后才能重试")
            if row["status"] == "blocked" and (row["kind"].startswith("product_") or row["kind"] == "video_publish"):
                raise HTTPException(409, "请修正配置或商品关联后重新选择商品 / 视频并预检；旧任务可取消")
            c.execute("UPDATE jobs SET status='queued',scheduled=?,result=?,updated=? WHERE id=?", (db.now(), db.dumps({"message": "等待用户发起的重试"}), db.now(), jid))
    db.audit("任务操作", {"job_id": jid, "action": body.action, "checked_remote": body.checked_remote})
    return {"ok": True}


@app.get("/api/audit", dependencies=[Depends(require_user)])
def audit():
    return db.all_rows("SELECT * FROM audit ORDER BY id DESC LIMIT 100")


@app.websocket("/desktop/websockify")
async def desktop_socket(ws: WebSocket):
    origin = urlsplit(ws.headers.get("origin", ""))
    if not session(ws.cookies.get("sd_session")) or origin.netloc != ws.headers.get("host"):
        await ws.close(code=1008)
        return
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", 5900)
    except OSError:
        await ws.close(code=1013)
        return
    await ws.accept(subprotocol="binary" if "binary" in ws.headers.get("sec-websocket-protocol", "") else None)
    async def browser_to_vnc():
        while True:
            data = await ws.receive_bytes()
            writer.write(data)
            await writer.drain()
    async def vnc_to_browser():
        while data := await reader.read(65536):
            await ws.send_bytes(data)
    tasks = [asyncio.create_task(browser_to_vnc()), asyncio.create_task(vnc_to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        await writer.wait_closed()
        try:
            await ws.close()
        except RuntimeError:
            pass


@app.get("/desktop/{path:path}", dependencies=[Depends(require_user)])
def desktop_files(path: str):
    root = Path("/opt/novnc").resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "浏览器桌面仅在完整 Docker 镜像中可用")
    return FileResponse(target)


@app.get("/assets/{path:path}")
def assets(path: str):
    target = (STATIC / path).resolve()
    if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
