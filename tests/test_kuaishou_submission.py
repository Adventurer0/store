"""Observed-column DOM contracts. No network or real merchant accounts."""
import copy
import json
import time
import pytest
from app import kuaishou_submission as adapter
from app.commerce_worker import Failure


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


def control(value='', name=''):
    return f'<input aria-label="{name}" value="{value}">'


def create_table(headers=None, *, key='defaultSku'):
    return table(headers or HEADERS, [(key, {'库存': control('88', 'stock'), '价格（元）': control('77', 'price'), 'SKU编码': control('', 'code')})])


def edit_table(*, code='A99', sku_id='900000001', stock='0', price='12.90'):
    return table(EDIT_HEADERS, [('saved', {'当前库存': control(stock), '价格（元）': control(price),
        'SKU编码': control(code), 'SKUID': sku_id, '库存调整': control('0'), '改后库存': '0', '可售库存': '0'})])


def html(content):
    return '<meta charset="utf-8"><div id="shop">测试店</div><main>' + content + '</main>'


@pytest.fixture
def page():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = browser.new_context()
        current = context.new_page()
        current.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(body=html(create_table()), content_type='text/html; charset=utf-8'))
        current.goto(adapter.CREATE_URL)
        yield current
        browser.close()


@pytest.fixture
def product():
    return {'title': '测试商品精确标题', 'skus': [{'code': 'A99', 'name': '默认', 'price_cents': 1290, 'stock': 98765}]}


def guard_for(original):
    def guard(target=None):
        target = target or original
        if target.locator('#shop').inner_text() != '测试店':
            raise Failure('session', '店铺身份不符')
    return guard


def replace(page, body):
    page.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(body=html(body), content_type='text/html; charset=utf-8'))
    page.reload()


def quality():
    return {'score': 97, 'source': 'platform_dom', 'source_url': adapter.CREATE_URL,
            'fingerprint': 'reviewed-fingerprint', 'observed_at': int(time.time())}


def payload(product):
    return {'product': product, 'listing': {}, 'fingerprint': 'reviewed-fingerprint'}


def test_default_sku_fills_current_header_columns_even_when_reordered(page, product):
    headers = ['SKU编码', '是否上架', '价格（元）', '商品条形码', '库存']
    replace(page, create_table(headers))
    result = adapter.fill_skus(page, product, guard_for(page))
    assert result['filled'] == ['skus'] and result['missing'] == []
    assert page.get_by_label('code').input_value() == 'A99'
    assert page.get_by_label('price').input_value() == '12.90'
    assert page.get_by_label('stock').input_value() == '0'
    assert product['skus'][0]['stock'] == 98765


def test_edit_page_is_never_written(page, product):
    page.goto(adapter.EDIT_URL + '?itemId=123')
    with pytest.raises(Failure, match='新增商品'):
        adapter.fill_skus(page, product, guard_for(page))
    assert page.get_by_label('stock').input_value() == '88'


def test_wrong_store_blocks_before_any_write(page, product):
    page.locator('#shop').evaluate("e=>e.textContent='其他店'")
    with pytest.raises(Failure) as caught:
        adapter.fill_skus(page, product, guard_for(page))
    assert caught.value.kind == 'session'
    assert page.get_by_label('stock').input_value() == '88'


def test_multiple_specs_match_attributes_not_row_order(page, product):
    product['skus'] = [
        {'code': 'RED-M', 'price_cents': 1001, 'attributes': {'颜色': '红色', '尺寸': 'M'}},
        {'code': 'BLACK-L', 'price_cents': 2002, 'attributes': {'颜色': '黑色', '尺寸': 'L'}}]
    rows = [('black-L', {'颜色': '黑色', '尺寸': 'L', '库存': control('8'), '价格（元）': control('0'), 'SKU编码': control('')}),
            ('red-M', {'颜色': '红色', '尺寸': 'M', '库存': control('9'), '价格（元）': control('0'), 'SKU编码': control('')})]
    replace(page, table(HEADERS, rows))
    result = adapter.fill_skus(page, product, guard_for(page))
    assert not result['missing']
    first = page.locator('[data-row-key="black-L"]')
    assert first.locator('input').nth(1).input_value() == '20.02'
    assert first.locator('input').nth(2).input_value() == 'BLACK-L'
    assert all(row['stock'] == 0 for row in result['rows'])


def test_uncreated_or_unmapped_dimensions_report_missing_without_filling(page, product):
    product['skus'][0]['attributes'] = {'颜色': '黑色', '尺寸': 'L'}
    result = adapter.fill_skus(page, product, guard_for(page))
    assert result['missing'][0]['field'] == 'skus'
    assert page.get_by_label('stock').input_value() == '88'
    product['skus'] *= 2
    product['skus'][1] = {**product['skus'][1], 'code': 'B'}
    result = adapter.fill_skus(page, product, guard_for(page))
    assert result['missing']
    assert page.get_by_label('price').input_value() == '77'


def test_unknown_row_alignment_is_not_used(page, product):
    markup = create_table().replace('<div class="ant-table-cell"></div>', '', 1)
    replace(page, markup)
    result = adapter.fill_skus(page, product, guard_for(page))
    assert result['missing']
    assert page.get_by_label('stock').input_value() == '88'


def test_saved_sku_ids_are_read_by_external_code_and_never_modified(page, product):
    replace(page, edit_table())
    page.goto(adapter.EDIT_URL + '?itemId=123')
    result = adapter.verify_skus(page, product, guard_for(page), require_ids=True)
    assert result['sku_map'] == {'A99': '900000001'}
    assert result['rows'][0]['price_cents'] == 1290
    assert page.locator('input').nth(1).input_value() == '0'  # inventory adjustment untouched


@pytest.mark.parametrize('score', [90, 89, None, True, 101])
def test_final_submit_requires_actual_score_strictly_above_90(page, product, score):
    replace(page, create_table() + '<button onclick="document.body.dataset.submitted=1">提交审核</button>')
    adapter.fill_skus(page, product, guard_for(page))
    read = lambda: {**quality(), 'score': score}
    with pytest.raises(Failure) as caught:
        adapter.submit(page, page.context, payload(product), guard_for(page), read)
    assert caught.value.kind == 'blocked'
    assert page.locator('body').get_attribute('data-submitted') is None


def test_platform_score_may_be_an_integral_or_decimal_number(page, product):
    for score in (97, 97.0, 97.5):
        result = adapter._quality(lambda: {**quality(), 'score': score}, payload(product), page)
        assert result['score'] == score


def test_final_submit_rechecks_score_immediately_before_click(page, product):
    replace(page, create_table() + '<button onclick="document.body.dataset.submitted=1">提交审核</button>')
    adapter.fill_skus(page, product, guard_for(page))
    reviews = iter([quality(), {**quality(), 'score': 90}])
    committed = []
    with pytest.raises(Failure):
        adapter.submit(page, page.context, payload(product), guard_for(page), lambda: next(reviews), on_commit=lambda: committed.append(1))
    assert committed == []
    assert page.locator('body').get_attribute('data-submitted') is None


def test_required_control_blocks_submission(page, product):
    replace(page, create_table() + '<input required aria-label="必填品牌"><button>提交审核</button>')
    adapter.fill_skus(page, product, guard_for(page))
    with pytest.raises(Failure, match='必填'):
        adapter.submit(page, page.context, payload(product), guard_for(page), quality)


def test_actual_success_receipt_and_readonly_edit_recover_ids(page, product):
    markup = html(create_table() + '''<button onclick="setTimeout(()=>{
      history.pushState({},'', '/zone/goods/nexus/self/release/success?itemId=123456789');
      document.body.insertAdjacentHTML('beforeend','<div>提交审核成功</div>');
    },100)">提交审核</button>''')
    page.context.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(
        body=html(edit_table()) if '/edit/' in route.request.url else markup, content_type='text/html; charset=utf-8'))
    page.unroute('https://s.kwaixiaodian.com/**')
    page.reload()
    adapter.fill_skus(page, product, guard_for(page))
    commits = []
    result = adapter.submit(page, page.context, payload(product), guard_for(page), quality, on_commit=lambda: commits.append(1))
    assert result['external_id'] == '123456789'
    assert result['sku_map'] == {'A99': '900000001'}
    assert result['receipt']['source'] == 'platform_dom'
    assert result['quality']['score'] == 97
    assert commits == [1]
    assert len(page.context.pages) == 1


def test_success_without_reliable_id_is_uncertain(page, product, monkeypatch):
    replace(page, create_table() + '<button onclick="document.body.insertAdjacentHTML(\'beforeend\',\'<div>提交审核成功</div>\')">提交审核</button>')
    adapter.fill_skus(page, product, guard_for(page))
    monkeypatch.setattr(adapter, 'RECEIPT_TIMEOUT', 0.2)
    with pytest.raises(Failure) as caught:
        adapter.submit(page, page.context, payload(product), guard_for(page), quality)
    assert caught.value.kind == 'uncertain'


def test_saved_sku_mismatch_after_submit_is_uncertain(page, product):
    markup = html(create_table() + '''<button onclick="history.pushState({},'', '?itemId=123');document.body.insertAdjacentHTML('beforeend','<div>提交审核成功</div>')">提交审核</button>''')
    page.context.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(
        body=html(edit_table(code='OTHER')) if '/edit/' in route.request.url else markup, content_type='text/html; charset=utf-8'))
    page.unroute('https://s.kwaixiaodian.com/**')
    page.reload()
    adapter.fill_skus(page, product, guard_for(page))
    with pytest.raises(Failure) as caught:
        adapter.submit(page, page.context, payload(product), guard_for(page), quality)
    assert caught.value.kind == 'uncertain'
