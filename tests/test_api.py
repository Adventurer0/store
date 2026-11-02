import csv
import io
import json
import time
import pytest
from app import db
from app.security import decrypt


def test_first_run_auth_and_assets(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/auth/status").json() == {"initialized": False, "authenticated": False, "csrf": "", "username": ""}
    assert client.get("/").status_code == 200
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/style.css").status_code == 200
    assert client.get("/api/media/private").status_code == 401
    assert client.get("/desktop/vnc.html").status_code == 401


def test_weak_setup_blocked(client):
    r = client.post("/api/auth/setup", json={"username": "admin", "password": "123456"})
    assert r.status_code == 422


def test_csrf_origin_host_and_setup_reuse(owner):
    assert owner.post("/api/auth/setup", json={"username": "x", "password": "otherlongpassword"}).status_code == 409
    r = owner.post("/api/stores", json={"name": "a", "platform": "douyin"}, headers={"X-CSRF-Token": ""})
    assert r.status_code == 403
    r = owner.post("/api/stores", json={"name": "a", "platform": "douyin"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert owner.get("/api/state", headers={"Host": "evil.example"}).status_code == 400


def test_logout_session_revocation(owner):
    assert owner.post("/api/auth/logout", json={}).status_code == 200
    assert owner.get("/api/state").status_code == 401
    r = owner.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    r = owner.post("/api/auth/login", json={"username": "admin", "password": "test-password-12345"})
    assert r.status_code == 200
    assert owner.get("/api/state").status_code == 200


def test_credentials_encrypted_and_never_returned(owner):
    secret = "secret-never-return-this-123"
    r = owner.post("/api/stores", json={"name": "微信店", "platform": "wechat", "credentials": {"access_token": secret}})
    assert r.status_code == 200
    assert secret not in r.text
    stored = db.one("SELECT credentials FROM stores WHERE id=?", (r.json()["id"],))["credentials"]
    assert secret not in stored and decrypt(stored)["access_token"] == secret
    assert secret not in owner.get("/api/state").text
    changed = owner.put("/api/stores/" + r.json()["id"], json={"name": "改名", "platform": "wechat", "credentials": {"access_token": ""}})
    assert changed.status_code == 200
    assert db.get_store(r.json()["id"], True)["credentials"]["access_token"] == secret


def test_product_edit_conflict_and_integer_money(owner, product):
    pid = product["id"]
    body = {k: product[k] for k in ("title", "description", "category", "skus", "images", "cover_id", "extra", "version")}
    body["title"] = "更新的商品"
    assert owner.put("/api/products/" + pid, json=body).json()["version"] == 2
    assert owner.put("/api/products/" + pid, json=body).status_code == 409
    body["skus"][0]["price_cents"] = 10.2
    assert owner.post("/api/products", json=body).status_code == 422


def test_sku_uniqueness_and_negative_stock(owner):
    sku = {"code": "a", "name": "red", "price_cents": 100, "stock": 1}
    assert owner.post("/api/products", json={"title": "a", "skus": [sku, sku]}).status_code == 422
    sku["stock"] = -1
    assert owner.post("/api/products", json={"title": "a", "skus": [sku]}).status_code == 422


def test_associations_reject_js_link(owner, product, store):
    path = f"/api/products/{product['id']}/listings/{store['id']}"
    assert owner.put(path, json={"external_id": "9999999999999999999", "product_link": "javascript:alert(1)"}).status_code == 422
    assert owner.put(path, json={"external_id": "9999999999999999999", "product_link": "https://example.com/product"}).status_code == 200
    assert owner.get("/api/state").json()["listings"][0]["external_id"] == "9999999999999999999"


def test_media_validation_and_storage(owner):
    r = owner.post("/api/media", files={"file": ("bad.png", b"<script>alert(1)</script>", "image/png")})
    assert r.status_code == 422
    assert not list((db.DATA / "media").iterdir())
    png = b"\x89PNG\r\n\x1a\n" + b"testimage"
    r = owner.post("/api/media", files={"file": ("../../safe.png", png, "image/png")})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    r = owner.get("/api/media/" + mid)
    assert r.status_code == 200 and r.content == png
    assert r.headers["content-type"] == "image/png"
    assert owner.post("/api/media", files={"file": ("bad.svg", b"<svg/>", "image/svg+xml")}).status_code == 422


def test_order_csv_atomic_validation_and_dedupe(owner, store):
    path = "/api/import/orders/" + store["id"]
    header = "order_id,status,amount_cents,title,quantity\n"
    bad = header + "1,待发货,100,商品,1\n2,待发货,NOT-NUMBER,商品,1\n"
    assert owner.post(path, files={"file": ("orders.csv", bad.encode())}).status_code == 422
    assert owner.get("/api/orders").json()["total"] == 0
    good = header + "1,待发货,100,商品,1\n"
    for _ in range(2):
        assert owner.post(path, files={"file": ("orders.csv", good.encode())}).status_code == 200
    result = owner.get("/api/orders").json()
    assert result["total"] == 1
    oid = result["rows"][0]["id"]
    assert owner.put(f"/api/orders/{oid}/note", json={"note": "本地备注"}).status_code == 200
    db.upsert_order(store["id"], {"external_id": "1", "status": "已发货", "amount_cents": 100, "items": []})
    r = owner.post(path, files={"file": ("orders.csv", good.encode())})
    assert r.json()["skipped"] == 1
    row = owner.get("/api/orders").json()["rows"][0]
    assert row["data"]["status"] == "已发货" and row["note"] == "本地备注"


def test_order_same_id_different_store(owner, store):
    second = owner.post("/api/stores", json={"name": "微信", "platform": "wechat"}).json()
    for s in (store, second):
        db.upsert_order(s["id"], {"external_id": "same", "status": "待付款", "amount_cents": None, "items": []})
    assert owner.get("/api/orders").json()["total"] == 2
    assert owner.get("/api/orders?store_id=" + store["id"]).json()["total"] == 1


def test_csv_excel_injection(owner, store):
    db.upsert_order(store["id"], {"external_id": "=1+1", "status": "待付款", "amount_cents": 100, "items": [{"title": "@SUM(1)", "quantity": 1}]})
    result = owner.get("/api/export/orders")
    rows = list(csv.reader(io.StringIO(result.text.lstrip("\ufeff"))))
    assert rows[1][1] == "'=1+1" and rows[1][4] == "'@SUM(1)"


def test_product_import_multiple_skus_and_reimport(owner):
    content = "product_key,title,category,description,sku_code,sku_name,price_cents,stock\na,箱子,箱包,说明,A,红,100,1\na,箱子,箱包,说明,B,银,200,2\n"
    for _ in range(2):
        r = owner.post("/api/import/products", files={"file": ("products.csv", content.encode())})
        assert r.status_code == 200, r.text
    products = owner.get("/api/state").json()["products"]
    assert len(products) == 1 and len(products[0]["skus"]) == 2


def test_unsupported_does_not_succeed(owner, product):
    store = owner.post("/api/stores", json={"name": "淘宝", "platform": "tmall"}).json()
    body = {"kind": "product_create", "store_ids": [store["id"]], "product_ids": [product["id"]]}
    pre = owner.post("/api/jobs/preflight", json=body)
    assert pre.status_code == 200 and pre.json()["ready"] == 0
    r = owner.post("/api/jobs", json=body)
    assert r.json()["jobs"][0]["status"] == "blocked"
    again = owner.post("/api/jobs", json=body)
    assert again.json()["jobs"][0]["duplicate"] is True
    assert owner.get("/api/jobs").json()["total"] == 1


def test_sync_window_validation(owner, store):
    body = {"kind": "order_sync", "store_ids": [store["id"]], "since": 1, "until": 9999999}
    assert owner.post("/api/jobs", json=body).status_code == 422


def test_cancel_and_delete(owner, product, store):
    r = owner.post("/api/jobs", json={"kind": "product_create", "store_ids": [store["id"]], "product_ids": [product["id"]]})
    jid = r.json()["jobs"][0]["id"]
    assert owner.post(f"/api/jobs/{jid}/action", json={"action": "cancel"}).status_code == 200
    assert owner.delete("/api/products/" + product["id"]).status_code == 200
    assert owner.get("/api/state").json()["products"] == []


def test_uncertain_retry_requires_remote_check(owner, product, store):
    jid = owner.post("/api/jobs", json={"kind": "product_create", "store_ids": [store["id"]], "product_ids": [product["id"]]}).json()["jobs"][0]["id"]
    with db.connection() as c:
        c.execute("UPDATE jobs SET status='uncertain' WHERE id=?", (jid,))
    assert owner.post(f"/api/jobs/{jid}/action", json={"action": "retry"}).status_code == 409
    assert owner.post(f"/api/jobs/{jid}/action", json={"action": "confirm", "evidence": ""}).status_code == 422
    assert owner.post(f"/api/jobs/{jid}/action", json={"action": "confirm", "evidence": "平台作品编号已核对"}).status_code == 200


def test_no_plain_secret_in_static_or_error(owner):
    assert owner.get("/assets/%2e%2e/db.py").status_code == 404
    assert owner.get("/api/jobs/../../master.key/log").status_code == 404
