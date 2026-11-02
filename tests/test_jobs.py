import json
import time
from concurrent.futures import ThreadPoolExecutor
from app import db, jobs
from app.connectors import Uncertain


def configure(owner, store):
    r = owner.put("/api/stores/" + store["id"], json={"name": store["name"], "platform": "douyin", "api_enabled": True, "credentials": {"app_key": "test-key", "app_secret": "test-secret", "access_token": "test-token"}, "templates": {"product_create": {"name": "{{product.title}}"}}})
    assert r.status_code == 200


def test_snapshot_idempotency_and_unknown_side_effect(owner, product, store, monkeypatch, commerce_setup):
    configure(owner, store)
    commerce_setup(store)
    body = {"kind": "product_create", "store_ids": [store["id"]], "product_ids": [product["id"]]}
    jid = owner.post("/api/jobs", json=body).json()["jobs"][0]["id"]
    worker = jobs.Worker()
    job = worker.claim()
    assert job["id"] == jid
    def failure(*args, **kwargs):
        raise Uncertain("timeout")
    monkeypatch.setattr(jobs.CommerceBrowser, "mutate_product", failure)
    worker.process(job)
    assert db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "uncertain"
    # Editing the title cannot bypass the uncertain earlier create request.
    payload = {"title": "修改后的标题", "skus": product["skus"], "version": 1}
    assert owner.put("/api/products/" + product["id"], json=payload).status_code == 200
    result = owner.post("/api/jobs", json=body).json()["jobs"][0]
    assert result["duplicate"] is True and result["id"] == jid
    assert worker.claim() is None


def test_publish_tracks_remote_id_but_not_audit_success(owner, product, store, monkeypatch, commerce_setup):
    configure(owner, store)
    commerce_setup(store)
    owner.post("/api/jobs", json={"kind": "product_create", "store_ids": [store["id"]], "product_ids": [product["id"]]})
    monkeypatch.setattr(jobs.CommerceBrowser, "mutate_product", lambda *a: {"external_id": "90071992547409999"})
    worker = jobs.Worker()
    job = worker.claim()
    worker.process(job)
    assert db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] == "submitted"
    assert db.one("SELECT external_id FROM listings")["external_id"] == "90071992547409999"


def test_schedule_survives_and_claim_is_atomic(owner, store):
    now = db.now()
    items = [{"kind": "account_check", "store_id": store["id"], "payload": {}, "scheduled": now, "dedupe_key": str(n)} for n in range(3)]
    items.append({"kind": "account_check", "store_id": store["id"], "payload": {}, "scheduled": now + 3600, "dedupe_key": "future"})
    jobs.enqueue(items)
    worker = jobs.Worker()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: worker.claim(), range(3)))
    assert len({r["id"] for r in results}) == 3
    assert worker.claim() is None
    assert db.one("SELECT status FROM jobs WHERE dedupe_key='future'")["status"] == "queued"


def test_restart_does_not_republish_running(owner, store):
    jobs.enqueue([{"kind": "account_check", "store_id": store["id"], "payload": {}, "scheduled": db.now(), "dedupe_key": "test"}])
    worker = jobs.Worker()
    job = worker.claim()
    worker.start()
    try:
        assert db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] == "uncertain"
    finally:
        worker.stop()


def test_repreflight_can_reactivate_repaired_blocked_and_cancelled(owner, store):
    item = {"kind": "video_publish", "store_id": store["id"], "entity_id": "media-1", "payload": {}, "scheduled": db.now(), "dedupe_key": "stable", "problem": "请先扫码"}
    jid = jobs.enqueue([item])["jobs"][0]["id"]
    item["problem"] = ""
    repaired = jobs.enqueue([item])["jobs"][0]
    assert repaired["id"] == jid and repaired["status"] == "queued"
    assert owner.post(f"/api/jobs/{jid}/action", json={"action": "cancel"}).status_code == 200
    assert jobs.enqueue([item])["jobs"][0]["status"] == "queued"


def test_empty_browser_receipt_is_uncertain(owner, product, store, monkeypatch, commerce_setup):
    configure(owner, store)
    commerce_setup(store)
    owner.put(f"/api/products/{product['id']}/listings/{store['id']}", json={"external_id": "123"})
    owner.post("/api/jobs", json={"kind": "product_delist", "store_ids": [store["id"]], "product_ids": [product["id"]]})
    monkeypatch.setattr(jobs.CommerceBrowser, "mutate_product", lambda *a: {})
    worker = jobs.Worker()
    job = worker.claim()
    worker.process(job)
    assert db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] == "uncertain"
