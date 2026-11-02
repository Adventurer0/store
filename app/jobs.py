try:
    import fcntl
except ImportError:  # Windows has no fcntl; fall back to the bundled shim.
    from . import win_fcntl as fcntl
import hashlib
import json
import threading
import traceback
from . import db, inventory
from .ks_video import KuaishouVideo
from .commerce_browser import (CommerceBrowser, product_payload, check_session,
                               validate_workflow, available as commerce_available)
from .connectors import (Blocked, Rejected, Uncertain,
                         mpau_args, run_mpau, mpau_available, mpau_platform)


def plans(batch):
    specs = batch.model_dump()
    if len(set(batch.store_ids)) != len(batch.store_ids) or len(set(batch.product_ids)) != len(batch.product_ids):
        raise ValueError("店铺或商品不能重复选择")
    if (batch.kind.startswith("product_") or batch.kind == "inventory_sync") and not batch.product_ids:
        raise ValueError("请至少选择一件商品")
    if batch.kind == "video_publish" and not batch.title:
        raise ValueError("请填写视频标题")
    start, end = batch.since, batch.until
    if batch.kind == "order_sync":
        end = end or db.now()
        start = start or end - 86400
        if end > db.now() + 60 or end <= start or end - start > 7 * 86400:
            raise ValueError("订单同步时间必须在过去且跨度不超过 7 天")
    scheduled = batch.scheduled or db.now()
    if scheduled > db.now() + 90 * 86400:
        raise ValueError("任务最多可预约到 90 天后")
    result = []
    for store_id in batch.store_ids:
        store = db.get_store(store_id, secrets=True)
        if not store:
            raise ValueError("所选店铺不存在")
        entities = batch.product_ids if (batch.kind.startswith("product_") or batch.kind == "inventory_sync") else [batch.media_id if batch.kind == "video_publish" else ""]
        for entity in entities:
            payload, problem = {}, ""
            try:
                if batch.kind == 'inventory_sync':
                    product = db.get_product(entity)
                    if not product:
                        raise ValueError('商品不存在')
                    listing = inventory.listing(db.one('SELECT * FROM listings WHERE product_id=? AND store_id=?', (entity, store_id)))
                    payload = {'product_id': entity, 'listing': listing, 'sales_paused': batch.sales_paused}
                    if not batch.sales_paused:
                        raise Blocked('请先暂停目标商品销售、核对占用，再确认全量库存校准')
                    check_session(store)
                    validate_workflow(store, 'inventory_sync')
                    targets = inventory.sku_targets(product, store_id)
                    if not listing.get('external_id') or any(s['code'] not in listing['sku_map'] for s in targets):
                        raise Blocked('请先关联平台商品 ID 和每个 SKU 的平台 ID')
                    payload['targets'] = targets
                    payload['browser_rules'] = store['templates'].get('commerce_browser', {})
                elif batch.kind.startswith("product_"):
                    product = db.get_product(entity)
                    if not product:
                        raise ValueError("所选商品不存在")
                    listing = db.one("SELECT * FROM listings WHERE product_id=? AND store_id=?", (entity, store_id)) or {}
                    listing = inventory.listing(listing)
                    payload = {"product": product, "listing": listing}
                    payload["request"] = product_payload(store, product, listing, batch.kind)
                    payload['browser_rules'] = store['templates'].get('commerce_browser', {})
                elif batch.kind == "order_sync":
                    payload = {"since": start, "until": end}
                    check_session(store)
                    validate_workflow(store, 'orders')
                else:
                    media = db.one("SELECT * FROM media WHERE id=? AND kind='video'", (batch.media_id,))
                    if not media:
                        raise ValueError("请选择已上传的视频")
                    listing = db.one("SELECT * FROM listings WHERE product_id=? AND store_id=?", (batch.product_id, store_id)) or {}
                    payload = {k: specs[k] for k in ("media_id", "product_id", "title", "description", "tags", "attach_product", "cover_id")}
                    payload['listing'] = inventory.listing(listing)
                    official = store['platform'] == 'kuaishou' and (batch.attach_product or store['credentials'].get('video_access_token'))
                    payload['video_engine'] = 'kuaishou_official' if official else 'mpau'
                    if official:
                        KuaishouVideo(store).validate(payload)
                    else:
                        mpau_args(store, payload, db.DATA / media['path'])
                        if store['video_status'] != 'valid':
                            raise Blocked('请先完成此店铺的视频账号扫码登录和登录检查')
            except Blocked as exc:
                problem = str(exc)
            # Stable fingerprint excludes now() so repeat clicks stay idempotent.
            identity = {"store": store_id, "kind": batch.kind, "entity": entity, "payload": payload, "scheduled": batch.scheduled}
            key = hashlib.sha256(db.dumps(identity).encode()).hexdigest()
            result.append({"store_id": store_id, "store_name": store["name"], "platform": store["platform"], "kind": batch.kind,
                           "entity_id": entity, "scheduled": scheduled, "payload": payload, "problem": problem, "dedupe_key": key})
    if len(result) > 300:
        raise ValueError("单次最多创建 300 项任务")
    return result


def enqueue(plan, account_group=()):
    batch_id, output = db.uid(), []
    with db.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        for item in plan:
            if account_group:
                marks = ','.join('?' for _ in account_group)
                active = c.execute(f"SELECT id,kind,status FROM jobs WHERE store_id=? AND kind IN ({marks}) AND status IN ('queued','running') LIMIT 1", (item['store_id'], *account_group)).fetchone()
                if active:
                    if active['kind'] != item['kind']:
                        raise Blocked('当前账号任务正在排队或执行，请等待完成后再发起其他登录操作')
                    output.append({'id': active['id'], 'status': active['status'], 'duplicate': True})
                    continue
            previous = c.execute("SELECT id,status FROM jobs WHERE dedupe_key=?", (item["dedupe_key"],)).fetchone()
            if previous:
                if previous["status"] in ("cancelled", "failed") or (previous["status"] == "blocked" and not item.get("problem")):
                    status = "blocked" if item.get("problem") else "queued"
                    c.execute("UPDATE jobs SET status=?,scheduled=?,payload=?,result=?,updated=? WHERE id=?", (status, item["scheduled"], db.dumps(item["payload"]), db.dumps({"message": item.get("problem") or "重新预检通过，等待执行"}), db.now(), previous["id"]))
                    output.append({"id": previous["id"], "status": status, "duplicate": False})
                    continue
                output.append({**dict(previous), "duplicate": True})
                continue
            if item["kind"] in ("product_create", "video_publish"):
                outstanding = c.execute("SELECT id,status FROM jobs WHERE kind=? AND entity_id=? AND store_id=? AND status IN ('queued','running','uncertain','submitted')", (item["kind"], item.get("entity_id", ""), item["store_id"])).fetchone()
                if outstanding:
                    output.append({**dict(outstanding), "duplicate": True})
                    continue
            jid = db.uid()
            status = "blocked" if item.get("problem") else "queued"
            c.execute('''INSERT INTO jobs(id,batch_id,kind,store_id,entity_id,status,scheduled,payload,result,dedupe_key,created,updated)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (jid, batch_id, item["kind"], item["store_id"], item.get("entity_id", ""), status, item["scheduled"], db.dumps(item["payload"]), db.dumps({"message": item.get("problem", "等待执行")}), item["dedupe_key"], db.now(), db.now()))
            output.append({"id": jid, "status": status, "duplicate": False})
    db.audit("创建任务", {"batch_id": batch_id, "count": len(output)})
    return {"batch_id": batch_id, "jobs": output}


def account_job(store, action):
    if action not in ("account_login", "account_check"):
        raise ValueError("未知账号操作")
    if not mpau_available():
        raise Blocked("视频引擎未安装；请使用默认 Docker 完整镜像")
    return enqueue([{"kind": action, "store_id": store["id"], "payload": {}, "scheduled": db.now(), "dedupe_key": db.uid()}], account_group=('account_login', 'account_check'))


def commerce_account_job(store, action):
    """Commerce sessions use their own profiles and never authorize video upload."""
    if action not in ('commerce_login', 'commerce_check', 'commerce_logout'):
        raise ValueError('未知电商账号操作')
    if action != 'commerce_logout' and not store.get('commerce_enabled'):
        raise Blocked('请先启用此店铺的网页电商管理')
    if action != 'commerce_logout' and not commerce_available():
        raise Blocked('网页电商浏览器未安装；请使用 Docker 完整镜像')
    return enqueue([{'kind': action, 'store_id': store['id'], 'payload': {}, 'scheduled': db.now(), 'dedupe_key': db.uid()}], account_group=('commerce_login', 'commerce_check', 'commerce_logout'))


def execute(job):
    store = db.get_store(job["store_id"], secrets=True)
    payload = json.loads(job["payload"])
    kind = job["kind"]
    if not store:
        raise Blocked('店铺不存在')
    if kind.startswith('commerce_'):
        action = kind.removeprefix('commerce_')
        if action not in ('login', 'check', 'logout'):
            raise Blocked('未知电商账号操作')
        try:
            result = CommerceBrowser(store).account(action, job_id=job['id'])
        except Exception:
            with db.connection() as c:
                c.execute("UPDATE stores SET commerce_status=CASE WHEN commerce_status='expired' THEN 'expired' ELSE 'unverified' END WHERE id=?", (store['id'],))
            raise
        with db.connection() as c:
            c.execute('UPDATE stores SET commerce_status=? WHERE id=?', ('unverified' if action == 'logout' else 'valid', store['id']))
        return 'succeeded', result
    if kind.startswith("account_"):
        action = "login" if kind == "account_login" else "check"
        args = ["mpau", mpau_platform(store["platform"]), action, "--account", store["id"]]
        if action == "login":
            args += ["--headed"]
        try:
            run_mpau(args, job["id"], timeout=600 if action == "login" else 180)
        except Exception:
            with db.connection() as c:
                c.execute("UPDATE stores SET video_status='unverified' WHERE id=?", (store["id"],))
            raise
        with db.connection() as c:
            c.execute("UPDATE stores SET video_status='valid' WHERE id=?", (store["id"],))
        return "succeeded", {"message": "视频账号已登录 / 登录检查通过"}
    if kind == "order_sync":
        count = 0
        for order in CommerceBrowser(store).orders(payload["since"], payload["until"]):
            db.upsert_order(store["id"], order, source='browser')
            count += 1
        with db.connection() as c:
            c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('orders_cursor_' + store['id'], str(payload['until'])))
        return "succeeded", {"message": f"同步完成：处理 {count} 条订单，重复订单已更新", "count": count}
    if kind == 'inventory_sync':
        product = db.get_product(payload['product_id'])
        if not product:
            raise Blocked('商品已删除，请重新预检')
        if payload.get('browser_rules') != store['templates'].get('commerce_browser', {}):
            raise Blocked('预检后的网页库存规则已变化，请重新预检')
        current = inventory.listing(db.one('SELECT * FROM listings WHERE product_id=? AND store_id=?', (product['id'], store['id'])))
        if current.get('external_id') != payload['listing'].get('external_id') or current['sku_map'] != payload['listing']['sku_map']:
            raise Blocked('平台 SKU 关联已变更，请重新预检')
        if inventory.sku_targets(product, store['id']) != payload['targets']:
            raise Blocked('预检后的库存已变化，请重新预检')
        return 'succeeded', inventory.sync_to_store(store, product, current, payload['sales_paused'])
    if kind == "video_publish":
        media = db.one("SELECT * FROM media WHERE id=?", (payload["media_id"],))
        if not media or not (db.DATA / media["path"]).exists():
            raise Blocked("视频文件已丢失")
        current = inventory.listing(db.one('SELECT * FROM listings WHERE product_id=? AND store_id=?', (payload.get('product_id', ''), store['id'])))
        if payload.get('attach_product') and (current.get('external_id') != payload['listing'].get('external_id') or current.get('product_link') != payload['listing'].get('product_link') or (store['platform'] == 'kuaishou' and not current.get('owned_product'))):
            raise Blocked('视频商品关联已变化，请重新预检')
        if payload.get('video_engine') == 'kuaishou_official':
            return 'submitted', KuaishouVideo(store).publish(payload, db.DATA / media['path'])
        args = mpau_args(store, payload, db.DATA / media["path"])
        return "submitted", run_mpau(args, job["id"])
    # Revalidate the reviewed browser form before any external side effect.
    if kind not in ('product_create', 'product_update', 'product_list', 'product_delist'):
        raise Blocked('未知商品操作')
    check_session(store)
    current = inventory.listing(db.one("SELECT * FROM listings WHERE product_id=? AND store_id=?", (job["entity_id"], store["id"])))
    if kind == "product_create" and current.get("external_id"):
        raise Blocked("该商品已在此店铺关联平台 ID，已阻止重复创建")
    if kind != "product_create" and current.get("external_id") != payload["listing"].get("external_id"):
        raise Blocked("商品关联已变更，请取消旧任务并重新预检")
    product = db.get_product(job['entity_id'])
    if not product or product.get('version') != payload['product'].get('version'):
        raise Blocked('预检后的商品资料或库存已变化，请重新预检')
    if payload.get('browser_rules') != store['templates'].get('commerce_browser', {}):
        raise Blocked('预检后的网页商品规则已变化，请重新预检')
    request = product_payload(store, product, current, kind)
    if request != payload.get('request'):
        raise Blocked('预检后的店铺规则或商品关联已变化，请重新预检')
    result = CommerceBrowser(store).mutate_product(kind, payload['request'])
    if not isinstance(result, dict):
        raise Uncertain('网页提交回执格式变化，请核对后台结果')
    external = str(result.get('external_id') or '')
    if not external:
        raise Uncertain('网页提交后未确认平台商品 ID，请核对后台并手动关联，勿直接重试')
    if kind != 'product_create' and external != str(current['external_id']):
        raise Uncertain('网页回执的商品 ID 与目标商品不同，请核对后台结果')
    sku_map = result.get('sku_map', {})
    codes = {s['code'] for s in product['skus']}
    if not isinstance(sku_map, dict) or not set(sku_map) <= codes or any(not isinstance(v, str) or not v for v in sku_map.values()) or len(set(sku_map.values())) != len(sku_map):
        raise Uncertain('网页回执 SKU 关联无效，请核对后台后手工关联')
    if any(code in current['sku_map'] and current['sku_map'][code] != remote for code, remote in sku_map.items()):
        raise Uncertain('网页回执 SKU ID 与已有库存关联不符，请核对后台，未自动改绑')
    combined_skus = {**current['sku_map'], **sku_map}
    if len(set(combined_skus.values())) != len(combined_skus):
        raise Uncertain('网页回执与现有 SKU 关联冲突，请核对后台后手工关联')
    with db.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        latest = inventory.listing(c.execute('SELECT * FROM listings WHERE product_id=? AND store_id=?', (product['id'], store['id'])).fetchone())
        if latest != current:
            raise Uncertain('网页提交期间本地商品关联已变化，请核对后台，未覆盖新的关联')
        duplicate = c.execute('SELECT product_id FROM listings WHERE store_id=? AND external_id=? AND product_id!=?', (store['id'], external, product['id'])).fetchone()
        if duplicate:
            raise Uncertain('网页回执商品已关联其他本地商品，请核对后台结果')
        c.execute('''INSERT INTO listings(product_id,store_id,external_id,product_link,status,updated) VALUES(?,?,?,?,?,?)
        ON CONFLICT(product_id,store_id) DO UPDATE SET external_id=excluded.external_id,status=excluded.status,updated=excluded.updated''',
                  (job["entity_id"], store["id"], external, current.get("product_link", ""), "submitted", db.now()))
        if sku_map:
            c.execute('UPDATE listings SET sku_map=? WHERE product_id=? AND store_id=?', (db.dumps(combined_skus), job['entity_id'], store['id']))
        if kind == 'product_create' and store['platform'] == 'kuaishou':
            c.execute('UPDATE listings SET owned_product=1 WHERE product_id=? AND store_id=?', (job['entity_id'], store['id']))
    return 'submitted', {'message': result.get('message') or '网页已确认提交；新商品以零库存创建，请核对 SKU 后分配店铺库存，审核结果以后台为准', 'external_id': external}


class Worker:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None
        self.lock_file = None

    def start(self):
        self.lock_file = (db.DATA / "worker.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("同一数据目录只能运行一个 ShopDock worker") from None
        with db.connection() as c:
            c.execute("UPDATE jobs SET status='uncertain',result=?,updated=? WHERE status='running'", (db.dumps({"message": "上次执行被中断，结果未知；请先核对平台后台"}), db.now()))
        self.thread = threading.Thread(target=self.loop, daemon=True, name="shopdock-worker")
        self.thread.start()

    def claim(self):
        with db.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM jobs WHERE status='queued' AND scheduled<=? ORDER BY created,id LIMIT 1", (db.now(),)).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status='running',attempts=attempts+1,updated=? WHERE id=?", (db.now(), row["id"]))
            return dict(row)

    def process(self, job):
        try:
            status, result = execute(job)
        except Blocked as exc:
            status, result = "blocked", {"message": str(exc)}
        except Rejected as exc:
            status, result = "failed", {"message": str(exc)}
        except Uncertain as exc:
            status, result = "uncertain", {"message": str(exc)}
        except Exception:
            status, result = "uncertain", {"message": "执行异常，结果未知；请查看服务日志并核对后台"}
            # Do not print credentials or HTTP exception URLs.
            print(f"Job {job['id']} unexpected error in {traceback.extract_tb(__import__('sys').exc_info()[2])[-1].name}", flush=True)
        with db.connection() as c:
            c.execute("UPDATE jobs SET status=?,result=?,updated=? WHERE id=?", (status, db.dumps(result), db.now(), job["id"]))
        db.audit("任务结果", {"job_id": job["id"], "status": status})

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.schedule_order_sync()
                job = self.claim()
                if job:
                    self.process(job)
                    continue
            except Exception:
                print("Worker 暂时无法访问任务队列", flush=True)
            self.stop_event.wait(1)

    def schedule_order_sync(self):
        for row in db.all_rows("SELECT id FROM stores WHERE commerce_enabled=1 AND commerce_status='valid'"):
            store = db.get_store(row['id'])
            if not store['templates'].get('auto_order_sync'):
                continue
            key = 'orders_poll_' + store['id']
            last = db.one('SELECT value FROM settings WHERE key=?', (key,))
            if last and int(last['value']) > db.now() - 60:
                continue
            if db.one("SELECT 1 FROM jobs WHERE kind='order_sync' AND store_id=? AND status IN ('queued','running')", (store['id'],)):
                continue
            cursor = db.one('SELECT value FROM settings WHERE key=?', ('orders_cursor_' + store['id'],))
            end = db.now()
            start = int(cursor['value']) - 300 if cursor else end - 86400
            if end - start > 7 * 86400:
                with db.connection() as c:
                    c.execute('INSERT INTO inventory_alerts VALUES(?,?,?,?) ON CONFLICT(store_id,order_id) DO UPDATE SET updated=excluded.updated', (store['id'], '__sync_gap__', '离线超过 7 天，自动同步已停止；请分段补拉历史订单后再启动', end))
                continue
            from .models import Batch
            enqueue(plans(Batch(kind='order_sync', store_ids=[store['id']], since=start, until=end)))
            with db.connection() as c:
                c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(end)))

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        # Lock is retained until process exit if an external operation is still active.
        if self.lock_file and (not self.thread or not self.thread.is_alive()):
            self.lock_file.close()
