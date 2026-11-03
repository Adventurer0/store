import json
from pathlib import Path

import pytest

from app import db


PNG = b'\x89PNG\r\n\x1a\n' + b'media-library-test-image'
VIDEO = b'\x00\x00\x00\x18ftypmp42' + b'media-library-test-video'


def upload(owner, name='photo.png', blob=PNG, **metadata):
    result = owner.post('/api/media', files={'file': (name, blob, 'application/octet-stream')}, data=metadata)
    assert result.status_code == 200, result.text
    return result.json()


def active_job(store, mid, status='queued', payload=None):
    jid = db.uid()
    with db.connection() as connection:
        connection.execute('''INSERT INTO jobs(id,batch_id,kind,store_id,entity_id,status,scheduled,payload,dedupe_key,created,updated)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                           (jid, db.uid(), 'video_publish', store['id'], mid, status, db.now(),
                            db.dumps(payload or {'media_id': mid}), db.uid(), db.now(), db.now()))
    return jid


def test_media_catalogue_requires_login_and_metadata_mutations_require_csrf(client, owner):
    row = upload(owner)
    path = '/api/media/' + row['id']
    assert owner.patch(path, json={'category': '箱包'}, headers={'X-CSRF-Token': ''}).status_code == 403
    assert owner.delete(path, headers={'X-CSRF-Token': ''}).status_code == 403
    owner.post('/api/auth/logout', json={})
    assert client.get('/api/media').status_code == 401
    assert client.delete(path).status_code == 401


def test_list_filter_pagination_and_state_use_safe_metadata(owner):
    first = upload(owner, '旅行箱.png', category='主图', tags=json.dumps(['银色', '新品'], ensure_ascii=False))
    upload(owner, '尺寸说明.png', category='详情', tags=json.dumps(['银色'], ensure_ascii=False))
    upload(owner, 'preview.mp4', VIDEO, category='视频')
    result = owner.get('/api/media', params={'kind': 'image', 'category': '主图', 'tag': '银色', 'q': '旅行', 'offset': 0, 'limit': 1})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['total'] == 1 and len(data['rows']) == 1
    assert data['rows'][0]['id'] == first['id']
    assert data['rows'][0]['tags'] == ['银色', '新品']
    assert data['rows'][0]['url'] == '/api/media/' + first['id']
    assert 'path' not in data['rows'][0] and 'sha256' not in data['rows'][0]
    assert {item['name'] for item in data['categories']} == {'主图', '详情'}
    assert owner.get('/api/media', params={'kind': 'image', 'offset': 1, 'limit': 1}).json()['total'] == 2
    assert owner.get('/api/media', params={'kind': 'audio'}).status_code == 422
    state = owner.get('/api/state').json()
    assert next(item for item in state['media'] if item['id'] == first['id'])['category'] == '主图'


def test_literal_search_and_tag_filter_do_not_treat_wildcards_as_sql(owner):
    upload(owner, '100%_exact.png', tags=json.dumps(['a%b']))
    upload(owner, '100other.png', tags=json.dumps(['axb']))
    assert owner.get('/api/media', params={'q': '%_'}).json()['total'] == 1
    assert owner.get('/api/media', params={'tag': 'a%b'}).json()['total'] == 1
    assert owner.get('/api/media', params={'q': "' OR 1=1 --"}).json()['total'] == 0


def test_edit_metadata_and_batch_classification_preserve_file_bytes(owner):
    first, second = upload(owner, 'a.png'), upload(owner, 'b.png')
    result = owner.patch('/api/media/' + first['id'], json={'name': '新名称.png', 'category': '主图', 'tags': ['新品', '新品', '银色']})
    assert result.status_code == 200, result.text
    assert result.json()['tags'] == ['新品', '银色']
    assert owner.get('/api/media/' + first['id']).content == PNG
    result = owner.post('/api/media/batch-update', json={'ids': [first['id'], second['id'], 'f' * 32], 'category': '归档', 'tags': []})
    assert set(result.json()['updated']) == {first['id'], second['id']}
    assert result.json()['missing'] == ['f' * 32]
    assert owner.get('/api/media', params={'category': '归档'}).json()['total'] == 2
    assert owner.patch('/api/media/' + first['id'], json={'name': '../escape.png'}).status_code == 422
    assert owner.patch('/api/media/' + first['id'], json={}).status_code == 422


def test_directory_import_preserves_relative_labels_without_host_path_access(owner):
    result = owner.post('/api/media/import', files=[
        ('files', ('a.png', PNG, 'image/png')), ('files', ('clip.mp4', VIDEO, 'video/mp4'))], data={
            'relative_paths': json.dumps(['商品资料/主图/a.png', '商品资料/视频/clip.mp4'], ensure_ascii=False),
            'category': '秋季上新', 'tags': json.dumps(['精选'], ensure_ascii=False)})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['imported'] == 2 and data['errors'] == []
    assert data['rows'][0]['source_path'] == '商品资料/主图/a.png'
    assert data['rows'][0]['tags'] == ['精选', '商品资料', '主图']
    assert all(item['category'] == '秋季上新' for item in data['rows'])
    assert all(owner.get(item['url']).status_code == 200 for item in data['rows'])
    assert not (db.DATA / '商品资料').exists()


@pytest.mark.parametrize('relative', ['../private.png', '/etc/private.png', 'C:\\private.png', 'folder/../../private.png', '//server/private.png'])
def test_import_rejects_absolute_and_traversing_relative_paths_before_saving(owner, relative):
    result = owner.post('/api/media/import', files=[('files', ('safe.png', PNG, 'image/png'))],
                        data={'relative_paths': json.dumps([relative])})
    assert result.status_code == 422
    assert db.one('SELECT count(*) n FROM media')['n'] == 0
    assert list((db.DATA / 'media').iterdir()) == []


def test_bulk_import_reports_invalid_files_and_keeps_valid_files(owner):
    result = owner.post('/api/media/import', files=[
        ('files', ('valid.png', PNG, 'image/png')), ('files', ('bad.png', b'<html>', 'image/png'))])
    assert result.status_code == 200
    assert result.json()['imported'] == 1
    assert result.json()['errors'] == [{'name': 'bad.png', 'message': '文件内容与扩展名不匹配'}]
    assert db.one('SELECT count(*) n FROM media')['n'] == 1


def test_product_reference_blocks_delete_and_reports_actual_product(owner, product):
    media = upload(owner)
    with db.connection() as connection:
        data = {**product, 'image_ids': [media['id']], 'cover_id': media['id']}
        connection.execute('UPDATE products SET data=? WHERE id=?', (db.dumps(data), product['id']))
    result = owner.delete('/api/media/' + media['id'])
    assert result.status_code == 409
    reference = result.json()['detail']['references'][0]
    assert reference == {'type': 'product', 'id': product['id'], 'title': product['title']}
    assert owner.get(media['url']).content == PNG


@pytest.mark.parametrize('status', ['queued', 'running', 'uncertain', 'submitted'])
def test_pending_or_unconfirmed_job_payload_protects_media(owner, store, status):
    media = upload(owner)
    jid = active_job(store, '', status, {'request': {'product': {'detail_image_ids': [media['id']]}}})
    result = owner.delete('/api/media/' + media['id'])
    assert result.status_code == 409
    assert result.json()['detail']['references'][0]['id'] == jid
    assert result.json()['detail']['references'][0]['type'] == 'job'


def test_batch_delete_is_partial_and_does_not_cascade_references(owner, product):
    used, free = upload(owner, 'used.png'), upload(owner, 'free.png')
    with db.connection() as connection:
        connection.execute('UPDATE products SET data=? WHERE id=?',
                           (db.dumps({**product, 'image_ids': [used['id']]}), product['id']))
    result = owner.post('/api/media/batch-delete', json={'ids': [used['id'], free['id'], 'f' * 32]})
    assert result.status_code == 200
    assert result.json()['deleted'] == [free['id']]
    assert result.json()['blocked'][0]['id'] == used['id']
    assert result.json()['missing'] == ['f' * 32]
    assert owner.get(free['url']).status_code == 404
    assert owner.get(used['url']).status_code == 200
    assert db.get_product(product['id']) is not None
    assert not list((db.DATA / '.media-trash').iterdir())


def test_active_share_protects_media_without_exporting_share_token(owner):
    media = upload(owner)
    with db.connection() as connection:
        connection.execute('INSERT INTO media_shares VALUES(?,?,?)', ('private-share-token', media['id'], db.now() + 600))
    result = owner.delete('/api/media/' + media['id'])
    assert result.status_code == 409
    assert result.json()['detail']['references'][0]['type'] == 'share'
    assert 'private-share-token' not in result.text


def test_expired_share_and_cache_are_removed_only_with_unreferenced_media(owner, store):
    media = upload(owner)
    with db.connection() as connection:
        connection.execute('INSERT INTO media_shares VALUES(?,?,?)', ('expired', media['id'], db.now() - 1))
        connection.execute('INSERT INTO image_cache VALUES(?,?,?,?)', (store['id'], media['id'], 'https://example.test/image', db.now()))
    assert owner.delete('/api/media/' + media['id']).status_code == 200
    assert db.one('SELECT count(*) n FROM media_shares')['n'] == 0
    assert db.one('SELECT count(*) n FROM image_cache')['n'] == 0


def test_publishing_draft_selected_images_are_protected_even_when_product_has_no_images(owner, product):
    from app.publish_workbench import init as init_publishing_schema
    media = upload(owner)
    draft_id = db.uid()
    with db.connection() as connection:
        init_publishing_schema(connection)
        connection.execute('''INSERT INTO publish_drafts(id,product_id,product_version,revision,common_values,store_values,status,created,updated)
                              VALUES(?,?,?,?,?,?,?,?,?)''',
                           (draft_id, product['id'], product['version'], 1, db.dumps({'title': '待发布的箱包', 'image_ids': [media['id']]}),
                            '{}', 'draft', db.now(), db.now()))
    result = owner.delete('/api/media/' + media['id'])
    assert result.status_code == 409
    assert result.json()['detail']['references'] == [{'type': 'publish_draft', 'id': draft_id, 'title': '待发布的箱包'}]
    assert owner.get(media['url']).content == PNG


def test_database_delete_failure_rolls_back_staged_files_and_metadata(owner):
    media = upload(owner, category='应保留')
    with db.connection() as connection:
        connection.execute("CREATE TRIGGER keep_media BEFORE DELETE ON media BEGIN SELECT RAISE(ABORT,'do-not-export-internal-details'); END")
    result = owner.delete('/api/media/' + media['id'])
    assert result.status_code == 409
    assert 'do-not-export-internal-details' not in result.text
    assert owner.get(media['url']).content == PNG
    assert owner.get('/api/media').json()['rows'][0]['category'] == '应保留'
    assert not list((db.DATA / '.media-trash').iterdir())


def test_storage_symlink_or_external_database_path_is_not_read_or_deleted(owner, tmp_path):
    media = upload(owner)
    outside = tmp_path / 'private-file'
    outside.write_bytes(b'private')
    original = db.one('SELECT path FROM media WHERE id=?', (media['id'],))['path']
    with db.connection() as connection:
        connection.execute('UPDATE media SET path=? WHERE id=?', (str(outside), media['id']))
    assert owner.get(media['url']).status_code == 409
    assert owner.delete(media['url']).status_code == 409
    assert outside.read_bytes() == b'private'
    with db.connection() as connection:
        connection.execute('UPDATE media SET path=? WHERE id=?', (original, media['id']))
    path = db.DATA / original
    path.unlink(); path.symlink_to(outside)
    assert owner.get(media['url']).status_code == 409
    assert owner.delete(media['url']).status_code == 409
    assert outside.read_bytes() == b'private'


def test_old_media_row_and_metadata_schema_migrate_without_rewriting_media(owner):
    media = upload(owner)
    original = db.one('SELECT * FROM media WHERE id=?', (media['id'],))
    with db.connection() as connection:
        connection.execute('DROP TABLE media_metadata')
    db.init(); db.init()
    assert db.one('SELECT * FROM media WHERE id=?', (media['id'],)) == original
    assert len(original) == 8
    listed = owner.get('/api/media').json()['rows'][0]
    assert listed['category'] == '' and listed['tags'] == []
