import pytest
from fastapi.testclient import TestClient
from app import db
from app.main import app, login_attempts
import json
from app import commerce_browser


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DISABLE_WORKER", "1")
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setattr(db, "DB", tmp_path / "shopdock.sqlite3")
    login_attempts.clear()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def owner(client):
    r = client.post("/api/auth/setup", json={"username": "admin", "password": "test-password-12345"})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf"]
    return client


@pytest.fixture
def store(owner):
    r = owner.post("/api/stores", json={"name": "抖音测试店", "platform": "douyin"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def product(owner):
    r = owner.post("/api/products", json={"title": "银色骑行箱测试商品", "description": "用于测试的银色骑行箱，带密码锁、万向轮和可拆卸内袋。", "category": "箱包", "skus": [{"code": "BOX-SILVER", "name": "银色", "price_cents": 129900, "stock": 10}]})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def commerce_setup(monkeypatch):
    """A configured merchant test session; actual browser calls are mocked per test."""
    monkeypatch.setattr(commerce_browser, 'available', lambda: True)
    def configure(store):
        current = db.get_store(store['id'])
        workflows = {}
        for action in ('product_create', 'product_update', 'product_list', 'product_delist'):
            workflows[action] = {'url': '/', 'steps': [{'op': 'click', 'target': '#submit', 'commit': True}],
                                 'result': {'success': '#saved', 'external_id': '#product-id'}}
        workflows['product_create']['steps'].insert(0, {'op': 'each_sku', 'steps': [
            {'op': 'fill', 'target': '#stock-{{index}}', 'field': 'stock', 'value': '{{sku.stock}}'}]})
        workflows['product_create']['result'].update(rows='.sku', fields={k: '.'+k for k in ('code','sku_id','stock')})
        workflows['orders'] = {'url': '/', 'steps': [], 'result': {'rows': '.order', 'fields': {
            k: '.' + k for k in ('external_id', 'status', 'created', 'remote_updated','product_id','sku','quantity')}}}
        workflows['inventory_sync'] = {'url': '/', 'steps': [
            {'op': 'each_sku', 'steps': [{'op':'fill','target':'#stock-{{index}}','field':'stock','value':'{{sku.stock}}'}]},
            {'op': 'click', 'target': '#save', 'commit': True}],
                                      'result': {'success': '#saved', 'rows': '.sku', 'fields': {'sku_id': '.id', 'stock': '.stock'}}}
        config = current['templates']
        config['commerce_browser'] = {'workflows': workflows,'identity':{'target':'#store-id','value':store['id']}}
        with db.connection() as c:
            c.execute("UPDATE stores SET commerce_status='valid', templates=? WHERE id=?", (db.dumps(config), store['id']))
        profile = commerce_browser.profile_path(store['id'], create=True)
        (profile / 'session.json').write_text(json.dumps({'verified': True}), encoding='utf-8')
        return db.get_store(store['id'])
    return configure
