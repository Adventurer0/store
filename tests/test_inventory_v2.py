import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from app import db, inventory, jobs
from app.connectors import Blocked, Uncertain
from app.models import Batch


def make_store(owner, platform='wechat'):
    return owner.post('/api/stores', json={'name': platform, 'platform': platform, 'api_enabled': True, 'credentials': {'access_token': 'token', 'app_key': 'key', 'sign_secret': 'sign', 'app_secret': 'secret'}}).json()


def map_product(owner, product, store, external='123', remote='456'):
    r = owner.put(f"/api/products/{product['id']}/listings/{store['id']}", json={'external_id': external, 'sku_map': {product['skus'][0]['code']: remote}})
    assert r.status_code == 200, r.text


def allocate(owner, product, quantities):
    pid = product['id']
    return owner.put(f'/api/products/{pid}/allocations', json={'version': db.get_product(pid)['version'], 'quantities': {product['skus'][0]['code']: quantities}})


def order(quantity=2, oid='order1', status='sold', updated=100):
    return {'external_id': oid, 'inventory_state': status, 'remote_updated': updated, 'status': '待发货', 'amount_cents': 100, 'items': [{'product_id': '123', 'sku': '456', 'title': '商品', 'quantity': quantity}]}


def test_partition_limits_rollback_and_old_version(owner, product):
    a,b = make_store(owner),make_store(owner,'kuaishou')
    assert allocate(owner,product,{a['id']:6,b['id']:4}).status_code == 200
    assert allocate(owner,product,{a['id']:6,b['id']:5}).status_code == 409
    rows = owner.get('/api/inventory').json()['rows']
    assert rows[0]['held'] == 10 and rows[0]['unallocated'] == 0
    assert owner.put(f"/api/products/{product['id']}/allocations", json={'version':1,'quantities':{}}).status_code == 409
    assert allocate(owner,product,{a['id']:-1}).status_code == 422


def test_concurrent_duplicate_order_consumes_exactly_once(owner, product):
    st=make_store(owner);map_product(owner,product,st)
    assert allocate(owner,product,{st['id']:10}).status_code==200
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda _: db.upsert_order(st['id'],order()),range(5)))
    assert db.get_product(product['id'])['skus'][0]['stock']==8
    assert db.one('SELECT count(*) n FROM inventory_ledger')['n']==1
    assert db.one('SELECT quantity FROM allocations')['quantity']==8
    # Shipped / completed updates and reconciling do not deduct again.
    db.upsert_order(st['id'],order(updated=101))
    assert owner.post('/api/inventory/reconcile',json={}).status_code==200
    assert db.get_product(product['id'])['skus'][0]['stock']==8


def test_cancel_refund_never_replenishes_physical_stock(owner, product):
    st=make_store(owner);map_product(owner,product,st)
    allocate(owner,product,{st['id']:10})
    db.upsert_order(st['id'],order(status='reserve'))
    db.upsert_order(st['id'],order(status='void',updated=101))
    assert db.get_product(product['id'])['skus'][0]['stock']==8
    assert '实物' in db.one('SELECT message FROM inventory_alerts')['message']
    p=db.get_product(product['id'])
    r=owner.post(f"/api/products/{p['id']}/inventory-adjustment",json={'version':p['version'],'sku_code':p['skus'][0]['code'],'delta':2,'reason':'退货已验收入库'})
    assert r.status_code==200
    assert db.get_product(product['id'])['skus'][0]['stock']==10


def test_missing_mapping_can_be_reconciled_no_silent_loss(owner, product):
    st=make_store(owner)
    db.upsert_order(st['id'],order())
    assert db.get_product(product['id'])['skus'][0]['stock']==10
    assert owner.get('/api/inventory').json()['alerts']
    map_product(owner,product,st)
    allocate(owner,product,{st['id']:10})
    owner.post('/api/inventory/reconcile',json={})
    assert db.get_product(product['id'])['skus'][0]['stock']==8
    assert not owner.get('/api/inventory').json()['alerts']


def test_order_update_time_fences_stale_snapshots_and_qty_reduction(owner, product):
    st=make_store(owner);map_product(owner,product,st);allocate(owner,product,{st['id']:10})
    db.upsert_order(st['id'],order(quantity=3,updated=200))
    db.upsert_order(st['id'],order(quantity=7,updated=100))
    assert db.get_product(product['id'])['skus'][0]['stock']==7
    db.upsert_order(st['id'],order(quantity=2,updated=201))
    assert db.get_product(product['id'])['skus'][0]['stock']==7
    assert '数量减少' in db.one('SELECT message FROM inventory_alerts')['message']


def test_mixed_unmapped_order_is_atomic_before_deduction(owner, product):
    st=make_store(owner);map_product(owner,product,st)
    data=order();data['items'].append({'product_id':'unknown','sku':'unknown','quantity':1})
    db.upsert_order(st['id'],data)
    assert db.get_product(product['id'])['skus'][0]['stock']==10
    assert not db.one('SELECT * FROM inventory_ledger')


def test_oversell_is_visible_negative_balance(owner, product):
    st=make_store(owner);map_product(owner,product,st)
    db.upsert_order(st['id'],order(quantity=11))
    assert db.get_product(product['id'])['skus'][0]['stock']==-1
    assert '不足' in db.one('SELECT message FROM inventory_alerts')['message']


def test_decrease_must_be_acknowledged_before_transfer(owner, product):
    a,b=make_store(owner),make_store(owner,'douyin')
    allocate(owner,product,{a['id']:10})
    with db.connection() as c:
        c.execute('UPDATE allocations SET remote_quantity=10')
    assert allocate(owner,product,{a['id']:0,b['id']:10}).status_code==409
    assert allocate(owner,product,{a['id']:0}).status_code==200
    assert owner.get('/api/inventory').json()['rows'][0]['unallocated']==0


def test_unknown_remote_stock_holds_budget_and_runtime_snapshot(owner, product, monkeypatch, commerce_setup):
    st=make_store(owner);map_product(owner,product,st);allocate(owner,product,{st['id']:8})
    commerce_setup(st)
    body={'kind':'inventory_sync','store_ids':[st['id']],'product_ids':[product['id']],'sales_paused':True}
    assert owner.post('/api/jobs/preflight',json={**body,'sales_paused':False}).json()['ready']==0
    result=owner.post('/api/jobs',json=body).json()
    def fail(*a,**k):raise Uncertain('timeout')
    monkeypatch.setattr('app.commerce_browser.CommerceBrowser.sync_inventory',fail)
    worker=jobs.Worker();worker.process(worker.claim())
    assert db.one('SELECT status FROM jobs WHERE id=?',(result['jobs'][0]['id'],))['status']=='uncertain'
    assert db.one('SELECT remote_quantity FROM allocations')['remote_quantity']==8
    assert allocate(owner,product,{st['id']:0}).status_code==409
    # Full absolute write of the same target after verification repairs the hold.
    monkeypatch.setattr('app.commerce_browser.CommerceBrowser.sync_inventory',lambda *a,**k:{'confirmed':True,'sku_count':1})
    inventory.sync_to_store(db.get_store(st['id'],True),db.get_product(product['id']),inventory.listing(db.one('SELECT * FROM listings')),True)
    assert db.one('SELECT uncertain FROM allocations')['uncertain']==0


@pytest.mark.parametrize('platform',['douyin','wechat','kuaishou','tmall','jd','pdd'])
def test_inventory_browser_receives_only_store_allocation(owner, product, monkeypatch, platform,commerce_setup):
    st=make_store(owner,platform);map_product(owner,product,st);allocate(owner,product,{st['id']:4})
    commerce_setup(st)
    calls=[]
    def sync(self,p,m,targets):
        calls.append((m,targets))
        return {'confirmed':True,'sku_count':len(targets)}
    monkeypatch.setattr('app.commerce_browser.CommerceBrowser.sync_inventory',sync)
    inventory.sync_to_store(db.get_store(st['id'],True),db.get_product(product['id']),inventory.listing(db.one('SELECT * FROM listings')),True)
    assert calls[0][0]['external_id']=='123'
    assert calls[0][1][0]['stock']==4
    assert db.one('SELECT remote_quantity FROM allocations')['remote_quantity']==4


def test_csv_cannot_overwrite_authoritative_orders_or_break_allocations(owner, product):
    st=make_store(owner);map_product(owner,product,st);allocate(owner,product,{st['id']:8})
    db.upsert_order(st['id'],order())
    assert not db.upsert_order(st['id'],order(oid='order1',quantity=8),source='csv')
    assert db.get_product(product['id'])['skus'][0]['stock']==8
    payload={k:v for k,v in db.get_product(product['id']).items() if k not in ('id','created','updated')}
    payload['skus'][0]['stock']=0
    assert owner.put('/api/products/'+product['id'],json=payload).status_code==409
    assert owner.delete('/api/products/'+product['id']).status_code==409


def test_cannot_rebind_historical_sku_or_duplicate_remote_product(owner, product):
    st=make_store(owner);map_product(owner,product,st);db.upsert_order(st['id'],order())
    r=owner.put(f"/api/products/{product['id']}/listings/{st['id']}",json={'external_id':'123','sku_map':{product['skus'][0]['code']:'999'}})
    assert r.status_code==409
    p2=owner.post('/api/products',json={'title':'second','skus':product['skus']}).json()
    r=owner.put(f"/api/products/{p2['id']}/listings/{st['id']}",json={'external_id':'123'})
    assert r.status_code==409


def test_polling_enqueues_and_does_not_duplicate_inflight(owner,commerce_setup):
    st=make_store(owner)
    owner.put('/api/stores/'+st['id'],json={'name':'auto','platform':'wechat','api_enabled':True,'templates':{'auto_order_sync':True}})
    commerce_setup(st)
    worker=jobs.Worker();worker.schedule_order_sync();worker.schedule_order_sync()
    assert db.one('SELECT count(*) n FROM jobs')['n']==1
    job=db.one('SELECT * FROM jobs')
    assert job['kind']=='order_sync'
    assert 0 < json.loads(job['payload'])['until']-json.loads(job['payload'])['since'] <=86400


def test_additive_migration_keeps_existing_product_and_listing(owner,product):
    st=make_store(owner);map_product(owner,product,st)
    with db.connection() as c:
        for col in ('publish_config','sku_map','owned_product'):
            c.execute('ALTER TABLE listings DROP COLUMN '+col)
    db.init();db.init()
    assert db.get_product(product['id'])['title']==product['title']
    row=db.one('SELECT * FROM listings')
    assert row['external_id']=='123' and row['sku_map']=='{}'
    assert db.one("SELECT value FROM settings WHERE key='schema_version'")['value']=='3'
