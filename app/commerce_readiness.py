"""Read-only, user-facing diagnosis of merchant publishing prerequisites.

An authenticated merchant session and a usable platform adapter are separate
requirements. This module reports both without opening a browser or returning
the adapter's selectors, stored credentials, or browser state.
"""
from . import commerce_browser
from .connectors import Blocked, PLATFORMS


OPERATIONS = (
    ('product_create', '创建商品'),
    ('product_update', '编辑商品'),
    ('product_list', '上架商品'),
    ('product_delist', '下架商品'),
    ('fetch_skus', '读取 SKU 关联'),
    ('inventory_sync', '校准店铺库存'),
    ('orders', '同步订单'),
)


def describe(store):
    """Return only readiness facts and fixed validation messages; never mutate."""
    enabled = bool(store.get('commerce_enabled'))
    engine_ready = commerce_browser.available()
    session_ready, session_problem = False, ''
    try:
        commerce_browser.check_session(store)
        session_ready = True
    except Blocked as exc:
        session_problem = str(exc)

    templates = store.get('templates', {})
    config = templates.get('commerce_browser', {}) if isinstance(templates, dict) else None
    workflows = config.get('workflows', {}) if isinstance(config, dict) else None
    malformed = not isinstance(workflows, dict)
    actions = []
    for action, label in OPERATIONS:
        configured = not malformed and action in workflows
        problem = ''
        try:
            commerce_browser.validate_workflow(store, action)
        except (Blocked, AttributeError, TypeError, ValueError) as exc:
            # Known validation failures contain fixed instructions only. Invalid
            # JSON shapes must not leak their repr or trigger an HTTP 500.
            problem = str(exc) if isinstance(exc, Blocked) else '平台适配配置格式有误，需要修正后再检查'

        builtin = store['platform'] == 'kuaishou' and action == 'product_create'
        if malformed or (configured and problem):
            status = 'adapter_invalid'
            summary = f'该平台的{label}适配需要修正'
        elif not configured and builtin:
            if not enabled:
                status = 'disabled'
                summary = '此店铺未启用电商网页管理'
            elif not engine_ready:
                status = 'engine_unavailable'
                summary = '电商浏览器运行环境不可用'
            elif not session_ready:
                status = 'login_required'
                summary = '请先登录此店铺的电商后台'
            else:
                status = 'builtin'
                summary = '统一发布可使用内置快手创建路径；旧批量创建仍需页面规则，提交时仍检查页面与评分'
        elif not configured:
            status = 'adapter_missing'
            summary = (f'该平台旧批量{label}适配尚未完成；统一发布可使用通用内置提交，需能识别新增商品入口'
                       if action == 'product_create'
                       else f'该平台的{label}适配尚未完成')
        elif not enabled:
            status = 'disabled'
            summary = '此店铺未启用电商网页管理'
        elif not engine_ready:
            status = 'engine_unavailable'
            summary = '电商浏览器运行环境不可用'
        elif not session_ready:
            status = 'login_required'
            summary = '请先登录此店铺的电商后台'
        else:
            status = 'ready'
            summary = '登录与页面规则预检通过；提交时仍会检查平台页面和商品资料'
        actions.append({'action': action, 'label': label, 'configured': configured,
                        'ready': status in ('ready', 'builtin'), 'status': status,
                        'summary': summary, 'problem': problem})

    return {'store_id': store['id'], 'platform': store['platform'],
            'platform_name': PLATFORMS[store['platform']]['name'],
            'commerce_enabled': enabled, 'login_status': store.get('commerce_status', 'unverified'),
            'engine_ready': engine_ready, 'session_ready': session_ready,
            'session_problem': session_problem, 'actions': actions}
