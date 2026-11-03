"""Submission regressions against isolated merchant DOMs; no remote writes."""
import copy
import time

import pytest

from app import commerce_forms as forms, generic_submission as generic
from app import commerce_browser, kuaishou_commerce
from app.commerce_worker import Failure, Runner
from app.connectors import Blocked


@pytest.fixture(scope='module')
def chromium():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        yield browser
        browser.close()


@pytest.fixture
def page(chromium):
    context = chromium.new_context()
    current = context.new_page()
    context.route('https://**/*', lambda route: route.fulfill(
        body='<meta charset="utf-8"><div id="shop">测试店</div><main></main>',
        content_type='text/html; charset=utf-8'))
    current.goto('https://fxg.jinritemai.com/products/new')
    yield current
    context.close()


def submit_button(page):
    page.locator('main').evaluate('''e=>e.innerHTML=`<input required value="商品">
      <button onclick="document.body.dataset.clicked='yes';history.pushState({},'', '?itemId=123');
        document.body.insertAdjacentHTML('beforeend','<div>提交审核成功</div>')">提交审核</button>`''')


def evidence(score=97, **changes):
    return {'source': 'platform_dom', 'score': score, 'fingerprint': 'reviewed',
            'observed_at': int(time.time()), **changes}


@pytest.mark.parametrize('score,changes', [
    (90, {}), (None, {}), (float('nan'), {}), (101, {}),
    (97, {'fingerprint': 'other'}), (97, {'observed_at': 1}),
])
def test_fresh_quality_gate_prevents_click_and_commit(page, score, changes):
    submit_button(page)
    committed = []
    with pytest.raises(Failure) as failure:
        generic.submit(page, {'fingerprint': 'reviewed', 'require_platform_quality': True},
                       lambda: None, read_quality=lambda: evidence(score, **changes),
                       on_commit=lambda: committed.append(True))
    assert failure.value.kind == 'blocked'
    assert not committed
    assert page.locator('body').get_attribute('data-clicked') is None


def test_generic_submission_preserves_fresh_platform_evidence(page):
    submit_button(page)
    checks = []
    result = generic.submit(page, {'fingerprint': 'reviewed', 'require_platform_quality': True},
                            lambda: checks.append('identity'),
                            read_quality=lambda: evidence(96))
    assert result['external_id'] == '123'
    assert result['quality']['score'] == 96
    assert result['quality']['fingerprint'] == 'reviewed'
    assert len(checks) == 2


def test_create_cannot_use_plain_save_or_a_draft_success(page):
    page.locator('main').evaluate('''e=>e.innerHTML=`<button onclick="document.body.dataset.clicked='yes'">保存</button><div>保存成功</div>`''')
    assert generic._success(page, 'create') == []
    assert generic._success(page, 'update') == ['保存成功']
    with pytest.raises(Failure) as failure:
        generic.submit(page, {}, lambda: None)
    assert failure.value.kind == 'blocked'
    assert page.locator('body').get_attribute('data-clicked') is None


def test_generic_failure_keeps_platform_error_code_after_click(page, monkeypatch):
    submit_button(page)
    calls = []

    def success(*args):
        calls.append(True)
        if len(calls) > 1:
            raise Failure('rejected', '品牌资质错误', code='BRAND001')
        return []

    monkeypatch.setattr(generic, '_success', success)
    with pytest.raises(Failure) as failure:
        generic.submit(page, {}, lambda: None)
    assert failure.value.kind == 'uncertain'
    assert failure.value.code == 'BRAND001'


def test_kuaishou_adapter_uploads_are_not_replayed_by_generic_fill(page, monkeypatch, tmp_path):
    page.goto('https://s.kwaixiaodian.com/zone/goods/nexus/self/release/add')
    page.locator('main').evaluate('''e=>e.innerHTML=`
      <div class="ant-form-item"><label>商品主图</label><input type="file" multiple
        onchange="this.dataset.uploads=Number(this.dataset.uploads||0)+1"></div>
      <div class="ant-form-item"><label>商品详情图</label><input type="file" multiple
        onchange="this.dataset.uploads=Number(this.dataset.uploads||0)+1"></div>
      <div class="ant-form-item"><label>商品标题</label><input value="旧标题"></div>`''')
    image = tmp_path / 'test.png'
    image.write_bytes(b'png')
    runner = Runner({'platform': 'kuaishou', 'profile': str(tmp_path),
                     'action': 'prepare_workbench', 'config': {'identity': {'target': '#shop', 'value': '测试店'}},
                     'media': {'img': str(image)}, 'payload': {'fingerprint': 'reviewed', 'upload_images': True,
                       'product': {'title': '新标题', 'image_ids': ['img'], 'detail_image_ids': ['img'], 'skus': []},
                       'schema': {'fields': []}, 'values': {}}})
    runner.page, runner.context = page, page.context
    monkeypatch.setattr(forms, 'open_editor', lambda current: None)
    monkeypatch.setattr('app.kuaishou_submission.fill_skus', lambda *args: {'missing': []})

    def prepare(current, *args):
        for control in current.locator('input[type=file]').all():
            control.set_input_files(str(image))
        return {'filled': ['image_ids', 'detail_image_ids'], 'missing': []}

    monkeypatch.setattr(kuaishou_commerce, 'prepare', prepare)
    schema = forms.collect(page, 'kuaishou', enumerate_options=False)
    runner.payload['values'] = {f['key']: ['img'] if f['type'] == 'images' else '新标题'
                                for f in schema['fields']}
    runner.payload['schema'] = schema
    forms.run(runner, 'prepare_workbench')
    assert page.locator('input[type=file]').evaluate_all("els=>els.map(e=>e.dataset.uploads)") == ['1', '1']
    assert page.locator('input:not([type])').input_value() == '新标题'


@pytest.mark.parametrize('mode,expected', [('create', 'product_create'), ('update', 'product_update')])
def test_custom_workflow_dispatches_mode_and_preserves_stock_boundary(page, monkeypatch, mode, expected):
    workflow = {'result': {'external_id': '#id'}}
    payload = {'mode': mode, 'fingerprint': 'reviewed', 'values': {}, 'schema': {'fields': []},
               'product': {'title': '商品', 'skus': [{'code': 'A1', 'price_cents': 1299, 'stock': 12}]}}
    before = copy.deepcopy(payload)
    runner = Runner({'platform': 'douyin', 'profile': '/tmp/profile', 'action': 'submit_workbench',
                     'config': {'identity': {'target': '#shop', 'value': '测试店'},
                                'workflows': {expected: workflow}}, 'media': {}, 'payload': payload})
    runner.page, runner.context = page, page.context
    monkeypatch.setattr(forms, 'open_editor', lambda current: None)
    monkeypatch.setattr(forms, 'collect', lambda *args: {'complete': True, 'fields': []})
    monkeypatch.setattr(forms, 'quality', lambda *args: evidence())
    seen = {}

    def operate():
        seen.update(action=runner.action, product=copy.deepcopy(runner.payload['product']))
        return {'external_id': '123', 'sku_map': {}}

    monkeypatch.setattr(runner, 'operate', operate)
    forms.run(runner, 'submit_workbench')
    assert seen['action'] == expected
    assert runner.action == 'submit_workbench'
    assert seen['product']['skus'][0]['price'] == '12.99'
    if mode == 'update':
        assert 'stock' not in seen['product']['skus'][0]
    else:
        assert seen['product']['skus'][0]['stock'] == 0
    assert payload == before


def test_unified_custom_workflow_is_validated_before_worker_launch(store, commerce_setup, monkeypatch):
    configured = commerce_setup(store)
    configured['templates']['commerce_browser'] = {
        'identity': {'target': '#shop', 'value': '测试店'},
        'workflows': {'product_create': {'url': '/new', 'steps': [], 'result': {'external_id': '#id'}}}}
    browser = commerce_browser.CommerceBrowser(configured)
    launched = []
    monkeypatch.setattr(browser, '_run', lambda *args: launched.append(True))
    with pytest.raises(Blocked):
        browser.submit_workbench({'mode': 'create'})
    assert not launched


def test_previously_observed_platform_score_cannot_become_optional(store, product, commerce_setup):
    from app import db, publish_workbench as wb
    commerce_setup(store)
    created = wb.create([product['id']], [store['id']])
    with db.connection() as conn:
        draft = wb._draft(conn, created['drafts'][0]['id'])
        target = wb._targets(conn, draft['id'])[0]
        target['platform'] = 'douyin'
        target['quality'] = {}
        assert wb._payload(conn, draft, target, {})['require_platform_quality'] is False
        target['quality'] = {'source': 'platform_dom', 'score': 97}
        assert wb._payload(conn, draft, target, {})['require_platform_quality'] is True


def test_unknown_structural_controls_report_missing_instead_of_silent_success(page):
    fields = [
        {'key': 'category', 'label': '类目', 'type': 'multiselect', 'semantic': 'category_path'},
        {'key': 'skus', 'label': '价格库存', 'type': 'skus', 'semantic': 'skus'}]
    missing = forms.fill(page, {'fields': fields}, {'category': ['箱包', '旅行箱'],
                         'skus': [{'code': 'A1', 'price_cents': 1299}]},
                         media={}, upload_images=False, guard=lambda: None)
    assert {item['key'] for item in missing} == {'category', 'skus'}


@pytest.mark.parametrize('required', [False, True])
def test_unavailable_platform_score_is_not_invalid_optional_evidence(page, monkeypatch, required):
    runner = Runner({'platform': 'douyin', 'profile': '/tmp/profile', 'action': 'prepare_workbench',
                     'config': {'identity': {'target': '#shop', 'value': '测试店'}}, 'media': {},
                     'payload': {'product': {'skus': []}, 'schema': {'fields': []}, 'values': {},
                                 'fingerprint': 'reviewed', 'require_platform_quality': required}})
    runner.page, runner.context = page, page.context
    monkeypatch.setattr(forms, 'open_editor', lambda current: None)
    result = forms.run(runner, 'prepare_workbench')
    if required:
        assert result['quality']['score'] is None
    else:
        assert result['quality'] == {}
