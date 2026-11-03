"""Built-in Kuaishou submission through the unified publishing worker path."""
import subprocess
import time

import pytest

from app import commerce_browser as browser
from app import commerce_forms
from app import db
from app import kuaishou_commerce
from app.commerce_worker import Failure, Runner
from app.connectors import Blocked, Uncertain
from app.kuaishou_submission import CREATE_URL


HEADERS = ['颜色', '尺寸', '库存', '价格（元）', 'SKU编码', '商品条形码', '预览图', '是否上架']
EDIT_HEADERS = ['当前库存', '库存调整', '改后库存', '价格（元）', '可售库存', 'SKU编码', '预留库存', 'SKUID', '商品条形码', '是否上架']


def table(headers, rows):
    result = '<div class="ant-table-container"><table><thead><tr>'
    result += ''.join('<th>' + name + '</th>' for name in headers)
    result += '</tr></thead></table>'
    for key, cells in rows:
        result += '<div class="ant-table-row" data-row-key="' + key + '">'
        result += ''.join('<div class="ant-table-cell">' + cells.get(name, '') + '</div>' for name in headers)
        result += '</div>'
    return result + '</div>'


def html(content):
    return '<meta charset="utf-8"><div id="shop">测试店</div><main>' + content + '</main>'


def create_markup():
    row = [('defaultSku', {'库存': '<input aria-label="stock" value="88">',
                           '价格（元）': '<input aria-label="price" value="77">',
                           'SKU编码': '<input aria-label="code" value="">'})]
    button = '''<button onclick="setTimeout(()=>{
      history.pushState({},'', '/zone/goods/nexus/self/release/success?itemId=123456789');
      document.body.insertAdjacentHTML('beforeend','<div>提交审核成功</div>');
    },100)">提交审核</button>'''
    return html(table(HEADERS, row) + button)


def edit_markup():
    row = [('saved', {'当前库存': '<input value="0">', '价格（元）': '<input value="12.90">',
                      'SKU编码': '<input value="A99">', 'SKUID': '900000001',
                      '库存调整': '<input value="0">', '改后库存': '0', '可售库存': '0'})]
    return html(table(EDIT_HEADERS, row))


def test_builtin_kuaishou_submit_runs_without_product_create_workflow(tmp_path, monkeypatch):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser_instance = p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = browser_instance.new_context()
        page = context.new_page()
        context.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(
            body=edit_markup() if '/edit/' in route.request.url else create_markup(),
            content_type='text/html; charset=utf-8'))
        page.goto(CREATE_URL)
        request = {
            'platform': 'kuaishou', 'profile': str(tmp_path / 'profile'), 'action': 'submit_workbench',
            'config': {'identity': {'target': '#shop', 'value': '测试店'}},
            'payload': {'product': {'title': '测试商品', 'skus': [
                {'code': 'A99', 'name': '默认', 'price_cents': 1290, 'stock': 10}]},
                'listing': {}, 'values': {}, 'schema': {'fields': []}, 'fingerprint': 'reviewed-fingerprint'},
            'media': {},
        }
        runner = Runner(request)
        runner.page, runner.context = page, context
        monkeypatch.setattr(commerce_forms, 'open_editor', lambda current: None)
        prepared = {}

        def prepare(current, payload, media, config):
            prepared.update(config)
            return {'missing': [{'field': 'merchant_review', 'label': '平台最终检查', 'reason': '人工核对'}]}
        monkeypatch.setattr(kuaishou_commerce, 'prepare', prepare)
        monkeypatch.setattr(commerce_forms, 'collect', lambda current, platform: {'complete': True, 'fields': []})
        monkeypatch.setattr(commerce_forms, 'fill', lambda *args, **kwargs: [])

        def quality(current, platform, fingerprint):
            return {'source': 'platform_dom', 'score': 97.0, 'fingerprint': fingerprint,
                    'observed_at': int(time.time()), 'source_url': commerce_forms.source_url(platform, current.url)}
        monkeypatch.setattr(commerce_forms, 'quality', quality)
        try:
            result = commerce_forms.run(runner, 'submit_workbench')
            assert len(context.pages) == 1, 'the receipt page must be closed after read-back'
        finally:
            browser_instance.close()

    assert prepared['defer_skus'] is True
    assert result['external_id'] == '123456789'
    assert result['sku_map'] == {'A99': '900000001'}
    assert result['quality']['score'] == 97.0
    assert result['receipt']['source'] == 'platform_dom'
    assert runner.write_started is True


def test_submit_worker_timeout_is_uncertain_not_a_safe_retry(store, commerce_setup, monkeypatch):
    commerce_setup(store)
    monkeypatch.setattr(browser, 'browser_python', lambda: '/usr/local/bin/python')

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('worker', 1)
    monkeypatch.setattr(browser, 'run_worker', timeout)
    current = browser.CommerceBrowser(db.get_store(store['id'], secrets=True))
    with pytest.raises(Uncertain):
        current._run('submit_workbench', {'product': {'skus': []}})
    with pytest.raises(Blocked):
        current._run('prepare_workbench', {'product': {'skus': []}})


def test_worker_audit_reads_status_and_preserves_platform_code():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = browser.new_context()
        page = context.new_page()
        context.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(
            body='<meta charset="utf-8"><div>商品审核中</div><div>错误码：A123</div>',
            content_type='text/html; charset=utf-8'))
        page.goto('https://s.kwaixiaodian.com/zone/goods/nexus/self/release/edit/ordinary?itemId=555')
        runner = Runner({'platform': 'kuaishou', 'profile': '/tmp/audit-profile', 'action': 'audit_product',
                         'config': {}, 'payload': {'external_id': '555'}, 'media': {}})
        runner.page, runner.context = page, context
        try:
            result = runner.audit_product()
            assert result['audit_status'] == 'pending'
            assert result['audit_code'] == 'A123'
            assert '审核中' in result['audit_message']
        finally:
            browser.close()
