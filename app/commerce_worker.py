"""DOM-only merchant worker. Stdlib until Playwright is launched.

Protocol: one JSON request on stdin, one bounded JSON response on stdout. Never
return cookies, page HTML, browser stderr or raw platform error messages.
"""
import copy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo
try:
    from .commerce_profiles import (PROFILES, READY_TEXTS, app_path, is_login_page,
                                    login_url, trusted_url)
    from .commerce_preview import paths as preview_paths, write_marker
except ImportError:
    from commerce_profiles import (PROFILES, READY_TEXTS, app_path, is_login_page,
                                   login_url, trusted_url)
    from commerce_preview import paths as preview_paths, write_marker


class Failure(Exception):
    def __init__(self, kind, message, code=''):
        self.kind, self.message = kind, message
        self.code = str(code or '')[:40]


PATTERN = re.compile(r'\{\{\s*([A-Za-z0-9_.]+)\s*\}\}')


def render(value, context):
    def lookup(path):
        item = context
        try:
            for key in path.split('.'):
                item = item[int(key)] if isinstance(item, list) else item[key]
        except (ValueError, KeyError, IndexError, TypeError):
            raise Failure('blocked', '网页工作流引用了未提供的字段，请检查配置与商品资料') from None
        return copy.deepcopy(item)
    if isinstance(value, str):
        match = PATTERN.fullmatch(value)
        if match:
            return lookup(match[1])
        return PATTERN.sub(lambda m: str(lookup(m[1])), value)
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    return value


def integer(value, *, nonnegative=True):
    value = str(value).strip()
    if not re.fullmatch(r'-?\d+', value):
        raise Failure('rejected', '网页表格数字字段无法按整数读取，请检查读取规则')
    number = int(value)
    if (nonnegative and number < 0) or abs(number) > 100_000_000_000:
        raise Failure('rejected', '网页表格数字超出支持范围')
    return number


def timestamp(value):
    value = str(value).strip()
    if re.fullmatch(r'(?:\d{10}|\d{13})', value):
        return int(value) // (1000 if len(value) == 13 else 1)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return int((parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo('Asia/Shanghai'))).timestamp())
    except (ValueError, OverflowError):
        raise Failure('rejected', '订单时间必须是秒 / 毫秒时间戳或完整日期时间，请检查网页读取规则') from None


def inventory_state(status):
    if any(s in status for s in ('退款', '关闭', '取消')):
        return 'void'
    if any(s in status for s in ('待付款', '待支付')):
        return 'reserve'
    if any(s in status for s in ('待发货', '已支付', '备货', '待收货', '已发货', '已完成', '交易成功', '已收货')):
        return 'sold'
    return 'unknown'


class Runner:
    def __init__(self, request):
        self.request = request
        self.platform = request['platform']
        self.config = request.get('config', {})
        self.action = request['action']
        self.payload = request.get('payload', {})
        self.profile = Path(request['profile'])
        self.write_started = False
        self.page = None
        self.context = None

    def target(self, root, spec, context=None):
        spec = render(spec, context or self.payload)
        if isinstance(spec, str):
            locator = root.locator(spec)
        elif isinstance(spec, dict):
            if 'css' in spec:
                locator = root.locator(spec['css'])
            elif 'role' in spec:
                locator = root.get_by_role(spec['role'], name=spec.get('name'), exact=spec.get('exact', True))
            elif 'label' in spec:
                locator = root.get_by_label(spec['label'], exact=spec.get('exact', True))
            elif 'text' in spec:
                locator = root.get_by_text(spec['text'], exact=spec.get('exact', True))
            elif 'placeholder' in spec:
                locator = root.get_by_placeholder(spec['placeholder'], exact=spec.get('exact', True))
            elif 'test_id' in spec:
                locator = root.get_by_test_id(spec['test_id'])
            else:
                raise Failure('blocked', '网页选择器缺少 css / role / label / text / placeholder / test_id')
            if 'nth' in spec:
                index = integer(spec['nth'])
                if index > 10000:
                    raise Failure('blocked', '网页控件序号超出范围')
                locator = locator.nth(index)
        else:
            raise Failure('blocked', '网页选择器必须是字符串或对象')
        return locator

    def unique(self, root, spec, context=None):
        target = self.target(root, spec, context)
        # SPA widgets and receipts are often inserted after a network roundtrip.
        # Wait for attachment before enforcing uniqueness; never choose the first.
        target.first.wait_for(state='attached', timeout=15000)
        if target.count() != 1:
            raise Failure('uncertain' if self.write_started else 'blocked', '网页控件未唯一匹配，已停止；请按当前平台页面调整规则')
        return target

    def read(self, root, spec, context=None):
        locator = self.unique(root, spec, context)
        options = spec if isinstance(spec, dict) else {}
        if options.get('attribute'):
            attr = options['attribute']
            if not re.fullmatch(r'(?:data-[a-zA-Z0-9_-]+|value|href|title|aria-label)', attr):
                raise Failure('blocked', '网页字段 attribute 只支持数据属性或常规显示属性')
            value = locator.get_attribute(attr)
        elif options.get('input'):
            value = locator.input_value()
        else:
            value = locator.inner_text()
        if value is None:
            raise Failure('rejected', '网页字段为空，请检查读取规则')
        value = value.strip()
        if len(value) > 10000:
            raise Failure('rejected', '网页字段过长，请缩小选择器范围')
        if options.get('regex'):
            try:
                match = re.search(options['regex'], value)
                value = match.group(1) if match else ''
            except (re.error, IndexError):
                raise Failure('blocked', '网页字段 regex 必须是包含一个捕获组的有效表达式') from None
        if not value:
            raise Failure('rejected', '网页字段无法提取，请检查读取规则')
        return value

    def navigate(self, url):
        try:
            url = trusted_url(self.platform, url)
        except (ValueError, KeyError, TypeError):
            raise Failure('blocked', '网页地址不属于当前平台的 HTTPS 域名') from None
        self.page.goto(url, wait_until='domcontentloaded', timeout=45000)
        self.assert_origin()

    def assert_origin(self):
        try:
            trusted_url(self.platform, self.page.url)
        except (ValueError, KeyError, TypeError):
            raise Failure('session', '商家网页跳转到了未识别域名，请重新检查电商登录') from None

    def logged_in(self):
        self.assert_origin()
        # A sign-in host or /login path is never a completed session, even when
        # the marketing shell around it renders a "登录" button.
        if is_login_page(self.platform, self.page.url):
            return False
        if self.config.get('login_required'):
            if self.target(self.page, self.config['login_required']).first.is_visible():
                return False
        probe = self.config.get('logged_in')
        if probe:
            ready = self.target(self.page, probe)
            valid = ready.count() == 1 and ready.is_visible()
        else:
            # Strong login-form text means the SPA is still asking for a scan or
            # a code. This catches platforms that keep the same path after login.
            login_texts = PROFILES[self.platform].get('login_texts', ())
            if any(self.page.get_by_text(name, exact=False).first.is_visible()
                   for name in login_texts):
                return False
            # A shop-selection or dashboard URL is enough: multi-shop accounts
            # land on the chooser before any navigation item exists.
            valid = app_path(self.platform, self.page.url) or any(
                self.page.get_by_text(name, exact=True).first.is_visible()
                for name in READY_TEXTS)
        if not valid:
            return False
        identity = self.config.get('identity')
        if identity and self.read(self.page, identity['target']) != identity.get('value'):
            raise Failure('session', '当前登录的商家身份与店铺配置不一致，请切换到正确店铺')
        cookies = self.context.cookies([PROFILES[self.platform]['portal'], self.page.url])
        return any(c.get('value') and (c.get('expires', -1) == -1 or c.get('expires', 0) > time.time()) for c in cookies)

    def assert_identity(self, page=None):
        identity = self.config.get('identity')
        if not identity or not identity.get('target') or not identity.get('value'):
            raise Failure('blocked', '网页写入必须配置并核对当前商家身份')
        # Receipt read-back can happen on a second, read-only product page.
        if self.read(page or self.page, identity['target']) != identity['value']:
            raise Failure('session', '商家网页当前店铺与配置不一致，已停止写入')

    def audit_product(self):
        try:
            from .platform_feedback import audit_detail
        except ImportError:
            from platform_feedback import audit_detail
        external = str(self.payload.get('external_id') or '')
        if not external:
            raise Failure('blocked', '审核回查缺少平台商品 ID')
        if self.platform == 'kuaishou':
            try:
                from .kuaishou_submission import EDIT_URL
            except ImportError:
                from kuaishou_submission import EDIT_URL
            from urllib.parse import urlencode
            url = EDIT_URL + '?' + urlencode({'itemId': external})
        else:
            configured = self.config.get('audit_url')
            if not isinstance(configured, str) or '{external_id}' not in configured:
                raise Failure('blocked', '未配置审核回查页面 audit_url（需包含 {external_id}），未猜测后台地址')
            from urllib.parse import quote
            url = configured.replace('{external_id}', quote(external, safe=''))
        self.navigate(url)
        self.assert_origin()
        if is_login_page(self.platform, self.page.url):
            raise Failure('session', '审核回查时登录已失效，请重新登录电商后台')
        text = self.page.locator('body').inner_text()[:20000]
        status, message, code = audit_detail(text)
        if status == 'unknown':
            raise Failure('blocked', '未能识别当前商品的审核状态，请核对商家后台', code=code)
        return {'audit_status': status, 'audit_message': message, 'audit_code': code}

    def _session_marker(self):
        marker = self.profile / 'session.json'
        if marker.is_symlink():
            raise Failure('blocked', '商家登录状态文件不能是符号链接')
        marker.write_text(json.dumps({'checked_at': int(time.time()), 'platform': self.platform}), encoding='utf-8')
        marker.chmod(0o600)

    def _session_message(self):
        return ('电商后台网页登录检查通过，Cookie 已保存在本店独立浏览器目录'
                if self.action == 'check' else '登录成功，浏览器窗口已自动关闭')

    def account(self):
        login = login_url(self.platform)
        # A probe page redirects to sign-in when the session is invalid, so it
        # verifies an existing session instead of trusting a marketing page.
        target = PROFILES[self.platform].get('probe', login) if self.action == 'check' else login
        confirm = self.profile / 'login.confirm'
        try:
            confirm.unlink()
        except FileNotFoundError:
            pass
        self.navigate(target)
        # Give the SPA time to restore an existing session before we judge it.
        self.page.wait_for_timeout(1500)
        if self.logged_in():
            self._session_marker()
            return {'message': self._session_message()}
        deadline = time.monotonic() + (300 if self.action == 'login' else 12)
        while time.monotonic() < deadline:
            if confirm.exists():
                try:
                    confirm.unlink()
                except FileNotFoundError:
                    pass
                if self.logged_in():
                    self._session_marker()
                    return {'message': '已确认登录完成，浏览器窗口已自动关闭'}
                raise Failure('session', '页面仍停留在登录页，未检测到登录成功；请完成扫码后再点“我已完成登录”')
            try:
                if self.page.is_closed():
                    raise Failure('session', '登录窗口已被关闭；请重新发起登录')
                if self.logged_in():
                    self._session_marker()
                    return {'message': self._session_message()}
            except Failure:
                raise
            except Exception:
                # A redirect or SPA remount can detach the page briefly; retry.
                if self.page.is_closed():
                    raise Failure('session', '登录窗口已被关闭；请重新发起登录') from None
            self.page.wait_for_timeout(1000)
        if self.action == 'check':
            raise Failure('session', '未检测到有效登录；请重新登录电商后台，或完成扫码后再检查')
        raise Failure('session', '未检测到登录成功，登录窗口已关闭；请重新发起登录并完成扫码')

    def prepare(self, steps, context):
        """Resolve every form variable before a browser action can submit anything."""
        for step in steps:
            if step['op'] == 'each_sku':
                skus = context.get('targets') if self.action == 'inventory_sync' else context.get('product', {}).get('skus', [])
                if not skus:
                    raise Failure('blocked', 'SKU 循环没有可处理的规格')
                for index, sku in enumerate(skus):
                    self.prepare(step['steps'], {**context, 'sku': sku, 'index': index})
            else:
                render(step, context)

    def steps(self, steps, context, success=None):
        for step in steps:
            self.assert_origin()
            op = step['op']
            if op == 'each_sku':
                skus = context.get('targets') if self.action == 'inventory_sync' else context['product']['skus']
                for index, sku in enumerate(skus):
                    self.steps(step['steps'], {**context, 'sku': sku, 'index': index}, success)
                continue
            target = self.unique(self.page, step['target'], context)
            if op == 'wait':
                target.wait_for(state='visible', timeout=15000)
                continue
            if step.get('commit') and success:
                self.assert_identity()
                if context.get('quality_gate'):
                    try:
                        from .commerce_forms import quality
                    except ImportError:
                        from commerce_forms import quality
                    gate = context['quality_gate']
                    score = quality(self.page, self.platform, gate['fingerprint'])
                    if score.get('score') is None or score['score'] <= gate.get('minimum', 90):
                        raise Failure('blocked', '点击提交前的平台真实商品分数未高于90分，已停止')
                before = self.target(self.page, success, context)
                if before.count() and before.first.is_visible():
                    raise Failure('uncertain' if self.write_started else 'blocked', '提交前已存在成功提示，无法识别新的提交回执；请调整 result.success')
            if self.action.startswith('product_') or self.action == 'inventory_sync':
                # Form changes can trigger autosave. Be conservative even before commit.
                self.write_started = True
            value = render(step.get('value', ''), context)
            if op == 'click':
                target.click(timeout=15000)
            elif op == 'fill':
                if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                    raise Failure('blocked', '网页填表值必须为文本或数字')
                target.fill(str(value), timeout=15000)
            elif op == 'select':
                target.select_option(str(value), timeout=15000)
            elif op == 'check':
                if not isinstance(value, bool):
                    raise Failure('blocked', '勾选操作 value 必须为布尔值')
                target.set_checked(value, timeout=15000)
            elif op == 'upload':
                ids = value if isinstance(value, list) else [value]
                if not ids or any(mid not in self.request.get('media', {}) for mid in ids):
                    raise Failure('blocked', '上传只能引用此商品已上传的图片素材 ID')
                target.set_input_files([self.request['media'][mid] for mid in ids], timeout=30000)
            else:
                raise Failure('blocked', '未知网页操作')

    def table(self, result):
        rows = self.target(self.page, result['rows'])
        deadline = time.monotonic() + 15
        while not rows.count():
            empty = result.get('empty')
            if empty and self.target(self.page, empty).first.is_visible():
                return []
            if time.monotonic() >= deadline:
                raise Failure('rejected', '网页未显示数据行或明确空列表提示，请检查规则')
            self.page.wait_for_timeout(100)
        count = rows.count()
        if count > 1000:
            raise Failure('rejected', '网页表格每页超过 1000 行，请缩小范围')
        return [{name: self.read(rows.nth(i), spec) for name, spec in result['fields'].items()} for i in range(count)]

    def sku_map(self, rows):
        codes = {s['code'] for s in self.payload['product']['skus']}
        result, remote = {}, set()
        for row in rows:
            code, sid = row['code'], row['sku_id']
            if code in codes:
                if code in result or sid in remote:
                    raise Failure('rejected', '网页 SKU 编码或平台 ID 重复，无法唯一关联')
                result[code] = sid
                remote.add(sid)
        if set(result) != codes:
            raise Failure('rejected', '网页 SKU 外部编码未完整匹配；请人工核对关联，不按行位置猜测')
        return result

    def orders(self, workflow):
        config = workflow['result']
        seen, orders = set(), {}
        for page_number in range(500):
            self.assert_origin()
            if not self.logged_in():
                raise Failure('session', '读取订单时商家登录失效，请重新登录电商后台')
            rows = self.table(config)
            fingerprint = json.dumps(rows, sort_keys=True, ensure_ascii=False)
            if fingerprint in seen:
                raise Failure('rejected', '订单网页翻页未推进，请调整下一页规则')
            seen.add(fingerprint)
            for row in rows:
                created, updated = timestamp(row['created']), timestamp(row['remote_updated'])
                if not self.payload['since'] <= updated <= self.payload['until']:
                    continue
                oid = row['external_id']
                status = row['status'][:200]
                amount = integer(row['amount_cents']) if 'amount_cents' in row else None
                if 'amount' in row:
                    try:
                        pennies = Decimal(row['amount'].replace(',', '').replace('¥', '').replace('￥', '')) * 100
                        if pennies != pennies.to_integral_value() or pennies < 0:
                            raise ValueError()
                        amount = int(pennies)
                    except (InvalidOperation, ValueError):
                        raise Failure('rejected', '订单金额无法准确转换为分，请调整字段规则') from None
                order = {'external_id': oid, 'status': status, 'raw_status': row.get('raw_status', status),
                         'inventory_state': inventory_state(status), 'created': created,
                         'remote_updated': updated, 'amount_cents': amount, 'items': []}
                if oid in orders and any(orders[oid][k] != order[k] for k in ('status', 'remote_updated', 'amount_cents')):
                    raise Failure('rejected', '同一订单的网页状态不一致，请重新同步')
                old = orders.setdefault(oid, order)
                if row.get('sku'):
                    item = {'sku': row['sku'], 'product_id': row.get('product_id', ''),
                            'title': row.get('title', '')[:200], 'quantity': integer(row.get('quantity', '0'))}
                    if item['quantity'] <= 0:
                        raise Failure('rejected', '订单商品数量缺失或无效，已停止读取')
                    same = next((i for i in old['items'] if (i['sku'], i['product_id']) == (item['sku'], item['product_id'])), None)
                    if same:
                        raise Failure('rejected', '同一订单存在重复 SKU 明细，无法确认是重复行还是多件商品；请调整网页读取规则')
                    old['items'].append(item)
            next_spec = config.get('next')
            if not next_spec:
                # Explicit single_page is required to avoid silently truncating orders.
                if not config.get('single_page'):
                    raise Failure('blocked', '订单工作流需要 result.next 翻页规则；单页数据须显式设置 single_page')
                return {'orders': list(orders.values())}
            nxt = self.target(self.page, next_spec)
            if nxt.count() == 0:
                raise Failure('rejected', '订单下一页控件缺失，无法确认是否读取完整')
            nxt = self.unique(self.page, next_spec)
            if not nxt.is_enabled() or nxt.get_attribute('aria-disabled') == 'true':
                return {'orders': list(orders.values())}
            nxt.click(timeout=15000)
            # Dynamic tables often update without navigation. Wait for observed change.
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                self.page.wait_for_timeout(250)
                try:
                    current = json.dumps(self.table(config), sort_keys=True, ensure_ascii=False)
                    if current != fingerprint:
                        break
                except Failure:
                    continue
            else:
                raise Failure('rejected', '订单翻页后内容没有变化，请调整翻页或表格规则')
        raise Failure('rejected', '订单网页达到 500 页上限，请缩小时间范围')

    def operate(self):
        workflow = self.config['workflows'][self.action]
        context = self.payload
        self.prepare(workflow.get('steps', []), context)
        self.navigate(PROFILES[self.platform]['portal'])
        deadline = time.monotonic() + 20
        while not self.logged_in():
            if time.monotonic() > deadline:
                raise Failure('session', '电商后台登录失效，请重新完成商家网页登录')
            self.page.wait_for_timeout(500)
        self.navigate(render(workflow.get('url', ''), context))
        if self.action.startswith('product_') or self.action == 'inventory_sync':
            self.assert_identity()
        self.steps(workflow.get('steps', []), context, workflow.get('result', {}).get('success'))
        result = workflow['result']
        if self.action == 'orders':
            return self.orders(workflow)
        if self.action == 'fetch_skus':
            return {'sku_map': self.sku_map(self.table(result))}
        self.assert_origin()
        self.unique(self.page, result['success'], context).wait_for(state='visible', timeout=30000)
        if self.action == 'inventory_sync':
            rows = self.table(result)
            quantities = {}
            for row in rows:
                if row['sku_id'] in quantities:
                    raise Failure('uncertain', '库存回读存在重复 SKU，无法确认校准结果')
                quantities[row['sku_id']] = integer(row['stock'])
            targets = self.payload['targets']
            mapping = self.payload['listing']['sku_map']
            if any(quantities.get(str(mapping[s['code']])) != s['stock'] for s in targets):
                raise Failure('uncertain', '网页提交后 SKU 库存回读与目标不一致，请核对商家后台')
            return {'confirmed': True, 'sku_count': len(targets), 'message': '商家网页库存已提交并逐项回读确认'}
        external = self.read(self.page, result['external_id'], context) if result.get('external_id') else str(self.payload['listing'].get('external_id', ''))
        if not external or len(external) > 100 or any(c.isspace() for c in external):
            raise Failure('uncertain', '网页提交后未获得有效商品 ID，请核对后台并手动关联')
        if self.action != 'product_create' and external != str(self.payload['listing']['external_id']):
            raise Failure('uncertain', '网页回执商品 ID 与目标不一致，请核对商家后台')
        rows = self.table(result) if result.get('rows') else []
        mapping = self.sku_map(rows) if rows else {}
        if self.action == 'product_create' and (not mapping or any(integer(row['stock']) != 0 for row in rows)):
            raise Failure('uncertain', '新商品提交后未确认全部 SKU 为零库存，请核对后台并暂停销售')
        return {'external_id': external, 'sku_map': mapping, 'message': '商家网页已提交并读取到成功回执；审核结果以平台后台为准'}

    def prepare_product(self):
        """Fill a Kuaishou form, then hand the visible browser to the merchant."""
        try:
            from .kuaishou_commerce import CREATE_URL, PrepareBlocked, prepare
        except ImportError:
            from kuaishou_commerce import CREATE_URL, PrepareBlocked, prepare
        if self.platform != 'kuaishou':
            raise Failure('blocked', '填表预览当前仅支持快手小店')
        settings = self.request.get('preview', {})
        try:
            marker, stop = preview_paths(self.profile.parents[2], settings.get('job_id'), create=True)
        except (ValueError, IndexError):
            raise Failure('blocked', '填表预览任务标识或目录无效') from None
        if stop.exists():
            return {'message': '填表预览已结束；程序未点击提交审核', 'submitted_by_app': False}
        self.navigate(PROFILES[self.platform]['portal'])
        deadline = time.monotonic() + 20
        while not self.logged_in():
            if time.monotonic() > deadline:
                raise Failure('session', '快手商家登录失效，请重新完成电商后台登录')
            self.page.wait_for_timeout(500)
        self.navigate(CREATE_URL)
        config = copy.deepcopy(self.config)
        config['business'] = self.payload.get('publish_config', {}).get('kuaishou_preview', {})
        try:
            result = prepare(self.page, self.payload, self.request.get('media', {}), config)
        except PrepareBlocked as exc:
            raise Failure('blocked', str(exc)) from None
        needs_manual = [f"{item.get('label', '待核对项目')}：{item.get('reason', '请在页面核对')}"
                        for item in result.get('missing', []) if isinstance(item, dict)]
        progress = {'ready': True, 'active': True,
                    'status': 'needs_input' if needs_manual else 'prepared',
                    'filled': result.get('filled', []), 'needs_manual': needs_manual,
                    'submitted_by_app': False, 'started_at': int(time.time()),
                    'message': '快手填表预览已打开，最多保留 10 分钟。程序未点击提交审核或保存草稿；请核对页面和待补项目。'}
        write_marker(marker, progress)
        # No DOM writes, navigation, final-submit detection or listing updates
        # happen during this handoff. Closing targets this job's own context.
        seconds = min(600, max(0, int(settings.get('seconds', 600))))
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline and not stop.exists() and not self.page.is_closed():
                try:
                    self.page.wait_for_timeout(1000)
                except Exception:
                    break
        finally:
            progress.update(active=False, status='closed', closed_at=int(time.time()),
                            message='填表预览已结束。程序未点击提交审核或保存草稿；如你自行提交，请以平台后台结果为准。')
            write_marker(marker, progress)
        return progress

    def run(self):
        from playwright.sync_api import sync_playwright
        # Persistent Chromium profile contains HTTP-only Cookie and local storage.
        # No storage_state JSON or Cookie values are exported to the application.
        with sync_playwright() as playwright:
            kwargs = {'user_data_dir': str(self.profile), 'headless': os.environ.get('COMMERCE_HEADLESS') == '1',
                      'viewport': {'width': 1360, 'height': 860}, 'locale': 'zh-CN',
                      'accept_downloads': False,
                      'args': ['--disable-dev-shm-usage', '--no-first-run',
                               '--no-default-browser-check', '--disable-translate',
                               '--lang=zh-CN',
                               '--disable-features=Translate,TranslateUI']}
            channel = os.environ.get('COMMERCE_BROWSER_CHANNEL', 'chrome')
            if channel and channel != 'chromium':
                kwargs['channel'] = channel
            self.context = playwright.chromium.launch_persistent_context(**kwargs)
            try:
                self.context.set_default_timeout(15000)
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
                if self.action in ('login', 'check'):
                    return self.account()
                if self.action == 'prepare_product':
                    return self.prepare_product()
                if self.action == 'audit_product':
                    return self.audit_product()
                if self.action in ('collect_requirements', 'prepare_workbench', 'submit_workbench'):
                    try:
                        from .commerce_forms import run as run_workbench
                    except ImportError:
                        from commerce_forms import run as run_workbench
                    return run_workbench(self, self.action)
                return self.operate()
            finally:
                # A platform confirmation dialog must not turn a completed login
                # into an error, and the window must close either way.
                try:
                    self.context.close()
                except Exception:
                    pass


def main():
    runner = None
    try:
        raw = sys.stdin.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise Failure('blocked', '网页任务请求过大')
        request = json.loads(raw)
        runner = Runner(request)
        result = runner.run()
        response = {'result': result}
    except Failure as exc:
        kind = exc.kind
        if runner and runner.write_started:
            kind = 'uncertain'
        response = {'error': {'kind': kind, 'message': exc.message,
                              'code': getattr(exc, 'code', ''),
                              'session_expired': exc.kind == 'session'}}
    except ImportError:
        response = {'error': {'kind': 'blocked', 'message': '商家浏览器缺少 Playwright 运行依赖，请重新构建完整镜像'}}
    except Exception:
        response = {'error': {'kind': 'uncertain' if runner and runner.write_started else 'blocked',
                              'message': '商家网页结构变化或浏览器不可用；请检查安装、登录和网页适配规则'}}
    encoded = json.dumps(response, ensure_ascii=False)
    if len(encoded) > 20 * 1024 * 1024:
        encoded = json.dumps({'error': {'kind': 'rejected', 'message': '网页读取结果过大，请缩小范围'}}, ensure_ascii=False)
    sys.stdout.write(encoded)


if __name__ == '__main__':
    main()
