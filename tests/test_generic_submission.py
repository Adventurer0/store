"""Generic built-in submission for platforms without a configured DOM workflow."""
import re
import time

import pytest

from app import commerce_forms
from app.commerce_worker import Failure, Runner


def html(body):
    return '<meta charset="utf-8"><div id="shop">测试店</div><main>' + body + '</main>'


def page_for(context, url, body):
    current = context.new_page()
    current.route(re.compile(r'https://.*'), lambda route: route.fulfill(body=html(body), content_type='text/html; charset=utf-8'))
    current.goto(url)
    return current


def runner_for(page, platform, mode='create', external=''):
    return Runner({
        'platform': platform, 'profile': '/tmp/generic-profile', 'action': 'submit_workbench',
        'config': {'identity': {'target': '#shop', 'value': '测试店'}},
        'payload': {'mode': mode, 'external_id': external,
                    'product': {'title': '测试商品', 'skus': [
                        {'code': 'A1', 'name': '默认', 'price_cents': 1000, 'stock': 0 if mode == 'create' else 5}]},
                    'listing': {'external_id': external} if external else {},
                    'values': {}, 'schema': {'fields': []}, 'fingerprint': 'fp'},
        'media': {},
    })


def run_case(monkeypatch, page, platform, mode='create', external='', score=97.0):
    runner = runner_for(page, platform, mode, external)
    runner.page, runner.context = page, page.context
    monkeypatch.setattr(commerce_forms, 'open_editor', lambda current: None)
    monkeypatch.setattr(commerce_forms, 'collect', lambda current, name: {'complete': True, 'fields': []})
    monkeypatch.setattr(commerce_forms, 'fill', lambda *args, **kwargs: [])
    monkeypatch.setattr(commerce_forms, 'quality', lambda current, name, fingerprint: {
        'source': 'platform_dom', 'score': score, 'fingerprint': fingerprint,
        'observed_at': int(time.time()), 'source_url': current.url})
    return runner, commerce_forms.run(runner, 'submit_workbench')


def test_douyin_create_uses_one_visible_submit_and_reads_item_id(monkeypatch):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = browser.new_context()
        page = page_for(context, 'https://fxg.jinritemai.com/products/new', '''<input required aria-label="标题" value="商品">
          <button onclick="setTimeout(()=>{history.pushState({},'', '?itemId=123456');document.body.insertAdjacentHTML('beforeend','<div>提交审核成功</div>')},100)">提交审核</button>''')
        try:
            runner, result = run_case(monkeypatch, page, 'douyin')
            assert result['external_id'] == '123456'
            assert result['sku_map'] == {}
            assert runner.write_started is True
            assert result['receipt']['source'] == 'platform_dom'
        finally:
            browser.close()


def test_wechat_update_keeps_existing_id_and_accepts_save_success(monkeypatch):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = browser.new_context()
        page = page_for(context, 'https://store.weixin.qq.com/product/edit', '''<input required aria-label="标题" value="商品">
          <button onclick="setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<div>保存成功</div>'),100)">保存</button>''')
        try:
            runner, result = run_case(monkeypatch, page, 'wechat', 'update', '987654')
            assert result['external_id'] == '987654'
            assert '更新已提交' in result['message']
            assert runner.write_started is True
        finally:
            browser.close()


def test_ambiguous_submit_controls_block_before_any_click(monkeypatch):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = browser.new_context()
        page = page_for(context, 'https://fxg.jinritemai.com/products/new', '''<input required aria-label="标题" value="商品">
          <button onclick="document.body.dataset.clicked=1">提交</button><button onclick="document.body.dataset.clicked=1">提交</button>''')
        try:
            with pytest.raises(Failure) as caught:
                run_case(monkeypatch, page, 'douyin')
            assert caught.value.kind == 'blocked'
            assert page.locator('body').get_attribute('data-clicked') is None
        finally:
            browser.close()


def test_kuaishou_update_uses_generic_edit_save_not_create_adapter(monkeypatch):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = browser.new_context()
        page = page_for(context, 'https://s.kwaixiaodian.com/zone/goods/nexus/self/release/edit/ordinary?itemId=555',
                        '''<input required aria-label="标题" value="商品"><button onclick="setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<div>保存成功</div>'),100)">保存</button>''')
        try:
            runner, result = run_case(monkeypatch, page, 'kuaishou', 'update', '555')
            assert result['external_id'] == '555'
            assert '更新已提交' in result['message']
            assert runner.write_started is True
        finally:
            browser.close()


def test_douyin_create_can_submit_without_a_platform_score(monkeypatch):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
        context = browser.new_context()
        page = page_for(context, 'https://fxg.jinritemai.com/products/new', '''<input required aria-label="标题" value="商品">
          <button onclick="setTimeout(()=>{history.pushState({},'', '?itemId=999');document.body.insertAdjacentHTML('beforeend','<div>提交审核成功</div>')},100)">提交审核</button>''')
        try:
            _, result = run_case(monkeypatch, page, 'douyin', score=None)
            assert result['external_id'] == '999'
        finally:
            browser.close()
