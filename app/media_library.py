"""Local media catalogue, explicit uploads and reference-safe deletion."""
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import ValidationError

from . import db
from .models import MediaBatchDelete, MediaBatchMetadata, MediaMetadataPatch
from .security import require_user


router = APIRouter(prefix='/api/media', dependencies=[Depends(require_user)])
MAX_UPLOAD = int(os.environ.get('MAX_UPLOAD_MB', '1024')) * 1024 * 1024
TYPES = {'.jpg': ('image', 'image/jpeg'), '.jpeg': ('image', 'image/jpeg'),
         '.png': ('image', 'image/png'), '.webp': ('image', 'image/webp'),
         '.mp4': ('video', 'video/mp4'), '.mov': ('video', 'video/quicktime'),
         '.webm': ('video', 'video/webm')}
SELECT = '''SELECT m.id,m.name,m.kind,m.mime,m.size,m.created,
            COALESCE(mm.category,'') category,COALESCE(mm.tags,'[]') tags,
            COALESCE(mm.source_path,'') source_path
            FROM media m LEFT JOIN media_metadata mm ON mm.media_id=m.id'''


def init_schema(connection):
    connection.executescript('''
    CREATE TABLE IF NOT EXISTS media_metadata (
      media_id TEXT PRIMARY KEY REFERENCES media(id),
      category TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
      source_path TEXT NOT NULL DEFAULT '', updated INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS media_metadata_category ON media_metadata(category);
    ''')


def _row(value):
    item = dict(value)
    try:
        tags = json.loads(item.get('tags', '[]'))
    except (ValueError, TypeError):
        tags = []
    item['tags'] = tags if isinstance(tags, list) else []
    item['url'] = '/api/media/' + item['id']
    return item


def state_rows(limit=1000):
    with db.connection() as connection:
        return [_row(row) for row in connection.execute(SELECT + ' ORDER BY m.created DESC,m.id DESC LIMIT ?', (limit,))]


def _like(text):
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


@router.get('')
def list_media(kind: str = '', category: str | None = None, tag: str = '', q: str = '',
               offset: int = Query(default=0, ge=0), limit: int = Query(default=60, ge=1, le=200)):
    if kind not in ('', 'image', 'video'):
        raise HTTPException(422, '素材类型只能是 image 或 video')
    if len(q) > 240 or len(tag) > 80 or (category is not None and len(category) > 100):
        raise HTTPException(422, '素材筛选条件过长')
    where, parameters = [], []
    if kind:
        where.append('m.kind=?'); parameters.append(kind)
    if category is not None:
        where.append("COALESCE(mm.category,'')=?"); parameters.append(category)
    if tag:
        where.append("COALESCE(mm.tags,'[]') LIKE ? ESCAPE '\\'")
        parameters.append('%' + _like(db.dumps(tag)) + '%')
    if q:
        where.append("(m.name LIKE ? ESCAPE '\\' OR COALESCE(mm.category,'') LIKE ? ESCAPE '\\' OR COALESCE(mm.source_path,'') LIKE ? ESCAPE '\\' OR COALESCE(mm.tags,'[]') LIKE ? ESCAPE '\\')")
        parameters.extend(['%' + _like(q) + '%'] * 4)
    suffix = ' WHERE ' + ' AND '.join(where) if where else ''
    with db.connection() as connection:
        total = connection.execute('SELECT count(*) FROM (' + SELECT + suffix + ')', parameters).fetchone()[0]
        rows = [_row(row) for row in connection.execute(SELECT + suffix + ' ORDER BY m.created DESC,m.id DESC LIMIT ? OFFSET ?', (*parameters, limit, offset))]
        facets = connection.execute(SELECT + (' WHERE m.kind=?' if kind else ''), (kind,) if kind else ()).fetchall()
    categories, tags = {}, {}
    for value in facets:
        value = _row(value)
        categories[value['category']] = categories.get(value['category'], 0) + 1
        for name in {tag for tag in value['tags'] if isinstance(tag, str)}:
            tags[name] = tags.get(name, 0) + 1
    return {'rows': rows, 'total': total, 'offset': offset, 'limit': limit,
            'categories': [{'name': name, 'count': count} for name, count in sorted(categories.items())],
            'tags': [{'name': name, 'count': count} for name, count in sorted(tags.items())]}


def _metadata(connection, mid, changes):
    existing = connection.execute('SELECT * FROM media_metadata WHERE media_id=?', (mid,)).fetchone()
    current = dict(existing) if existing else {'category': '', 'tags': '[]', 'source_path': ''}
    if 'name' in changes:
        connection.execute('UPDATE media SET name=? WHERE id=?', (changes['name'], mid))
    for key in ('category', 'source_path'):
        if key in changes:
            current[key] = changes[key]
    if 'tags' in changes:
        current['tags'] = db.dumps(changes['tags'])
    connection.execute('''INSERT INTO media_metadata(media_id,category,tags,source_path,updated) VALUES(?,?,?,?,?)
        ON CONFLICT(media_id) DO UPDATE SET category=excluded.category,tags=excluded.tags,
        source_path=excluded.source_path,updated=excluded.updated''',
                       (mid, current['category'], current['tags'], current['source_path'], db.now()))


@router.patch('/{mid}')
def update_media(mid: str, body: MediaMetadataPatch):
    with db.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        if not connection.execute('SELECT 1 FROM media WHERE id=?', (mid,)).fetchone():
            raise HTTPException(404, '素材不存在')
        _metadata(connection, mid, body.model_dump(exclude_unset=True))
        result = _row(connection.execute(SELECT + ' WHERE m.id=?', (mid,)).fetchone())
    db.audit('更新素材分类', {'media_id': mid, 'fields': sorted(body.model_fields_set)})
    return result


@router.post('/batch-update')
def update_media_batch(body: MediaBatchMetadata):
    updated, missing = [], []
    changes = body.model_dump(exclude_unset=True, exclude={'ids'})
    with db.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        for mid in body.ids:
            if not connection.execute('SELECT 1 FROM media WHERE id=?', (mid,)).fetchone():
                missing.append(mid); continue
            _metadata(connection, mid, changes)
            updated.append(_row(connection.execute(SELECT + ' WHERE m.id=?', (mid,)).fetchone()))
    db.audit('批量更新素材分类', {'count': len(updated), 'fields': sorted(changes)})
    return {'rows': updated, 'updated': [row['id'] for row in updated], 'missing': missing}


def relative_path(value):
    if not value:
        return ''
    value = value.replace('\\', '/')
    parts = value.split('/')
    if (len(value) > 1000 or value.startswith('/') or any(part in ('', '.', '..') for part in parts)
            or any(':' in part or any(ord(ch) < 32 for ch in part) for part in parts)):
        raise HTTPException(422, '目录导入只接受安全的相对路径')
    return str(PurePosixPath(*parts))


def _upload_metadata(category, tags, source_path):
    try:
        parsed = json.loads(tags) if isinstance(tags, str) and tags.strip() else tags or []
        if not isinstance(parsed, list):
            raise ValueError()
        metadata = MediaMetadataPatch(category=category, tags=parsed)
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(422, '素材分类或标签格式错误：标签应为最多 30 项的 JSON 字符串列表') from None
    source_path = relative_path(source_path)
    folder_tags = source_path.split('/')[:-1] if source_path else []
    combined = list(dict.fromkeys(metadata.tags + folder_tags))
    if len(combined) > 30 or any(len(tag) > 80 for tag in combined):
        raise HTTPException(422, '目录层级或标签过多、过长，请分批导入')
    return {'category': metadata.category, 'tags': combined, 'source_path': source_path}


def media_path(relative):
    base = db.DATA.resolve() / 'media'
    candidate = db.DATA / relative
    if base.is_symlink() or base.resolve() != base or candidate.is_symlink() or candidate.resolve().parent != base:
        raise HTTPException(409, '素材存储路径异常，已停止操作')
    return candidate


async def store_upload(file, category='', tags='[]', source_path='', max_bytes=MAX_UPLOAD):
    name = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1][:240]
    ext = Path(name).suffix.lower()
    if ext not in TYPES:
        await file.close()
        raise HTTPException(422, '只支持 JPG、PNG、WebP、MP4、MOV、WebM')
    try:
        metadata = _upload_metadata(category, tags, source_path)
    except BaseException:
        await file.close()
        raise
    mid, total, digest = db.uid(), 0, hashlib.sha256()
    relative = 'media/' + mid + ext
    path = media_path(relative)
    try:
        with path.open('xb') as output:
            first = True
            while chunk := await file.read(1024 * 1024):
                if first:
                    valid = ((ext in ('.jpg', '.jpeg') and chunk[:3] == b'\xff\xd8\xff')
                             or (ext == '.png' and chunk[:8] == b'\x89PNG\r\n\x1a\n')
                             or (ext == '.webp' and chunk[:4] == b'RIFF' and chunk[8:12] == b'WEBP')
                             or (ext in ('.mp4', '.mov') and chunk[4:8] in (b'ftyp', b'moov', b'wide', b'mdat'))
                             or (ext == '.webm' and chunk[:4] == b'\x1aE\xdf\xa3'))
                    if not valid:
                        raise HTTPException(422, '文件内容与扩展名不匹配')
                    first = False
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(413, '文件超过上传上限')
                digest.update(chunk); output.write(chunk)
        if not total:
            raise HTTPException(422, '不能上传空文件')
        kind, mime = TYPES[ext]
        with db.connection() as connection:
            connection.execute('INSERT INTO media(id,name,kind,mime,path,size,sha256,created) VALUES(?,?,?,?,?,?,?,?)',
                               (mid, name, kind, mime, relative, total, digest.hexdigest(), db.now()))
            _metadata(connection, mid, metadata)
            result = _row(connection.execute(SELECT + ' WHERE m.id=?', (mid,)).fetchone())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return result


@router.post('/import')
async def import_media(files: list[UploadFile] = File(...), category: str = Form(''),
                       tags: str = Form('[]'), relative_paths: str = Form('[]')):
    try:
        paths = json.loads(relative_paths)
        if not isinstance(paths, list) or (paths and len(paths) != len(files)) or any(not isinstance(path, str) for path in paths):
            raise ValueError()
        if not 1 <= len(files) <= 50:
            raise ValueError()
        metadata = [_upload_metadata(category, tags, path) for path in (paths or [''] * len(files))]
    except (ValueError, TypeError, HTTPException) as exc:
        for file in files:
            await file.close()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(422, '每批最多 50 个文件，relative_paths 须为对应文件的相对路径 JSON 列表') from None
    rows, errors, total = [], [], 0
    for file, meta in zip(files, metadata):
        name = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1][:240]
        try:
            row = await store_upload(file, meta['category'], meta['tags'], meta['source_path'], max_bytes=MAX_UPLOAD-total)
            total += row['size']; rows.append(row)
        except HTTPException as exc:
            errors.append({'name': name, 'message': str(exc.detail)})
    db.audit('导入本地素材', {'imported': len(rows), 'failed': len(errors)})
    return {'rows': rows, 'imported': len(rows), 'duplicates': 0, 'errors': errors}


def _contains(value, ids):
    if isinstance(value, str):
        found = {value} & ids
        found |= set(re.findall(r'(?:/api/media/|\bmedia/)([a-f0-9]{32})(?:\b|[.?/#])', value)) & ids
        return found
    if isinstance(value, dict):
        found = set()
        for nested in value.values():
            found |= _contains(nested, ids)
        return found
    if isinstance(value, list):
        found = set()
        for nested in value:
            found |= _contains(nested, ids)
        return found
    return set()


def references(connection, ids):
    found = {mid: [] for mid in ids}
    for row in connection.execute('SELECT id,data FROM products'):
        try:
            data = json.loads(row['data'])
            used = _contains(data, ids)
            title = data.get('title') or '未命名商品'
        except (ValueError, TypeError, AttributeError):
            used, title = ids, '商品资料无法核对，需修复后再删除素材'
        for mid in used:
            found[mid].append({'type': 'product', 'id': row['id'], 'title': title})
    for row in connection.execute("SELECT j.id,j.kind,j.entity_id,j.payload,j.status,s.name store_name FROM jobs j JOIN stores s ON s.id=j.store_id WHERE j.status IN ('queued','running','uncertain','submitted')"):
        try:
            used = _contains(json.loads(row['payload']), ids) | ({row['entity_id']} & ids)
        except (ValueError, TypeError):
            used = ids
        for mid in used:
            found[mid].append({'type': 'job', 'id': row['id'], 'title': f"{row['store_name']} · {row['kind']} · {row['status']}"})
    for row in connection.execute('SELECT media_id,count(*) count FROM media_shares WHERE expires>? GROUP BY media_id', (db.now(),)):
        if row['media_id'] in found:
            found[row['media_id']].append({'type': 'share', 'id': row['media_id'], 'title': f"仍有 {row['count']} 个有效素材分享"})
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publish_drafts'").fetchone():
        for row in connection.execute('SELECT id,common_values,store_values FROM publish_drafts'):
            try:
                common, stores = json.loads(row['common_values']), json.loads(row['store_values'])
                used = _contains(common, ids) | _contains(stores, ids)
                title = common.get('title') or '发布草稿'
            except (ValueError, TypeError, AttributeError):
                used, title = ids, '发布草稿资料无法核对，需修复后再删除素材'
            for mid in used:
                found[mid].append({'type': 'publish_draft', 'id': row['id'], 'title': title})
    return found


def validate_product_media(connection, data):
    ids = set(data.get('image_ids', []) + data.get('detail_image_ids', []))
    if data.get('cover_id'):
        ids.add(data['cover_id'])
    for mid in ids:
        if not connection.execute("SELECT 1 FROM media WHERE id=? AND kind='image'", (mid,)).fetchone():
            raise HTTPException(422, '商品图片不存在或已删除，请重新选择')


def delete_media(ids):
    deleted, blocked, missing, moves = [], [], [], []
    trash = db.DATA.resolve() / '.media-trash'
    try:
        with db.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            refs = references(connection, set(ids))
            for mid in ids:
                row = connection.execute('SELECT * FROM media WHERE id=?', (mid,)).fetchone()
                if not row:
                    missing.append(mid); continue
                if refs[mid]:
                    blocked.append({'id': mid, 'name': row['name'], 'references': refs[mid]}); continue
                try:
                    path = media_path(row['path'])
                    if trash.is_symlink() or trash.resolve() != trash:
                        raise HTTPException(409, '素材临时目录异常')
                    trash.mkdir(mode=0o700, exist_ok=True)
                    if path.exists():
                        if not path.is_file():
                            raise HTTPException(409, '素材文件类型异常')
                        staged = trash / (mid + '-' + db.uid() + path.suffix)
                        path.replace(staged); moves.append((path, staged))
                except (HTTPException, OSError):
                    blocked.append({'id': mid, 'name': row['name'], 'references': [
                        {'type': 'storage', 'id': mid, 'title': '素材存储路径或文件权限异常，未删除'}]})
                    continue
                connection.execute('DELETE FROM media_shares WHERE media_id=? AND expires<=?', (mid, db.now()))
                connection.execute('DELETE FROM image_cache WHERE media_id=?', (mid,))
                connection.execute('DELETE FROM media_metadata WHERE media_id=?', (mid,))
                connection.execute('DELETE FROM media WHERE id=?', (mid,))
                deleted.append(mid)
    except BaseException as exc:
        for original, staged in reversed(moves):
            if staged.exists():
                staged.replace(original)
        if isinstance(exc, sqlite3.IntegrityError):
            raise HTTPException(409, '数据库仍有素材引用，整批删除已回滚，文件保持完整') from None
        raise
    warnings = []
    for _, staged in moves:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            warnings.append('素材索引已移除，部分临时文件需要稍后清理')
    db.audit('删除本地素材', {'deleted': deleted, 'blocked': [item['id'] for item in blocked]})
    return {'deleted': deleted, 'blocked': blocked, 'missing': missing, 'warnings': list(dict.fromkeys(warnings))}


@router.post('/batch-delete')
def delete_media_batch(body: MediaBatchDelete):
    return delete_media(body.ids)


@router.delete('/{mid}')
def delete_one_media(mid: str):
    result = delete_media([mid])
    if result['missing']:
        raise HTTPException(404, '素材不存在')
    if result['blocked']:
        raise HTTPException(409, {'message': '素材仍被引用或暂时无法删除，未删除文件',
                                  'references': result['blocked'][0]['references']})
    return result
