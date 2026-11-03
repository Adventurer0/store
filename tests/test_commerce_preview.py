import json
from types import SimpleNamespace

import pytest

from app import commerce_browser, db, jobs
from app.commerce_preview import paths, read_marker, request_close, write_marker
from app.connectors import Blocked


@pytest.fixture
def preview_store(owner, commerce_setup):
    store = owner.post('/api/stores', json={'name': 'preview-store', 'platform': 'kuaishou'}).json()
    store = commerce_setup(store)
    config = {'commerce_browser': {'identity': {'target': '#shop-id', 'value': 'test-shop'}},
              'publish_defaults': {'kuaishou_preview': {'brand': '已确认品牌', 'material': '已确认材质'}}}
    with db.connection() as conn:
        conn.execute('UPDATE stores SET templates=? WHERE id=?', (db.dumps(config), store['id']))
    return db.get_store(store['id'])


def queue(owner, store, product, **extra):
    result = owner.post(f"/api/stores/{store['id']}/commerce/prepare-product",
                        json={'product_id': product['id'], **extra})
    assert result.status_code == 200, result.text
    return result.json()['jobs'][0]


def test_preview_accepts_business_snapshot_without_workflow_or_changing_store(owner, preview_store, product):
    before = db.get_store(preview_store['id'])
    response = queue(owner, preview_store, product, upload_images=False,
                     business={'category_path': ['箱包', '旅行箱'], 'brand': '本次品牌'})
    saved = db.one('SELECT * FROM jobs WHERE id=?', (response['id'],))
    payload = json.loads(saved['payload'])['request']
    assert saved['kind'] == 'commerce_prepare_product'
    assert saved['status'] == 'queued'
    assert payload['upload_images'] is False
    assert payload['product']['skus'][0]['stock'] == 0
    assert payload['publish_config']['kuaishou_preview'] == {
        'brand': '本次品牌', 'material': '已确认材质', 'category_path': ['箱包', '旅行箱']}
    assert db.get_store(preview_store['id']) == before
    assert db.get_product(product['id'])['skus'][0]['stock'] == 10


def test_upload_consent_requires_boolean_and_business_cannot_inject_selectors(owner, preview_store, product):
    path = f"/api/stores/{preview_store['id']}/commerce/prepare-product"
    assert owner.post(path, json={'product_id': product['id'], 'upload_images': 'true'}).status_code == 422
    assert owner.post(path, json={'product_id': product['id'], 'business': {'selectors': '#submit'}}).status_code == 422
    assert db.one('SELECT count(*) n FROM jobs')['n'] == 0


def test_active_preview_is_deduplicated_but_different_business_is_blocked(owner, preview_store, product):
    first = queue(owner, preview_store, product, upload_images=True)
    again = queue(owner, preview_store, product, upload_images=True)
    assert first['id'] == again['id'] and again['duplicate'] is True
    path = f"/api/stores/{preview_store['id']}/commerce/prepare-product"
    assert owner.post(path, json={'product_id': product['id'], 'upload_images': False}).status_code == 409
    assert owner.post(f"/api/stores/{preview_store['id']}/commerce/logout", json={}).status_code == 409


def test_preview_executes_only_prepare_and_does_not_create_listing_or_change_stock(owner, preview_store, product, monkeypatch):
    item = queue(owner, preview_store, product, upload_images=False)
    calls = []
    def prepare(self, payload, job_id):
        calls.append((payload, job_id))
        return {'message': '填表预览结束，程序未点击提交审核', 'submitted_by_app': False}
    monkeypatch.setattr(commerce_browser.CommerceBrowser, 'prepare_product', prepare)
    monkeypatch.setattr(commerce_browser.CommerceBrowser, 'mutate_product', lambda *a: pytest.fail('No publish during preview'))
    worker = jobs.Worker()
    worker.process(worker.claim())
    assert calls[0][1] == item['id']
    assert db.one('SELECT status FROM jobs WHERE id=?', (item['id'],))['status'] == 'succeeded'
    assert db.one('SELECT count(*) n FROM listings')['n'] == 0
    assert db.get_product(product['id'])['skus'][0]['stock'] == 10


def test_product_changes_block_queued_preview_before_browser(owner, preview_store, product, monkeypatch):
    item = queue(owner, preview_store, product)
    with db.connection() as conn:
        conn.execute('UPDATE products SET version=version+1 WHERE id=?', (product['id'],))
    monkeypatch.setattr(commerce_browser.CommerceBrowser, 'prepare_product', lambda *a, **k: pytest.fail('Stale preview reached browser'))
    worker = jobs.Worker()
    worker.process(worker.claim())
    assert db.one('SELECT status FROM jobs WHERE id=?', (item['id'],))['status'] == 'blocked'


def test_false_upload_consent_never_passes_media_files_to_worker(preview_store, product, monkeypatch):
    payload = commerce_browser.prepare_payload(preview_store, product, {}, upload_images=False)
    payload['product']['image_ids'] = ['nonexistent-image-must-not-be-read']
    seen = []
    def run(command, **kwargs):
        seen.append(json.loads(kwargs['input']))
        return SimpleNamespace(returncode=0, stdout=json.dumps({'result': {'submitted_by_app': False}}))
    monkeypatch.setattr(commerce_browser, 'run_worker', run)
    monkeypatch.setattr(commerce_browser, 'browser_python', lambda: 'python')
    commerce_browser.CommerceBrowser(preview_store)._run('prepare_product', payload, job_id='a' * 32)
    assert seen[0]['media'] == {}
    assert seen[0]['payload']['upload_images'] is False
    assert 'credentials' not in seen[0]


def test_preview_close_cancels_only_its_queued_job(owner, preview_store, product):
    item = queue(owner, preview_store, product)
    result = owner.post(f"/api/jobs/{item['id']}/preview/close", json={})
    assert result.status_code == 200 and result.json()['closing'] is True
    assert db.one('SELECT status FROM jobs WHERE id=?', (item['id'],))['status'] == 'cancelled'
    _, stop = paths(db.DATA, item['id'])
    assert stop.is_file()
    with db.connection() as conn:
        conn.execute("UPDATE jobs SET kind='product_create',status='running' WHERE id=?", (item['id'],))
    assert owner.post(f"/api/jobs/{item['id']}/preview/close", json={}).status_code == 404
    assert owner.get(f"/api/jobs/{item['id']}/preview").status_code == 404
    assert db.one('SELECT status FROM jobs WHERE id=?', (item['id'],))['status'] == 'running'


def test_preview_progress_is_read_only_and_does_not_export_extra_fields(owner, preview_store, product):
    item = queue(owner, preview_store, product)
    marker, _ = paths(db.DATA, item['id'], create=True)
    write_marker(marker, {'ready': True, 'active': True, 'status': 'needs_input',
                         'filled': ['title'], 'needs_manual': ['请核对规格'], 'submitted_by_app': False,
                         'cookie': 'must-not-be-exported', 'html': '<private>'})
    result = owner.get(f"/api/jobs/{item['id']}/preview")
    assert result.status_code == 200
    assert result.json()['needs_manual'] == ['请核对规格']
    assert 'must-not-be-exported' not in result.text and '<private>' not in result.text
    assert read_marker(db.DATA, item['id'], running=False)['active'] is False


@pytest.mark.parametrize('status', ['blocked', 'failed', 'uncertain', 'cancelled'])
def test_terminal_preview_without_marker_reports_actual_failure(owner, preview_store, product, status):
    item = queue(owner, preview_store, product)
    with db.connection() as conn:
        conn.execute('UPDATE jobs SET status=?,result=? WHERE id=?',
                     (status, db.dumps({'message': '快手控件未唯一匹配，请等待页面加载后再试',
                                      'cookie': 'private-cookie-must-not-export'}), item['id']))
    response = owner.get(f"/api/jobs/{item['id']}/preview")
    assert response.status_code == 200
    result = response.json()
    assert result['job_status'] == status and result['status'] == status
    assert result['ready'] is False and result['active'] is False
    assert result['message'] == '快手控件未唯一匹配，请等待页面加载后再试'
    assert '正在准备' not in result['message']
    assert 'private-cookie-must-not-export' not in response.text


def test_failed_job_overrides_stale_active_preview_marker(owner, preview_store, product):
    item = queue(owner, preview_store, product)
    marker, _ = paths(db.DATA, item['id'], create=True)
    write_marker(marker, {'ready': True, 'active': True, 'status': 'needs_input',
                         'filled': ['title'], 'needs_manual': [], 'message': '正在准备'})
    with db.connection() as conn:
        conn.execute("UPDATE jobs SET status='failed',result=? WHERE id=?",
                     (db.dumps({'message': '浏览器已关闭 cookie=hidden-token'}), item['id']))
    response = owner.get(f"/api/jobs/{item['id']}/preview")
    assert response.json()['job_status'] == 'failed'
    assert response.json()['active'] is False and response.json()['ready'] is False
    assert 'hidden-token' not in response.text


def test_preview_marker_paths_refuse_traversal_and_symlinks(tmp_path):
    for invalid in ('../x', '', 'a/b', 'a' * 31, 'A' * 32):
        with pytest.raises(ValueError):
            paths(tmp_path, invalid, create=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / 'commerce-previews').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        request_close(tmp_path, 'a' * 32)
