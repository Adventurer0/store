import copy
import json
import subprocess
from types import SimpleNamespace
import pytest
from app import db
from app import commerce_browser as browser
from app.commerce_profiles import PROFILES, trusted_url
from app.commerce_worker import Failure, Runner, render, timestamp
from app.connectors import Blocked, Uncertain


def workflow(action='product_create'):
    result = {'success': '#success', 'external_id': '#product-id', 'rows': '.sku-row',
              'fields': {'code': '.code', 'sku_id': '.sku-id', 'stock': {'css': '.stock', 'input': True}}}
    steps = [{'op': 'fill', 'target': {'label': '商品名称'}, 'value': '{{product.title}}'},
             {'op': 'each_sku', 'steps': [{'op': 'fill', 'field': 'stock',
                                         'target': {'css': '.stock', 'nth': '{{index}}'}, 'value': '{{sku.stock}}'}]},
             {'op': 'click', 'target': '#save', 'commit': True}]
    if action != 'product_create':
        steps = [{'op': 'click', 'target': '#save', 'commit': True}]
    return {'url': '/products/edit', 'steps': steps, 'result': result}


@pytest.fixture
def connected(store, monkeypatch):
    config = {'logged_in': '#nav', 'identity': {'target': '#shop-name', 'value': '测试店'},
              'workflows': {'product_create': workflow(), 'product_update': workflow('product_update')}}
    with db.connection() as c:
        c.execute("UPDATE stores SET commerce_status='valid',templates=? WHERE id=?", (db.dumps({'commerce_browser': config}), store['id']))
    path = browser.profile_path(store['id'], create=True)
    (path / 'session.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(browser, 'available', lambda: True)
    return db.get_store(store['id'], secrets=True)


@pytest.mark.parametrize('platform', PROFILES)
def test_all_merchant_portals_reject_foreign_targets(platform):
    assert trusted_url(platform, PROFILES[platform]['portal']).startswith('https://')
    for url in ('http://example.com/', 'https://example.com/', 'https://evil.test/?target=https://shop.jd.com/',
                'https://shop.jd.com@evil.test/', 'javascript:alert(1)'):
        with pytest.raises(ValueError):
            trusted_url(platform, url)
    assert 'guanghe' not in PROFILES['tmall']['portal']


def test_video_login_and_api_token_do_not_authorize_commerce(store, monkeypatch):
    store.update(video_status='valid', credentials={'access_token': 'legacy-secret'}, api_enabled=True)
    monkeypatch.setattr(browser, 'available', lambda: True)
    with pytest.raises(Blocked, match='电商后台'):
        browser.check_session(store)


def test_profile_is_separate_and_path_cannot_escape(client):
    assert browser.profile_path('test-store') == db.DATA / 'browser' / 'commerce' / 'test-store'
    for sid in ('../x', 'a/b', 'a\\b', '.', '', 'a:stream'):
        with pytest.raises(Blocked):
            browser.profile_path(sid)


def test_create_payload_uses_zero_stock_and_update_omits_warehouse(connected, product):
    creating = browser.product_payload(connected, product, {}, 'product_create')
    assert creating['product']['skus'][0]['stock'] == 0
    assert creating['product']['skus'][0]['price'] == '1299.00'
    updating = browser.product_payload(connected, product, {'external_id': '123'}, 'product_update')
    assert 'stock' not in updating['product']['skus'][0]
    assert product['skus'][0]['stock'] == 10


def test_workflow_requires_identity_stock_write_and_readback(connected):
    config = connected['templates']['commerce_browser']
    del config['identity']
    with pytest.raises(Blocked, match='身份'):
        browser.validate_workflow(connected, 'product_create')
    config['identity'] = {'target': '#shop', 'value': '测试店'}
    config['workflows']['product_create']['steps'].pop(1)
    with pytest.raises(Blocked, match='库存'):
        browser.validate_workflow(connected, 'product_create')
    config['workflows']['product_create'] = workflow()
    del config['workflows']['product_create']['result']['fields']['stock']
    with pytest.raises(Blocked, match='字段'):
        browser.validate_workflow(connected, 'product_create')


@pytest.mark.parametrize('value', [[], 'bad', None, 3])
def test_malformed_workflows_are_blocked(connected, value):
    connected['templates']['commerce_browser']['workflows'] = value
    with pytest.raises(Blocked):
        browser.validate_workflow(connected, 'product_create')


def test_product_update_cannot_write_stock(connected):
    connected['templates']['commerce_browser']['workflows']['product_update']['steps'].insert(
        0, {'op': 'fill', 'field': 'stock', 'target': '#number', 'value': '999'})
    with pytest.raises(Blocked, match='统一库存'):
        browser.validate_workflow(connected, 'product_update')


def test_subprocess_never_receives_api_credentials(connected, monkeypatch):
    connected['credentials'] = {'access_token': 'SECRET-API', 'video_access_token': 'SECRET-VIDEO'}
    monkeypatch.setattr(browser, 'browser_python', lambda: 'python-test')
    captured = {}
    def run(args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='{"result":{"external_id":"123","sku_map":{}}}', stderr='SECRET-COOKIE')
    monkeypatch.setattr(browser, 'run_worker', run)
    result = browser.CommerceBrowser(connected)._run('product_create', {'product': {'skus': []}, 'listing': {}})
    assert result['external_id'] == '123'
    assert 'SECRET' not in captured['input']
    assert json.loads(captured['input'])['profile'].endswith(connected['id'])


def test_write_timeout_is_uncertain_and_stderr_not_exposed(connected, monkeypatch):
    monkeypatch.setattr(browser, 'browser_python', lambda: 'python-test')
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired('worker', 10, output='cookie=SECRET')
    monkeypatch.setattr(browser, 'run_worker', run)
    with pytest.raises(Uncertain) as caught:
        browser.CommerceBrowser(connected)._run('product_create')
    assert 'SECRET' not in str(caught.value)


def test_expired_after_write_preserves_uncertain_and_session_status(connected, monkeypatch):
    monkeypatch.setattr(browser, 'browser_python', lambda: 'python-test')
    monkeypatch.setattr(browser, 'run_worker', lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps({'error': {'kind': 'uncertain', 'session_expired': True, 'message': '登录失效'}})))
    with pytest.raises(Uncertain):
        browser.CommerceBrowser(connected)._run('product_create')
    assert db.get_store(connected['id'])['commerce_status'] == 'expired'


def test_logout_clears_only_merchant_profile_without_browser(connected, monkeypatch):
    video = db.DATA / 'mpau' / 'cookies' / (connected['id'] + '.json')
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_text('VIDEO-SESSION', encoding='utf-8')
    monkeypatch.setattr(browser, 'browser_python', lambda: None)
    browser.CommerceBrowser(connected).account('logout')
    assert video.read_text(encoding='utf-8') == 'VIDEO-SESSION'
    assert not browser.profile_path(connected['id']).exists()
    assert db.get_store(connected['id'])['commerce_status'] == 'unverified'


def test_inventory_worker_receives_only_allocated_quantities(connected, product, monkeypatch):
    config = connected['templates']['commerce_browser']
    config['workflows']['inventory_sync'] = workflow()
    config['workflows']['inventory_sync']['result']['fields'] = {'sku_id': '.sku-id', 'stock': '.stock'}
    target = {**product['skus'][0], 'stock': 2, 'revision': 1}
    captured = {}
    def run(self, action, payload):
        captured.update(payload)
        return {'confirmed': True, 'sku_count': 1}
    monkeypatch.setattr(browser.CommerceBrowser, '_run', run)
    browser.CommerceBrowser(connected).sync_inventory(product, {'sku_map': {'BOX-SILVER': '900'}}, [target])
    assert captured['product']['skus'][0]['stock'] == 2
    assert captured['targets'][0]['stock'] == 2
    assert product['skus'][0]['stock'] == 10


def test_template_missing_stock_fails_before_actions():
    with pytest.raises(Failure):
        render('{{product.skus.0.stock}}', {'product': {'skus': [{'code': 'A'}]}})
    for invalid in ('12345678901', '123456789012'):
        with pytest.raises(Failure):
            timestamp(invalid)


def test_timeout_reaps_only_owned_worker_group(monkeypatch):
    if browser.os.name == 'nt':
        pytest.skip('POSIX process-group cleanup verified in the supported Docker runtime')
    calls = []
    class Process:
        pid = 12345
        returncode = -9
        def communicate(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired('owned-worker', 1)
            return '', ''
    def popen(command, **kwargs):
        assert kwargs['start_new_session'] is True
        return Process()
    groups = []
    monkeypatch.setattr(browser.subprocess, 'Popen', popen)
    monkeypatch.setattr(browser.os, 'killpg', lambda pid, sig: groups.append((pid, sig)))
    with pytest.raises(subprocess.TimeoutExpired):
        browser.run_worker(['owned-worker'], input='{}', timeout=1)
    assert groups == [(12345, browser.signal.SIGKILL)]
    assert len(calls) == 2


@pytest.fixture
def dom_runner(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        chrome = p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = chrome.new_context()
        context.add_cookies([{'name': 'merchant_session', 'value': 'test', 'domain': 'fxg.jinritemai.com', 'path': '/', 'secure': True}])
        page = context.new_page()
        runner = Runner({'platform': 'douyin', 'action': 'product_create', 'profile': str(tmp_path),
                         'config': {'logged_in': '#nav', 'identity': {'target': '#shop-name', 'value': '测试店'}, 'workflows': {}},
                         'payload': {'product': {'title': '测试商品', 'skus': [{'code': 'A', 'stock': 0}]}, 'listing': {}}})
        runner.context, runner.page = context, page
        yield runner
        chrome.close()


BASE = '<meta charset="utf-8"><div id="nav">商品管理</div><div id="shop-name">测试店</div>'


def test_actual_dom_create_waits_for_async_receipt(dom_runner):
    html = BASE + '''<label>商品名称<input id="title"></label>
    <div class="sku-row"><span class="code">A</span><span class="sku-id">900</span><input class="stock" value="99"></div>
    <button id="save" onclick="setTimeout(()=>{document.body.insertAdjacentHTML('beforeend','<div id=success>成功</div><span id=product-id>123</span>')},150)">保存</button>'''
    dom_runner.page.route('https://fxg.jinritemai.com/**', lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
    dom_runner.config['workflows']['product_create'] = workflow()
    result = dom_runner.operate()
    assert result['external_id'] == '123'
    assert result['sku_map'] == {'A': '900'}
    assert dom_runner.page.locator('#title').input_value() == '测试商品'
    assert dom_runner.page.locator('.stock').input_value() == '0'


def test_actual_dom_refuses_wrong_shop_before_edit(dom_runner):
    html = BASE.replace('测试店', '别的店') + '<button id="save">保存</button>'
    dom_runner.page.route('https://fxg.jinritemai.com/**', lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
    dom_runner.config['workflows']['product_create'] = workflow()
    with pytest.raises(Failure, match='') as error:
        dom_runner.operate()
    assert error.value.kind == 'session'
    assert not dom_runner.write_started


def test_actual_dom_inventory_requires_exact_readback(dom_runner):
    html = BASE + '''<div class="sku-row"><span class="sku-id">900</span><input class="stock" value="9"></div>
    <button id="save" onclick="document.body.insertAdjacentHTML('beforeend','<div id=success>成功</div>');document.querySelector('.stock').value='8'">保存</button>'''
    dom_runner.page.route('https://fxg.jinritemai.com/**', lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
    dom_runner.action = 'inventory_sync'
    dom_runner.payload.update(targets=[{'code': 'A', 'stock': 2}], listing={'sku_map': {'A': '900'}})
    rule = workflow()
    rule['steps'] = rule['steps'][1:]
    rule['result']['fields'] = {'sku_id': '.sku-id', 'stock': {'css': '.stock', 'input': True}}
    dom_runner.config['workflows']['inventory_sync'] = rule
    with pytest.raises(Failure) as error:
        dom_runner.operate()
    assert error.value.kind == 'uncertain'
    assert dom_runner.write_started


def test_actual_dom_orders_read_only_operational_fields(dom_runner):
    html = BASE + '''<table><tr class="order"><td class="oid">O-1</td><td class="status">待发货</td><td class="created">1770000000</td><td class="updated">1770000100</td><td class="pid">123</td><td class="sku">900</td><td class="qty">2</td><td class="private">私人买家地址</td></tr></table>'''
    dom_runner.page.route('https://fxg.jinritemai.com/**', lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
    dom_runner.action = 'orders'
    dom_runner.payload = {'since': 1770000000, 'until': 1770000200}
    dom_runner.config['workflows']['orders'] = {'url': '/orders', 'steps': [], 'result': {
        'rows': '.order', 'single_page': True, 'fields': {'external_id': '.oid', 'status': '.status',
        'created': '.created', 'remote_updated': '.updated', 'product_id': '.pid', 'sku': '.sku', 'quantity': '.qty'}}}
    result = dom_runner.operate()
    assert result['orders'][0]['items'] == [{'sku': '900', 'product_id': '123', 'title': '', 'quantity': 2}]
    assert result['orders'][0]['inventory_state'] == 'sold'
    assert '私人' not in json.dumps(result, ensure_ascii=False)
    assert not dom_runner.write_started
