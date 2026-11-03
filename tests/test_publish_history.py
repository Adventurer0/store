import json

import pytest

from app import db
from app import publish_workbench as wb
from app.commerce_browser import CommerceBrowser
from app.connectors import Blocked


def observed_schema(platform):
    return {
        'complete': True, 'stage': 'product_details',
        'source_url': {'douyin': 'https://fxg.jinritemai.com/products/new',
                       'kuaishou': 'https://s.kwaixiaodian.com/goods/new'}[platform],
        'fields': [{'key': 'title-' + platform, 'label': '商品标题', 'semantic': 'title',
                    'scope': 'common', 'type': 'text', 'required': True}],
    }


def evidence(payload, score=97):
    return {'source': 'platform_dom', 'score': score, 'fingerprint': payload['fingerprint'],
            'observed_at': db.now()}


def run_all(monkeypatch, method, callback):
    monkeypatch.setattr(CommerceBrowser, method, callback, raising=False)
    for job in db.all_rows("SELECT * FROM jobs WHERE status='queued' ORDER BY created,id"):
        status, result = wb.execute(job)
        with db.connection() as conn:
            conn.execute('UPDATE jobs SET status=?,result=? WHERE id=?',
                         (status, db.dumps(result), job['id']))


@pytest.fixture
def merchant(owner, commerce_setup):
    store = owner.post('/api/stores', json={'name': '抖音历史店', 'platform': 'douyin'}).json()
    commerce_setup(store)
    return store


def create_group(owner, product, store):
    response = owner.post('/api/publish-groups',
                          json={'product_ids': [product['id']], 'store_ids': [store['id']]})
    assert response.status_code == 200, response.text
    return response.json()


def test_group_history_summarises_states_and_records_batch_identity(owner, product, merchant, monkeypatch):
    group = create_group(owner, product, merchant)
    draft_id = group['drafts'][0]['id']
    assert wb.list_groups()['groups'][0]['status'] == 'editing'

    wb.queue_group(group['id'], 'collect')
    job = db.one("SELECT payload FROM jobs WHERE kind='workbench_collect'")
    assert json.loads(job['payload'])['group_id'] == group['id']
    run_all(monkeypatch, 'collect_requirements', lambda self, payload: observed_schema(self.platform))

    summary = wb.list_groups()['groups'][0]
    assert summary['status'] == 'editing'
    assert summary['store_count'] == 1 and summary['target_count'] == 1
    assert summary['product_titles'] == [product['title']]
    assert summary['submitted_count'] == 0 and summary['uncertain_count'] == 0

    draft = wb.save(draft_id, wb.get(draft_id)['revision'], {}, {})
    wb.queue_group(group['id'], 'prepare', {draft_id: draft['revision']})
    run_all(monkeypatch, 'prepare_workbench',
            lambda self, payload: {'schema': observed_schema(self.platform), 'quality': evidence(payload)})
    assert wb.list_groups()['groups'][0]['status'] == 'ready'

    draft = wb.get(draft_id)
    wb.queue_group(group['id'], 'submit', {draft_id: draft['revision']})

    def submit(self, payload):
        code = payload['product']['skus'][0]['code']
        return {'quality': evidence(payload), 'external_id': 'item-1', 'sku_map': {code: 'sku-1'}}
    run_all(monkeypatch, 'submit_workbench', submit)

    summary = wb.list_groups()['groups'][0]
    assert summary['status'] == 'submitted'
    assert summary['submitted_count'] == 1
    assert db.one('SELECT external_id FROM listings')['external_id'] == 'item-1'


def test_discard_group_removes_every_draft_but_keeps_local_products(owner, product, merchant):
    group = create_group(owner, product, merchant)
    assert wb.delete_group(group['id']) == {'ok': True, 'drafts': 1}
    assert not db.one('SELECT 1 FROM publish_groups')
    assert not db.one('SELECT 1 FROM publish_drafts')
    assert not db.one('SELECT 1 FROM publish_targets')
    assert db.get_product(product['id']) is not None
    with pytest.raises(KeyError, match='不存在'):
        wb.delete_group(group['id'])


def test_discard_group_refuses_running_jobs_and_submitted_receipts(owner, product, merchant):
    group = create_group(owner, product, merchant)
    wb.queue_group(group['id'], 'collect')
    with pytest.raises(Blocked, match='任务'):
        wb.delete_group(group['id'])
    assert db.one('SELECT 1 FROM publish_groups')

    with db.connection() as conn:
        conn.execute("UPDATE jobs SET status='cancelled' WHERE kind='workbench_collect'")
        conn.execute("UPDATE publish_targets SET status='submitted'")
    with pytest.raises(Blocked, match='对账'):
        wb.delete_group(group['id'])
    assert db.one('SELECT 1 FROM publish_drafts')


def test_group_history_paginates_without_dropping_batches(owner, product, merchant):
    for _ in range(3):
        create_group(owner, product, merchant)
    first = wb.list_groups(limit=2)
    second = wb.list_groups(limit=2, offset=2)
    assert first['total'] == 3 and len(first['groups']) == 2
    assert len(second['groups']) == 1 and second['offset'] == 2
    assert not {g['id'] for g in first['groups']} & {g['id'] for g in second['groups']}
    with pytest.raises(ValueError, match='1–50'):
        wb.list_groups(limit=0)
    with pytest.raises(ValueError, match='偏移量'):
        wb.list_groups(offset=-1)


def test_corrupted_batch_identity_does_not_hide_the_rest_of_the_history(owner, product, merchant):
    group = create_group(owner, product, merchant)
    with db.connection() as conn:
        conn.execute("UPDATE publish_groups SET draft_ids='not-json' WHERE id=?", (group['id'],))
    summary = wb.list_groups()['groups'][0]
    assert summary['product_count'] == 0 and summary['product_titles'] == []
    assert wb.delete_group(group['id']) == {'ok': True, 'drafts': 0}
    assert not db.one('SELECT 1 FROM publish_groups')


def test_group_history_route_paginates_requires_csrf_and_reports_missing(owner, product, merchant):
    group = create_group(owner, product, merchant)
    listing = owner.get('/api/publish-groups?limit=5').json()
    assert listing['total'] == 1 and listing['limit'] == 5
    assert listing['groups'][0]['product_titles'] == [product['title']]
    assert owner.get('/api/publish-groups?limit=100').status_code == 422
    assert owner.delete('/api/publish-groups/' + group['id'], headers={'X-CSRF-Token': ''}).status_code == 403
    assert owner.delete('/api/publish-groups/' + group['id']).json() == {'ok': True, 'drafts': 1}
    assert owner.delete('/api/publish-groups/' + group['id']).status_code == 404


def test_interrupted_jobs_do_not_leave_the_record_list_stuck_in_working(owner, product, merchant):
    group = create_group(owner, product, merchant)
    wb.queue_group(group['id'], 'collect')
    job = db.one("SELECT id FROM jobs WHERE kind='workbench_collect'")
    with db.connection() as conn:
        conn.execute("UPDATE jobs SET status='uncertain' WHERE id=?", (job['id'],))
        conn.execute("UPDATE publish_targets SET status='queued_collect'")
    assert wb.list_groups()['groups'][0]['status'] == 'editing'
    with pytest.raises(Blocked, match='任务'):
        wb.delete_group(group['id'])

    with db.connection() as conn:
        conn.execute("UPDATE jobs SET kind='workbench_submit',status='uncertain'")
        conn.execute("UPDATE publish_targets SET status='queued_submit'")
    summary = wb.list_groups()['groups'][0]
    assert summary['status'] == 'uncertain' and summary['uncertain_count'] == 1
