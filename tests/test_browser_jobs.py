"""Job/ledger integration at the browser boundary, without real shop writes."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import db, inventory, jobs
from app.connectors import Blocked, Uncertain


@pytest.fixture
def browser_boundary(monkeypatch, commerce_setup):
    """Use real session and workflow validation; replace only browser execution."""
    monkeypatch.setattr(jobs, 'commerce_available', lambda: True)
    for store in db.all_rows('SELECT id FROM stores'):
        commerce_setup(store)
    return commerce_setup


def set_valid(store):
    with db.connection() as c:
        c.execute("UPDATE stores SET commerce_status='valid',video_status='unverified' WHERE id=?", (store['id'],))
    return db.get_store(store['id'], secrets=True)


def claim_and_process():
    worker = jobs.Worker()
    job = worker.claim()
    assert job is not None
    worker.process(job)
    return db.one('SELECT * FROM jobs WHERE id=?', (job['id'],))


@pytest.mark.parametrize('platform', ['douyin', 'kuaishou', 'wechat', 'tmall', 'jd', 'pdd'])
def test_every_platform_runs_products_through_browser(owner, product, browser_boundary, monkeypatch, platform):
    store = owner.post('/api/stores', json={'name': platform, 'platform': platform}).json()
    browser_boundary(store)
    set_valid(store)
    calls = []

    class Browser:
        def __init__(self, current):
            assert current['id'] == store['id']

        def mutate_product(self, action, payload):
            calls.append((action, payload))
            return {'external_id': '90071992547409999', 'sku_map': {product['skus'][0]['code']: 'sku-9'}}

    monkeypatch.setattr(jobs, 'CommerceBrowser', Browser)
    body = {'kind': 'product_create', 'store_ids': [store['id']], 'product_ids': [product['id']]}
    assert owner.post('/api/jobs/preflight', json=body).json()['ready'] == 1
    owner.post('/api/jobs', json=body)
    result = claim_and_process()
    assert result['status'] == 'submitted'
    mapped = inventory.listing(db.one('SELECT * FROM listings WHERE store_id=?', (store['id'],)))
    assert mapped['external_id'] == '90071992547409999'
    assert mapped['sku_map'] == {product['skus'][0]['code']: 'sku-9'}
    assert len(calls) == 1 and calls[0][0] == 'product_create'
    assert db.get_store(store['id'])['video_status'] == 'unverified'


def test_commerce_login_check_logout_do_not_authorize_video(owner, store, browser_boundary, monkeypatch):
    calls = []

    class Browser:
        def __init__(self, current):
            pass

        def account(self, action, job_id=''):
            calls.append(action)
            return {'message': action}

    monkeypatch.setattr(jobs, 'CommerceBrowser', Browser)
    for action, status in [('login', 'valid'), ('check', 'valid'), ('logout', 'unverified')]:
        jobs.commerce_account_job(db.get_store(store['id']), 'commerce_' + action)
        assert claim_and_process()['status'] == 'succeeded'
        current = db.get_store(store['id'])
        assert current['commerce_status'] == status
        assert current['video_status'] == 'unverified'
    assert calls == ['login', 'check', 'logout']


def test_commerce_login_and_video_login_have_distinct_queues(owner, store, browser_boundary, monkeypatch):
    monkeypatch.setattr(jobs, 'mpau_available', lambda: True)
    first = jobs.commerce_account_job(db.get_store(store['id']), 'commerce_login')
    duplicate = jobs.commerce_account_job(db.get_store(store['id']), 'commerce_login')
    for action in ('commerce_check', 'commerce_logout'):
        with pytest.raises(Blocked, match='等待完成'):
            jobs.commerce_account_job(db.get_store(store['id']), action)
    video = jobs.account_job(db.get_store(store['id']), 'account_login')
    assert first['jobs'][0]['id'] == duplicate['jobs'][0]['id']
    assert duplicate['jobs'][0]['duplicate'] is True
    assert video['jobs'][0]['id'] != first['jobs'][0]['id']


def test_video_account_actions_only_deduplicate_matching_action(owner, store, browser_boundary, monkeypatch):
    monkeypatch.setattr(jobs, 'mpau_available', lambda: True)
    first = jobs.account_job(db.get_store(store['id']), 'account_login')
    same = jobs.account_job(db.get_store(store['id']), 'account_login')
    assert same['jobs'][0]['duplicate'] is True and same['jobs'][0]['id'] == first['jobs'][0]['id']
    with pytest.raises(Blocked, match='等待完成'):
        jobs.account_job(db.get_store(store['id']), 'account_check')


def test_concurrent_account_requests_create_only_one_job(owner, store, browser_boundary):
    current = db.get_store(store['id'])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: jobs.commerce_account_job(current, 'commerce_login'), range(4)))
    assert len({result['jobs'][0]['id'] for result in results}) == 1
    assert db.one("SELECT count(*) n FROM jobs WHERE kind='commerce_login'")['n'] == 1


def test_login_expiration_survives_job_error_handling(owner, store, browser_boundary, monkeypatch):
    class Browser:
        def __init__(self, current):
            pass

        def account(self, action, job_id=''):
            with db.connection() as c:
                c.execute("UPDATE stores SET commerce_status='expired' WHERE id=?", (store['id'],))
            raise Blocked('电商 Cookie 已失效')

    monkeypatch.setattr(jobs, 'CommerceBrowser', Browser)
    jobs.commerce_account_job(db.get_store(store['id']), 'commerce_check')
    assert claim_and_process()['status'] == 'blocked'
    assert db.get_store(store['id'])['commerce_status'] == 'expired'
    assert db.get_store(store['id'])['video_status'] == 'unverified'


def test_missing_product_receipt_is_uncertain_and_cannot_republish(owner, store, product, browser_boundary, monkeypatch):
    class Browser:
        def __init__(self, current):
            pass

        def mutate_product(self, action, payload):
            return {'message': '点击过提交但无法确定回执'}

    monkeypatch.setattr(jobs, 'CommerceBrowser', Browser)
    body = {'kind': 'product_create', 'store_ids': [store['id']], 'product_ids': [product['id']]}
    first = owner.post('/api/jobs', json=body).json()['jobs'][0]
    assert claim_and_process()['status'] == 'uncertain'
    assert db.one('SELECT * FROM listings') is None
    again = owner.post('/api/jobs', json=body).json()['jobs'][0]
    assert again['duplicate'] is True and again['id'] == first['id']


def test_edited_product_is_blocked_before_browser_write(owner, store, product, browser_boundary, monkeypatch):
    body = {'kind': 'product_create', 'store_ids': [store['id']], 'product_ids': [product['id']]}
    owner.post('/api/jobs', json=body)
    update = {'title': '修改资料后需要重新预检', 'skus': product['skus'], 'version': product['version']}
    assert owner.put('/api/products/' + product['id'], json=update).status_code == 200
    monkeypatch.setattr(jobs, 'CommerceBrowser', lambda *args: pytest.fail('stale form must not launch browser'))
    result = claim_and_process()
    assert result['status'] == 'blocked'
    assert '变化' in json.loads(result['result'])['message']


def test_changed_browser_rules_are_blocked_before_write(owner, store, product, browser_boundary, monkeypatch):
    body = {'kind': 'product_create', 'store_ids': [store['id']], 'product_ids': [product['id']]}
    owner.post('/api/jobs', json=body)
    assert owner.put('/api/stores/' + store['id'], json={
        'name': store['name'], 'platform': store['platform'],
        'templates': {'commerce_browser': {'workflows': {'product_create': {'url': 'changed'}}}},
    }).status_code == 200
    monkeypatch.setattr(jobs, 'CommerceBrowser', lambda *args: pytest.fail('changed rules must not launch browser'))
    result = claim_and_process()
    assert result['status'] == 'blocked'
    assert '规则已变化' in json.loads(result['result'])['message']


def test_mapping_edited_during_browser_submission_is_not_overwritten(owner, store, product, browser_boundary, monkeypatch):
    class Browser:
        def __init__(self, current):
            pass

        def mutate_product(self, action, payload):
            # Simulate a user correcting the mapping while the browser is working.
            assert owner.put(f"/api/products/{product['id']}/listings/{store['id']}", json={'external_id': 'user-confirmed'}).status_code == 200
            return {'external_id': 'browser-created'}

    monkeypatch.setattr(jobs, 'CommerceBrowser', Browser)
    owner.post('/api/jobs', json={'kind': 'product_create', 'store_ids': [store['id']], 'product_ids': [product['id']]})
    assert claim_and_process()['status'] == 'uncertain'
    assert db.one('SELECT external_id FROM listings')['external_id'] == 'user-confirmed'


def prepare_inventory(owner, product, store):
    code = product['skus'][0]['code']
    assert owner.put(f"/api/products/{product['id']}/listings/{store['id']}", json={'external_id': '123', 'sku_map': {code: '456'}}).status_code == 200
    assert owner.put(f"/api/products/{product['id']}/allocations", json={'version': product['version'], 'quantities': {code: {store['id']: 4}}}).status_code == 200
    return db.get_product(product['id']), inventory.listing(db.one('SELECT * FROM listings'))


def test_inventory_does_not_release_budget_without_all_sku_readbacks(owner, store, product, browser_boundary, monkeypatch):
    from app import commerce_browser

    current, mapped = prepare_inventory(owner, product, store)
    calls = []

    class Browser:
        def __init__(self, current_store):
            pass

        def sync_inventory(self, product, listing, targets):
            # Holding stock is committed before any browser interaction.
            held = db.one('SELECT * FROM allocations')
            assert held['remote_quantity'] == 4 and held['uncertain'] == 1
            calls.append(targets)
            return {'confirmed': True, 'sku_count': 0}

    monkeypatch.setattr(commerce_browser, 'CommerceBrowser', Browser)
    with pytest.raises(Uncertain):
        inventory.sync_to_store(db.get_store(store['id'], True), current, mapped, True)
    held = db.one('SELECT * FROM allocations')
    assert held['remote_quantity'] == 4 and held['uncertain'] == 1
    with pytest.raises(Blocked):
        inventory.allocate(product['id'], current['version'], {product['skus'][0]['code']: {store['id']: 0}})
    assert len(calls) == 1


def test_inventory_acknowledges_confirmed_absolute_budget(owner, store, product, browser_boundary, monkeypatch):
    from app import commerce_browser

    current, mapped = prepare_inventory(owner, product, store)

    class Browser:
        def __init__(self, current_store):
            pass

        def sync_inventory(self, product, listing, targets):
            assert [s['stock'] for s in targets] == [4]
            return {'confirmed': True, 'sku_count': 1}

    monkeypatch.setattr(commerce_browser, 'CommerceBrowser', Browser)
    result = inventory.sync_to_store(db.get_store(store['id'], True), current, mapped, True)
    assert result['sku_count'] == 1
    held = db.one('SELECT * FROM allocations')
    assert held['remote_quantity'] == 4 and held['uncertain'] == 0


def test_browser_orders_apply_stock_once_and_advance_cursor_only_on_completion(owner, store, product, browser_boundary, monkeypatch):
    current, mapped = prepare_inventory(owner, product, store)
    remote = {'external_id': 'order-browser-1', 'status': '待发货', 'inventory_state': 'sold', 'remote_updated': 100,
              'created': db.now(), 'items': [{'product_id': '123', 'sku': '456', 'quantity': 2}]}

    class Browser:
        fail = True

        def __init__(self, current_store):
            pass

        def orders(self, start, end):
            yield remote
            if self.fail:
                raise Blocked('第二页登录失效')

    monkeypatch.setattr(jobs, 'CommerceBrowser', Browser)
    body = {'kind': 'order_sync', 'store_ids': [store['id']]}
    owner.post('/api/jobs', json=body)
    assert claim_and_process()['status'] == 'blocked'
    assert db.one('SELECT source FROM orders')['source'] == 'browser'
    assert db.get_product(product['id'])['skus'][0]['stock'] == 8
    assert db.one('SELECT * FROM settings WHERE key=?', ('orders_cursor_' + store['id'],)) is None
    Browser.fail = False
    owner.post('/api/jobs', json=body)
    assert claim_and_process()['status'] == 'succeeded'
    assert db.get_product(product['id'])['skus'][0]['stock'] == 8
    assert db.one('SELECT * FROM settings WHERE key=?', ('orders_cursor_' + store['id'],)) is not None
