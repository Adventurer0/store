import json
import pytest
from app import db


def test_legacy_api_flag_cannot_reenable_merchant_api(owner):
    data = {'name': 'test', 'platform': 'pdd', 'api_enabled': True}
    store = owner.post('/api/stores', json=data).json()
    assert store['api_enabled'] == 0
    assert store['commerce_enabled'] == 1
    assert store['commerce_status'] == 'unverified'
    assert store['video_status'] == 'unverified'
    assert owner.put('/api/stores/' + store['id'], json=data).json()['api_enabled'] == 0


def test_upgrades_revalidate_html_and_load_versioned_assets(client):
    import re
    page = client.get('/')
    assert page.headers['cache-control'] == 'no-cache'
    scripts = re.findall(r'<script src="([^"]+)"', page.text)
    styles = re.findall(r'<link rel="stylesheet" href="([^"]+)"', page.text)
    assert len(scripts) == 3 and len(styles) == 3
    for url in scripts + styles:
        assert url.endswith('?v=0.3.7')
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-cache'


def test_state_reports_distinct_sessions_and_no_cookie_data(owner, store):
    state = owner.get('/api/state').json()
    assert isinstance(state['commerce_browser_ready'], bool)
    assert state['version'] == '0.3.7'
    assert state['platforms']['tmall']['portal'] != state['platforms']['tmall']['video_portal']
    for platform in state['platforms'].values():
        assert platform['commerce_mode'] == 'browser_cookie'
        assert 'product_delist' in platform['commerce']
    assert 'credentials' not in state['stores'][0]


def test_merchant_endpoint_is_authenticated_and_independent(owner, store, monkeypatch):
    calls = []
    monkeypatch.setattr('app.jobs.commerce_account_job', lambda s, a: calls.append((s['id'], a)) or {'jobs': []})
    base = '/api/stores/' + store['id'] + '/commerce/'
    for action in ('login', 'check', 'logout'):
        assert owner.post(base + action, json={}).status_code == 200
    assert [a for _, a in calls] == ['commerce_login', 'commerce_check', 'commerce_logout']
    assert owner.post(base + 'unknown', json={}).status_code == 404
    assert owner.post(base + 'login', json={}, headers={'X-CSRF-Token': ''}).status_code == 403
    assert db.get_store(store['id'])['video_status'] == 'unverified'


def test_additive_migration_preserves_video_and_disables_old_api(owner, store):
    with db.connection() as c:
        c.execute("UPDATE stores SET video_status='valid',api_enabled=1 WHERE id=?", (store['id'],))
        c.execute('ALTER TABLE stores DROP COLUMN commerce_enabled')
        c.execute('ALTER TABLE stores DROP COLUMN commerce_status')
    db.init()
    db.init()
    migrated = db.get_store(store['id'])
    assert migrated['video_status'] == 'valid'
    assert migrated['commerce_status'] == 'unverified'
    assert migrated['commerce_enabled'] == 1
    assert migrated['api_enabled'] == 0


def test_csv_cannot_replace_browser_order_and_reconcile_keeps_source(owner, store):
    order = {'external_id': 'o1', 'status': '待发货', 'items': [], 'remote_updated': 100}
    db.upsert_order(store['id'], order, source='browser')
    db.upsert_order(store['id'], {**order, 'status': '已取消'}, source='csv')
    assert owner.post('/api/inventory/reconcile', json={}).json()['count'] == 1
    saved = db.one('SELECT * FROM orders')
    assert saved['source'] == 'browser'
    assert json.loads(saved['data'])['status'] == '待发货'


def test_login_confirmation_requires_an_active_login_job(owner, store):
    path = '/api/stores/' + store['id'] + '/commerce/login/confirm'
    assert owner.post(path, json={}).status_code == 409
    now = int(__import__('time').time())
    with db.connection() as conn:
        conn.execute('INSERT INTO jobs(id,batch_id,kind,store_id,entity_id,status,scheduled,payload,result,'
                     'dedupe_key,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     (db.uid(), db.uid(), 'commerce_login', store['id'], '', 'running', now,
                      '{}', '{}', db.uid(), now, now))
    response = owner.post(path, json={})
    assert response.status_code == 200, response.text
    marker = db.DATA / 'browser' / 'commerce' / store['id'] / 'login.confirm'
    assert marker.is_file()
    assert response.json()['ok'] is True
