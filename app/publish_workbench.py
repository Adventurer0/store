"""Source-backed multi-store publishing drafts and strict platform quality gates.

Only the browser worker can attach observed requirements or quality evidence.
The HTTP surface accepts business values, never selectors, scores or receipts.
"""
import copy
import hashlib
import json
import math
import re
from urllib.parse import urlsplit, urlunsplit

from . import db
from . import quality_checker
from .commerce_profiles import trusted_url
from .connectors import Blocked, Uncertain, PLATFORMS


QUALITY_THRESHOLD = 90
LOCAL_QUALITY_THRESHOLD = quality_checker.LOCAL_THRESHOLD
EVIDENCE_TTL = 15 * 60
KINDS = {'workbench_collect', 'workbench_prepare', 'workbench_submit'}
LOCKED = {'queued_submit', 'running_submit', 'submitted', 'uncertain', 'succeeded'}
TYPES = {'text', 'textarea', 'number', 'integer', 'select', 'multiselect', 'boolean', 'images', 'skus'}
STORE_FIELDS = {'category', 'category_id', 'category_path', 'freight', 'freight_id',
                'freight_template', 'shipping_template', 'shipping_template_id'}
ALIASES = {'name': 'title', 'product_title': 'title', 'head_imgs': 'image_ids',
           'main_images': 'image_ids', 'detail_images': 'detail_image_ids',
           'sku_list': 'skus', 'brand_name': 'brand'}
SENSITIVE = re.compile(r'password|passwd|secret|token|cookie|csrf|authorization|密码|验证码|身份证|收货人|收货地址', re.I)
ID = re.compile(r'^[a-f0-9]{32}$')
PLATFORM_SCORE_REQUIRED = {'kuaishou'}


def platform_score_required(platform, mode='create'):
    return platform in PLATFORM_SCORE_REQUIRED and mode == 'create'


def _media_paths(conn, *sources):
    """Resolve only local image IDs used by this draft; no URL fetching."""
    ids = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        for values in source.values():
            if isinstance(values, list):
                ids.update(value for value in values if isinstance(value, str) and ID.fullmatch(value))
    if not ids:
        return {}
    marks = ','.join('?' for _ in ids)
    return {row['id']: str(db.DATA / row['path'])
            for row in conn.execute(f'SELECT id,path FROM media WHERE id IN ({marks})', list(ids))}


def _platform_quality_ok(target, fingerprint, mode='create'):
    if not target.get('quality'):
        return not platform_score_required(target['platform'], mode)
    return _quality_ok(target['quality'], fingerprint)


def init(conn=None):
    script = '''
    CREATE TABLE IF NOT EXISTS publish_drafts (
      id TEXT PRIMARY KEY, product_id TEXT NOT NULL,
      product_version INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
      common_values TEXT NOT NULL DEFAULT '{}', store_values TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL DEFAULT 'draft', mode TEXT NOT NULL DEFAULT 'create',
      created INTEGER NOT NULL, updated INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS publish_targets (
      draft_id TEXT NOT NULL REFERENCES publish_drafts(id), store_id TEXT NOT NULL REFERENCES stores(id),
      schema_data TEXT NOT NULL DEFAULT '{}', schema_revision INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT 'uncollected', quality TEXT NOT NULL DEFAULT '{}',
      receipt TEXT NOT NULL DEFAULT '{}', problem TEXT NOT NULL DEFAULT '',
      error_code TEXT NOT NULL DEFAULT '', audit_status TEXT NOT NULL DEFAULT 'unchecked',
      audit_message TEXT NOT NULL DEFAULT '', audit_code TEXT NOT NULL DEFAULT '',
      audit_checked INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL,
      PRIMARY KEY(draft_id,store_id));
    CREATE TABLE IF NOT EXISTS publish_groups (
      id TEXT PRIMARY KEY, draft_ids TEXT NOT NULL, created INTEGER NOT NULL);
    '''
    target_columns = [('error_code', "TEXT NOT NULL DEFAULT ''"),
                      ('audit_status', "TEXT NOT NULL DEFAULT 'unchecked'"),
                      ('audit_message', "TEXT NOT NULL DEFAULT ''"),
                      ('audit_code', "TEXT NOT NULL DEFAULT ''"),
                      ('audit_checked', 'INTEGER NOT NULL DEFAULT 0')]

    def migrate(current):
        columns = {row[1] for row in current.execute('PRAGMA table_info(publish_drafts)')}
        if 'mode' not in columns:
            current.execute("ALTER TABLE publish_drafts ADD COLUMN mode TEXT NOT NULL DEFAULT 'create'")
        columns = {row[1] for row in current.execute('PRAGMA table_info(publish_targets)')}
        for name, ddl in target_columns:
            if name not in columns:
                current.execute(f'ALTER TABLE publish_targets ADD COLUMN {name} {ddl}')

    if conn is not None:
        conn.executescript(script)
        migrate(conn)
    else:
        with db.connection() as current:
            current.executescript(script)
            migrate(current)


def _decode(row):
    value = dict(row)
    for key in ('common_values', 'store_values', 'schema_data', 'quality', 'receipt', 'draft_ids'):
        if key in value:
            value[key] = json.loads(value[key])
    return value


def _draft(conn, draft_id):
    row = conn.execute('SELECT * FROM publish_drafts WHERE id=?', (draft_id,)).fetchone()
    if not row:
        raise KeyError('发布草稿不存在')
    return _decode(row)


def _targets(conn, draft_id):
    result = [_decode(row) for row in conn.execute(
        'SELECT t.*,s.name store_name,s.platform,s.commerce_status,s.commerce_enabled,s.templates store_templates FROM publish_targets t '
        'JOIN stores s ON s.id=t.store_id WHERE draft_id=? ORDER BY s.created,s.id', (draft_id,))]
    for target in result:
        job = conn.execute("SELECT kind,status,result FROM jobs WHERE store_id=? AND json_extract(payload,'$.draft_id')=? ORDER BY created DESC,id DESC LIMIT 1",
                           (target['store_id'], draft_id)).fetchone()
        if job and target['status'].startswith(('queued_', 'running_')) and job['status'] in ('cancelled', 'blocked', 'failed', 'uncertain'):
            target['status'] = 'uncertain' if job['kind'] == 'workbench_submit' and job['status'] == 'uncertain' else 'needs_input'
            target['problem'] = json.loads(job['result']).get('message', '任务未完成，请重新检查')
    return result


def _semantic(field):
    raw = field.get('semantic') or field.get('canonical') or field.get('key') or field.get('id', '')
    return ALIASES.get(raw, raw)


def _field_public(field):
    allowed = ('key', 'label', 'type', 'required', 'options', 'value', 'semantic', 'stage',
               'min', 'max', 'max_length', 'min_length', 'help', 'scope', 'observed')
    return {key: copy.deepcopy(field[key]) for key in allowed if key in field}


def normalize_schema(platform, raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('fields'), list) or len(raw['fields']) > 300:
        raise Blocked('平台必填项读取结果无效，请重新读取当前页面')
    if not isinstance(raw.get('source_url'), str) or not raw['source_url'].strip():
        raise Blocked('平台需求缺少实际读取页面的来源地址')
    try:
        source = trusted_url(platform, raw.get('source_url', ''))
    except (ValueError, KeyError, TypeError):
        raise Blocked('平台需求必须来自当前商家后台的 HTTPS 页面') from None
    seen, fields = set(), []
    for original in raw['fields']:
        if not isinstance(original, dict):
            raise Blocked('平台字段结构无效')
        field = copy.deepcopy(original)
        key = field.get('key') or field.get('id')
        label = field.get('label')
        if not isinstance(key, str) or not 1 <= len(key) <= 150 or key in seen:
            raise Blocked('平台字段缺少唯一标识，已停止合并')
        if not isinstance(label, str) or not label.strip() or len(label) > 200:
            raise Blocked('平台字段缺少可显示名称')
        if SENSITIVE.search(key) or SENSITIVE.search(label):
            continue
        seen.add(key)
        if field.get('type', 'text') not in TYPES:
            raise Blocked('平台字段类型尚不支持，请在平台核对')
        options = field.get('options', [])
        if not isinstance(options, list) or len(options) > 500:
            raise Blocked('平台字段可选项无效')
        clean_options = []
        for item in options:
            if isinstance(item, str):
                item = {'value': item, 'label': item}
            if not isinstance(item, dict) or not isinstance(item.get('value'), (str, int, float, bool)):
                raise Blocked('平台字段可选项无效')
            clean_options.append({'value': item['value'], 'label': str(item.get('label', item['value']))[:200]})
        semantic = _semantic(field)
        field.update(key=key, label=label.strip(), type=field.get('type', 'text'),
                     required=bool(field.get('required')), options=clean_options,
                     semantic=semantic, observed=True)
        # IDs and platform category/shipping choices are never merged globally.
        field['scope'] = 'common' if field.get('scope') == 'common' and semantic not in STORE_FIELDS else 'store'
        for limit in ('min', 'max', 'max_length', 'min_length'):
            if limit in field and (isinstance(field[limit], bool) or not isinstance(field[limit], (int, float))
                                   or not math.isfinite(field[limit])):
                raise Blocked('平台字段限制无效')
        fields.append(field)
    if raw.get('complete') and not fields:
        raise Blocked('平台返回空字段列表，不能认定商品信息已读取完整')
    url = urlsplit(source)
    return {'fields': fields, 'complete': raw.get('complete') is True,
            'stage': str(raw.get('stage', 'unknown'))[:100],
            'source_url': urlunsplit((url.scheme, url.netloc, url.path, '', '')),
            'observed_at': db.now()}


def _shared_fields(targets):
    candidates = {}
    for target in targets:
        for field in target['schema_data'].get('fields', []):
            if field.get('scope') == 'common':
                candidates.setdefault(field['semantic'], []).append((target['store_id'], field))
    merged = {}
    for semantic, occurrences in candidates.items():
        # A single store still belongs to the common form for a known shared
        # semantic. Multiple distinct controls in one store cannot be collapsed.
        if len({sid for sid, _ in occurrences}) != len(occurrences):
            continue
        fields = [field for _, field in occurrences]
        if len({field['type'] for field in fields}) != 1:
            continue
        result = _field_public(fields[0])
        result.update(key=semantic, semantic=semantic, required=any(f['required'] for f in fields),
                      store_ids=list(dict.fromkeys(sid for sid, _ in occurrences)))
        defaults = [field.get('value') for field in fields]
        result['value'] = defaults[0] if all(value == defaults[0] for value in defaults) else None
        if result['type'] in ('select', 'multiselect'):
            allowed = {db.dumps(o['value']) for o in fields[0]['options']}
            for field in fields[1:]:
                allowed &= {db.dumps(o['value']) for o in field['options']}
            if not allowed:
                continue
            result['options'] = [o for o in fields[0]['options'] if db.dumps(o['value']) in allowed]
        for name in ('max_length', 'max'):
            bounds = [f[name] for f in fields if name in f]
            if bounds:
                result[name] = min(bounds)
        for name in ('min_length', 'min'):
            bounds = [f[name] for f in fields if name in f]
            if bounds:
                result[name] = max(bounds)
        if result.get('min', -math.inf) > result.get('max', math.inf) or result.get('min_length', 0) > result.get('max_length', math.inf):
            continue
        merged[semantic] = result
    return merged


def _values(draft, target, shared=None):
    shared = shared if shared is not None else {}
    local = draft['store_values'].get(target['store_id'], {})
    values = {}
    for field in target['schema_data'].get('fields', []):
        key, semantic = field['key'], field['semantic']
        if key in local:
            values[key] = copy.deepcopy(local[key])
        elif semantic in shared and semantic in draft['common_values']:
            values[key] = copy.deepcopy(draft['common_values'][semantic])
        elif semantic in ('title', 'description', 'image_ids', 'detail_image_ids', 'skus') and semantic in draft['common_values']:
            values[key] = copy.deepcopy(draft['common_values'][semantic])
        elif 'value' in field:
            values[key] = copy.deepcopy(field['value'])
    return values


def validate_values(fields, values):
    problems = []
    for field in fields:
        value, label = values.get(field['key']), field['label']
        empty = value is None or value == '' or value == []
        if field.get('required') and empty:
            problems.append({'key': field['key'], 'label': label, 'message': '请填写' + label})
            continue
        if empty:
            continue
        kind, error = field['type'], ''
        if kind in ('text', 'textarea') and not isinstance(value, str):
            error = '字段格式不正确'
        elif kind == 'select' and not isinstance(value, (str, int, float, bool)):
            error = '请选择平台提供的值'
        elif kind in ('number', 'integer'):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                error = '请填写有效数字'
            elif kind == 'integer' and not isinstance(value, int):
                error = '请填写整数'
            elif value < field.get('min', -math.inf) or value > field.get('max', math.inf):
                error = '数值超出平台允许范围'
        elif kind == 'boolean' and not isinstance(value, bool):
            error = '请选择是或否'
        elif kind in ('images', 'skus', 'multiselect') and not isinstance(value, list):
            error = '请提供列表'
        if not error and isinstance(value, list) and kind in ('images', 'skus', 'multiselect'):
            if len(value) < field.get('min', 0) or len(value) > field.get('max', math.inf):
                error = '数量不符合平台要求'
        if not error and isinstance(value, str):
            if len(value) > field.get('max_length', math.inf) or len(value) < field.get('min_length', 0):
                error = '文字长度不符合平台要求'
        if not error and field.get('options') and kind in ('select', 'multiselect'):
            allowed = {db.dumps(option['value']) for option in field['options']}
            selected = value if kind == 'multiselect' else [value]
            if any(db.dumps(v) not in allowed for v in selected):
                error = '所选值不在当前平台选项中'
        if error:
            problems.append({'key': field['key'], 'label': label, 'message': label + '：' + error})
    return problems


def _fingerprint(draft, target, values):
    templates = json.loads(target.get('store_templates', '{}'))
    identity = {'draft_id': draft['id'], 'revision': draft['revision'], 'product_id': draft['product_id'],
                'product_version': draft['product_version'], 'store_id': target['store_id'],
                'schema_revision': target['schema_revision'], 'values': values,
                'store_configuration': {'platform': target['platform'],
                                        'commerce_browser': templates.get('commerce_browser', {}),
                                        'publish_defaults': templates.get('publish_defaults', {})}}
    return hashlib.sha256(db.dumps(identity).encode()).hexdigest()


def _valid_quality(quality, fingerprint):
    return (isinstance(quality, dict) and quality.get('source') == 'platform_dom'
            and quality.get('fingerprint') == fingerprint
            and isinstance(quality.get('score'), (int, float)) and not isinstance(quality['score'], bool)
            and math.isfinite(quality['score']) and 0 <= quality['score'] <= 100
            and isinstance(quality.get('observed_at'), int)
            and db.now() - EVIDENCE_TTL <= quality['observed_at'] <= db.now() + 30)


def _quality_ok(quality, fingerprint):
    return _valid_quality(quality, fingerprint) and quality['score'] > QUALITY_THRESHOLD


def _view(conn, draft):
    targets = _targets(conn, draft['id'])
    shared = _shared_fields(targets)
    values_by_target = [(target, _values(draft, target, shared)) for target in targets]
    product = db.get_product(draft['product_id'])
    quality_product = copy.deepcopy(product or {})
    # The draft's reviewed values, not the warehouse balance, are what the
    # platform will receive (new products always carry zero SKU stock).
    for key in ('title', 'description', 'image_ids', 'detail_image_ids', 'skus'):
        if key in draft['common_values']:
            quality_product[key] = copy.deepcopy(draft['common_values'][key])
    media_paths = _media_paths(conn, quality_product, draft['common_values'],
                               *[values for _, values in values_by_target],
                               *draft['store_values'].values())
    stores = []
    ready = True
    for target, values in values_by_target:
        schema = target['schema_data']
        missing = validate_values(schema.get('fields', []), values)
        fingerprint = _fingerprint(draft, target, values)
        required_platform_score = platform_score_required(target['platform'], draft.get('mode', 'create'))
        quality_ok = _platform_quality_ok(target, fingerprint, draft.get('mode', 'create'))
        local = quality_checker.evaluate(quality_product, values, schema.get('fields', []),
                                         target['platform'], mode=draft.get('mode', 'create'),
                                         media_paths=media_paths)
        problems = []
        if target['commerce_status'] != 'valid' or not target['commerce_enabled']:
            problems.append('请先登录并启用此店铺电商后台')
        if not schema:
            problems.append('尚未读取平台商品要求')
        elif not schema.get('complete'):
            problems.append('平台页面尚未读取完整，请补充当前阶段资料后继续读取')
        problems.extend(p['message'] for p in missing)
        problems.extend(local['blockers'])
        if not local['blockers'] and not local['passed']:
            problems.append(f"本地质量预评分 {local['score']}，需达到 {LOCAL_QUALITY_THRESHOLD} 分")
        if not quality_ok:
            problems.append('需要平台实际商品质量分高于 90 分；当前分数不可用、过期或未达标'
                            if required_platform_score else '平台评分证据已过期或无效，请重新回填检查')
        if target['problem']:
            problems.append(target['problem'])
        can_submit = (not problems and local['passed'] and quality_ok
                      and target['status'] not in LOCKED)
        ready = ready and can_submit
        fields = []
        for field in schema.get('fields', []):
            if field['semantic'] in shared and field['key'] not in draft['store_values'].get(target['store_id'], {}):
                continue
            public = _field_public(field)
            public['value'] = values.get(field['key'])
            fields.append(public)
        stores.append({'store_id': target['store_id'], 'store_name': target['store_name'], 'platform': target['platform'],
                       'platform_name': PLATFORMS[target['platform']]['name'], 'status': target['status'],
                       'stage': schema.get('stage', 'uncollected'), 'complete': schema.get('complete', False),
                       'observed_at': schema.get('observed_at'), 'fields': fields,
                       'values': values, 'missing': missing, 'quality': target['quality'],
                       'quality_passed': bool(target['quality']) and _quality_ok(target['quality'], fingerprint),
                       'platform_quality_required': required_platform_score,
                       'platform_quality_ok': quality_ok,
                       'local_quality': local, 'local_quality_passed': local['passed'],
                       'problems': problems, 'ready': can_submit,
                       'receipt': target['receipt'], 'error_code': target.get('error_code', ''),
                       'audit_status': target.get('audit_status', 'unchecked'),
                       'audit_message': target.get('audit_message', ''),
                       'audit_code': target.get('audit_code', ''),
                       'audit_checked': target.get('audit_checked', 0),
                       'updated': target['updated']})
    public_shared = []
    for semantic, field in shared.items():
        item = copy.deepcopy(field)
        item['value'] = draft['common_values'].get(semantic, item.get('value'))
        public_shared.append(item)
    stale = not product or product['version'] != draft['product_version']
    return {**{k: draft[k] for k in ('id', 'product_id', 'product_version', 'revision', 'status', 'mode', 'created', 'updated')},
            'product_title': product['title'] if product else '已删除商品',
            'common_values': draft['common_values'], 'store_values': draft['store_values'],
            'common_fields': public_shared, 'stores': stores, 'ready': ready and not stale,
            'product_stale': stale, 'minimum_quality': QUALITY_THRESHOLD,
            'minimum_local_quality': LOCAL_QUALITY_THRESHOLD,
            'quality_comparison': 'gt', 'local_quality_comparison': 'gte'}


def create(product_ids, store_ids, mode='create'):
    init()
    if mode not in ('create', 'update'):
        raise ValueError('发布方式只能是新建或更新')
    if not product_ids or len(product_ids) > 100 or len(set(product_ids)) != len(product_ids):
        raise ValueError('请选择 1–100 件不重复的商品')
    if not store_ids or len(store_ids) > 30 or len(set(store_ids)) != len(store_ids):
        raise ValueError('请选择 1–30 家不重复的店铺')
    if len(product_ids) * len(store_ids) > 300:
        raise ValueError('一次最多准备 300 个商品店铺组合')
    group_id, draft_ids, now = db.uid(), [], db.now()
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for sid in store_ids:
            if not conn.execute('SELECT 1 FROM stores WHERE id=?', (sid,)).fetchone():
                raise KeyError('所选店铺不存在')
        for pid in product_ids:
            row = conn.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
            if not row:
                raise KeyError('所选商品不存在')
            product = json.loads(row['data'])
            if mode == 'update':
                for sid in store_ids:
                    linked = conn.execute('SELECT external_id FROM listings WHERE product_id=? AND store_id=?',
                                          (pid, sid)).fetchone()
                    if not linked or not linked['external_id']:
                        raise Blocked(f"商品「{product.get('title') or pid}」尚未关联所选店铺的平台商品 ID，不能作为更新提交")
            values = {key: copy.deepcopy(product.get(key, [] if key in ('image_ids', 'detail_image_ids', 'skus') else ''))
                      for key in ('title', 'description', 'image_ids', 'detail_image_ids', 'skus')}
            # A common warehouse amount is not an instruction to duplicate stock.
            if mode == 'create':
                for sku in values['skus']:
                    sku['stock'] = 0
            did = db.uid()
            draft_ids.append(did)
            conn.execute('INSERT INTO publish_drafts(id,product_id,product_version,revision,common_values,store_values,status,mode,created,updated) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (did, pid, row['version'], 1, db.dumps(values), '{}', 'draft', mode, now, now))
            for sid in store_ids:
                conn.execute('INSERT INTO publish_targets(draft_id,store_id,updated) VALUES(?,?,?)', (did, sid, now))
        conn.execute('INSERT INTO publish_groups VALUES(?,?,?)', (group_id, db.dumps(draft_ids), now))
    db.audit('创建统一发布草稿', {'group_id': group_id, 'products': len(draft_ids),
                                  'stores': len(store_ids), 'mode': mode})
    return get_group(group_id)


def get(draft_id):
    init()
    with db.connection() as conn:
        return _view(conn, _draft(conn, draft_id))


def _group_ids(conn, group_id):
    row = conn.execute('SELECT draft_ids FROM publish_groups WHERE id=?', (group_id,)).fetchone()
    if not row:
        raise KeyError('发布批次不存在')
    return json.loads(row['draft_ids'])


def get_group(group_id):
    init()
    with db.connection() as conn:
        drafts = [_view(conn, _draft(conn, did)) for did in _group_ids(conn, group_id)]
    statuses = [s['status'] for d in drafts for s in d['stores']]
    ready = all(d['ready'] for d in drafts)
    state = ('submitted' if statuses and all(s in ('submitted', 'succeeded') for s in statuses)
             else 'uncertain' if 'uncertain' in statuses
             else 'working' if any(s.startswith(('queued_', 'running_')) for s in statuses)
             else 'ready' if ready else 'editing')
    return {'id': group_id, 'drafts': drafts, 'ready': ready, 'status': state,
            'updated': max([d['updated'] for d in drafts] + [s['updated'] for d in drafts for s in d['stores']], default=0),
            'minimum_quality': QUALITY_THRESHOLD, 'minimum_local_quality': LOCAL_QUALITY_THRESHOLD,
            'quality_comparison': 'gt', 'local_quality_comparison': 'gte'}


def _group_state(statuses):
    """Summarise target states without running the full per-store validation."""
    statuses = list(statuses)
    if statuses and all(status in ('submitted', 'succeeded') for status in statuses):
        return 'submitted'
    if 'uncertain' in statuses:
        return 'uncertain'
    if any(str(status).startswith(('queued_', 'running_')) for status in statuses):
        return 'working'
    if statuses and all(status == 'ready' for status in statuses):
        return 'ready'
    return 'editing'


def _group_title(value):
    try:
        title = json.loads(value).get('title')
    except (ValueError, TypeError, AttributeError):
        title = None
    return title if isinstance(title, str) and title.strip() else '未命名商品'


def _ids(value):
    """A corrupted batch row must not hide the rest of the history."""
    try:
        ids = json.loads(value)
    except (ValueError, TypeError):
        return []
    return [item for item in ids if isinstance(item, str)] if isinstance(ids, list) else []


def _target_state(target, latest):
    """A queued label left behind by an interrupted job is not still working."""
    status = target['status']
    job = latest.get((target['draft_id'], target['store_id']))
    if job and status.startswith(('queued_', 'running_')) and job['status'] in ('cancelled', 'blocked', 'failed', 'uncertain'):
        return 'uncertain' if job['kind'] == 'workbench_submit' and job['status'] == 'uncertain' else 'needs_input'
    return status


def list_groups(limit=20, offset=0):
    """Bounded batch history; one query per table instead of one per draft."""
    init()
    try:
        limit, offset = int(limit), int(offset)
    except (TypeError, ValueError):
        raise ValueError('发布记录分页参数无效') from None
    if not 1 <= limit <= 50 or offset < 0:
        raise ValueError('发布记录每页 1–50 条，偏移量不能为负')
    with db.connection() as conn:
        total = conn.execute('SELECT count(*) FROM publish_groups').fetchone()[0]
        rows = conn.execute('SELECT id,draft_ids,created FROM publish_groups ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',
                            (limit, offset)).fetchall()
        draft_ids = [did for row in rows for did in _ids(row['draft_ids'])]
        drafts, targets = {}, {}
        if draft_ids:
            marks = ','.join('?' for _ in draft_ids)
            for row in conn.execute(f'SELECT id,product_id,revision,status,common_values,updated '
                                    f'FROM publish_drafts WHERE id IN ({marks})', draft_ids):
                drafts[row['id']] = {'id': row['id'], 'product_id': row['product_id'], 'revision': row['revision'],
                                     'status': row['status'], 'updated': row['updated'],
                                     'title': _group_title(row['common_values'])}
            for row in conn.execute(f'SELECT draft_id,store_id,status,updated '
                                    f'FROM publish_targets WHERE draft_id IN ({marks})', draft_ids):
                targets.setdefault(row['draft_id'], []).append(dict(row))
            latest = {}
            for row in conn.execute(f"SELECT store_id,json_extract(payload,'$.draft_id') draft_id,kind,status "
                                    f"FROM jobs WHERE kind LIKE 'workbench_%' "
                                    f"AND json_extract(payload,'$.draft_id') IN ({marks}) "
                                    f"ORDER BY created DESC,id DESC", draft_ids):
                latest.setdefault((row['draft_id'], row['store_id']), dict(row))
        else:
            latest = {}
        groups = []
        for row in rows:
            items = [drafts[did] for did in _ids(row['draft_ids']) if did in drafts]
            entries = [target for item in items for target in targets.get(item['id'], [])]
            statuses = [_target_state(target, latest) for target in entries]
            groups.append({
                'id': row['id'], 'created': row['created'],
                'updated': max([item['updated'] for item in items] +
                               [target['updated'] for target in entries], default=row['created']),
                'product_count': len(items),
                'product_titles': [item['title'] for item in items],
                'store_count': len({target['store_id'] for target in entries}),
                'target_count': len(entries),
                'submitted_count': sum(1 for status in statuses if status in ('submitted', 'succeeded')),
                'uncertain_count': sum(1 for status in statuses if status == 'uncertain'),
                'status': _group_state(statuses),
                'drafts': [{'id': item['id'], 'product_id': item['product_id'], 'title': item['title'],
                            'revision': item['revision'], 'status': item['status']} for item in items],
            })
    return {'groups': groups, 'total': total, 'limit': limit, 'offset': offset}


def delete_group(group_id):
    """Discard every draft in an unsubmitted batch, all or nothing."""
    init()
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT draft_ids FROM publish_groups WHERE id=?', (group_id,)).fetchone()
        if not row:
            raise KeyError('发布批次不存在')
        ids = _ids(row['draft_ids'])
        if ids:
            marks = ','.join('?' for _ in ids)
            lock_values = sorted(LOCKED)
            locks = ','.join('?' for _ in lock_values)
            if conn.execute(f'SELECT 1 FROM publish_targets WHERE draft_id IN ({marks}) '
                            f'AND status IN ({locks}) LIMIT 1', ids + lock_values).fetchone():
                raise Blocked('批次中已有提交任务或待核对结果，不能删除；请保留用于平台对账')
            active = conn.execute(f"SELECT 1 FROM jobs WHERE json_extract(payload,'$.draft_id') IN ({marks}) "
                                  f"AND status IN ('queued','running','uncertain','submitted') LIMIT 1", ids).fetchone()
            if active:
                raise Blocked('批次仍有排队、执行中或待核对任务，请先结束任务')
            conn.execute(f'DELETE FROM publish_targets WHERE draft_id IN ({marks})', ids)
            conn.execute(f'DELETE FROM publish_drafts WHERE id IN ({marks})', ids)
        conn.execute('DELETE FROM publish_groups WHERE id=?', (group_id,))
    db.audit('删除发布批次', {'group_id': group_id, 'drafts': len(ids)})
    return {'ok': True, 'drafts': len(ids)}


def save(draft_id, revision, common_values, store_values):
    init()
    if not isinstance(common_values, dict) or not isinstance(store_values, dict) or len(db.dumps([common_values, store_values])) > 500_000:
        raise ValueError('发布表单内容无效或过大')
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        draft, targets = _draft(conn, draft_id), _targets(conn, draft_id)
        if draft['revision'] != revision:
            raise Blocked('发布表单已被修改，请刷新后继续')
        if any(target['status'] in LOCKED for target in targets):
            raise Blocked('此商品已有提交任务，先核对平台结果，不能修改后重复创建')
        known_common = {'title', 'description', 'image_ids', 'detail_image_ids', 'skus'} | set(_shared_fields(targets))
        if set(common_values) - known_common:
            raise ValueError('公共表单包含未读取的平台字段')
        by_store = {target['store_id']: target for target in targets}
        if set(store_values) - set(by_store):
            raise ValueError('表单包含未选择的店铺')
        for sid, values in store_values.items():
            if not isinstance(values, dict) or set(values) - {f['key'] for f in by_store[sid]['schema_data'].get('fields', [])}:
                raise ValueError('店铺表单包含未读取的平台字段')
            for field in by_store[sid]['schema_data'].get('fields', []):
                if field['key'] in values and field.get('semantic') == 'skus':
                    rows = values[field['key']]
                    if (draft.get('mode', 'create') == 'create'
                            and (not isinstance(rows, list) or any(not isinstance(sku, dict) or sku.get('stock', 0) != 0 for sku in rows))):
                        raise ValueError('新建商品库存必须为零，库存分配请在统一库存处理')
        merged = {**draft['common_values'], **copy.deepcopy(common_values)}
        if not isinstance(merged.get('title'), str) or not 1 <= len(merged['title'].strip()) <= 200:
            raise ValueError('商品标题须为 1–200 个字符')
        if not isinstance(merged.get('description', ''), str) or len(merged.get('description', '')) > 10000:
            raise ValueError('商品描述过长或格式错误')
        for name in ('image_ids', 'detail_image_ids'):
            images = merged.get(name, [])
            if not isinstance(images, list) or len(images) > 30 or any(not isinstance(mid, str) or not ID.fullmatch(mid) for mid in images):
                raise ValueError('商品图片须引用本地仓库的有效图片素材')
            for mid in images:
                if not conn.execute("SELECT 1 FROM media WHERE id=? AND kind='image'", (mid,)).fetchone():
                    raise ValueError('所选商品图片已不存在')
        if not isinstance(merged.get('skus'), list) or not 1 <= len(merged['skus']) <= 200:
            raise ValueError('请填写 1–200 个商品规格')
        # Reject accidental distribution of a warehouse stock balance.
        for sku in merged.get('skus', []):
            if draft.get('mode', 'create') == 'create' and sku.get('stock', 0) != 0:
                raise ValueError('新建商品库存必须为零，库存分配请在统一库存处理')
            if (not isinstance(sku.get('code'), str) or not sku['code'].strip() or len(sku['code']) > 80
                    or not isinstance(sku.get('name'), str) or not sku['name'].strip() or len(sku['name']) > 120
                    or not isinstance(sku.get('price_cents'), int) or isinstance(sku['price_cents'], bool)
                    or not 0 <= sku['price_cents'] <= 100_000_000):
                raise ValueError('商品规格的编码、名称或整数分价格无效')
        if len({s['code'] for s in merged['skus']}) != len(merged['skus']):
            raise ValueError('商品规格编码不能重复')
        per_store = copy.deepcopy(draft['store_values'])
        for sid, values in store_values.items():
            per_store.setdefault(sid, {}).update(values)
        conn.execute('UPDATE publish_drafts SET common_values=?,store_values=?,revision=revision+1,status=\'draft\',updated=? WHERE id=?',
                     (db.dumps(merged), db.dumps(per_store), db.now(), draft_id))
        conn.execute("UPDATE publish_targets SET quality='{}',problem='',status=CASE WHEN schema_revision>0 THEN 'collected' ELSE 'uncollected' END,updated=? WHERE draft_id=?", (db.now(), draft_id))
    return get(draft_id)


def delete(draft_id):
    """Discard an unsubmitted draft without touching a platform product."""
    init()
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _draft(conn, draft_id)
        if any(t['status'] in LOCKED for t in _targets(conn, draft_id)):
            raise Blocked('草稿已有提交记录或待核对结果，请保留用于核对')
        active = conn.execute("SELECT 1 FROM jobs WHERE json_extract(payload,'$.draft_id')=? AND status IN ('queued','running','uncertain','submitted')", (draft_id,)).fetchone()
        if active:
            raise Blocked('草稿仍有执行中或待核对任务，请先结束任务')
        conn.execute('DELETE FROM publish_targets WHERE draft_id=?', (draft_id,))
        conn.execute('DELETE FROM publish_drafts WHERE id=?', (draft_id,))
        for group in conn.execute('SELECT id,draft_ids FROM publish_groups').fetchall():
            try:
                ids = [did for did in json.loads(group['draft_ids']) if did != draft_id]
            except (ValueError, TypeError):
                continue
            if ids:
                conn.execute('UPDATE publish_groups SET draft_ids=? WHERE id=?', (db.dumps(ids), group['id']))
            else:
                conn.execute('DELETE FROM publish_groups WHERE id=?', (group['id'],))
    db.audit('删除未提交发布草稿', {'draft_id': draft_id})
    return {'ok': True}


def _payload(conn, draft, target, shared):
    row = conn.execute('SELECT * FROM products WHERE id=?', (draft['product_id'],)).fetchone()
    if not row or row['version'] != draft['product_version']:
        raise Blocked('本地商品已变化，请新建发布草稿以免使用旧资料')
    product = json.loads(row['data'])
    product.update(id=row['id'], version=row['version'])
    product.update({k: copy.deepcopy(v) for k, v in draft['common_values'].items()
                    if k in ('title', 'description', 'image_ids', 'detail_image_ids', 'skus')})
    listing = conn.execute('SELECT * FROM listings WHERE product_id=? AND store_id=?', (draft['product_id'], target['store_id'])).fetchone()
    mode = draft.get('mode', 'create')
    external = str(listing['external_id']) if listing and listing['external_id'] else ''
    if mode == 'create' and external:
        raise Blocked('此商品已经关联平台商品，不允许作为新商品重复提交')
    if mode == 'update' and not external:
        raise Blocked('此商品在此店铺尚未关联平台商品 ID，不能作为更新提交')
    values = _values(draft, target, shared)
    # Platform overrides for recognized common product semantics must also be
    # visible to the browser media whitelist and reviewed product snapshot.
    for field in target['schema_data'].get('fields', []):
        semantic = field['semantic']
        if semantic in ('title', 'description', 'image_ids', 'detail_image_ids', 'skus') and field['key'] in values:
            product[semantic] = copy.deepcopy(values[field['key']])
    if mode == 'create':
        for sku in product.get('skus', []):
            if not isinstance(sku, dict) or sku.get('stock', 0) != 0:
                raise Blocked('新建商品库存必须为零，请重新核对规格')
    return {'draft_id': draft['id'], 'revision': draft['revision'], 'mode': mode,
            'external_id': external, 'product': product,
            'listing': dict(listing) if listing else {}, 'values': values,
            'schema': target['schema_data'], 'fingerprint': _fingerprint(draft, target, values),
            'require_platform_quality': (platform_score_required(target['platform'], mode)
                                         or bool(target.get('quality'))),
            'minimum_quality': QUALITY_THRESHOLD, 'minimum_local_quality': LOCAL_QUALITY_THRESHOLD,
            'quality_comparison': 'gt', 'local_quality_comparison': 'gte'}


def queue(draft_ids, action, revisions=None, upload_images=False, only_failed=False):
    init()
    if action not in ('collect', 'prepare', 'submit'):
        raise ValueError('未知统一发布操作')
    output, batch_id = [], db.uid()
    queued_any = False
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        group_of = {}
        for group in conn.execute('SELECT id,draft_ids FROM publish_groups'):
            for draft_id in _ids(group['draft_ids']):
                group_of[draft_id] = group['id']
        prepared = []
        for did in draft_ids:
            draft, targets = _draft(conn, did), _targets(conn, did)
            if revisions is not None and revisions.get(did) != draft['revision']:
                raise Blocked('发布表单已变化，请刷新后重新确认')
            selected = targets
            if only_failed:
                selected = [target for target in targets if target['status'] == 'needs_input']
                if not selected:
                    continue
            elif any(target['status'] in LOCKED for target in targets):
                raise Blocked('已有提交任务或结果待核对，不能自动重复发布')
            shared = _shared_fields(targets)
            view = _view(conn, draft)
            if action == 'submit' and not only_failed and not view['ready']:
                raise Blocked('全部目标店铺须补齐实际必填项、本地预检达到 95 分，且可用平台评分高于 90 分')
            ready_map = {store['store_id']: store['ready'] for store in view['stores']}
            for target in selected:
                if target['commerce_status'] != 'valid' or not target['commerce_enabled']:
                    raise Blocked('请先登录并启用所有目标店铺的电商后台')
                if action == 'submit' and not ready_map.get(target['store_id']):
                    raise Blocked('失败店铺仍未通过本地质量、必填项或平台评分检查，请补充后重试')
                active = conn.execute("SELECT id FROM jobs WHERE store_id=? AND kind LIKE 'workbench_%' AND status IN ('queued','running')", (target['store_id'],)).fetchone()
                if active:
                    raise Blocked('目标店铺已有页面读取或填表任务，请等待完成')
                payload = _payload(conn, draft, target, shared)
                payload['upload_images'] = bool(upload_images)
                # The task centre uses this to reopen the reviewed batch.
                payload['group_id'] = group_of.get(draft['id'], '')
                prepared.append((draft, target, payload))
                queued_any = True
        if only_failed and not prepared:
            raise Blocked('没有可重试的失败店铺；已提交或待核对的目标不会自动重发')
        # All targets are validated before any queue row is inserted.
        for draft, target, payload in prepared:
            key = hashlib.sha256(db.dumps({'draft': draft['id'], 'revision': draft['revision'],
                                         'store': target['store_id'], 'schema': target['schema_revision'], 'action': action,
                                         'payload': payload}).encode()).hexdigest()
            old = conn.execute('SELECT id,status FROM jobs WHERE dedupe_key=?', (key,)).fetchone()
            if old:
                output.append({**dict(old), 'duplicate': True})
                continue
            jid, now = db.uid(), db.now()
            conn.execute('INSERT INTO jobs(id,batch_id,kind,store_id,entity_id,status,scheduled,payload,dedupe_key,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         (jid, batch_id, 'workbench_' + action, target['store_id'], draft['product_id'], 'queued', now, db.dumps(payload), key, now, now))
            conn.execute('UPDATE publish_targets SET status=?,problem=\'\',updated=? WHERE draft_id=? AND store_id=?',
                         ('queued_' + action, now, draft['id'], target['store_id']))
            output.append({'id': jid, 'status': 'queued', 'duplicate': False})
    db.audit('统一发布操作', {'action': action, 'batch_id': batch_id, 'count': len(output)})
    return {'batch_id': batch_id, 'jobs': output}


def queue_group(group_id, action, revisions=None, upload_images=False, only_failed=False):
    init()
    with db.connection() as conn:
        ids = _group_ids(conn, group_id)
    return queue(ids, action, revisions, upload_images, only_failed=only_failed)


def retry_failed(group_id, upload_images=False):
    """Queue only failed targets; submitted and uncertain targets stay untouched."""
    return queue_group(group_id, 'submit', None, upload_images, only_failed=True)


def audit_group(group_id):
    """Queue read-only audit checks for targets that already have a receipt."""
    init()
    output, batch_id = [], db.uid()
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        ids = _group_ids(conn, group_id)
        for draft_id in ids:
            draft = _draft(conn, draft_id)
            for target in _targets(conn, draft_id):
                external = str((target.get('receipt') or {}).get('external_id') or '')
                if not external:
                    continue
                key = hashlib.sha256(db.dumps({'draft': draft_id, 'store': target['store_id'],
                                              'external': external, 'audit': True}).encode()).hexdigest()
                old = conn.execute('SELECT id,status FROM jobs WHERE dedupe_key=?', (key,)).fetchone()
                if old and old['status'] in ('queued', 'running'):
                    continue
                payload = {'draft_id': draft_id, 'revision': draft['revision'],
                           'external_id': external, 'mode': draft.get('mode', 'create'),
                           'group_id': group_id}
                jid, now = db.uid(), db.now()
                if old:
                    conn.execute("UPDATE jobs SET status='queued',scheduled=?,payload=?,updated=? WHERE id=?",
                                 (now, db.dumps(payload), now, old['id']))
                    jid = old['id']
                else:
                    conn.execute('INSERT INTO jobs(id,batch_id,kind,store_id,entity_id,status,scheduled,payload,dedupe_key,created,updated) '
                                 'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                 (jid, batch_id, 'workbench_audit', target['store_id'], draft['product_id'],
                                  'queued', now, db.dumps(payload), key, now, now))
                conn.execute("UPDATE publish_targets SET audit_status='checking',audit_message='',updated=? WHERE draft_id=? AND store_id=?",
                             (now, draft_id, target['store_id']))
                output.append({'id': jid, 'store_id': target['store_id'], 'duplicate': False})
    if not output:
        raise Blocked('没有可回查的已提交商品；请先成功提交并保存平台商品 ID')
    db.audit('回查平台审核状态', {'group_id': group_id, 'count': len(output)})
    return {'batch_id': batch_id, 'jobs': output}


def _quality(result, fingerprint, required=True):
    evidence = copy.deepcopy(result.get('quality', {}))
    if not isinstance(evidence, dict) or not evidence:
        return {}, ('尚未读取到平台实际商品质量分' if required else '')
    if not _valid_quality(evidence, fingerprint):
        return {}, ('尚未读取到与当前填写内容对应、且高于 90 分的平台实际评分'
                    if required else '平台评分证据已过期或无效，请重新回填检查')
    clean = {key: evidence[key] for key in ('source', 'score', 'fingerprint', 'observed_at')}
    clean['label'] = str(evidence.get('label', '平台商品质量分'))[:100]
    clean['evidence'] = str(evidence.get('evidence', ''))[:300]
    return clean, '' if clean['score'] > QUALITY_THRESHOLD else f"平台实际商品质量分为 {clean['score']}，须高于 90 分后才能提交"


def _schema_identity(schema):
    value = {key: copy.deepcopy(item) for key, item in schema.items() if key != 'observed_at'}
    for field in value.get('fields', []):
        field.pop('value', None)
    return value


def _execute_audit(payload, store):
    from .commerce_browser import CommerceBrowser
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        draft = _draft(conn, payload['draft_id'])
        target = next((item for item in _targets(conn, draft['id']) if item['store_id'] == store['id']), None)
        external = str((target or {}).get('receipt', {}).get('external_id') or '')
        if not target or not external or external != str(payload.get('external_id') or ''):
            raise Blocked('审核回查目标与已保存的商品回执不一致')
        conn.execute("UPDATE publish_targets SET audit_status='checking',updated=? WHERE draft_id=? AND store_id=?",
                     (db.now(), draft['id'], store['id']))
    result = CommerceBrowser(store).audit_workbench(payload)
    status = result.get('audit_status') if isinstance(result, dict) else ''
    if status not in ('pending', 'approved', 'rejected', 'offline'):
        raise Blocked('审核状态无法确认，未写入结果')
    message = str(result.get('audit_message') or '')[:300]
    code = str(result.get('audit_code') or '')[:40]
    with db.connection() as conn:
        conn.execute('UPDATE publish_targets SET audit_status=?,audit_message=?,audit_code=?,audit_checked=?,updated=? '
                     'WHERE draft_id=? AND store_id=?',
                     (status, message, code, db.now(), db.now(), payload['draft_id'], store['id']))
    db.audit('审核状态回查', {'draft_id': payload['draft_id'], 'store_id': store['id'], 'status': status})
    return 'succeeded', {'audit_status': status, 'audit_message': message,
                         'audit_code': code, 'external_id': payload['external_id']}


def _execute(job):
    """Worker dispatch, called before the legacy commerce task branches."""
    init()
    payload = json.loads(job['payload'])
    action = job['kind'].removeprefix('workbench_')
    from .commerce_browser import CommerceBrowser, check_session
    store = db.get_store(job['store_id'], secrets=True)
    if not store:
        raise Blocked('目标店铺不存在')
    check_session(store)
    if action == 'audit':
        return _execute_audit(payload, store)
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        draft = _draft(conn, payload['draft_id'])
        targets = _targets(conn, draft['id'])
        target = next((item for item in targets if item['store_id'] == store['id']), None)
        if not target or draft['revision'] != payload['revision']:
            raise Blocked('发布表单已变化，旧任务不再执行')
        reviewed = _payload(conn, draft, target, _shared_fields(targets))
        if reviewed['fingerprint'] != payload['fingerprint']:
            raise Blocked('平台必填项或商品资料已变化，请重新读取并确认')
        local = quality_checker.evaluate(reviewed['product'], reviewed['values'],
                                         target['schema_data'].get('fields', []), store['platform'],
                                         mode=draft.get('mode', 'create'),
                                         media_paths=_media_paths(conn, reviewed['product'], reviewed['values']))
        if action == 'submit' and not local['passed']:
            detail = '；'.join(local['blockers']) or f"本地质量预评分 {local['score']}，需达到 {LOCAL_QUALITY_THRESHOLD} 分"
            raise Blocked('本地质量检查未通过：' + detail)
        if action == 'submit' and not _platform_quality_ok(target, payload['fingerprint'], draft.get('mode', 'create')):
            raise Blocked('平台质量分证据已过期或未达到高于 90 分，已阻止提交')
        conn.execute("UPDATE publish_targets SET status=?,error_code='',updated=? WHERE draft_id=? AND store_id=?",
                     ('running_' + action, db.now(), draft['id'], store['id']))
    browser = CommerceBrowser(store)
    method = getattr(browser, {'collect': 'collect_requirements', 'prepare': 'prepare_workbench', 'submit': 'submit_workbench'}.get(action, ''), None)
    try:
        if not callable(method):
            raise Blocked('当前平台的页面读取或统一发布适配尚未完成')
        result = method(payload)
        if not isinstance(result, dict):
            raise (Uncertain if action == 'submit' else Blocked)('平台页面返回结构无法识别')
        schema = normalize_schema(store['platform'], result.get('schema', result)) if action == 'collect' or result.get('schema') else None
        quality_required = platform_score_required(store['platform'], draft.get('mode', 'create'))
        quality, problem = (_quality(result, payload['fingerprint'], required=quality_required)
                            if action != 'collect' else ({}, ''))
        evidence_present = bool(result.get('quality'))
        platform_ok = (_quality_ok(quality, payload['fingerprint']) if quality
                       else not quality_required and not evidence_present)
        if action == 'submit' and (not platform_ok or not isinstance(result.get('external_id'), str) or not result['external_id']):
            raise Uncertain('平台提交结果或实际评分无法确认，请核对后台，勿自动重发')
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = _draft(conn, draft['id'])
            if current['revision'] != payload['revision']:
                raise (Uncertain if action == 'submit' else Blocked)('执行期间发布表单被修改，请重新核对')
            schema_changed = schema is not None and _schema_identity(schema) != _schema_identity(target['schema_data'])
            if schema_changed:
                conn.execute('UPDATE publish_targets SET schema_data=?,schema_revision=schema_revision+1,quality=\'{}\' WHERE draft_id=? AND store_id=?',
                             (db.dumps(schema), draft['id'], store['id']))
                # A changed requirements snapshot invalidates an old fingerprint.
                if action != 'collect':
                    quality, problem = {}, '已读取到下一阶段要求，请保存核对后再次准备并读取平台评分'
            unresolved = [item for item in result.get('missing', []) if isinstance(item, dict)
                          and item.get('field', item.get('key')) != 'merchant_review']
            if unresolved:
                problem = '；'.join(str(item.get('label') or item.get('message') or item.get('reason') or '平台字段待核对')[:100]
                                   for item in unresolved[:15])
            active_schema = schema or target['schema_data']
            local = quality_checker.evaluate(payload['product'], payload['values'],
                                             active_schema.get('fields', []), store['platform'],
                                             mode=draft.get('mode', 'create'),
                                             media_paths=_media_paths(conn, payload['product'], payload['values']))
            local_ok = local['passed']
            if action == 'submit':
                platform_ok = (_quality_ok(quality, payload['fingerprint']) if quality
                               else not quality_required and not evidence_present)
            state = 'collected' if action == 'collect' else 'ready' if local_ok and platform_ok and not problem else 'needs_input'
            receipt = {}
            if action == 'submit':
                external = result['external_id']
                if len(external) > 100 or any(c.isspace() for c in external):
                    raise Uncertain('平台商品编号无效，请核对后台')
                sku_map = result.get('sku_map', {})
                codes = {s['code'] for s in payload['product']['skus']}
                if not isinstance(sku_map, dict) or not set(sku_map) <= codes or any(not isinstance(v, str) or not v for v in sku_map.values()):
                    raise Uncertain('平台 SKU 回执无法确认，请核对后台')
                mode = payload.get('mode', 'create')
                if mode == 'update':
                    expected = str(payload.get('external_id') or '')
                    if external != expected:
                        raise Uncertain('更新回执的商品 ID 与目标不一致，请核对后台')
                    linked = conn.execute('SELECT sku_map FROM listings WHERE product_id=? AND store_id=?',
                                          (draft['product_id'], store['id'])).fetchone()
                    if not linked:
                        raise Uncertain('更新目标已不存在，请核对后台')
                    merged = json.loads(linked['sku_map'] or '{}')
                    merged.update(sku_map)
                    conn.execute('UPDATE listings SET status=\'submitted\',sku_map=?,updated=? WHERE product_id=? AND store_id=?',
                                 (db.dumps(merged), db.now(), draft['product_id'], store['id']))
                    sku_map = merged
                else:
                    duplicate = conn.execute('SELECT product_id FROM listings WHERE store_id=? AND external_id=? AND product_id!=?', (store['id'], external, draft['product_id'])).fetchone()
                    if duplicate:
                        raise Uncertain('平台回执商品已关联其他本地商品，请核对后台')
                    conn.execute('INSERT INTO listings(product_id,store_id,external_id,status,sku_map,updated) VALUES(?,?,?,?,?,?) '
                                 'ON CONFLICT(product_id,store_id) DO UPDATE SET external_id=excluded.external_id,status=excluded.status,sku_map=excluded.sku_map,updated=excluded.updated',
                                 (draft['product_id'], store['id'], external, 'submitted', db.dumps(sku_map), db.now()))
                if store['platform'] == 'kuaishou':
                    # A product created here belongs to this video account; the
                    # existing video attach flow requires that explicit fact.
                    conn.execute('UPDATE listings SET owned_product=1 WHERE product_id=? AND store_id=?',
                                 (draft['product_id'], store['id']))
                receipt, state, problem = {'external_id': external, 'sku_map': sku_map}, 'submitted', ''
            conn.execute('UPDATE publish_targets SET status=?,quality=?,receipt=?,problem=?,updated=? WHERE draft_id=? AND store_id=?',
                         (state, db.dumps(quality), db.dumps(receipt), problem, db.now(), draft['id'], store['id']))
        return ('submitted' if action == 'submit' else 'succeeded'), {
            'message': result.get('message') or ('平台已受理，审核结果以后台为准' if action == 'submit' else problem or '已读取当前平台商品要求'),
            'draft_id': draft['id'], 'quality': quality, **receipt}
    except Exception as exc:
        state = 'uncertain' if action == 'submit' and not isinstance(exc, Blocked) else 'needs_input'
        message = str(exc) if isinstance(exc, (Blocked, Uncertain)) else '平台页面操作未完成，请查看任务并重新检查'
        code = str(getattr(exc, 'code', '') or '')[:40]
        with db.connection() as conn:
            if action == 'audit':
                conn.execute("UPDATE publish_targets SET audit_status='unknown',audit_message=?,audit_code=?,"
                             "audit_checked=?,updated=? WHERE draft_id=? AND store_id=?",
                             (message, code, db.now(), db.now(), draft['id'], store['id']))
            else:
                conn.execute('UPDATE publish_targets SET status=?,problem=?,error_code=?,updated=? WHERE draft_id=? AND store_id=?',
                             (state, message, code, db.now(), draft['id'], store['id']))
        raise


def execute(job):
    """Keep draft diagnostics correct even if validation stops before the browser."""
    try:
        return _execute(job)
    except Exception as exc:
        try:
            payload = json.loads(job['payload'])
            action = job['kind'].removeprefix('workbench_')
            state = 'uncertain' if action == 'submit' and not isinstance(exc, Blocked) else 'needs_input'
            message = str(exc) if isinstance(exc, (Blocked, Uncertain)) else '平台页面操作未完成，请查看任务并重新检查'
            code = str(getattr(exc, 'code', '') or '')[:40]
            with db.connection() as conn:
                if action == 'audit':
                    conn.execute("UPDATE publish_targets SET audit_status='unknown',audit_message=?,audit_code=?,"
                                 "audit_checked=?,updated=? WHERE draft_id=? AND store_id=?",
                                 (message, code, db.now(), db.now(), payload['draft_id'], job['store_id']))
                else:
                    conn.execute('UPDATE publish_targets SET status=?,problem=?,error_code=?,updated=? WHERE draft_id=? AND store_id=?',
                                 (state, message, code, db.now(), payload['draft_id'], job['store_id']))
        except (KeyError, TypeError, ValueError):
            pass
        raise
