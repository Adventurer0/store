"""Translate platform feedback without inventing scores or success states."""
import re

KEYWORDS = (
    (('资质', 'qualification'), '缺少或不符合平台资质要求，请补齐后重试'),
    (('品牌授权', '授权'), '品牌授权未通过，请核对授权材料'),
    (('类目', '属性'), '类目或属性不符合平台要求，请重新读取并核对'),
    (('图片', '主图', '详情图'), '图片不符合平台要求，请检查尺寸、清晰度和数量'),
    (('价格', '库存', 'sku', '规格'), 'SKU、价格或库存不合法，请核对后重试'),
    (('验证码', '风控', '安全验证'), '平台要求人工验证，请在扫码窗口完成'),
    (('权限', '子账号'), '当前子账号权限不足，请使用有发布权限的账号'),
    (('登录', '失效', '过期'), '登录已失效，请重新登录电商后台'),
    (('限流', '频繁'), '平台请求过于频繁，请稍后重试'),
)
STATUS = (
    ('rejected', re.compile(r'审核(?:未通过|驳回|失败)|未通过|违规|已拒绝|审核不通过')),
    ('approved', re.compile(r'审核通过|已上架|上架成功|在售|发布成功')),
    ('offline', re.compile(r'已下架|下架|已停止')),
    ('pending', re.compile(r'审核中|待审核|待平台审核|提交审核|待发布')),
)
LABELS = {'unchecked': '未回查', 'checking': '回查中', 'pending': '审核中',
          'approved': '审核通过', 'rejected': '审核驳回', 'offline': '已下架', 'unknown': '状态未知'}


def translate(platform, code='', message=''):
    code = str(code or '').strip()[:40]
    message = str(message or '').strip()[:500]
    text = message
    for words, hint in KEYWORDS:
        if any(word in text.lower() for word in words):
            text = hint
            break
    if not text:
        text = '平台返回错误，请查看商家后台'
    if code:
        text = f'{text}（原错误码 {code}）' if str(code) not in text else text
    return text, code


def audit_status(text):
    value = str(text or '')
    for status, pattern in STATUS:
        if pattern.search(value):
            return status
    return 'unknown'


CODE = re.compile(r'(?:错误码|错误编号|err(?:or)?_?code|code)\s*[:：]?\s*([A-Za-z0-9_-]{2,40})', re.I)


def audit_detail(text):
    value = str(text or '')
    for line in value.splitlines():
        status = audit_status(line)
        if status != 'unknown':
            match = CODE.search(line) or CODE.search(value)
            return status, line.strip()[:200], match.group(1) if match else ''
    return 'unknown', '', (CODE.search(value).group(1) if CODE.search(value) else '')


def extract_code(text):
    match = CODE.search(str(text or '')[:20000])
    return match.group(1) if match else ''
