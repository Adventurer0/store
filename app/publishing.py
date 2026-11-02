"""Built-in single-spec-dimension product mapping and per-store media upload."""
import copy
import hashlib
import re
import time
import zlib
from urllib.parse import urlsplit
import httpx
from . import db
from .connectors import Blocked, Rejected, Uncertain
from .inventory import listing as decode_listing


def external_number(*parts):
    # Numeric identifiers are persisted deterministically, never JavaScript Numbers.
    return int(hashlib.sha256('\0'.join(parts).encode()).hexdigest()[:14], 16)


def merged_config(store, listing):
    base = copy.deepcopy(store.get('templates', {}).get('publish_defaults', {}))
    base.update(decode_listing(listing).get('publish_config', {}))
    return base


def protect_stock(value, creating):
    """Product content writes never reset remote stock outside the budget flow."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in ('stock_num_map', 'warehouse_stock', 'batch_stock_items'):
                raise Blocked('当前统一库存只支持普通库存，区域仓 / 批次库存须单独适配')
            if key in ('skuStockChangeType', 'skuChangeStock'):
                continue
            if key in ('stock', 'stock_num', 'skuStock'):
                if creating:
                    result[key] = 0
                continue
            if key in ('spec_prices', 'spec_prices_v2') and isinstance(item, str):
                try:
                    item = db.dumps(protect_stock(json_load(item), creating))
                except ValueError:
                    raise Blocked('规格价格 JSON 无效') from None
                result[key] = item
            else:
                result[key] = protect_stock(item, creating)
        return result
    if isinstance(value, list):
        return [protect_stock(item, creating) for item in value]
    return value


def built_in_payload(store, product, listing, action):
    listing = decode_listing(listing)
    config, p = merged_config(store, listing), store['platform']
    if not config.get('category_id') or config.get('freight_id') in (None, '') or '__REQUIRED__' in db.dumps(config):
        raise Blocked('请配置此商品 / 店铺的类目 ID 与运费模板 ID（包邮模板可填 0）')
    imgs = product.get('images', [])
    mids = product.get('image_ids', []) or ([product['cover_id']] if product.get('cover_id') else [])
    if not imgs and not mids:
        raise Blocked('请上传商品主图或填写素材 URL')
    if p == 'douyin' and not 3 <= len(imgs) + len(mids) <= 5:
        raise Blocked('当前抖店标准映射要求 3–5 张主图')
    # Preflight uses explicit placeholders; execution replaces them with real URLs.
    imgs = imgs + ['https://pending.shopdock.invalid/' + mid for mid in mids]
    details = product.get('detail_images', []) + ['https://pending.shopdock.invalid/' + mid for mid in product.get('detail_image_ids', [])]
    update = action == 'product_update'
    sku_map = listing['sku_map']
    if update and any(s['code'] not in sku_map for s in product['skus']):
        raise Blocked('更新商品前请补齐每个本地 SKU 对应的平台 SKU ID')
    native = copy.deepcopy(config.get('native', {}))
    if not isinstance(native, dict):
        raise Blocked('平台附加字段 native 必须是 JSON 对象')
    name = config.get('spec_name', '规格')
    if p == 'wechat':
        payload = {**native, 'title': product['title'], 'head_imgs': imgs,
                   'cats': [{'cat_id': str(config['category_id'])}],
                   'express_info': {'template_id': str(config['freight_id'])},
                   'desc_info': {'desc': product['description'], 'imgs': details},
                   'skus': []}
        payload['product_id' if update else 'out_product_id'] = listing['external_id'] if update else product['id']
        for s in product['skus']:
            sku = {**config.get('sku_native', {}).get(s['code'], {}), 'out_sku_id': s['code'], 'sale_price': s['price_cents'], 'sku_attrs': [{'attr_key': name, 'attr_value': s['name']}]}
            if update:
                sku['sku_id'] = sku_map[s['code']]
            else:
                sku['stock_num'] = 0
            payload['skus'].append(sku)
    elif p == 'kuaishou':
        payload = {**native, 'title': product['title'], 'categoryId': int(config['category_id']), 'expressTemplateId': int(config['freight_id']), 'imageUrls': imgs, 'detailImageUrls': details, 'details': product['description'], 'skuList': []}
        payload['itemId' if update else 'relItemId'] = int(listing['external_id']) if update else external_number(product['id'])
        for s in product['skus']:
            sku = {**config.get('sku_native', {}).get(s['code'], {}), 'relSkuId': external_number(product['id'], s['code']), 'skuSalePrice': s['price_cents'], 'skuProps': [{'propName': name, 'propValueName': s['name'], 'isMainProp': 1}]}
            if update:
                sku['skuId'] = int(sku_map[s['code']])
            else:
                sku['skuStock'] = 0
            payload['skuList'].append(sku)
    elif p == 'douyin':
        payload = {**native, 'name': product['title'], 'category_leaf_id': int(config['category_id']), 'freight_id': int(config['freight_id']), 'product_type': 0,
                   'pic': '|'.join(imgs), 'description': '|'.join(details or imgs), 'spec_info': db.dumps([{'name': name, 'values': [{'name': s['name']} for s in product['skus']]}]), 'spec_prices': ''}
        skus = []
        for s in product['skus']:
            sku = {**config.get('sku_native', {}).get(s['code'], {}), 'spec_detail_name1': s['name'], 'price': s['price_cents'], 'code': s['code'], 'out_sku_id': external_number(product['id'], s['code'])}
            if update:
                sku['sku_id'] = int(sku_map[s['code']])
            else:
                sku['stock_num'] = 0
            skus.append(sku)
        payload['spec_prices'] = db.dumps(skus)
        payload['product_id' if update else 'out_product_id'] = int(listing['external_id']) if update else external_number(product['id'])
    else:
        raise Blocked('此平台没有内置商品发布映射')
    if '__REQUIRED__' in db.dumps(payload):
        raise Blocked('类目附加字段尚未填写完整')
    return payload


def trusted_url(value, suffixes):
    parts = urlsplit(value)
    host = parts.hostname or ''
    if parts.scheme != 'https' or parts.username or parts.password or parts.port not in (None, 443) or not any(host == s or host.endswith('.' + s) for s in suffixes):
        raise Blocked('平台返回了未识别的上传域名，已停止；请更新连接器后重试')
    return value


def upload_image(api, mid):
    media = db.one('SELECT * FROM media WHERE id=? AND kind=\'image\'', (mid,))
    if not media or media['mime'] not in ('image/jpeg', 'image/png'):
        raise Blocked('商品发布图片请使用 JPG / PNG')
    cached = db.one('SELECT * FROM image_cache WHERE store_id=? AND media_id=?', (api.store['id'], mid))
    if cached and cached['created'] > db.now() - 86400:
        return cached['url']
    path = db.DATA / media['path']
    if not path.is_file() or path.stat().st_size > (2 if api.platform == 'kuaishou' else 3) * 1024 * 1024:
        raise Blocked('图片丢失或过大：快手限 2MB，当前其他通道限 3MB')
    blob = path.read_bytes()
    if api.platform == 'wechat':
        result = api.call('shop/ec/basics/img/upload', {}, write=True, files={'media': (media['name'], blob, media['mime'])}, query={'upload_type': 0, 'resp_type': 1})
        url = result.get('img_info', {}).get('temp_img_url') or result.get('img_info', {}).get('img_url')
    elif api.platform == 'kuaishou':
        result = api.call('open.item.image.upload', {'uploadType': 1}, write=True, files={'imgBytes': (media['name'], blob, media['mime'])})
        data = result.get('data')
        url = data if isinstance(data, str) else (data or {}).get('imageUrl') or (data or {}).get('imgUrl')
    else:
        token = api.call('open.materialToken', {'file_extension': path.suffix, 'upload_num': 1})
        auth = trusted_url(token.get('data', {}).get('auth_query', ''), ('pstatp.com', 'bytedance.com', 'byteimg.com', 'ibytedtos.com', 'volces.com', 'douyin.com', 'bytescm.com', 'ecombdimg.com'))
        try:
            with httpx.Client(timeout=60, follow_redirects=False, transport=api.transport) as client:
                resp = client.get(auth); resp.raise_for_status()
                address = resp.json()['Result']['UploadAddress']
                host, info = address['UploadHosts'][0], address['StoreInfos'][0]
                target = trusted_url('https://' + host + '/' + info['StoreUri'], ('pstatp.com', 'bytedance.com', 'byteimg.com', 'ibytedtos.com', 'volces.com', 'douyin.com', 'bytescm.com', 'ecombdimg.com'))
                resp = client.put(target, content=blob, headers={'Authorization': info['Auth'], 'Content-CRC32': format(zlib.crc32(blob) & 0xffffffff, 'x')})
                resp.raise_for_status()
                if resp.json().get('success') != 0:
                    raise Rejected('抖店图片二进制上传失败')
        except (httpx.HTTPError, KeyError, ValueError, IndexError):
            raise Blocked('抖店图片上传协议未匹配；商品尚未提交，请检查素材接口权限和当前 SDK 协议') from None
        result = api.call('material.uploadImageSync', {'folder_id': '0', 'file_uri': info['StoreUri'], 'material_name': media['name'][:50], 'need_distinct': True}, write=True)
        material = result.get('data', {}).get('material_info', {})
        for _ in range(5):
            if material.get('byte_url'):
                break
            if not material.get('material_id'):
                break
            time.sleep(2)
            material = api.call('material.queryMaterialDetail', {'material_id': str(material['material_id'])}).get('data', {}).get('material_info', {})
        url = material.get('byte_url')
    if not isinstance(url, str) or not url.startswith('https://'):
        raise Blocked('图片尚在审核或上传返回格式不匹配；商品尚未提交，请稍后重新预检')
    with db.connection() as c:
        c.execute('INSERT INTO image_cache VALUES(?,?,?,?) ON CONFLICT(store_id,media_id) DO UPDATE SET url=excluded.url,created=excluded.created', (api.store['id'], mid, url, db.now()))
    return url


def materialize_request(api, payload):
    encoded = db.dumps(payload)
    mids = list(dict.fromkeys(re.findall(r'https://pending\.shopdock\.invalid/([a-f0-9]{32})', encoded)))
    for mid in mids:
        encoded = encoded.replace('https://pending.shopdock.invalid/' + mid, upload_image(api, mid))
    if 'pending.shopdock.invalid' in encoded:
        raise Blocked('图片关联无效')
    return json_load(encoded)


def json_load(value):
    import json
    return json.loads(value)


def capture_sku_map(store, product, data):
    mapping = {}
    if store['platform'] == 'kuaishou':
        lookup = {str(external_number(product['id'], s['code'])): s['code'] for s in product['skus']}
        for x in data.get('skuIdMapping', data.get('skuInfos', [])):
            remote = x.get('kwaiSkuId') or x.get('skuId')
            if str(x.get('relSkuId')) in lookup and remote:
                mapping[lookup[str(x['relSkuId'])]] = str(remote)
    else:
        lookup = {s['code']: s['code'] for s in product['skus']} if store['platform'] == 'wechat' else {str(external_number(product['id'], s['code'])): s['code'] for s in product['skus']}
        for x in data.get('skus', data.get('sku_list', [])):
            if str(x.get('out_sku_id')) in lookup and x.get('sku_id'):
                mapping[lookup[str(x['out_sku_id'])]] = str(x['sku_id'])
    return mapping


def fetch_sku_map(store, product, mapped):
    from .commerce_browser import CommerceBrowser
    if not mapped.get('external_id'):
        raise Blocked('请先等待商品创建回执或填写平台商品 ID')
    found = CommerceBrowser(store).fetch_sku_map(product, decode_listing(mapped))
    if not found:
        raise Blocked('商品网页没有可按外部 SKU 编码唯一匹配的 ID；请在关联中手动填写，程序不会按位置猜测')
    codes = {sku['code'] for sku in product['skus']}
    if not isinstance(found, dict) or not set(found) <= codes or any(not isinstance(v, str) or not v for v in found.values()) or len(set(found.values())) != len(found):
        raise Blocked('商品网页 SKU 关联无效，请核对后台后手动填写')
    return found
