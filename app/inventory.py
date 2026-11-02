"""Transactional stock ledger and bounded per-store sellable budgets.

Budgets are NOT copies of warehouse stock. A reduced budget remains held until
the remote reduction is acknowledged. Orders consume stock exactly once; returns
require an explicit physical inbound adjustment.
"""
import json
from . import db
from .connectors import Blocked


def listing(row):
    value = dict(row or {})
    for k in ('publish_config', 'sku_map'):
        if isinstance(value.get(k), str):
            value[k] = json.loads(value[k])
        value.setdefault(k, {})
    return value


def held(c, pid, code):
    return c.execute('SELECT coalesce(sum(max(quantity,remote_quantity)),0) FROM allocations WHERE product_id=? AND sku_code=?', (pid, code)).fetchone()[0]


def validate_stock(c, pid, data):
    codes = {s['code'] for s in data['skus']}
    used = {r[0] for r in c.execute('SELECT sku_code FROM allocations WHERE product_id=? UNION SELECT sku_code FROM inventory_effects WHERE product_id=?', (pid, pid))}
    if used - codes:
        raise Blocked('SKU 已有关联库存或订单，不能删除 / 改编码；请保留该 SKU')
    for sku in data['skus']:
        if sku['stock'] < held(c, pid, sku['code']):
            raise Blocked('仓库库存不能低于已占用的店铺额度；先降低并同步店铺额度')


def allocate(pid, version, quantities):
    with db.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
        if not row or row['version'] != version:
            raise Blocked('商品已变更，请刷新库存后再分配')
        product = json.loads(row['data'])
        codes = {s['code'] for s in product['skus']}
        if not set(quantities) <= codes:
            raise Blocked('分配中包含不存在的 SKU')
        for code, stores in quantities.items():
            for sid, qty in stores.items():
                if not c.execute('SELECT id FROM stores WHERE id=?', (sid,)).fetchone():
                    raise Blocked('目标店铺不存在')
                old = c.execute('SELECT * FROM allocations WHERE product_id=? AND sku_code=? AND store_id=?', (pid, code, sid)).fetchone()
                if old and old['uncertain']:
                    raise Blocked('该店铺库存同步结果未知；先核对远端，再同步原额度')
                c.execute('''INSERT INTO allocations(product_id,sku_code,store_id,quantity) VALUES(?,?,?,?)
                ON CONFLICT(product_id,sku_code,store_id) DO UPDATE SET quantity=excluded.quantity,revision=revision+1''', (pid, code, sid, qty))
        validate_stock(c, pid, product)
        c.execute('UPDATE products SET version=version+1,updated=? WHERE id=?', (db.now(), pid))
    db.audit('分配店铺库存', {'product_id': pid})


def adjust(pid, body):
    with db.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
        if not row or row['version'] != body.version:
            raise Blocked('库存已变化，请刷新后重试')
        data = json.loads(row['data'])
        sku = next((s for s in data['skus'] if s['code'] == body.sku_code), None)
        if sku is None:
            raise Blocked('SKU 不存在')
        sku['stock'] += body.delta
        if not 0 <= sku['stock'] <= 100_000_000:
            raise Blocked('调整后库存超出范围')
        validate_stock(c, pid, data)
        c.execute('UPDATE products SET data=?,version=version+1,updated=? WHERE id=?', (db.dumps(data), db.now(), pid))
        c.execute('INSERT INTO inventory_ledger(created,product_id,sku_code,delta,reason) VALUES(?,?,?,?,?)', (db.now(), pid, body.sku_code, body.delta, body.reason))


def apply_order(c, sid, order):
    oid = order['external_id']
    def alert(message):
        c.execute('INSERT INTO inventory_alerts VALUES(?,?,?,?) ON CONFLICT(store_id,order_id) DO UPDATE SET message=excluded.message,updated=excluded.updated', (sid, oid, message, db.now()))
    if order.get('inventory_state') in ('void', 'unknown', None):
        # Never return physical stock just because an order is closed/refunded.
        old = c.execute('SELECT 1 FROM inventory_effects WHERE store_id=? AND order_id=?', (sid, oid)).fetchone()
        if old or order.get('inventory_state') != 'void':
            alert('订单状态未识别或已取消 / 售后；库存未自动回补，请核对实物后入库')
        return
    items = order.get('items', [])
    if not items:
        alert('订单缺少 SKU 明细，尚未扣库')
        return
    mapped = {}
    for item in items:
        quantity = item.get('quantity')
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 0 < quantity <= 100_000_000:
            alert('订单数量无效，尚未扣库'); return
        found = []
        for row in c.execute('SELECT * FROM listings WHERE store_id=?', (sid,)):
            row = listing(row)
            # Some platforms reuse sku_id across different products.
            if item.get('product_id') and str(item['product_id']) != row['external_id']:
                continue
            for code, remote in row['sku_map'].items():
                if item.get('sku') and str(item['sku']) == str(remote):
                    found.append((row['product_id'], code))
        if len(found) != 1:
            alert('订单 SKU 未唯一关联本地商品；补齐关联后点击重新核算'); return
        mapped[found[0]] = mapped.get(found[0], 0) + quantity
    errors = []
    for (pid, code), qty in mapped.items():
        old = c.execute('SELECT quantity FROM inventory_effects WHERE store_id=? AND order_id=? AND product_id=? AND sku_code=?', (sid, oid, pid, code)).fetchone()
        applied = old['quantity'] if old else 0
        if qty < applied:
            errors.append('订单数量减少，未自动回补；请核对退货实物')
        delta = max(0, qty - applied)
        if not delta:
            continue
        row = c.execute('SELECT data FROM products WHERE id=?', (pid,)).fetchone()
        data = json.loads(row['data']) if row else {}
        sku = next((s for s in data.get('skus', []) if s['code'] == code), None)
        if not sku:
            errors.append('关联的本地 SKU 不存在'); continue
        # Negative balances are visible exceptions, never silently clamped.
        sku['stock'] -= delta
        if sku['stock'] < 0:
            errors.append('发生库存不足，仓库余额为负，请立即核对')
        budget = c.execute('SELECT * FROM allocations WHERE product_id=? AND sku_code=? AND store_id=?', (pid, code, sid)).fetchone()
        if not budget or budget['quantity'] < delta:
            errors.append('订单超过该店分配额度，请核对平台库存')
        c.execute('UPDATE allocations SET quantity=max(0,quantity-?),remote_quantity=max(0,remote_quantity-?),revision=revision+1 WHERE product_id=? AND sku_code=? AND store_id=?', (delta, delta, pid, code, sid))
        c.execute('UPDATE products SET data=?,version=version+1,updated=? WHERE id=?', (db.dumps(data), db.now(), pid))
        c.execute('INSERT INTO inventory_effects VALUES(?,?,?,?,?,?) ON CONFLICT(store_id,order_id,product_id,sku_code) DO UPDATE SET quantity=excluded.quantity,updated=excluded.updated', (sid, oid, pid, code, max(qty, applied), db.now()))
        c.execute('INSERT INTO inventory_ledger(created,product_id,sku_code,delta,reason,store_id,order_id) VALUES(?,?,?,?,?,?,?)', (db.now(), pid, code, -delta, '订单占用 / 销售扣库（不在发货时重复扣）', sid, oid))
    if errors:
        alert('；'.join(sorted(set(errors))))
    else:
        c.execute('DELETE FROM inventory_alerts WHERE store_id=? AND order_id=?', (sid, oid))


def snapshot():
    result = []
    with db.connection() as c:
        for row in c.execute('SELECT * FROM products ORDER BY updated DESC LIMIT 1000').fetchall():
            p = json.loads(row['data'])
            for sku in p['skus']:
                budgets = [dict(r) for r in c.execute('SELECT * FROM allocations WHERE product_id=? AND sku_code=?', (row['id'], sku['code']))]
                reserved = sum(max(b['quantity'], b['remote_quantity']) for b in budgets)
                result.append({'product_id': row['id'], 'title': p['title'], 'version': row['version'], **sku, 'held': reserved, 'unallocated': sku['stock'] - reserved, 'allocations': budgets})
    return {'rows': result, 'alerts': db.all_rows('SELECT * FROM inventory_alerts ORDER BY updated DESC LIMIT 200'), 'ledger': db.all_rows('SELECT * FROM inventory_ledger ORDER BY id DESC LIMIT 100')}


def sku_targets(product, sid):
    rows = db.all_rows('SELECT * FROM allocations WHERE product_id=? AND store_id=?', (product['id'], sid))
    by_code = {r['sku_code']: r for r in rows}
    if any(s['code'] not in by_code for s in product['skus']):
        raise Blocked('请先在统一库存中为该店铺分配每个 SKU 的可售额度（可为 0）')
    if any(s['stock'] < 0 for s in product['skus']):
        raise Blocked('仓库库存不足，先处理负库存异常')
    return [{**s, 'stock': by_code[s['code']]['quantity'], 'revision': by_code[s['code']]['revision']} for s in product['skus']]


def sync_to_store(store, product, mapped, sales_paused):
    from .commerce_browser import CommerceBrowser, check_session, validate_workflow
    from .connectors import Uncertain
    if not sales_paused:
        raise Blocked('全量库存校准前，请暂停目标商品销售并核对待付款占用，再勾选确认')
    mapped = listing(mapped)
    if not mapped.get('external_id'):
        raise Blocked('尚未关联平台商品 ID')
    targets = sku_targets(product, store['id'])
    if any(s['code'] not in mapped['sku_map'] for s in targets):
        raise Blocked('请补齐平台 SKU ID 后再同步库存')
    check_session(store)
    validate_workflow(store, 'inventory_sync')
    browser = CommerceBrowser(store)
    # Each hold is durable BEFORE the external write, including crash recovery.
    def hold(skus):
        with db.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            for s in skus:
                row = c.execute('SELECT * FROM allocations WHERE product_id=? AND sku_code=? AND store_id=?', (product['id'], s['code'], store['id'])).fetchone()
                if not row or row['revision'] != s['revision']:
                    raise Blocked('库存额度已经变化，请重新预检')
                c.execute('UPDATE allocations SET remote_quantity=max(remote_quantity,?),uncertain=1 WHERE product_id=? AND sku_code=? AND store_id=?', (s['stock'], product['id'], s['code'], store['id']))
    def ack(skus):
        with db.connection() as c:
            for s in skus:
                c.execute('UPDATE allocations SET remote_quantity=?,uncertain=0 WHERE product_id=? AND sku_code=? AND store_id=? AND revision=?', (s['stock'], product['id'], s['code'], store['id'], s['revision']))
    hold(targets)
    result = browser.sync_inventory(product, mapped, targets)
    if not isinstance(result, dict) or result.get('confirmed') is not True or result.get('sku_count') != len(targets):
        raise Uncertain('网页库存提交后未逐一确认所有 SKU 的目标数量，请核对后台，库存占用继续保留')
    ack(targets)
    return {'message': result.get('message') or '网页已逐一读回并确认店铺 SKU 库存；请同时核对锁定库存', 'sku_count': len(targets)}
