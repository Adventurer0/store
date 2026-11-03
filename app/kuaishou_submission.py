"""Observed Kuaishou Nexus SKU table and receipt-only submission adapter.

Creation is the only writable route. Column positions are derived from the
current visible header; edit pages are used strictly to read saved SKU IDs.
There is no HTTP endpoint, application-state inspection or guessed SKU order.
"""
from decimal import Decimal, InvalidOperation
import math
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

try:
    from .commerce_worker import Failure
    from .kuaishou_commerce import CREATE_URL
    from .platform_feedback import extract_code, translate
except ImportError:
    from commerce_worker import Failure
    from kuaishou_commerce import CREATE_URL
    from platform_feedback import extract_code, translate


EDIT_URL = 'https://s.kwaixiaodian.com/zone/goods/nexus/self/release/edit/ordinary'
LIST_URL = 'https://s.kwaixiaodian.com/zone/goods/v1/list'
RECEIPT_TIMEOUT = 30
SUCCESS = re.compile(r'^\s*(?:商品)?(?:已)?(?:提交审核成功|提交成功|发布成功|已提交审核)[！!。]?\s*$')
PRICE = '价格(元)'
CODE = 'SKU编码'
STOCK = '库存'
SKU_ID = 'SKUID'


def _normalized(value):
    return re.sub(r'[\s*＊]+', '', value).replace('（', '(').replace('）', ')').upper()


def _origin(page):
    parts = urlsplit(page.url)
    if (parts.scheme != 'https' or parts.hostname != 's.kwaixiaodian.com'
            or parts.port not in (None, 443) or parts.username or parts.password):
        raise Failure('blocked', '快手商品操作必须位于已核对的商家 HTTPS 页面')
    return parts


def _guard(page, guard, *, create=False, target=False):
    parts = _origin(page)
    if create and parts.path.rstrip('/') != urlsplit(CREATE_URL).path:
        raise Failure('blocked', 'SKU 填写和审核提交只允许在快手新增商品页面执行')
    guard(page) if target else guard()


def _one(locator, label):
    locator = locator.filter(visible=True)
    if locator.count() != 1:
        raise Failure('blocked', f'{label}未唯一识别，请重新读取平台当前要求')
    return locator


def _value(cell):
    controls = cell.locator('input:not([type="hidden"]),textarea,select').filter(visible=True)
    if controls.count() > 1:
        raise Failure('blocked', 'SKU 单元格存在多个可编辑控件，未猜测字段')
    return controls.input_value().strip() if controls.count() else cell.inner_text().strip()


def _input(cell, label):
    return _one(cell.locator('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]),textarea'), label)


def _price(value):
    try:
        amount = Decimal(str(value).strip()) * 100
        if not amount.is_finite() or amount != amount.to_integral_value() or not 0 <= amount <= 100_000_000:
            raise ValueError()
        return int(amount)
    except (InvalidOperation, ValueError, TypeError):
        raise Failure('blocked', '平台 SKU 价格不能准确读为整数分') from None


def _stock(value):
    if not re.fullmatch(r'\d+', str(value).strip()):
        raise Failure('blocked', '平台 SKU 库存不是有效整数')
    return int(str(value).strip())


def _local_skus(product):
    skus = product.get('skus')
    if not isinstance(skus, list) or not 1 <= len(skus) <= 200:
        raise Failure('blocked', '请提供 1–200 个已核对的本地规格')
    codes = set()
    result = []
    for sku in skus:
        if not isinstance(sku, dict):
            raise Failure('blocked', '本地 SKU 资料格式无效')
        code, price = sku.get('code'), sku.get('price_cents')
        if not isinstance(code, str) or not code.strip() or len(code) > 80 or code in codes:
            raise Failure('blocked', '本地 SKU 编码必须非空且唯一')
        if not isinstance(price, int) or isinstance(price, bool) or not 0 <= price <= 100_000_000:
            raise Failure('blocked', '本地 SKU 价格必须是非负整数分')
        attrs = sku.get('attributes', {})
        if not isinstance(attrs, dict) or any(not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip() for k, v in attrs.items()):
            raise Failure('blocked', '多规格需要明确的 attributes 属性名与属性值，不能按名称或行号猜测')
        result.append({**sku, 'attributes': {k.strip(): v.strip() for k, v in attrs.items()}})
        codes.add(code)
    return result


def _table(page, *, editing=False):
    required = {_normalized(PRICE), _normalized(CODE), _normalized('当前库存' if editing else STOCK)}
    candidates = []
    containers = page.locator('.ant-table-container').filter(visible=True)
    for index in range(containers.count()):
        container = containers.nth(index)
        header_rows = container.locator('thead > tr').filter(visible=True)
        if header_rows.count() != 1:
            continue
        headers = header_rows.locator(':scope > th')
        labels = [_normalized(headers.nth(i).inner_text()) for i in range(headers.count())]
        if required <= set(labels):
            candidates.append((container, headers, labels))
    if len(candidates) != 1:
        raise Failure('blocked', '快手 SKU 表格或库存 / 价格 / 编码表头未唯一识别')
    container, headers, labels = candidates[0]
    if len(labels) != len(set(labels)) or any(not label for label in labels):
        raise Failure('blocked', 'SKU 表头为空或重复，未按固定列号猜测')
    if any(headers.nth(i).get_attribute('colspan') not in (None, '1') for i in range(headers.count())):
        raise Failure('blocked', 'SKU 表头分组结构尚未识别，未填写规格')
    rows = container.locator('.ant-table-row[data-row-key]').filter(visible=True)
    if not 1 <= rows.count() <= 200:
        raise Failure('blocked', 'SKU 数据行尚未生成或超过支持范围')
    expanded, active, keys = [], {}, set()
    for index in range(rows.count()):
        row = rows.nth(index)
        key = row.get_attribute('data-row-key')
        if not key or key in keys:
            raise Failure('blocked', 'SKU 数据行标识缺失或重复')
        keys.add(key)
        cells = row.locator(':scope > .ant-table-cell')
        columns = {column: item[0] for column, item in active.items()}
        following = {column: (item[0], item[1] - 1) for column, item in active.items() if item[1] > 1}
        cursor = 0
        for number in range(cells.count()):
            while cursor in columns:
                cursor += 1
            cell = cells.nth(number)
            colspan = cell.get_attribute('colspan') or '1'
            rowspan = cell.get_attribute('rowspan') or '1'
            if colspan != '1' or not rowspan.isdigit() or not 1 <= int(rowspan) <= rows.count() - index or cursor >= len(labels):
                raise Failure('blocked', 'SKU 单元格合并结构未识别，未按位置猜测')
            columns[cursor] = cell
            if int(rowspan) > 1:
                if labels[cursor] in required or labels[cursor] == _normalized(SKU_ID):
                    raise Failure('blocked', '价格、库存或 SKU 编码不能共用合并单元格')
                following[cursor] = (cell, int(rowspan) - 1)
            cursor += 1
        if set(columns) != set(range(len(labels))):
            raise Failure('blocked', 'SKU 行与当前表头没有完整对齐，未填写规格')
        active = following
        expanded.append({'key': key, 'cells': {label: columns[i] for i, label in enumerate(labels)}})
    return labels, expanded


def _matches(page, product, *, editing=False):
    skus = _local_skus(product)
    labels, rows = _table(page, editing=editing)
    if len(rows) != len(skus):
        raise Failure('blocked', '平台已生成规格数量与本地不一致，请先完善颜色 / 尺寸等规格选项')
    if editing:
        by_code = {}
        for row in rows:
            code = _value(row['cells'][_normalized(CODE)])
            if not code or code in by_code:
                raise Failure('blocked', '保存后的 SKU 编码缺失或重复，无法建立可靠关联')
            by_code[code] = row
        if set(by_code) != {s['code'] for s in skus}:
            raise Failure('blocked', '保存后的 SKU 编码未完整匹配本地编码')
        return [(sku, by_code[sku['code']]) for sku in skus]
    if len(skus) == 1 and not skus[0]['attributes']:
        if rows[0]['key'] != 'defaultSku':
            raise Failure('blocked', '当前不是唯一默认规格，请填写明确的颜色 / 尺寸属性')
        return [(skus[0], rows[0])]
    attributes = set().union(*(set(s['attributes']) for s in skus))
    observed = {label for label in labels if label in (_normalized('颜色'), _normalized('尺寸'))}
    if not attributes or any(set(s['attributes']) != attributes for s in skus) or not observed <= {_normalized(a) for a in attributes}:
        raise Failure('blocked', '多规格需完整提供平台颜色 / 尺寸属性，不能按表格行号匹配')
    if any(_normalized(a) not in labels for a in attributes) or any(row['key'] == 'defaultSku' for row in rows):
        raise Failure('blocked', '平台对应规格维度尚未生成，请先选择实际颜色 / 尺寸选项')
    names = sorted(attributes)
    observed_rows = {}
    for row in rows:
        signature = tuple(_value(row['cells'][_normalized(name)]) for name in names)
        if signature in observed_rows:
            raise Failure('blocked', '平台规格属性组合重复，无法唯一关联')
        observed_rows[signature] = row
    expected = [tuple(s['attributes'][name] for name in names) for s in skus]
    if len(set(expected)) != len(expected) or set(expected) != set(observed_rows):
        raise Failure('blocked', '本地属性与平台实际规格组合不一致，未猜测填写')
    return [(sku, observed_rows[signature]) for sku, signature in zip(skus, expected)]


def verify_skus(page, product, guard, *, require_ids=False):
    """Read visible values; on edit routes SKU external codes establish identity."""
    _guard(page, guard, create=not require_ids, target=require_ids)
    if require_ids and urlsplit(page.url).path.rstrip('/') != urlsplit(EDIT_URL).path:
        raise Failure('blocked', '平台 SKU ID 只能从已核对的商品编辑页只读回读')
    normalized, mapping = [], {}
    for sku, row in _matches(page, product, editing=require_ids):
        cells = row['cells']
        code = _value(cells[_normalized(CODE)])
        price = _price(_value(cells[_normalized(PRICE)]))
        stock = _stock(_value(cells[_normalized('当前库存' if require_ids else STOCK)]))
        if code != sku['code'] or price != sku['price_cents'] or stock != 0:
            raise Failure('blocked', 'SKU 编码、价格或零库存没有通过平台表单回读核对')
        item = {'code': code, 'price_cents': price, 'stock': stock, 'attributes': sku['attributes']}
        if require_ids:
            if _normalized(SKU_ID) not in cells:
                raise Failure('blocked', '平台已保存 SKU 表缺少 SKUID 列')
            value = _value(cells[_normalized(SKU_ID)])
            ids = set(re.findall(r'(?<!\d)\d{1,100}(?!\d)', value))
            if len(ids) != 1:
                raise Failure('blocked', '平台 SKUID 无法唯一读取')
            item['sku_id'] = ids.pop()
            if item['sku_id'] in mapping.values():
                raise Failure('blocked', '平台 SKUID 重复，未建立不可靠关联')
            mapping[code] = item['sku_id']
        normalized.append(item)
    return {'rows': normalized, 'sku_map': mapping}


def fill_skus(page, product, guard):
    """Fill every observed SKU by header and property identity, or report missing."""
    _guard(page, guard, create=True)
    try:
        matches = _matches(page, product)
        # Resolve every cell first: no partial filling on an ambiguous table.
        targets = [(sku, {key: _input(row['cells'][_normalized(label)], label)
                         for key, label in (('code', CODE), ('price', PRICE), ('stock', STOCK))}) for sku, row in matches]
        for sku, controls in targets:
            for field, value in (('code', sku['code']), ('price', format(Decimal(sku['price_cents']) / 100, '.2f')), ('stock', '0')):
                _guard(page, guard, create=True)
                controls[field].fill(value, timeout=15000)
                controls[field].press('Tab')
        verified = verify_skus(page, product, guard)
        return {'filled': ['skus'], 'missing': [], **verified}
    except Failure as exc:
        if exc.kind in ('session', 'uncertain'):
            raise
        return {'filled': [], 'rows': [], 'sku_map': {}, 'missing': [
            {'field': 'skus', 'label': '商品规格', 'reason': exc.message}]}


def _required(page):
    invalid = page.locator('input,textarea,select,[aria-invalid="true"]').filter(visible=True).evaluate_all(r'''els => els.filter(e => {
      if (e.getAttribute('aria-invalid') === 'true') return true;
      if (['hidden','password','submit','button'].includes(e.type) || e.disabled) return false;
      return (e.required || e.getAttribute('aria-required') === 'true') &&
        ((!e.value || !e.value.trim()) || (e.type === 'checkbox' && !e.checked) || (e.validity && !e.validity.valid));
    }).length''')
    errors = page.locator('.ant-form-item-explain-error,[role="alert"][aria-live="assertive"]').filter(visible=True)
    if invalid or errors.count():
        text = page.locator('body').inner_text()[:20000]
        raise Failure('blocked', '平台仍显示必填项或校验错误，未提交审核', code=extract_code(text))


def _quality(read_quality, payload, page):
    result = read_quality()
    score = result.get('score') if isinstance(result, dict) else None
    if (not isinstance(result, dict) or result.get('source') != 'platform_dom'
            or not isinstance(score, (int, float)) or isinstance(score, bool)
            or not math.isfinite(score) or not 90 < score <= 100
            or not payload.get('fingerprint') or result.get('fingerprint') != payload['fingerprint']
            or not isinstance(result.get('observed_at'), int) or abs(time.time() - result['observed_at']) > 30
            or urlsplit(result.get('source_url', '')).scheme != 'https'
            or urlsplit(result.get('source_url', '')).hostname != 's.kwaixiaodian.com'
            or urlsplit(result.get('source_url', '')).path.rstrip('/') != urlsplit(page.url).path.rstrip('/')):
        raise Failure('blocked', '提交前必须读取本次商品的平台真实评分且高于90分')
    return result


def _success(page):
    candidates = page.get_by_text(SUCCESS, exact=True).filter(visible=True)
    messages = {candidates.nth(i).inner_text().strip() for i in range(candidates.count())}
    return sorted(messages)


def _item_id(page):
    _origin(page)
    values = parse_qs(urlsplit(page.url).query).get('itemId', [])
    ids = {v for v in values if re.fullmatch(r'\d{1,100}', v)}
    labels = page.get_by_text(re.compile(r'^\s*商品\s*(?:ID|编号)\s*[:：]\s*\d{1,100}\s*$', re.I), exact=True).filter(visible=True)
    for i in range(labels.count()):
        ids.add(re.search(r'\d{1,100}', labels.nth(i).inner_text()).group())
    if len(ids) > 1:
        raise Failure('uncertain', '提交回执出现多个不同商品 ID，请人工核对快手后台')
    return next(iter(ids), '')


def submit(page, context, payload, guard, read_quality, *, on_commit=None):
    """Click the observed final button only after fresh DOM checks; no retries."""
    _guard(page, guard, create=True)
    if payload.get('listing', {}).get('external_id'):
        raise Failure('blocked', '已有平台商品关联不能作为新建商品提交')
    product = payload.get('product', {})
    verify_skus(page, product, guard)
    _required(page)
    review = _quality(read_quality, payload, page)
    button = _one(page.get_by_role('button', name='提交审核', exact=True), '提交审核按钮')
    if not button.is_enabled():
        raise Failure('blocked', '平台提交审核按钮不可用，请补齐页面要求')
    if _success(page) or _item_id(page):
        raise Failure('blocked', '新增页面已存在提交回执或商品 ID，未重复点击审核')
    _guard(page, guard, create=True)
    _required(page)
    verify_skus(page, product, guard)
    review = _quality(read_quality, payload, page)
    if on_commit is not None:
        on_commit()
    detail = None
    try:
        button.click(timeout=15000)
        deadline = time.monotonic() + RECEIPT_TIMEOUT
        messages, external = [], ''
        while time.monotonic() < deadline:
            _origin(page)
            messages = _success(page)
            if messages:
                external = _item_id(page)
                if external:
                    break
            page.wait_for_timeout(250)
        if not messages or not external:
            text = page.locator('body').inner_text()[:20000]
            code = extract_code(text)
            message, code = translate('kuaishou', code, '已点击审核但未读取到明确的新商品成功回执与商品 ID，请核对后台，勿重复提交')
            raise Failure('uncertain', message, code=code)
        guard(page)
        receipt_url = urlunsplit((*urlsplit(page.url)[:3], '', ''))
        detail = context.new_page()
        detail.goto(EDIT_URL + '?' + urlencode({'itemId': external}), wait_until='domcontentloaded', timeout=45000)
        _guard(detail, guard, target=True)
        if _item_id(detail) != external:
            raise Failure('uncertain', '只读商品详情的 ID 与新建回执不一致')
        # Wait for the observed edit table without changing any existing product.
        detail.locator('.ant-table-container').filter(visible=True).first.wait_for(state='visible', timeout=20000)
        saved = verify_skus(detail, product, guard, require_ids=True)
        return {'external_id': external, 'sku_map': saved['sku_map'], 'quality': review,
                'receipt': {'source': 'platform_dom', 'source_url': receipt_url, 'messages': messages,
                            'sku_source_url': EDIT_URL, 'observed_at': int(time.time())},
                'message': '快手网页已提交审核，并从商品详情读取到完整 SKU ID 与零库存；审核结果以平台后台为准'}
    except Failure as exc:
        raise Failure('uncertain', exc.message) from None
    except Exception:
        raise Failure('uncertain', '审核提交后页面或回执读取异常，请核对商家后台实际结果，勿直接重试') from None
    finally:
        if detail is not None:
            try:
                detail.close()
            except Exception:
                pass
