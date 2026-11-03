"""Deterministic local pre-flight score; platform evidence remains separate."""
import math
import struct

LOCAL_THRESHOLD = 95
WEIGHTS = {
    'category': 20, 'completeness': 20, 'images': 20, 'detail': 15,
    'title': 10, 'sku': 10, 'logistics': 5,
}
BASE_SEMANTICS = {
    'title', 'description', 'image_ids', 'detail_image_ids', 'skus',
    'category_path', 'category', 'category_id', 'brand', 'material',
    'freight_template', 'freight', 'shipping_template', 'selling_points', 'short_title',
}
IMAGE_SEMANTICS = {'image_ids'}
DETAIL_SEMANTICS = {'detail_image_ids'}


def dimensions(data):
    """Read PNG/JPEG/WebP/GIF dimensions without a third-party image library."""
    try:
        if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
            return struct.unpack('>II', data[16:24])
        if data[:6] in (b'GIF87a', b'GIF89a') and len(data) >= 10:
            return struct.unpack('<HH', data[6:10])
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return _webp(data)
        if data[:2] == b'\xff\xd8':
            return _jpeg(data)
    except (IndexError, struct.error, ValueError):
        return None
    return None


def _webp(data):
    chunk = data[12:16]
    if chunk == b'VP8X' and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], 'little')
        height = 1 + int.from_bytes(data[27:30], 'little')
        return width, height
    if chunk == b'VP8 ' and len(data) >= 30 and data[23:26] == b'\x9d\x01*':
        width = int.from_bytes(data[26:28], 'little') & 0x3fff
        height = int.from_bytes(data[28:30], 'little') & 0x3fff
        return width, height
    if chunk == b'VP8L' and len(data) >= 25 and data[20] == 0x2f:
        bits = int.from_bytes(data[21:25], 'little')
        return 1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff)
    return None


def _jpeg(data):
    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xff:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
            offset += 2
            continue
        length = int.from_bytes(data[offset + 2:offset + 4], 'big')
        if length < 2 or offset + 2 + length > len(data):
            return None
        if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                      0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
            height = int.from_bytes(data[offset + 5:offset + 7], 'big')
            width = int.from_bytes(data[offset + 7:offset + 9], 'big')
            return width, height
        offset += 2 + length
    return None


def probe(path, limit=512 * 1024):
    try:
        with open(path, 'rb') as handle:
            return dimensions(handle.read(limit))
    except (OSError, ValueError):
        return None


def _empty(value):
    return value is None or value == '' or value == []


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _ratio(width, height):
    return width / height if width and height else 0


def evaluate(product, values, fields, platform, mode='create', media_paths=None):
    """Return a 0–100 local score plus hard blockers; never claims official score."""
    media_paths = media_paths or {}
    semantic = {}
    for field in fields or []:
        key, name = field.get('key'), field.get('semantic') or field.get('canonical') or ''
        if name and key in values:
            semantic[name] = values[key]
    product = product or {}

    title = _text(semantic.get('title', product.get('title')))
    description = _text(semantic.get('description', product.get('description')))
    images = semantic.get('image_ids', product.get('image_ids', []))
    details = semantic.get('detail_image_ids', product.get('detail_image_ids', []))
    skus = semantic.get('skus', product.get('skus', []))
    category = semantic.get('category_path') or semantic.get('category') or \
        semantic.get('category_id') or product.get('category') or ''
    blockers = []
    breakdown = {}

    required = [field for field in fields or [] if field.get('required')]
    missing = [field for field in required if _empty(values.get(field.get('key')))]
    if not fields:
        blockers.append('尚未读取平台要求，不能计算本地质量分')
    if missing:
        blockers.append('必填项未完成：' + '、'.join(str(f.get('label') or f.get('key'))[:40] for f in missing[:8]))
    completeness = 0 if not required else WEIGHTS['completeness'] * (len(required) - len(missing)) / len(required)
    breakdown['completeness'] = round(completeness, 1)

    category_present = bool(category) and category not in ([], {}, '')
    if not category_present:
        blockers.append('请选择平台商品类目')
    attrs = [field for field in required if field.get('semantic') not in BASE_SEMANTICS]
    filled_attrs = [field for field in attrs if not _empty(values.get(field.get('key')))]
    category_score = (10 if category_present else 0)
    category_score += 10 * (len(filled_attrs) / len(attrs)) if attrs else (10 if category_present else 0)
    breakdown['category'] = round(category_score, 1)

    image_ids = images if isinstance(images, list) else []
    image_field = any(field.get('semantic') in IMAGE_SEMANTICS for field in fields or [])
    image_active = image_field or bool(image_ids)
    minimum = 3 if platform == 'kuaishou' else 1
    count_score = 10 if len(image_ids) >= 5 else 6 if len(image_ids) >= minimum else 0
    if image_active and len(image_ids) < minimum:
        blockers.append(f'至少需要 {minimum} 张商品主图')
    known, sizes = [], []
    for media_id in image_ids:
        path = media_paths.get(media_id)
        size = probe(path) if path else None
        if size:
            known.append(size)
            sizes.append(size)
        else:
            known.append(None)
    dimension_score = 5 if image_ids and all(known) else 0
    if image_ids and not all(known):
        blockers.append('部分商品主图无法读取尺寸，请使用本地仓库中的真实图片')
    if platform == 'kuaishou':
        aspect_score = 5 if any(abs(_ratio(*size) - 3 / 4) <= 0.02 for size in sizes) else 0
        if image_active and image_ids and not aspect_score:
            blockers.append('快手主图需至少一张 3:4 竖图')
    else:
        aspect_score = 5 if any(0.5 <= _ratio(*size) <= 2 for size in sizes) else 0
    breakdown['images'] = round(count_score + dimension_score + aspect_score, 1) if image_active else 0

    detail_ids = details if isinstance(details, list) else []
    detail_field = any(field.get('semantic') in DETAIL_SEMANTICS for field in fields or [])
    detail_active = detail_field or bool(detail_ids)
    detail_score = 10 if len(detail_ids) >= 3 else 6 if detail_ids else 0
    detail_score += 5 if len(description) >= 200 else 3 if len(description) >= 50 else 0
    breakdown['detail'] = round(min(detail_score, WEIGHTS['detail']), 1) if detail_active else 0

    if not title:
        blockers.append('请填写商品标题')
    title_limit = next((field.get('max_length') for field in fields or []
                        if field.get('semantic') == 'title' and field.get('max_length')), 60)
    if title and len(title) > int(title_limit):
        blockers.append(f'商品标题超过平台上限 {int(title_limit)} 字')
    title_score = 6 if 5 <= len(title) <= int(title_limit) else 3 if title else 0
    title_score += 4 if len(description) >= 20 or _text(semantic.get('selling_points')) else 0
    breakdown['title'] = round(title_score, 1)

    valid_skus = []
    if not isinstance(skus, list) or not skus:
        blockers.append('请至少提供一个商品规格')
    else:
        codes = set()
        for sku in skus:
            if not isinstance(sku, dict):
                continue
            code, price = sku.get('code'), sku.get('price_cents')
            stock = sku.get('stock', 0)
            if (not isinstance(code, str) or not code.strip() or code in codes
                    or not isinstance(price, int) or isinstance(price, bool) or price <= 0
                    or not isinstance(stock, int) or isinstance(stock, bool) or stock < 0):
                continue
            if mode == 'create' and stock != 0:
                continue
            codes.add(code)
            valid_skus.append(sku)
        if len(valid_skus) != len(skus):
            blockers.append('SKU 编码、价格或库存无效；新建商品库存必须为零')
    sku_score = (6 if valid_skus else 0) + (4 if valid_skus else 0)
    breakdown['sku'] = round(sku_score, 1)

    logistics = semantic.get('freight_template', semantic.get('freight', semantic.get('shipping_template')))
    logistics_field = any(field.get('semantic') in ('freight_template', 'freight', 'shipping_template')
                          for field in fields or [])
    logistics_active = logistics_field or not _empty(logistics)
    service = any(field.get('semantic') in ('service', 'after_sale') and not _empty(values.get(field.get('key')))
                  for field in fields or [])
    logistics_score = (3 if not _empty(logistics) else 0) + (2 if service else 0)
    breakdown['logistics'] = round(logistics_score, 1) if logistics_active else 0

    active = {'category', 'completeness', 'title', 'sku'}
    if image_active:
        active.add('images')
    if detail_active:
        active.add('detail')
    if logistics_active:
        active.add('logistics')
    possible = sum(WEIGHTS[name] for name in active)
    earned = sum(breakdown[name] for name in active)
    score = max(0, min(100, round(earned * 100 / possible, 1))) if possible else 0
    return {'score': score, 'threshold': LOCAL_THRESHOLD, 'comparison': 'gte',
            'passed': score >= LOCAL_THRESHOLD and not blockers,
            'blockers': list(dict.fromkeys(blockers)), 'breakdown': breakdown,
            'image_count': len(image_ids), 'detail_image_count': len(detail_ids)}


def valid_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
