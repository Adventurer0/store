import contextlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

DATA = Path(os.environ.get("APP_DATA", "./data")).resolve()
DB = DATA / "shopdock.sqlite3"


def now():
    return int(time.time())


def uid():
    return uuid.uuid4().hex


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


@contextlib.contextmanager
def connection():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    os.umask(0o077)
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    DATA.chmod(0o700)
    for folder in ("media", "logs", "mpau/cookies", "browser/commerce"):
        (DATA / folder).mkdir(parents=True, exist_ok=True, mode=0o700)
    with connection() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, password TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS stores (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL,
          api_enabled INTEGER NOT NULL DEFAULT 0, video_enabled INTEGER NOT NULL DEFAULT 0,
          credentials TEXT NOT NULL, templates TEXT NOT NULL DEFAULT '{}',
          video_status TEXT NOT NULL DEFAULT 'unverified', created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS products (
          id TEXT PRIMARY KEY, data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
          created INTEGER NOT NULL, updated INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS listings (
          product_id TEXT NOT NULL REFERENCES products(id), store_id TEXT NOT NULL REFERENCES stores(id),
          external_id TEXT NOT NULL DEFAULT '', product_link TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'mapped', updated INTEGER NOT NULL,
          PRIMARY KEY(product_id,store_id));
        CREATE TABLE IF NOT EXISTS media (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, mime TEXT NOT NULL,
          path TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY, store_id TEXT NOT NULL REFERENCES stores(id),
          external_id TEXT NOT NULL, data TEXT NOT NULL, source TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '', updated INTEGER NOT NULL,
          UNIQUE(store_id,external_id));
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, kind TEXT NOT NULL,
          store_id TEXT NOT NULL REFERENCES stores(id), entity_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL, scheduled INTEGER NOT NULL, payload TEXT NOT NULL,
          result TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0,
          dedupe_key TEXT UNIQUE NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(status,scheduled);
        CREATE INDEX IF NOT EXISTS orders_updated ON orders(updated);
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER NOT NULL,
          action TEXT NOT NULL, detail TEXT NOT NULL);
        INSERT OR IGNORE INTO settings VALUES ('schema_version','1');
        ''')
        # Additive migration: safe for an existing v0.1 data volume.
        columns = {r[1] for r in c.execute('PRAGMA table_info(listings)')}
        for name, ddl in [('publish_config', "TEXT NOT NULL DEFAULT '{}'"), ('sku_map', "TEXT NOT NULL DEFAULT '{}'"), ('owned_product', 'INTEGER NOT NULL DEFAULT 0')]:
            if name not in columns:
                c.execute(f'ALTER TABLE listings ADD COLUMN {name} {ddl}')
        columns = {r[1] for r in c.execute('PRAGMA table_info(stores)')}
        for name, ddl in [('commerce_enabled', 'INTEGER NOT NULL DEFAULT 1'),
                          ('commerce_status', "TEXT NOT NULL DEFAULT 'unverified'")]:
            if name not in columns:
                c.execute(f'ALTER TABLE stores ADD COLUMN {name} {ddl}')
        # Old API credentials remain encrypted for rollback; they cannot enable
        # commerce operations. A video login never verifies a merchant session.
        c.execute('UPDATE stores SET api_enabled=0 WHERE api_enabled!=0')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS allocations (
          product_id TEXT NOT NULL REFERENCES products(id), sku_code TEXT NOT NULL,
          store_id TEXT NOT NULL REFERENCES stores(id), quantity INTEGER NOT NULL DEFAULT 0,
          remote_quantity INTEGER NOT NULL DEFAULT 0, uncertain INTEGER NOT NULL DEFAULT 0,
          revision INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(product_id,sku_code,store_id));
        CREATE TABLE IF NOT EXISTS inventory_effects (
          store_id TEXT NOT NULL, order_id TEXT NOT NULL, product_id TEXT NOT NULL,
          sku_code TEXT NOT NULL, quantity INTEGER NOT NULL, updated INTEGER NOT NULL,
          PRIMARY KEY(store_id,order_id,product_id,sku_code));
        CREATE TABLE IF NOT EXISTS inventory_ledger (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER NOT NULL, product_id TEXT NOT NULL,
          sku_code TEXT NOT NULL, delta INTEGER NOT NULL, reason TEXT NOT NULL,
          store_id TEXT NOT NULL DEFAULT '', order_id TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS inventory_alerts (
          store_id TEXT NOT NULL, order_id TEXT NOT NULL, message TEXT NOT NULL, updated INTEGER NOT NULL,
          PRIMARY KEY(store_id,order_id));
        CREATE TABLE IF NOT EXISTS image_cache (
          store_id TEXT NOT NULL, media_id TEXT NOT NULL, url TEXT NOT NULL,
          created INTEGER NOT NULL, PRIMARY KEY(store_id,media_id));
        CREATE TABLE IF NOT EXISTS media_shares (
          token TEXT PRIMARY KEY, media_id TEXT NOT NULL REFERENCES media(id), expires INTEGER NOT NULL);
        UPDATE settings SET value='3' WHERE key='schema_version';
        ''')


def one(sql, args=()):
    with connection() as c:
        row = c.execute(sql, args).fetchone()
        return dict(row) if row else None


def all_rows(sql, args=()):
    with connection() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def audit(action, detail):
    with connection() as c:
        c.execute("INSERT INTO audit(created,action,detail) VALUES(?,?,?)", (now(), action, dumps(detail)))


def get_product(product_id):
    row = one("SELECT * FROM products WHERE id=?", (product_id,))
    if row:
        row.update(json.loads(row.pop("data")))
    return row


def get_store(store_id, secrets=False):
    row = one("SELECT * FROM stores WHERE id=?", (store_id,))
    if row:
        row["templates"] = json.loads(row["templates"])
        if secrets:
            from .security import decrypt
            row["credentials"] = decrypt(row["credentials"])
        else:
            row.pop("credentials")
    return row


def upsert_order(store_id, data, source="browser"):
    external_id = str(data["external_id"])
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        # CSV must never overwrite an authoritative merchant record.
        old = c.execute("SELECT source,data FROM orders WHERE store_id=? AND external_id=?", (store_id, external_id)).fetchone()
        if old and old["source"] in ("api", "browser") and source == "csv":
            return False
        if old and source in ('api', 'browser') and data.get('remote_updated', 0) < json.loads(old['data']).get('remote_updated', 0):
            return False
        c.execute('''INSERT INTO orders(id,store_id,external_id,data,source,updated) VALUES(?,?,?,?,?,?)
        ON CONFLICT(store_id,external_id) DO UPDATE SET data=excluded.data,source=excluded.source,updated=excluded.updated''',
                  (uid(), store_id, external_id, dumps(data), source, now()))
        if source in ('api', 'browser'):
            from .inventory import apply_order
            st = c.execute('SELECT created,templates FROM stores WHERE id=?', (store_id,)).fetchone()
            config = json.loads(st['templates'])
            start = int(config.get('inventory_start') or st['created'])
            if not data.get('created') or int(data['created']) >= start:
                if config.get('inventory_status_map', {}).get(data.get('raw_status')) in ('reserve', 'sold', 'void', 'unknown'):
                    data = {**data, 'inventory_state': config['inventory_status_map'][data['raw_status']]}
                apply_order(c, store_id, data)
    return True
