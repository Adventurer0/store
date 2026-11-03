"""Built-in receipt-verified submission for merchant pages without a workflow.

Only visible controls and newly observed receipts are used. A missing,
ambiguous or changed page fails closed; no private endpoint is called.
"""
import re
import math
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit

try:
    from .commerce_worker import Failure
    from .platform_feedback import extract_code, translate
except ImportError:
    from commerce_worker import Failure
    from platform_feedback import extract_code, translate


SUCCESS = re.compile(r'^\s*(?:商品)?(?:已)?(?:提交审核成功|提交成功|发布成功|保存成功|已提交审核|操作成功)[！!。]?\s*$')
ID_TEXT = re.compile(r'(?:商品|产品)?\s*(?:ID|编号)\s*[:：]?\s*(\d{1,100})', re.I)
ID_KEYS = ('itemId', 'item_id', 'productId', 'product_id', 'goodsId', 'goods_id', 'id')
BUTTONS = ('提交审核', '提交发布', '发布商品', '提交', '发布', '保存并提交', '保存')
REJECT = re.compile(r'草稿|取消|返回|删除|下架|关闭')


def _one(page, name):
    for role in ('button', 'link'):
        locator = page.get_by_role(role, name=name, exact=True).filter(visible=True)
        if locator.count() == 1:
            return locator
        if locator.count() > 1:
            raise Failure('blocked', f'页面存在多个“{name}”控件，未猜测提交按钮')
    return None


def _button(page, mode='create'):
    for name in BUTTONS:
        if mode == 'create' and name == '保存':
            continue
        if REJECT.search(name):
            continue
        button = _one(page, name)
        if button is not None:
            return button, name
    raise Failure('blocked', '未唯一识别提交按钮，请在平台页面核对或配置页面规则')


def _required(page):
    invalid = page.locator('input,textarea,select,[aria-invalid="true"]').filter(visible=True).evaluate_all(r'''els => els.filter(e => {
      if (e.getAttribute('aria-invalid') === 'true') return true;
      if (['hidden','password','submit','button'].includes(e.type) || e.disabled) return false;
      return (e.required || e.getAttribute('aria-required') === 'true') &&
        ((!e.value || !e.value.trim()) || (e.type === 'checkbox' && !e.checked) || (e.validity && !e.validity.valid));
    }).length''')
    errors = page.locator('.ant-form-item-explain-error,[role="alert"][aria-live="assertive"]').filter(visible=True)
    if invalid or errors.count():
        text = page.locator('body').inner_text()[:20000]
        raise Failure('blocked', '平台仍显示必填项或校验错误，未提交', code=extract_code(text))


def _success(page, mode='create'):
    success = SUCCESS if mode == 'update' else re.compile(
        r'^\s*(?:商品)?(?:已)?(?:提交审核成功|提交成功|发布成功|已提交审核)[！!。]?\s*$')
    candidates = page.get_by_text(success, exact=True).filter(visible=True)
    return [candidates.nth(index).inner_text().strip() for index in range(min(candidates.count(), 10))]


def _external_id(page):
    values = set()
    query = parse_qs(urlsplit(page.url).query)
    for key in ID_KEYS:
        for value in query.get(key, []):
            if re.fullmatch(r'\d{1,100}', value):
                values.add(value)
    text = page.locator('body').inner_text()[:20000]
    values.update(ID_TEXT.findall(text))
    if len(values) > 1:
        raise Failure('uncertain', '页面出现多个不同商品 ID，请人工核对后台')
    return next(iter(values), '')


def submit(page, payload, guard, *, read_quality=None, on_commit=None):
    """Click one observed submit control and require a new success receipt."""
    mode = payload.get('mode', 'create')
    existing = str((payload.get('listing') or {}).get('external_id') or '')
    if mode == 'create' and existing:
        raise Failure('blocked', '已关联平台商品的商品不能作为新建商品提交')
    if mode == 'update' and not existing:
        raise Failure('blocked', '更新商品缺少已关联的平台商品 ID')
    guard()
    _required(page)
    button, name = _button(page, mode)
    if not button.is_enabled():
        raise Failure('blocked', f'“{name}”按钮不可用，请补齐页面要求')
    if _success(page, mode) or (mode == 'create' and _external_id(page)):
        raise Failure('blocked', '页面已存在成功提示或商品 ID，未重复提交')
    # Resolve controls first, then recheck identity and fresh score immediately
    # before the irreversible click. Missing score is allowed only for a
    # platform which has never provided one for this reviewed draft.
    guard()
    evidence = read_quality() if read_quality is not None else {}
    score = evidence.get('score') if isinstance(evidence, dict) else None
    required_score = payload.get('require_platform_quality') is True
    if required_score or score is not None:
        if (not isinstance(evidence, dict) or evidence.get('source') != 'platform_dom'
                or evidence.get('fingerprint') != payload.get('fingerprint')
                or isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score) or not 90 < score <= 100
                or not isinstance(evidence.get('observed_at'), int)
                or not time.time() - 60 <= evidence['observed_at'] <= time.time() + 30):
            raise Failure('blocked', '提交前平台评分不可用、已变化或未高于90分，请重新检查')
    if on_commit is not None:
        on_commit()
    try:
        button.click(timeout=15000)
        deadline = time.monotonic() + 30
        messages, external = [], ''
        while time.monotonic() < deadline:
            messages = _success(page, mode)
            if messages:
                external = _external_id(page)
                break
            page.wait_for_timeout(250)
        if not messages:
            text = page.locator('body').inner_text()[:20000]
            code = extract_code(text)
            message, code = translate('', code, '已点击提交但未读到明确的成功提示，请核对后台，勿重复提交')
            raise Failure('uncertain', message, code=code)
        if mode == 'create' and not external:
            raise Failure('uncertain', '已提交但未读取到商品 ID，请核对后台并手动关联')
        if mode == 'update' and not external:
            external = existing
        if external != existing and mode == 'update':
            raise Failure('uncertain', '更新回执的商品 ID 与目标不一致，请核对后台')
        parts = urlsplit(page.url)
        return {'external_id': external, 'sku_map': {},
                **({'quality': evidence} if score is not None else {}),
                'receipt': {'source': 'platform_dom',
                            'source_url': urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')),
                            'messages': messages[:5], 'observed_at': int(time.time())},
                'message': ('商品已提交审核，审核结果以平台后台为准' if mode == 'create'
                            else '商品更新已提交，平台商品 ID 保持不变；审核结果以后台为准')}
    except Failure as exc:
        raise Failure('uncertain' if exc.kind != 'session' else 'session', exc.message, code=exc.code) from None
    except Exception:
        raise Failure('uncertain', '提交后页面或回执读取异常，请核对后台，勿直接重试') from None
