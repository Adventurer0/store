"""Subprocess boundary for Cookie-backed merchant webpage automation.

The web worker does not receive API credentials. Playwright runs outside FastAPI's
event loop, optionally in the full image's existing MPAU Python environment.
"""
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from . import db
from .connectors import Blocked, Rejected, Uncertain
from .commerce_profiles import PROFILES, trusted_url


ACTIONS = {'product_create', 'product_update', 'product_list', 'product_delist', 'orders', 'fetch_skus', 'inventory_sync'}


def run_worker(command, *, input, timeout, encoding='utf-8', **unused):
    """Own and reap this worker's process tree, including its Chrome children."""
    windows = os.name == 'nt'
    flags = (getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)) if windows else 0
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding=encoding, creationflags=flags, start_new_session=not windows)
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except BaseException:
        # Only this freshly created PID/tree is targeted, never an existing browser.
        if windows:
            try:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.communicate(timeout=10)
        raise


@contextlib.contextmanager
def profile_lock(store_id):
    # The lock lives outside the profile so logout cannot remove an active lock.
    try:
        import fcntl
    except ImportError:
        from . import win_fcntl as fcntl
    profile = profile_path(store_id, create=True)
    lock_path = profile.parent / (store_id + '.lock')
    if lock_path.is_symlink():
        raise Blocked('商家浏览器锁文件不能是符号链接')
    with lock_path.open('a+b') as handle:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Blocked('此店铺的电商浏览器正在处理其他操作，请稍后再试') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def browser_python():
    configured = os.environ.get('COMMERCE_PYTHON')
    if configured:
        return configured if Path(configured).is_file() else shutil.which(configured)
    if importlib.util.find_spec('playwright') is not None:
        return sys.executable
    candidate = Path('/opt/mpau-venv/bin/python')
    return str(candidate) if candidate.is_file() else None


def available():
    return bool(browser_python())


def profile_path(store_id, create=False):
    if not isinstance(store_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', store_id):
        raise Blocked('无效的店铺标识')
    root = db.DATA.resolve()
    path = root / 'browser' / 'commerce' / store_id
    for ancestor in (path.parent.parent, path.parent, path):
        if ancestor.is_symlink():
            raise Blocked('商家浏览器目录不能是符号链接')
    if root not in path.resolve().parents:
        raise Blocked('商家浏览器目录超出数据范围')
    if create:
        for ancestor in (path.parent.parent, path.parent, path):
            ancestor.mkdir(mode=0o700, parents=True, exist_ok=True)
            ancestor.chmod(0o700)
    return path


def check_session(store):
    if not store.get('commerce_enabled', True):
        raise Blocked('此店铺未启用电商网页管理')
    if store.get('commerce_status') != 'valid' or not (profile_path(store['id']) / 'session.json').is_file():
        raise Blocked('请先完成此店铺的电商后台网页登录和登录检查；视频账号登录不能代替电商登录')
    if not available():
        raise Blocked('商家浏览器未安装；请使用 Docker 完整镜像或安装 Playwright 和 Chrome')


def _config(store):
    config = store.get('templates', {}).get('commerce_browser', {})
    if not isinstance(config, dict):
        raise Blocked('commerce_browser 配置必须是 JSON 对象')
    return config


def validate_workflow(store, action):
    if action not in ACTIONS:
        raise Blocked('不支持的网页管理操作')
    config = _config(store)
    workflows = config.get('workflows', {})
    if not isinstance(workflows, dict):
        raise Blocked('commerce_browser.workflows 必须是 JSON 对象')
    workflow = workflows.get(action)
    if not isinstance(workflow, dict):
        raise Blocked(f'请配置电商网页工作流 commerce_browser.workflows.{action}；平台页面需要适配后才能执行')
    if '__REQUIRED__' in db.dumps(workflow):
        raise Blocked('网页工作流仍含 __REQUIRED__ 占位字段，请先按当前商家页面补齐')
    try:
        trusted_url(store['platform'], workflow.get('url', ''))
    except (ValueError, KeyError, TypeError):
        raise Blocked('电商网页工作流地址不属于当前平台') from None
    steps = workflow.get('steps', [])
    result = workflow.get('result', {})
    if not isinstance(steps, list) or not isinstance(result, dict):
        raise Blocked('网页工作流 steps / result 格式错误')
    if not isinstance(result.get('fields', {}), dict):
        raise Blocked('网页工作流 result.fields 必须是 JSON 对象')
    stock_writes = []
    def inspect(items, depth=0, in_skus=False):
        if depth > 3 or len(items) > 100:
            raise Blocked('网页工作流过长或嵌套过深')
        commits = 0
        for step in items:
            if not isinstance(step, dict) or step.get('op') not in ('click', 'fill', 'select', 'check', 'upload', 'wait', 'each_sku'):
                raise Blocked('网页工作流只能使用点击、填表、选择、勾选、上传、等待与 SKU 循环')
            if step['op'] == 'each_sku':
                if not isinstance(step.get('steps'), list):
                    raise Blocked('each_sku 需要 steps 列表')
                commits += inspect(step['steps'], depth + 1, True)
            else:
                if not step.get('target'):
                    raise Blocked('网页工作流缺少控件选择器 target')
                stock_control = step.get('field') == 'stock' or bool(re.search(r'库存|stock|quantity', db.dumps(step['target']), re.I))
                if stock_control and step['op'] in ('fill', 'select', 'check'):
                    if action not in ('product_create', 'inventory_sync'):
                        raise Blocked('商品资料编辑不能写入库存；请通过统一库存校准操作')
                    allowed = ('{{sku.stock}}',) if action == 'inventory_sync' else (0, '0', '{{sku.stock}}')
                    if step['op'] != 'fill' or step.get('value') not in allowed or not in_skus:
                        raise Blocked('库存字段必须在 each_sku 中绑定 {{sku.stock}}；新建时也可填 0')
                    stock_writes.append(step)
                if step.get('commit'):
                    if step['op'] != 'click':
                        raise Blocked('提交标记 commit 只能用于点击操作')
                    commits += 1
        return commits
    commits = inspect(steps)
    if action.startswith('product_') or action == 'inventory_sync':
        identity = config.get('identity')
        if not isinstance(identity, dict) or not isinstance(identity.get('target'), (str, dict)) or not identity.get('target') or not isinstance(identity.get('value'), str) or not identity['value'].strip():
            raise Blocked('写入前请配置 commerce_browser.identity.target 与 value，核对后台当前店铺身份')
        if not commits or not result.get('success'):
            raise Blocked('写入工作流必须配置提交按钮 commit 与成功回执 result.success')
    if action == 'product_create' and not result.get('external_id'):
        raise Blocked('创建商品必须配置网页回执中的商品 ID 读取规则 result.external_id')
    if action in ('product_create', 'inventory_sync') and not stock_writes:
        raise Blocked('创建商品 / 校准库存必须逐 SKU 填入库存；请在 each_sku 的填表步骤标记 field: stock')
    if action in ('product_create', 'orders', 'fetch_skus', 'inventory_sync') and not result.get('rows'):
        raise Blocked('读取 / 校验工作流必须配置 result.rows 及字段读取规则')
    required = {'orders': ('external_id', 'status', 'created', 'remote_updated', 'product_id', 'sku', 'quantity'),
                'product_create': ('code', 'sku_id', 'stock'),
                'fetch_skus': ('code', 'sku_id'), 'inventory_sync': ('sku_id', 'stock')}.get(action, ())
    if any(not result.get('fields', {}).get(k) for k in required):
        raise Blocked('网页表格缺少字段读取规则：' + '、'.join(required))
    return copy.deepcopy(workflow)


def product_payload(store, product, listing, action):
    check_session(store)
    validate_workflow(store, action)
    listing = copy.deepcopy(listing)
    for key in ('publish_config', 'sku_map'):
        if isinstance(listing.get(key), str):
            listing[key] = json.loads(listing[key])
        listing.setdefault(key, {})
    if action == 'product_create' and listing.get('external_id'):
        raise Blocked('此商品已关联平台 ID，请选择更新以避免重复创建')
    if action != 'product_create' and not listing.get('external_id'):
        raise Blocked('请先关联该店铺的平台商品 ID')
    config = copy.deepcopy(store.get('templates', {}).get('publish_defaults', {}))
    if not isinstance(config, dict) or not isinstance(listing['publish_config'], dict):
        raise Blocked('商品与店铺发布配置必须是 JSON 对象')
    config.update(listing['publish_config'])
    safe_product = copy.deepcopy(product)
    # Warehouse stock is never copied into remote product forms.
    for sku in safe_product['skus']:
        sku['price'] = format(sku['price_cents'] / 100, '.2f')
        sku.pop('stock', None)
        if action == 'product_create':
            sku['stock'] = 0
    return {'product': safe_product, 'listing': listing, 'publish_config': config}


class CommerceBrowser:
    def __init__(self, store):
        self.store = store
        self.platform = store['platform']
        if self.platform not in PROFILES:
            raise Blocked('不支持的平台')

    def _status(self, value):
        with db.connection() as c:
            c.execute('UPDATE stores SET commerce_status=? WHERE id=?', (value, self.store['id']))

    def _run(self, action, payload=None):
        interpreter = browser_python()
        if not interpreter:
            raise Blocked('商家浏览器未安装；请使用 Docker 完整镜像或安装 Playwright 和 Chrome')
        path = profile_path(self.store['id'], create=True)
        config = copy.deepcopy(_config(self.store))
        # No credentials or video account identifiers cross this process boundary.
        media = {}
        product = (payload or {}).get('product', {})
        mids = product.get('image_ids', []) + product.get('detail_image_ids', [])
        if product.get('cover_id'):
            mids += [product['cover_id']]
        for mid in set(mids):
            row = db.one("SELECT * FROM media WHERE id=? AND kind='image'", (mid,))
            if not row:
                raise Blocked('商品图片已丢失')
            target = (db.DATA / row['path']).resolve()
            if db.DATA.resolve() not in target.parents or not target.is_file():
                raise Blocked('商品图片路径无效')
            media[mid] = str(target)
        request = {'platform': self.platform, 'profile': str(path), 'action': action,
                   'config': config, 'payload': payload or {}, 'media': media}
        timeout = 660 if action == 'login' else 600
        try:
            completed = run_worker([interpreter, str(Path(__file__).with_name('commerce_worker.py'))],
                                       input=db.dumps(request), text=True, encoding='utf-8',
                                       capture_output=True, timeout=timeout, check=False,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            raise (Uncertain if action in ACTIONS and action not in ('orders', 'fetch_skus') else Blocked)('网页操作超时，请核对商家后台后再决定是否重试') from None
        except OSError:
            raise Blocked('商家浏览器进程无法启动，请检查 Playwright 安装') from None
        try:
            response = json.loads(completed.stdout)
        except (ValueError, TypeError):
            # Browser stderr may contain URLs or tokens and must never reach logs/UI.
            raise (Uncertain if action in ACTIONS and action not in ('orders', 'fetch_skus') else Blocked)('商家浏览器异常退出，请核对后台实际结果') from None
        if not isinstance(response, dict) or completed.returncode != 0:
            raise Uncertain('商家浏览器进程未正常完成，请核对后台实际结果')
        error = response.get('error')
        if error:
            kind = error.get('kind', 'uncertain')
            if kind == 'session' or error.get('session_expired'):
                self._status('expired')
            exc = {'blocked': Blocked, 'session': Blocked, 'rejected': Rejected}.get(kind, Uncertain)
            # Worker errors use fixed messages, never page text or exception details.
            raise exc(error.get('message', '网页操作未能确认，请核对后台'))
        return response.get('result', {})

    def account(self, action, job_id=''):
        if action not in ('login', 'check', 'logout'):
            raise Blocked('未知电商账号操作')
        if action != 'logout' and not self.store.get('commerce_enabled', True):
            raise Blocked('此店铺未启用电商网页管理')
        if action == 'logout':
            with profile_lock(self.store['id']):
                path = profile_path(self.store['id'])
                if path.exists():
                    # Logout invalidates all local persisted authentication, including IndexedDB.
                    shutil.rmtree(path)
            self._status('unverified')
            return {'message': '电商后台本地登录会话已清除；请在平台后台撤销远端会话（如需要）'}
        try:
            with profile_lock(self.store['id']):
                result = self._run(action)
        except Exception:
            current = db.get_store(self.store['id'])
            if not current or current.get('commerce_status') != 'expired':
                self._status('unverified')
            raise
        self._status('valid')
        return result

    def mutate_product(self, action, payload):
        check_session(self.store)
        validate_workflow(self.store, action)
        if action not in ('product_create', 'product_update', 'product_list', 'product_delist'):
            raise Blocked('未知商品操作')
        with profile_lock(self.store['id']):
            return self._run(action, payload)

    def orders(self, start, end):
        check_session(self.store)
        validate_workflow(self.store, 'orders')
        if end <= start or end - start > 7 * 86400:
            raise Blocked('订单查询范围须大于零且不超过 7 天')
        with profile_lock(self.store['id']):
            result = self._run('orders', {'since': start, 'until': end})
        yield from result['orders']

    def fetch_sku_map(self, product, listing):
        check_session(self.store)
        validate_workflow(self.store, 'fetch_skus')
        with profile_lock(self.store['id']):
            return self._run('fetch_skus', {'product': product, 'listing': listing})['sku_map']

    def sync_inventory(self, product, listing, targets):
        check_session(self.store)
        validate_workflow(self.store, 'inventory_sync')
        safe_product = copy.deepcopy(product)
        by_code = {s['code']: s for s in targets}
        if len(by_code) != len(targets) or set(by_code) != {s['code'] for s in product['skus']}:
            raise Blocked('库存校准目标必须覆盖并唯一匹配全部本地 SKU')
        for sku in safe_product['skus']:
            sku['stock'] = by_code[sku['code']]['stock']
        with profile_lock(self.store['id']):
            return self._run('inventory_sync', {'product': safe_product, 'listing': listing, 'targets': targets})
