import threading
import time

import pytest

from app import db
from app import commerce_browser as browser
from app.commerce_profiles import app_path, is_login_page, login_url
from app.commerce_worker import Failure, Runner
from app.jobs import Worker


def test_douyin_opens_the_real_login_page_not_the_marketing_homepage():
    assert login_url('douyin') == 'https://fxg.jinritemai.com/login/common'
    assert is_login_page('douyin', 'https://fxg.jinritemai.com/login/common')
    # The landing page the user was sent to before is not a login page.
    landing = 'https://fxg.jinritemai.com/ffa/mshop/homepage/index?channel=zhaoshang'
    assert not is_login_page('douyin', landing)
    assert app_path('douyin', 'https://fxg.jinritemai.com/ffa/mshop/shop/select')
    assert is_login_page('kuaishou', 'https://login.kwaixiaodian.com/?biz=zone')
    assert not is_login_page('kuaishou', 'https://s.kwaixiaodian.com/zone/goods/list')
    assert is_login_page('jd', 'https://passport.shop.jd.com/login/index.action')


def test_douyin_login_completes_after_the_shop_chooser_even_without_a_menu(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    html = '''<meta charset="utf-8"><div>手机登录</div><div>扫码登录</div><script>
      setTimeout(function(){
        document.cookie='merchant_session=test; path=/; secure';
        history.pushState({}, '', '/ffa/mshop/shop/select');
        document.body.innerHTML='<div>选择店铺</div><div>商品管理</div>';
      }, 900);
    </script>'''
    runner = Runner({'platform': 'douyin', 'action': 'login', 'profile': str(tmp_path),
                     'config': {}, 'payload': {}})
    with playwright.sync_playwright() as p:
        chrome = p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = chrome.new_context()
        page = context.new_page()
        seen = []

        def route(handler):
            seen.append(handler.request.url)
            handler.fulfill(body=html, content_type='text/html; charset=utf-8')

        page.route('https://fxg.jinritemai.com/**', route)
        runner.context, runner.page = context, page
        try:
            result = runner.account()
            assert seen and seen[0] == 'https://fxg.jinritemai.com/login/common'
            assert '登录成功' in result['message']
            assert page.url.startswith('https://fxg.jinritemai.com/ffa/mshop/shop/select')
            assert (tmp_path / 'session.json').is_file()
        finally:
            chrome.close()


def test_login_confirmation_is_rejected_while_the_page_still_asks_for_login(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    html = '<meta charset="utf-8"><div>手机登录</div><div>扫码登录</div>'
    runner = Runner({'platform': 'douyin', 'action': 'login', 'profile': str(tmp_path),
                     'config': {}, 'payload': {}})
    with playwright.sync_playwright() as p:
        chrome = p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = chrome.new_context()
        page = context.new_page()
        page.route('https://fxg.jinritemai.com/**', lambda route: route.fulfill(
            body=html, content_type='text/html; charset=utf-8'))
        runner.context, runner.page = context, page
        # The user may click "I finished" before the platform redirects.
        threading.Timer(0.4, lambda: (tmp_path / 'login.confirm').write_text('1', encoding='utf-8')).start()
        try:
            with pytest.raises(Failure, match='仍停留在登录页'):
                runner.account()
        finally:
            chrome.close()


def test_confirm_login_marker_is_private_and_scoped_to_the_store(store):
    marker = browser.confirm_login(store['id'])
    assert marker == db.DATA / 'browser' / 'commerce' / store['id'] / 'login.confirm'
    assert marker.read_text(encoding='utf-8').isdigit()
    assert (marker.stat().st_mode & 0o777) == 0o600


def _running_job(store, kind, updated):
    now = int(time.time())
    job_id = db.uid()
    with db.connection() as conn:
        conn.execute('INSERT INTO jobs(id,batch_id,kind,store_id,entity_id,status,scheduled,payload,result,'
                     'dedupe_key,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     (job_id, db.uid(), kind, store['id'], '', 'running', now, '{}', '{}',
                      db.uid(), now, updated))
    return job_id


def test_stale_login_job_is_released_but_a_fresh_one_is_not(owner, store):
    stale = _running_job(store, 'commerce_login', int(time.time()) - 500)
    fresh = _running_job(store, 'commerce_login', int(time.time()) - 5)
    Worker().reap_stale()
    assert db.one('SELECT status FROM jobs WHERE id=?', (stale,))['status'] == 'uncertain'
    assert db.one('SELECT status FROM jobs WHERE id=?', (fresh,))['status'] == 'running'
