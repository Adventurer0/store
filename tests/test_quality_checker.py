import struct
import zlib

import pytest

from app import quality_checker as quality


def png(width, height):
    header = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    chunk = b'IHDR' + header
    return (b'\x89PNG\r\n\x1a\n' + struct.pack('>I', len(header)) + chunk +
            struct.pack('>I', zlib.crc32(chunk) & 0xffffffff) + b'\x00\x00\x00\x00IEND\xaeB`\x82')


@pytest.fixture
def product():
    return {
        'title': '加厚透明收纳箱大容量', 'description': '带盖收纳箱，适合衣物与玩具整理。' * 16,
        'category': '家居', 'image_ids': ['i%d' % n for n in range(5)],
        'detail_image_ids': ['d1', 'd2', 'd3'],
        'skus': [{'code': 'BOX-L', 'name': '大号', 'price_cents': 9900, 'stock': 0}],
    }


@pytest.fixture
def fields():
    return [
        {'key': 'title', 'label': '商品标题', 'semantic': 'title', 'type': 'text', 'required': True, 'max_length': 60},
        {'key': 'description', 'label': '商品描述', 'semantic': 'description', 'type': 'textarea', 'required': False},
        {'key': 'category', 'label': '商品类目', 'semantic': 'category_path', 'type': 'multiselect', 'required': True},
        {'key': 'images', 'label': '商品主图', 'semantic': 'image_ids', 'type': 'images', 'required': True},
        {'key': 'details', 'label': '详情图', 'semantic': 'detail_image_ids', 'type': 'images', 'required': False},
        {'key': 'skus', 'label': '价格库存', 'semantic': 'skus', 'type': 'skus', 'required': True},
        {'key': 'freight', 'label': '运费模板', 'semantic': 'freight_template', 'type': 'select', 'required': True},
        {'key': 'service', 'label': '售后服务', 'semantic': 'service', 'type': 'text', 'required': False},
    ]


@pytest.fixture
def media(tmp_path, product):
    paths = {}
    for index, media_id in enumerate(product['image_ids']):
        path = tmp_path / (media_id + '.png')
        path.write_bytes(png(300, 400))
        paths[media_id] = str(path)
    for index, media_id in enumerate(product['detail_image_ids']):
        path = tmp_path / (media_id + '.png')
        path.write_bytes(png(800, 600))
        paths[media_id] = str(path)
    return paths


def values(product, **extra):
    result = {
        'title': product['title'], 'description': product['description'],
        'category': ['家居', '收纳'], 'images': product['image_ids'],
        'details': product['detail_image_ids'], 'skus': product['skus'], 'freight': '默认运费', 'service': '7天无理由',
    }
    result.update(extra)
    return result


def test_perfect_local_product_reaches_full_score(product, fields, media):
    result = quality.evaluate(product, values(product), fields, 'douyin', media_paths=media)
    assert result['score'] == 100 and result['passed'] is True
    assert result['blockers'] == []
    assert result['breakdown'] == {'category': 20, 'completeness': 20, 'images': 20,
                                   'detail': 15, 'title': 10, 'sku': 10, 'logistics': 5}


@pytest.mark.parametrize('change,message', [
    ({'category': []}, '类目'),
    ({'title': ''}, '标题'),
    ({'skus': [{'code': 'BOX-L', 'name': '大号', 'price_cents': 0, 'stock': 0}]}, 'SKU'),
    ({'skus': [{'code': 'BOX-L', 'name': '大号', 'price_cents': 9900, 'stock': 2}]}, '库存'),
])
def test_hard_blocks_override_any_score(product, fields, media, change, message):
    result = quality.evaluate(product, values(product, **change), fields, 'douyin', media_paths=media)
    assert result['passed'] is False
    assert any(message in item for item in result['blockers']), result['blockers']


def test_kuaishou_requires_a_three_by_four_image(product, fields, media, tmp_path):
    for media_id in product['image_ids']:
        path = tmp_path / (media_id + '.png')
        path.write_bytes(png(400, 400))
        media[media_id] = str(path)
    result = quality.evaluate(product, values(product), fields, 'kuaishou', media_paths=media)
    assert result['passed'] is False
    assert any('3:4' in item for item in result['blockers'])
    assert result['score'] < 100


def test_missing_required_field_and_unknown_image_size_block(product, fields, media):
    result = quality.evaluate(product, values(product, freight=''), fields, 'douyin', media_paths=media)
    assert result['passed'] is False
    assert any('运费模板' in item for item in result['blockers'])
    broken = quality.evaluate(product, values(product), fields, 'douyin', media_paths={})
    assert broken['passed'] is False
    assert any('尺寸' in item for item in broken['blockers'])


def test_score_below_95_fails_without_a_hard_block(product, fields, media):
    product['detail_image_ids'] = []
    product['image_ids'] = ['i0']
    short = {**values(product), 'details': [], 'description': '短描述'}
    result = quality.evaluate(product, short, fields, 'douyin', media_paths=media)
    assert result['score'] < 95 and result['passed'] is False
    assert result['blockers'] == []


def test_update_mode_allows_existing_stock_but_create_does_not(product, fields, media):
    changed = {'skus': [{'code': 'BOX-L', 'name': '大号', 'price_cents': 9900, 'stock': 8}]}
    assert quality.evaluate(product, values(product, **changed), fields, 'douyin', media_paths=media)['passed'] is False
    assert quality.evaluate(product, values(product, **changed), fields, 'douyin',
                            mode='update', media_paths=media)['passed'] is True


def test_image_dimensions_read_png_and_unknown_bytes(tmp_path):
    path = tmp_path / 'a.png'
    path.write_bytes(png(640, 480))
    assert quality.probe(str(path)) == (640, 480)
    path.write_bytes(b'not-an-image')
    assert quality.probe(str(path)) is None
