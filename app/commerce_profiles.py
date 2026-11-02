"""Merchant browser origins. Creator/video origins never share these profiles."""
from urllib.parse import urlsplit, urljoin


PROFILES = {
    'douyin': {'portal': 'https://fxg.jinritemai.com/', 'domains': ('jinritemai.com', 'douyin.com')},
    'kuaishou': {'portal': 'https://s.kwaixiaodian.com/', 'domains': ('kwaixiaodian.com', 'kuaishou.com')},
    'wechat': {'portal': 'https://store.weixin.qq.com/', 'domains': ('weixin.qq.com', 'wechat.com')},
    'tmall': {'portal': 'https://myseller.taobao.com/', 'domains': ('taobao.com', 'tmall.com')},
    'jd': {'portal': 'https://shop.jd.com/', 'domains': ('jd.com', 'jdseller.com')},
    'pdd': {'portal': 'https://mms.pinduoduo.com/', 'domains': ('pinduoduo.com',)},
}


def trusted_url(platform, value):
    profile = PROFILES[platform]
    value = urljoin(profile['portal'], value)
    parts = urlsplit(value)
    host = (parts.hostname or '').lower()
    if (parts.scheme != 'https' or parts.username or parts.password
            or parts.port not in (None, 443)
            or not any(host == d or host.endswith('.' + d) for d in profile['domains'])):
        raise ValueError('网页地址必须属于当前平台的 HTTPS 商家域名')
    return value
