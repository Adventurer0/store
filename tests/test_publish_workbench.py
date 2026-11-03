import copy
import json
import pytest

from app import db, publish_workbench as wb
from app.commerce_browser import CommerceBrowser
from app.connectors import Blocked, Uncertain
from app.publish_workbench_routes import router
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


def schema(platform='douyin', *, complete=True, title_limit=60, brands=('无品牌', '品牌A')):
    portal = {'douyin': 'https://fxg.jinritemai.com/products/new', 'kuaishou': 'https://s.kwaixiaodian.com/new'}[platform]
    return {'complete': complete, 'stage': 'product_info', 'source_url': portal, 'fields': [
        {'key': 'title-' + platform, 'label': '商品标题', 'semantic': 'title', 'scope': 'common',
         'type': 'text', 'required': True, 'max_length': title_limit, 'locator': {'css': '#private-title-selector'}},
        {'key': 'brand-' + platform, 'label': '品牌', 'semantic': 'brand', 'scope': 'common',
         'type': 'select', 'required': True, 'options': [{'value': x, 'label': x} for x in brands]},
        {'key': 'category-' + platform, 'label': '类目', 'semantic': 'category_path', 'scope': 'common',
         'type': 'text', 'required': True},
        {'key': 'shipping-' + platform, 'label': '运费模板', 'semantic': 'freight_template', 'scope': 'common',
         'type': 'text', 'required': True},
    ]}


@pytest.fixture
def context(owner, product, commerce_setup):
    stores = []
    for platform in ('douyin', 'kuaishou'):
        store = owner.post('/api/stores', json={'name': platform, 'platform': platform}).json()
        commerce_setup(store)
        stores.append(store)
    group = wb.create([product['id']], [s['id'] for s in stores])
    return {'group': group, 'draft': group['drafts'][0], 'stores': stores, 'product': product}


def run_all(monkeypatch, method, callback):
    monkeypatch.setattr(CommerceBrowser, method, callback, raising=False)
    rows = db.all_rows("SELECT * FROM jobs WHERE status='queued' ORDER BY created,id")
    for job in rows:
        try:
            status, result = wb.execute(job)
        except Blocked as exc:
            status, result = 'blocked', {'message': str(exc)}
        except Uncertain as exc:
            status, result = 'uncertain', {'message': str(exc)}
        with db.connection() as conn:
            conn.execute('UPDATE jobs SET status=?,result=? WHERE id=?', (status, db.dumps(result), job['id']))
    return rows


def collect(context, monkeypatch, callback=None):
    wb.queue_group(context['group']['id'], 'collect')
    run_all(monkeypatch, 'collect_requirements', callback or (lambda self, payload: schema(self.platform)))
    return wb.get(context['draft']['id'])


def fill(context, monkeypatch):
    draft = collect(context, monkeypatch)
    per_store = {s['store_id']: {'category-' + s['platform']: '旅行箱', 'shipping-' + s['platform']: '本店运费模板'} for s in draft['stores']}
    return wb.save(draft['id'], draft['revision'], {'brand': '无品牌'}, per_store)


def prepare(context, monkeypatch, score=97, source='platform_dom', extra=None):
    draft = fill(context, monkeypatch)
    wb.queue_group(context['group']['id'], 'prepare', {draft['id']: draft['revision']}, upload_images=False)
    def result(self, payload):
        value = {'quality': {'source': source, 'score': score, 'fingerprint': payload['fingerprint'], 'observed_at': db.now()}}
        if extra:
            value.update(extra(self, payload))
        return value
    run_all(monkeypatch, 'prepare_workbench', result)
    return wb.get(draft['id'])


def test_actual_observed_fields_merge_common_and_keep_category_shipping_per_store(context, monkeypatch):
    draft = collect(context, monkeypatch, lambda self, payload: schema(self.platform, title_limit=30 if self.platform == 'kuaishou' else 60))
    common = {field['key']: field for field in draft['common_fields']}
    assert set(common) == {'title', 'brand'}
    assert common['title']['max_length'] == 30
    assert common['title']['value'] == context['product']['title']
    assert common['brand']['options'] == [{'value': '无品牌', 'label': '无品牌'}, {'value': '品牌A', 'label': '品牌A'}]
    assert all({f['semantic'] for f in s['fields']} == {'category_path', 'freight_template'} for s in draft['stores'])
    assert 'private-title-selector' not in json.dumps(draft)
    assert 'locator' not in json.dumps(draft)
    assert draft['common_values']['skus'][0]['stock'] == 0
    assert context['product']['skus'][0]['stock'] == 10


def test_conflicting_enumerations_are_not_merged(context, monkeypatch):
    draft = collect(context, monkeypatch, lambda self, payload: schema(self.platform, brands=('平台专用A',) if self.platform == 'douyin' else ('平台专用B',)))
    assert 'brand' not in {f['key'] for f in draft['common_fields']}
    assert all('brand' in {f['semantic'] for f in s['fields']} for s in draft['stores'])


def test_incomplete_first_page_never_becomes_submittable(context, monkeypatch):
    draft = collect(context, monkeypatch, lambda self, payload: schema(self.platform, complete=False))
    assert not draft['ready']
    assert all(not store['complete'] for store in draft['stores'])
    with pytest.raises(Blocked, match='90'):
        wb.queue_group(context['group']['id'], 'submit', {draft['id']: draft['revision']})
    assert not db.one("SELECT 1 FROM jobs WHERE kind='workbench_submit'")


@pytest.mark.parametrize('score,source', [(90, 'platform_dom'), (89, 'platform_dom'), (None, 'platform_dom'), (97, 'local_estimate'), (101, 'platform_dom'), (True, 'platform_dom')])
def test_platform_score_must_strictly_exceed_90_and_be_actual(context, monkeypatch, score, source):
    draft = prepare(context, monkeypatch, score, source)
    assert not draft['ready']
    assert all(not store['quality_passed'] for store in draft['stores'])
    with pytest.raises(Blocked):
        wb.queue_group(context['group']['id'], 'submit', {draft['id']: draft['revision']})


def test_missing_values_block_whole_batch_and_no_user_score_injection(context, monkeypatch):
    draft = collect(context, monkeypatch)
    with pytest.raises(ValueError, match='未读取'):
        wb.save(draft['id'], draft['revision'], {'quality': {'score': 99}}, {})
    assert any(s['missing'] for s in draft['stores'])
    with pytest.raises(Blocked):
        wb.queue_group(context['group']['id'], 'submit', {draft['id']: draft['revision']})
    assert not db.one("SELECT 1 FROM jobs WHERE kind='workbench_submit'")


def test_save_invalidates_quality_and_enforces_each_platform_constraints(context, monkeypatch):
    draft = prepare(context, monkeypatch)
    assert draft['ready']
    changed = wb.save(draft['id'], draft['revision'], {'title': '更新后的行李箱'}, {})
    assert changed['revision'] == draft['revision'] + 1
    assert not changed['ready']
    assert all(s['quality'] == {} for s in changed['stores'])
    with pytest.raises(Blocked, match='修改'):
        wb.save(draft['id'], draft['revision'], {}, {})
    with pytest.raises(ValueError, match='库存必须为零'):
        wb.save(changed['id'], changed['revision'], {'skus': context['product']['skus']}, {})


def test_same_schema_returned_after_prepare_does_not_invalidate_quality(context, monkeypatch):
    draft = prepare(context, monkeypatch, extra=lambda self, payload: {'schema': schema(self.platform)})
    assert draft['ready']
    assert all(s['quality_passed'] for s in draft['stores'])


def test_actual_low_quality_is_displayed_but_still_blocks(context, monkeypatch):
    draft = prepare(context, monkeypatch, score=85)
    assert not draft['ready']
    assert all(s['quality']['score'] == 85 for s in draft['stores'])
    assert all(s['status'] == 'needs_input' for s in draft['stores'])


def test_unresolved_platform_controls_block_even_when_score_is_high(context, monkeypatch):
    draft = prepare(context, monkeypatch, extra=lambda *a: {'missing': [{'field': 'skus', 'label': '商品规格', 'reason': '无法匹配规格'}]})
    assert not draft['ready']
    assert all(s['status'] == 'needs_input' and '商品规格' in s['problems'] for s in draft['stores'])


def test_new_stage_schema_discards_old_score(context, monkeypatch):
    def extra(self, payload):
        value = schema(self.platform)
        value['fields'].append({'key': 'certificate', 'label': '平台资质材料', 'type': 'text', 'required': True})
        return {'schema': value}
    draft = prepare(context, monkeypatch, extra=extra)
    assert not draft['ready']
    assert all('certificate' in {f['key'] for f in s['fields']} for s in draft['stores'])
    assert all(s['quality'] == {} for s in draft['stores'])


def test_expired_or_wrong_revision_score_blocks_submit(context, monkeypatch):
    draft = prepare(context, monkeypatch)
    with db.connection() as conn:
        for target in conn.execute('SELECT * FROM publish_targets').fetchall():
            q = json.loads(target['quality'])
            q['observed_at'] = db.now() - wb.EVIDENCE_TTL - 1
            conn.execute('UPDATE publish_targets SET quality=? WHERE draft_id=? AND store_id=?', (db.dumps(q), target['draft_id'], target['store_id']))
    assert not wb.get(draft['id'])['ready']
    with pytest.raises(Blocked):
        wb.queue_group(context['group']['id'], 'submit', {draft['id']: draft['revision']})


def test_platform_configuration_change_invalidates_review_evidence(context, monkeypatch):
    draft = prepare(context, monkeypatch)
    store = db.get_store(context['stores'][0]['id'])
    store['templates']['commerce_browser']['identity']['value'] = 'changed-shop'
    with db.connection() as conn:
        conn.execute('UPDATE stores SET templates=? WHERE id=?', (db.dumps(store['templates']), store['id']))
    assert not wb.get(draft['id'])['ready']
    with pytest.raises(Blocked):
        wb.queue_group(context['group']['id'], 'submit', {draft['id']: draft['revision']})


def test_one_store_below_threshold_blocks_every_target_atomically(context, monkeypatch):
    draft = prepare(context, monkeypatch)
    target = db.one('SELECT * FROM publish_targets LIMIT 1')
    quality = json.loads(target['quality'])
    quality['score'] = 90
    with db.connection() as conn:
        conn.execute('UPDATE publish_targets SET quality=? WHERE draft_id=? AND store_id=?',
                     (db.dumps(quality), target['draft_id'], target['store_id']))
    with pytest.raises(Blocked):
        wb.queue_group(context['group']['id'], 'submit', {draft['id']: draft['revision']})
    assert not db.one("SELECT 1 FROM jobs WHERE kind='workbench_submit'")


def test_stale_browser_score_is_not_retimestamped_as_fresh(context, monkeypatch):
    draft = prepare(context, monkeypatch, extra=lambda self, payload: {'quality': {
        'source': 'platform_dom', 'score': 97, 'fingerprint': payload['fingerprint'],
        'observed_at': db.now() - wb.EVIDENCE_TTL - 10}})
    assert not draft['ready']
    assert all(store['quality'] == {} for store in draft['stores'])


def test_confirmed_batch_saves_independent_receipts_and_prevents_duplicate_create(context, monkeypatch):
    draft = prepare(context, monkeypatch)
    queued = wb.queue_group(context['group']['id'], 'submit', {draft['id']: draft['revision']})
    assert len(queued['jobs']) == 2
    def submit(self, payload):
        assert payload['minimum_quality'] == 90 and payload['quality_comparison'] == 'gt'
        return {'quality': {'score': 97, 'source': 'platform_dom', 'fingerprint': payload['fingerprint'], 'observed_at': db.now()},
                'external_id': 'item-' + self.platform, 'sku_map': {payload['product']['skus'][0]['code']: 'sku-' + self.platform}}
    run_all(monkeypatch, 'submit_workbench', submit)
    final = wb.get(draft['id'])
    assert {s['receipt']['external_id'] for s in final['stores']} == {'item-douyin', 'item-kuaishou'}
    assert len(db.all_rows('SELECT * FROM listings')) == 2
    owned = {row['store_id']: row['owned_product'] for row in db.all_rows('SELECT store_id,owned_product FROM listings')}
    assert owned[context['stores'][0]['id']] == 0
    assert owned[context['stores'][1]['id']] == 1, 'Kuaishou products from unified publishing must be attachable to video'
    with pytest.raises(Blocked, match='提交'):
        wb.queue_group(context['group']['id'], 'submit', {draft['id']: draft['revision']})
    with pytest.raises(Blocked, match='提交'):
        wb.save(draft['id'], draft['revision'], {'title': '试图再发'}, {})


def test_lost_receipt_after_submit_is_uncertain_never_retried(context, monkeypatch):
    draft = prepare(context, monkeypatch)
    wb.queue_group(context['group']['id'], 'submit', {draft['id']: draft['revision']})
    run_all(monkeypatch, 'submit_workbench', lambda self, payload: {})
    assert all(s['status'] == 'uncertain' for s in wb.get(draft['id'])['stores'])
    assert not db.one('SELECT 1 FROM listings')
    with pytest.raises(Blocked):
        wb.queue_group(context['group']['id'], 'submit', {draft['id']: draft['revision']})


def test_multiple_products_submit_is_atomic_if_one_is_not_ready(owner, context, monkeypatch):
    p2 = owner.post('/api/products', json={'title': '另一个箱子', 'skus': context['product']['skus']}).json()
    group = wb.create([context['product']['id'], p2['id']], [s['id'] for s in context['stores']])
    with pytest.raises(Blocked):
        wb.queue_group(group['id'], 'submit', {d['id']: d['revision'] for d in group['drafts']})
    assert not db.one("SELECT 1 FROM jobs WHERE kind='workbench_submit'")
    assert len(group['drafts']) == 2


def test_old_product_snapshot_blocks_browser_before_read_or_write(owner, context, monkeypatch):
    draft = context['draft']
    wb.queue_group(context['group']['id'], 'collect')
    owner.put('/api/products/' + context['product']['id'], json={'title': '后来修改', 'version': context['product']['version'], 'skus': context['product']['skus']})
    monkeypatch.setattr(CommerceBrowser, 'collect_requirements', lambda *a: pytest.fail('stale product reached platform'), raising=False)
    job = db.one("SELECT * FROM jobs WHERE status='queued' LIMIT 1")
    with pytest.raises(Blocked, match='变化'):
        wb.execute(job)
    assert not wb.get(draft['id'])['ready']


def test_schema_rejects_sensitive_or_untrusted_fields_without_exposing_selectors():
    raw = schema()
    raw['fields'].append({'key': 'csrf_token', 'label': 'csrf token', 'type': 'text', 'required': True, 'value': 'sensitive'})
    result = wb.normalize_schema('douyin', raw)
    assert len(result['fields']) == 4
    bad = copy.deepcopy(raw)
    bad['source_url'] = 'https://evil.example/product'
    with pytest.raises(Blocked):
        wb.normalize_schema('douyin', bad)
    with pytest.raises(Blocked):
        wb.normalize_schema('douyin', {'complete': True, 'source_url': 'https://fxg.jinritemai.com/', 'fields': []})


def test_discard_unsubmitted_draft_releases_local_media_references(context):
    assert wb.delete(context['draft']['id']) == {'ok': True}
    assert not db.one('SELECT 1 FROM publish_drafts')
    assert db.get_product(context['product']['id']) is not None


def test_route_auth_csrf_and_rejects_client_quality_evidence(owner, context):
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(Blocked, lambda request, exc: JSONResponse({'detail': str(exc)}, status_code=409))
    with TestClient(app) as client:
        assert client.get('/api/publish-groups').status_code == 401
        client.cookies.update(owner.cookies)
        body = {'product_ids': [context['product']['id']], 'store_ids': [s['id'] for s in context['stores']]}
        assert client.post('/api/publish-groups', json=body).status_code == 403
        client.headers['X-CSRF-Token'] = owner.headers['X-CSRF-Token']
        created = client.post('/api/publish-groups', json=body)
        assert created.status_code == 200
        group = created.json()
        assert len(group['drafts']) == 1
        assert client.get('/api/publish-groups').json()['groups']
        did = group['drafts'][0]['id']
        forged = client.post(f'/api/publish-drafts/{did}/prepare', json={'revision': 1, 'quality': {'source': 'platform_dom', 'score': 99}})
        assert forged.status_code == 422
        wrong_type = client.post(f'/api/publish-drafts/{did}/prepare', json={'revision': 1, 'upload_images': 'yes'})
        assert wrong_type.status_code == 422
        assert client.post(f'/api/publish-groups/{group["id"]}/submit', json={}).status_code == 422
        assert client.delete(f'/api/publish-drafts/{did}').json() == {'ok': True}


def test_single_target_shared_semantics_stay_in_common_form(owner, product, commerce_setup, monkeypatch):
    store = owner.post('/api/stores', json={'name': 'only', 'platform': 'douyin'}).json()
    commerce_setup(store)
    group = wb.create([product['id']], [store['id']])
    wb.queue_group(group['id'], 'collect')
    run_all(monkeypatch, 'collect_requirements', lambda self, payload: schema(self.platform))
    draft = wb.get(group['drafts'][0]['id'])
    assert {'title', 'brand'} == {f['key'] for f in draft['common_fields']}
    assert 'title' not in {f['semantic'] for f in draft['stores'][0]['fields']}


def test_retry_failed_only_queues_needs_input_targets(context, monkeypatch):
    draft = prepare(context, monkeypatch)
    first, second = context['stores'][0]['id'], context['stores'][1]['id']
    with db.connection() as conn:
        conn.execute("UPDATE publish_targets SET status='submitted' WHERE draft_id=? AND store_id=?", (draft['id'], first))
        conn.execute("UPDATE publish_targets SET status='needs_input' WHERE draft_id=? AND store_id=?", (draft['id'], second))
    queued = wb.retry_failed(context['group']['id'])
    assert len(queued['jobs']) == 1
    job = db.one("SELECT store_id FROM jobs WHERE kind='workbench_submit'")
    assert job['store_id'] == second
    with db.connection() as conn:
        conn.execute("UPDATE publish_targets SET status='submitted' WHERE draft_id=?", (draft['id'],))
        conn.execute("DELETE FROM jobs WHERE kind='workbench_submit'")
    with pytest.raises(Blocked, match='没有可重试'):
        wb.retry_failed(context['group']['id'])


def test_update_batch_requires_links_and_never_creates_a_second_product(context, monkeypatch):
    product, stores = context['product'], context['stores']
    with pytest.raises(Blocked, match='尚未关联'):
        wb.create([product['id']], [s['id'] for s in stores], 'update')
    with db.connection() as conn:
        for store in stores:
            conn.execute("INSERT INTO listings(product_id,store_id,external_id,status,sku_map,updated) "
                         "VALUES(?,?,?,?,?,?)", (product['id'], store['id'], 'old-' + store['platform'],
                                                'mapped', '{}', db.now()))
    group = wb.create([product['id']], [s['id'] for s in stores], 'update')
    draft_id = group['drafts'][0]['id']
    assert group['drafts'][0]['mode'] == 'update'
    wb.queue_group(group['id'], 'collect')
    run_all(monkeypatch, 'collect_requirements', lambda self, payload: schema(self.platform))
    draft = wb.get(draft_id)
    per_store = {s['store_id']: {'category-' + s['platform']: '旅行箱',
                                 'shipping-' + s['platform']: '本店运费模板'} for s in draft['stores']}
    draft = wb.save(draft_id, draft['revision'], {'brand': '无品牌'}, per_store)
    wb.queue_group(group['id'], 'prepare', {draft_id: draft['revision']}, upload_images=False)
    run_all(monkeypatch, 'prepare_workbench', lambda self, payload: {
        'schema': schema(self.platform),
        'quality': {'source': 'platform_dom', 'score': 97, 'fingerprint': payload['fingerprint'],
                    'observed_at': db.now()}})
    draft = wb.get(draft_id)
    assert draft['ready']
    wb.queue_group(group['id'], 'submit', {draft_id: draft['revision']})
    run_all(monkeypatch, 'submit_workbench', lambda self, payload: {
        'external_id': payload['external_id'],
        'sku_map': {payload['product']['skus'][0]['code']: 'sku-' + self.platform},
        'quality': {'source': 'platform_dom', 'score': 97, 'fingerprint': payload['fingerprint'],
                    'observed_at': db.now()}})
    final = wb.get(draft_id)
    assert {s['receipt']['external_id'] for s in final['stores']} == {'old-douyin', 'old-kuaishou'}
    rows = db.all_rows('SELECT external_id FROM listings ORDER BY external_id')
    assert [row['external_id'] for row in rows] == ['old-douyin', 'old-kuaishou']


def test_audit_group_reads_status_without_changing_publish_status(context, monkeypatch):
    draft = prepare(context, monkeypatch)
    store_id = context['stores'][0]['id']
    with db.connection() as conn:
        conn.execute("UPDATE publish_targets SET status='submitted',receipt=? WHERE draft_id=? AND store_id=?",
                     (db.dumps({'external_id': 'old-1', 'sku_map': {}}), draft['id'], store_id))
    queued = wb.audit_group(context['group']['id'])
    assert len(queued['jobs']) == 1
    job = db.one("SELECT * FROM jobs WHERE kind='workbench_audit'")
    monkeypatch.setattr(CommerceBrowser, 'audit_workbench', lambda self, payload: {
        'audit_status': 'approved', 'audit_message': '审核通过', 'audit_code': ''})
    status, result = wb.execute(job)
    assert status == 'succeeded' and result['audit_status'] == 'approved'
    target = db.one('SELECT status,audit_status,audit_message,receipt FROM publish_targets WHERE draft_id=? AND store_id=?',
                    (draft['id'], store_id))
    assert target['status'] == 'submitted' and target['audit_status'] == 'approved'
    assert json.loads(target['receipt'])['external_id'] == 'old-1'


def test_audit_failure_does_not_change_publish_status(context, monkeypatch):
    draft = prepare(context, monkeypatch)
    store_id = context['stores'][0]['id']
    with db.connection() as conn:
        conn.execute("UPDATE publish_targets SET status='submitted',receipt=? WHERE draft_id=? AND store_id=?",
                     (db.dumps({'external_id': 'old-1', 'sku_map': {}}), draft['id'], store_id))
    wb.audit_group(context['group']['id'])
    job = db.one("SELECT * FROM jobs WHERE kind='workbench_audit'")
    monkeypatch.setattr(CommerceBrowser, 'audit_workbench', lambda self, payload: {
        'audit_status': 'unknown', 'audit_message': '', 'audit_code': ''})
    with pytest.raises(Blocked):
        wb.execute(job)
    target = db.one('SELECT status,audit_status FROM publish_targets WHERE draft_id=? AND store_id=?',
                    (draft['id'], store_id))
    assert target['status'] == 'submitted' and target['audit_status'] == 'unknown'


def test_non_kuaishou_can_ready_on_local_score_without_platform_score(owner, product, commerce_setup, monkeypatch):
    store = owner.post('/api/stores', json={'name': 'only-douyin', 'platform': 'douyin'}).json()
    commerce_setup(store)
    group = wb.create([product['id']], [store['id']])
    draft_id = group['drafts'][0]['id']
    wb.queue_group(group['id'], 'collect')
    run_all(monkeypatch, 'collect_requirements', lambda self, payload: schema('douyin'))
    draft = wb.get(draft_id)
    draft = wb.save(draft_id, draft['revision'], {'brand': '无品牌'},
                    {store['id']: {'category-douyin': '旅行箱', 'shipping-douyin': '本店运费模板'}})
    wb.queue_group(group['id'], 'prepare', {draft_id: draft['revision']})
    run_all(monkeypatch, 'prepare_workbench', lambda self, payload: {'schema': schema('douyin'), 'quality': {}})
    draft = wb.get(draft_id)
    assert draft['ready']
    assert draft['stores'][0]['platform_quality_required'] is False
    assert draft['stores'][0]['local_quality_passed'] is True


def test_local_score_below_95_blocks_even_with_high_platform_score(owner, commerce_setup, monkeypatch):
    product = owner.post('/api/products', json={'title': '缺少描述的商品', 'category': '箱包', 'skus': [
        {'code': 'A', 'name': '默认', 'price_cents': 1000, 'stock': 0}]}).json()
    store = owner.post('/api/stores', json={'name': 'score-store', 'platform': 'douyin'}).json()
    commerce_setup(store)
    group = wb.create([product['id']], [store['id']])
    draft_id = group['drafts'][0]['id']
    wb.queue_group(group['id'], 'collect')
    run_all(monkeypatch, 'collect_requirements', lambda self, payload: schema('douyin'))
    draft = wb.get(draft_id)
    draft = wb.save(draft_id, draft['revision'], {'brand': '无品牌'},
                    {store['id']: {'category-douyin': '旅行箱', 'shipping-douyin': '本店运费模板'}})
    wb.queue_group(group['id'], 'prepare', {draft_id: draft['revision']})
    run_all(monkeypatch, 'prepare_workbench', lambda self, payload: {
        'schema': schema('douyin'),
        'quality': {'source': 'platform_dom', 'score': 97, 'fingerprint': payload['fingerprint'],
                    'observed_at': db.now()}})
    draft = wb.get(draft_id)
    assert draft['ready'] is False
    assert draft['stores'][0]['local_quality']['score'] < 95
    assert any('本地质量预评分' in problem for problem in draft['stores'][0]['problems'])
