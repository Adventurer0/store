"""Local DOM contracts for the prepare-only adapter, never merchant writes."""
import pytest
import json

from app.kuaishou_commerce import CREATE_URL, TITLE_PLACEHOLDER, PrepareBlocked, prepare


IDENTITY = '<div id="shop">测试店</div>'
FORM = IDENTITY + f'''<main><input placeholder="{TITLE_PLACEHOLDER}">
<input id="code"><input id="price"><input id="stock" value="100">
<input id="detail" type="file" multiple>
<button id="submit" onclick="document.body.dataset.submitted='yes'">提交审核</button>
<button id="draft" onclick="document.body.dataset.saved='yes'">保存草稿</button></main>'''


@pytest.fixture
def page():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = browser.new_context()
        page = context.new_page()
        page.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(body=FORM, content_type='text/html; charset=utf-8'))
        page.goto(CREATE_URL)
        yield page
        browser.close()


@pytest.fixture
def inputs():
    return ({'product': {'title': '测试行李箱', 'skus': [
        {'code': 'A99', 'price_cents': 999900, 'stock': 100}],
        'image_ids': [], 'detail_image_ids': []}, 'listing': {}},
        {'identity': {'target': '#shop', 'value': '测试店'},
         'business': {'category_path': ['箱包/饰品', '箱包', '功能箱包/双肩包', '旅行箱'],
                      'brand': '无品牌', 'material': 'ABS+PC', 'closure': '拉链', 'freight_template': '周总'},
         'selectors': {'sku_fields': {'single_default': True, 'code': '#code', 'price': '#price', 'stock': '#stock'}}})


def test_prepare_sets_zero_stock_and_never_submits(page, inputs):
    payload, config = inputs
    result = prepare(page, payload, {}, config)
    assert result['submitted'] is False
    assert result['status'] == 'needs_input'
    assert page.locator('#stock').input_value() == '0'
    assert page.locator('#price').input_value() == '9999.00'
    assert page.locator('#code').input_value() == 'A99'
    assert payload['product']['skus'][0]['stock'] == 100
    assert page.locator('body').get_attribute('data-submitted') is None
    assert page.locator('body').get_attribute('data-saved') is None
    assert {x['field'] for x in result['missing']} >= {'closure', 'merchant_review'}


def test_wrong_identity_stops_before_any_fill(page, inputs):
    payload, config = inputs
    config['identity']['value'] = '其他店铺'
    with pytest.raises(PrepareBlocked, match='身份'):
        prepare(page, payload, {}, config)
    assert page.get_by_placeholder(TITLE_PLACEHOLDER, exact=True).input_value() == ''
    assert page.locator('#stock').input_value() == '100'


def test_delayed_spa_identity_is_waited_before_preparing(page, inputs):
    payload, config = inputs
    delayed = FORM.replace(IDENTITY, '') + '''<script>
      setTimeout(() => document.body.insertAdjacentHTML('afterbegin',
        '<div id="shop">测试店</div>'), 200);
    </script>'''
    page.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(
        body=delayed, content_type='text/html; charset=utf-8'))
    page.reload(wait_until='domcontentloaded')
    result = prepare(page, payload, {}, config)
    assert result['submitted'] is False
    assert 'title' in result['filled']
    assert page.locator('#stock').input_value() == '0'


def test_hidden_duplicate_identity_does_not_override_visible_shop(page, inputs):
    payload, config = inputs
    config['identity']['target'] = '.merchant-id'
    duplicate = FORM.replace(IDENTITY, '<div class="merchant-id" style="display:none">其他店</div>'
                             '<div class="merchant-id">测试店</div>')
    page.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(
        body=duplicate, content_type='text/html; charset=utf-8'))
    page.reload()
    result = prepare(page, payload, {}, config)
    assert 'title' in result['filled']


def test_multiple_visible_identities_block_before_fill(page, inputs):
    payload, config = inputs
    config['identity']['target'] = '.merchant-id'
    duplicate = FORM.replace(IDENTITY, '<div class="merchant-id">测试店</div>'
                             '<div class="merchant-id">测试店</div>')
    page.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(
        body=duplicate, content_type='text/html; charset=utf-8'))
    page.reload()
    with pytest.raises(PrepareBlocked, match='唯一'):
        prepare(page, payload, {}, config)
    assert page.get_by_placeholder(TITLE_PLACEHOLDER, exact=True).input_value() == ''


def test_existing_product_url_is_rejected(page, inputs):
    page.goto('https://s.kwaixiaodian.com/zone/goods/nexus/self/release/edit/ordinary?itemId=123')
    payload, config = inputs
    with pytest.raises(PrepareBlocked, match='新增商品'):
        prepare(page, payload, {}, config)


def test_existing_listing_is_not_prepared_as_new(page, inputs):
    payload, config = inputs
    payload['listing']['external_id'] = '123'
    with pytest.raises(PrepareBlocked, match='已有平台'):
        prepare(page, payload, {}, config)


def test_ambiguous_sku_mapping_does_not_choose_first(page, inputs):
    payload, config = inputs
    config['selectors']['sku_fields']['stock'] = 'input'
    result = prepare(page, payload, {}, config)
    assert 'skus' in {x['field'] for x in result['missing']}
    assert page.locator('#stock').input_value() == '100'


def test_stale_business_selector_cannot_click_submit(page, inputs):
    payload, config = inputs
    config['selectors']['brand'] = '#submit'
    result = prepare(page, payload, {}, config)
    assert 'brand' in {x['field'] for x in result['missing']}
    assert page.locator('body').get_attribute('data-submitted') is None


def test_multispec_is_left_for_review_instead_of_positional_mapping(page, inputs):
    payload, config = inputs
    payload['product']['skus'].append({'code': 'A100', 'price_cents': 100, 'stock': 100})
    result = prepare(page, payload, {}, config)
    assert 'skus' in {x['field'] for x in result['missing']}
    assert page.locator('#stock').input_value() == '100'


def test_first_step_requires_three_main_images(page, inputs):
    page.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(
        body=IDENTITY + '<main><input type="file" multiple></main>', content_type='text/html; charset=utf-8'))
    page.reload()
    payload, config = inputs
    result = prepare(page, payload, {}, config)
    assert result['filled'] == []
    assert result['missing'][0]['field'] == 'image_ids'
    assert result['submitted'] is False


def test_detail_upload_only_uses_current_product_ids(page, inputs, tmp_path):
    payload, config = inputs
    payload['upload_images'] = True
    payload['product']['detail_image_ids'] = ['photo']
    config['selectors']['detail_images'] = '#detail'
    photo = tmp_path / 'photo.png'
    photo.write_bytes(b'\x89PNG\r\n\x1a\nlocal-test')
    result = prepare(page, payload, {'photo': str(photo)}, config)
    assert 'detail_image_ids' in result['filled']
    assert page.locator('#detail').evaluate('(e)=>e.files.length') == 1
    assert page.locator('body').get_attribute('data-submitted') is None
    with pytest.raises(PrepareBlocked, match='此商品'):
        prepare(page, payload, {}, config)


def test_image_permission_is_required_even_when_media_is_available(page, inputs, tmp_path):
    payload, config = inputs
    payload['product']['detail_image_ids'] = ['photo']
    config['selectors']['detail_images'] = '#detail'
    photo = tmp_path / 'photo.png'
    photo.write_bytes(b'\x89PNG\r\n\x1a\nlocal-test')
    result = prepare(page, payload, {'photo': str(photo)}, config)
    assert 'detail_image_ids' not in result['filled']
    assert page.locator('#detail').evaluate('(e)=>e.files.length') == 0


def test_closure_uses_actual_custom_selected_class(page, inputs):
    payload, config = inputs
    page.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(
        body=FORM + '''<div class="kwaishop-goods-nexus-pc-formily-item">*闭合方式
        <div title="拉链" class="kwaishop-goods-nexus-pc-pro-checkableTag"
             onclick="this.classList.add('kwaishop-goods-nexus-pc-pro-checkableTag__checked')">拉链</div>
        <div title="锁扣" class="kwaishop-goods-nexus-pc-pro-checkableTag">锁扣</div></div>''',
        content_type='text/html; charset=utf-8'))
    page.reload()
    result = prepare(page, payload, {}, config)
    assert 'closure' in result['filled']
    assert 'closure' not in {x['field'] for x in result['missing']}
    assert page.locator('[title="拉链"]').get_attribute('class').endswith('__checked')


def test_first_step_waits_for_uploaded_images_before_next(page, inputs, tmp_path):
    payload, config = inputs
    payload['upload_images'] = True
    payload['product']['image_ids'] = ['a', 'b', 'c']
    photo = tmp_path / 'photo.png'
    photo.write_bytes(b'\x89PNG\r\n\x1a\nlocal-test')
    path = '箱包/饰品 > 箱包 > 功能箱包/双肩包 > 旅行箱'
    html = IDENTITY + f'''<main>
    <div class="kwaishop-goods-nexus-pc-formily-item">*商品主图:
      <input type="file" multiple onchange="setTimeout(()=>document.querySelector('#count').textContent='上传图片(3/9)',100)">
      <button id="count">上传图片(0/9)</button>
    </div>
    <div class="kwaishop-goods-nexus-pc-formily-item">*商品类目:
      <div onclick="document.querySelector('#path').textContent='{path}'">旅行箱</div>
      <span class="selectedArea___observed" id="path"></span>
    </div>
    <button onclick="nextStage()">下一步，完善商品信息</button></main>
    <script>function nextStage(){{
      window.nextBeforeReady = !document.querySelector('#count').textContent.includes('(3/9)');
      document.body.innerHTML = {json.dumps(FORM)};
    }}</script>'''
    page.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
    page.reload()
    result = prepare(page, payload, {k: str(photo) for k in ('a', 'b', 'c')}, config)
    assert {'image_ids', 'category_path', 'title', 'skus'} <= set(result['filled'])
    assert page.evaluate('window.nextBeforeReady') is False
    assert result['submitted'] is False


def test_dropdown_is_not_marked_filled_without_selected_readback(page, inputs):
    payload, config = inputs
    html = FORM + '''<div class="kwaishop-goods-nexus-pc-formily-item">*品牌:
      <button role="combobox" onclick="document.querySelector('#option').style.display='block'">选择</button>
      <span class="kwaishop-goods-nexus-pc-select-selection-item" title="其他品牌">其他品牌</span>
    </div><div id="option" role="option" style="display:none">无品牌</div>'''
    page.route('https://s.kwaixiaodian.com/**', lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
    page.reload()
    result = prepare(page, payload, {}, config)
    assert 'brand' not in result['filled']
    assert 'brand' in {x['field'] for x in result['missing']}
