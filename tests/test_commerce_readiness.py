import json

from app import commerce_browser, db


def readiness(owner, store):
    result = owner.get(f"/api/stores/{store['id']}/commerce/readiness")
    assert result.status_code == 200, result.text
    return result.json()


def action(data, key):
    return next(item for item in data['actions'] if item['action'] == key)


def test_readiness_requires_owner_and_existing_store(client, owner, store):
    path = f"/api/stores/{store['id']}/commerce/readiness"
    assert owner.get('/api/stores/missing/commerce/readiness').status_code == 404
    owner.post('/api/auth/logout', json={})
    assert client.get(path).status_code == 401


def test_logged_in_cookie_does_not_mean_publishing_is_implemented(owner, store, commerce_setup):
    commerce_setup(store)
    with db.connection() as conn:
        conn.execute("UPDATE stores SET templates='{}' WHERE id=?", (store['id'],))
    before = db.one('SELECT * FROM stores WHERE id=?', (store['id'],))
    data = readiness(owner, store)
    assert data['login_status'] == 'valid'
    assert data['session_ready'] is True
    assert data['engine_ready'] is True
    assert action(data, 'product_create')['status'] == 'adapter_missing'
    assert action(data, 'product_create')['ready'] is False
    assert '适配尚未完成' in action(data, 'product_create')['summary']
    assert len(data['actions']) == 7
    assert db.one('SELECT * FROM stores WHERE id=?', (store['id'],)) == before


def test_ready_means_session_and_action_rules_validate_not_live_platform_validation(owner, store, commerce_setup):
    commerce_setup(store)
    data = readiness(owner, store)
    assert action(data, 'product_create')['ready'] is True
    assert action(data, 'product_create')['status'] == 'ready'
    assert '提交时仍会检查' in action(data, 'product_create')['summary']
    assert action(data, 'fetch_skus')['status'] == 'adapter_missing'


def test_readiness_never_returns_templates_credentials_or_browser_data(owner, store, commerce_setup):
    commerce_setup(store)
    current = db.get_store(store['id'])
    config = current['templates']
    config['commerce_browser']['identity']['target'] = '#private-selector-sentinel'
    owner.put('/api/stores/' + store['id'], json={
        'name': store['name'], 'platform': store['platform'], 'templates': config,
        'credentials': {'access_token': 'real-secret-sentinel'},
    })
    data = readiness(owner, store)
    serialized = json.dumps(data)
    assert 'private-selector-sentinel' not in serialized
    assert 'real-secret-sentinel' not in serialized
    assert 'templates' not in data and 'credentials' not in data


def test_invalid_rule_is_action_specific_and_has_fixed_diagnostic(owner, store, commerce_setup):
    commerce_setup(store)
    current = db.get_store(store['id'])
    config = current['templates']
    del config['commerce_browser']['workflows']['product_create']['result']['fields']['stock']
    with db.connection() as conn:
        conn.execute('UPDATE stores SET templates=? WHERE id=?', (db.dumps(config), store['id']))
    data = readiness(owner, store)
    assert action(data, 'product_create')['status'] == 'adapter_invalid'
    assert '字段读取规则' in action(data, 'product_create')['problem']
    assert action(data, 'product_update')['status'] == 'ready'


def test_valid_rules_report_expired_session_without_changing_video_status(owner, store, commerce_setup):
    commerce_setup(store)
    with db.connection() as conn:
        conn.execute("UPDATE stores SET commerce_status='expired',video_status='valid' WHERE id=?", (store['id'],))
    data = readiness(owner, store)
    assert action(data, 'product_create')['status'] == 'login_required'
    assert data['session_ready'] is False
    assert db.get_store(store['id'])['video_status'] == 'valid'


def test_missing_engine_is_distinct_from_missing_platform_rules(owner, store, commerce_setup, monkeypatch):
    commerce_setup(store)
    monkeypatch.setattr(commerce_browser, 'available', lambda: False)
    data = readiness(owner, store)
    assert data['engine_ready'] is False
    assert action(data, 'product_create')['status'] == 'engine_unavailable'
    assert action(data, 'fetch_skus')['status'] == 'adapter_missing'


def test_malformed_rule_container_returns_diagnostic_not_exception(owner, store):
    with db.connection() as conn:
        conn.execute('UPDATE stores SET templates=? WHERE id=?', (db.dumps({'commerce_browser': {'workflows': []}}), store['id']))
    data = readiness(owner, store)
    assert all(item['status'] == 'adapter_invalid' for item in data['actions'])


def test_kuaishou_builtin_unified_create_is_reported_without_a_workflow(owner, commerce_setup):
    store = owner.post('/api/stores', json={'name': '快手内置适配店', 'platform': 'kuaishou'}).json()
    commerce_setup(store)
    with db.connection() as conn:
        conn.execute("UPDATE stores SET templates='{}' WHERE id=?", (store['id'],))
    data = readiness(owner, store)
    create = action(data, 'product_create')
    assert create['status'] == 'builtin' and create['ready'] is True
    assert '内置快手创建路径' in create['summary']
    assert '内置快手创建路径' not in action(data, 'product_update')['summary']
