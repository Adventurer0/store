"""Merchant browser origins. Creator/video origins never share these profiles."""
from urllib.parse import urlsplit, urljoin


PROFILES = {
    'douyin': {'portal': 'https://fxg.jinritemai.com/',
               'login': 'https://fxg.jinritemai.com/login/common',
               'probe': 'https://fxg.jinritemai.com/ffa/mshop/homepage/index',
               'domains': ('jinritemai.com', 'douyin.com'),
               'app_paths': ('/ffa/',),
               'login_texts': ('手机登录', '邮箱登录', '扫码登录', '验证码登录')},
    'kuaishou': {'portal': 'https://s.kwaixiaodian.com/',
                 'login': 'https://s.kwaixiaodian.com/',
                 'probe': 'https://s.kwaixiaodian.com/zone/goods/list',
                 'domains': ('kwaixiaodian.com', 'kuaishou.com'),
                 'app_paths': ('/zone/',),
                 'login_texts': ('快手商家账号登录', '密码登录', '短信登录', '扫码登录')},
    'wechat': {'portal': 'https://store.weixin.qq.com/',
               'login': 'https://store.weixin.qq.com/',
               'probe': 'https://store.weixin.qq.com/shop',
               'domains': ('weixin.qq.com', 'wechat.com'),
               'app_paths': ('/shop',),
               'login_texts': ('扫码进入我的小店', '扫码登录', '微信扫码登录')},
    'tmall': {'portal': 'https://myseller.taobao.com/',
              'login': 'https://myseller.taobao.com/',
              'probe': 'https://myseller.taobao.com/home.htm',
              'domains': ('taobao.com', 'tmall.com'),
              'app_paths': ('/home',),
              'login_texts': ('扫码登录', '账号登录', '密码登录')},
    'jd': {'portal': 'https://shop.jd.com/',
           'login': 'https://shop.jd.com/',
           'probe': 'https://shop.jd.com/',
           'domains': ('jd.com', 'jdseller.com'),
           'app_paths': ('/shop/',),
           'login_texts': ('扫码登录', '账号登录', '密码登录')},
    'pdd': {'portal': 'https://mms.pinduoduo.com/',
            'login': 'https://mms.pinduoduo.com/',
            'probe': 'https://mms.pinduoduo.com/',
            'domains': ('pinduoduo.com',),
            'app_paths': ('/home',),
            'login_texts': ('扫码登录', '账号登录', '密码登录')},
}

# Hosts that only serve a sign-in page. They are trusted for the redirect, but a
# page still on one of them is never treated as a completed login.
LOGIN_HOSTS = ('login.', 'passport.', 'loginmyseller.', 'passport2.')

LOGIN_PATHS = ('/login', '/signin', '/signup', '/register', '/passport')

# Text that only a signed-in merchant shell shows. Kept narrow on purpose: a
# marketing homepage must not pass as a dashboard.
READY_TEXTS = ('商品管理', '商品列表', '订单管理', '订单列表', '店铺管理', '发布商品',
               '数据中心', '退款售后', '物流工具', '营销中心')


def login_url(platform):
    profile = PROFILES[platform]
    return profile.get('login') or profile['portal']


def is_login_page(platform, value):
    """True when a URL is a sign-in page rather than the merchant workspace."""
    parts = urlsplit(value)
    host = (parts.hostname or '').lower()
    path = parts.path.lower()
    if any(host == item.rstrip('.') or host.startswith(item) for item in LOGIN_HOSTS):
        return True
    return any(word in path for word in LOGIN_PATHS)


def app_path(platform, value):
    """True when the URL sits inside the merchant application area."""
    path = urlsplit(value).path
    return any(path.startswith(prefix) for prefix in PROFILES[platform].get('app_paths', ()))


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
