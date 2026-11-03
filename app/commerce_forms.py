"""Read product requirements from the merchant DOM and fill reviewed values.

No private merchant HTTP endpoints, browser state, passwords or hidden form
values are exposed. A missing page, score or submission receipt fails closed.
"""
import copy
import hashlib
import json
import re
import time
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

try:
    from .commerce_profiles import PROFILES, trusted_url
    from .commerce_worker import Failure
except ImportError:
    from commerce_profiles import PROFILES, trusted_url
    from commerce_worker import Failure


ALIASES = {
    '商品标题': 'title', '商品名称': 'title', '商品描述': 'description',
    '商品主图': 'image_ids', '主图': 'image_ids', '商品详情图': 'detail_image_ids',
    '详情图': 'detail_image_ids', '品牌': 'brand', '主要材质': 'material',
    '材质': 'material', '闭合方式': 'closure', '商品类目': 'category_path',
    '类目': 'category_path', '运费模板': 'freight_template', '运费模板名称': 'freight_template',
    '商品短标题': 'short_title', '商品卖点': 'selling_points', '商品规格': 'skus',
    '价格库存': 'skus',
}
STORE_FIELDS = {'category_path', 'category_id', 'freight_template', 'freight_id', 'shipping_template'}
SENSITIVE = re.compile(r'密码|验证码|手机号|手机号码|收货人|收货地址|身份证|cookie|token|secret|password|authorization', re.I)

# The script reads visible product form controls only. It does not inspect
# application globals, hidden fields, browser storage, network or script tags.
SCAN = r"""() => {
 const shown=e=>!!e && !!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)
                  && getComputedStyle(e).visibility!=='hidden';
 const trim=v=>(v||'').replace(/\s+/g,' ').trim();
 const root=document.querySelector('main,[role=main]')||document.querySelector('form');
 if(!root)return {groups:[],unidentified:0};
 let groups=Array.from(root.querySelectorAll('.kwaishop-goods-nexus-pc-formily-item,.ant-form-item,[data-field]'));
 if(!groups.length)groups=Array.from(root.querySelectorAll('label')).map(e=>e.parentElement);
 const results=[],seen=new Set();let unidentified=0;
 for(const group of groups.slice(0,250)){
   if(!shown(group))continue;
   const controls=Array.from(group.querySelectorAll('input,textarea,select,[role=combobox],[class*=pro-checkableTag]'))
     .filter(e=>!['hidden','password','submit','button'].includes(e.type) && (e.type==='file'||shown(e)));
   if(!controls.length)continue;
   const nested=Array.from(group.querySelectorAll('.kwaishop-goods-nexus-pc-formily-item,.ant-form-item'));
   if(nested.some(e=>shown(e)&&e.querySelector('input,textarea,select,[role=combobox]')))continue;
   const labelNode=group.querySelector('[class*=formily-item-label],.ant-form-item-label,label');
   let label=trim(labelNode?.textContent||group.getAttribute('data-label')||controls[0].getAttribute('aria-label'))
      .replace(/^[*＊\s]+/,'').replace(/[：:]$/,'').trim();
   if(!label || label.length>80){if(controls.some(e=>e.required||e.getAttribute('aria-required')==='true'))unidentified++;continue;}
   if(seen.has(label))continue;
   seen.add(label);
   const first=controls[0], files=controls.filter(e=>e.type==='file');
   const selected=group.querySelector('[class*=select-selection-item]');
   const chips=controls.filter(e=>e.title&&e.className?.includes('pro-checkableTag'));
   const radios=controls.filter(e=>e.type==='radio');
   const radioLabel=e=>trim(e.closest('label')?.textContent||e.getAttribute('aria-label')||e.value);
   const options=radios.length?radios.filter(e=>!e.disabled||e.checked).map(e=>({value:radioLabel(e),label:radioLabel(e)})):
     chips.length?chips.map(e=>({value:trim(e.title||e.textContent),label:trim(e.title||e.textContent)})):
     first.tagName==='SELECT'?Array.from(first.options).filter(o=>o.value&&!o.disabled)
      .slice(0,200).map(o=>({value:o.value,label:trim(o.textContent)})):[];
   let type=files.length?'images':radios.length||chips.length?'select':first.tagName==='TEXTAREA'?'textarea':
     first.tagName==='SELECT'||first.getAttribute('role')==='combobox'?'select':
     first.type==='number'||first.getAttribute('role')==='spinbutton'?'number':first.type==='checkbox'?'boolean':'text';
   let value=type==='images'?[]:type==='boolean'?first.checked:radios.length?
     (radios.find(e=>e.checked)?radioLabel(radios.find(e=>e.checked)):''):chips.length?
     trim(chips.find(e=>e.className.includes('__checked'))?.title):
     selected?trim(selected.getAttribute('title')||selected.textContent):first.value||'';
   if(typeof value==='string'&&/^请选择/.test(value))value='';
   const required=controls.some(e=>e.required||e.getAttribute('aria-required')==='true')||
     /^[\s*＊]*[*＊]/.test(group.textContent||'')||!!group.querySelector('[class*=required]');
   const descriptor={label,type,required,options,value,chips:chips.length>0,radios:radios.length>0,placeholder:first.getAttribute('placeholder')||'',
     control_count:controls.filter(e=>e.type!=='file').length,file_multiple:files.some(e=>e.multiple),
     selector_kind:group.classList.contains('kwaishop-goods-nexus-pc-formily-item')?'formily':
                   group.classList.contains('ant-form-item')?'ant':'label',
     control_tag:first.tagName.toLowerCase(),control_type:first.type||'',
     max_length:first.maxLength>0?first.maxLength:null,min:first.min||null,max:first.max||null};
   results.push(descriptor);
 }
 return {groups:results,unidentified};
}"""


def source_url(platform, value):
    parsed = urlsplit(trusted_url(platform, value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


def _scope(page, field):
    loc = field.get('locator', {})
    label = field['label']
    kind = loc.get('kind')
    if kind in ('formily', 'ant'):
        css = '.kwaishop-goods-nexus-pc-formily-item' if kind == 'formily' else '.ant-form-item'
        group = page.locator(css).filter(has_text=re.compile(r'^\s*[*＊]?\s*' + re.escape(label)))
        # Nested attribute groups are excluded by their leading label.
        return group.filter(visible=True)
    return page.get_by_label(label, exact=True)


def _one(locator):
    locator = locator.filter(visible=True)
    locator.wait_for(state='visible', timeout=15000)
    if locator.count() != 1:
        raise Failure('blocked', '平台字段控件不能唯一识别，已停止，请重新读取页面要求')
    return locator


def collect(page, platform, *, enumerate_options=True):
    """Return a bounded, observed form schema; never claim future steps read."""
    url = source_url(platform, page.url)
    observed = page.evaluate(SCAN)
    fields = []
    for raw in observed.get('groups', []):
        label = raw['label']
        if SENSITIVE.search(label):
            continue
        semantic = ALIASES.get(label, '')
        typ = 'skus' if semantic == 'skus' else raw['type']
        if semantic == 'category_path':
            typ = 'multiselect'
        key = 'field_' + hashlib.sha256((label + '\0' + typ).encode()).hexdigest()[:16]
        field = {'key': key, 'label': label, 'type': typ, 'required': raw['required'],
                 'semantic': semantic, 'scope': 'store' if semantic in STORE_FIELDS or not semantic else 'common',
                 'options': raw['options'], 'value': raw['value'], 'observed': True,
                 'max_length': raw.get('max_length'), 'locator': {'kind': raw['selector_kind'],
                 'control_tag': raw['control_tag'], 'control_type': raw['control_type'],
                 'placeholder': raw['placeholder'], 'file_multiple': raw['file_multiple'], 'chips': raw.get('chips', False), 'radios': raw.get('radios', False)}}
        if field.get('max_length') is None:
            field.pop('max_length', None)
        for number in ('min', 'max'):
            if raw.get(number) not in (None, ''):
                try:
                    field[number] = float(raw[number])
                except ValueError:
                    pass
        if typ in ('number', 'integer') and field.get('value') not in (None, ''):
            try:
                field['value'] = float(field['value'])
            except (TypeError, ValueError):
                field['value'] = ''
        if typ == 'select' and not field['options'] and enumerate_options:
            try:
                scope = _scope(page, field)
                control = scope if raw['selector_kind'] == 'label' else scope.get_by_role('combobox')
                _one(control).click(timeout=5000)
                options = page.get_by_role('option').filter(visible=True)
                if options.count() <= 200:
                    values = options.evaluate_all("els=>els.map(e=>({value:e.getAttribute('data-value')||e.getAttribute('value')||e.textContent.trim(),label:e.textContent.trim()}))")
                    field['options'] = [x for x in values if x['value'] and len(x['label']) <= 200]
                control.press('Escape')
            except Exception:
                field['help'] = '平台选项尚未完全展开，可填写后重新读取下一步要求'
        if semantic == 'image_ids':
            field.update(min=3 if platform == 'kuaishou' else 1, max=9 if platform == 'kuaishou' else 30)
        if semantic == 'category_path':
            field.update(value=[], help='按实际类目路径逐级填写；不同平台分别保存')
        if semantic == 'skus':
            field['help'] = '规格、价格和库存将逐项核对；新建商品库存为零'
        fields.append(field)
    has_title = any(f['semantic'] == 'title' for f in fields)
    stage = 'product_details' if has_title else 'category' if any(f['semantic'] == 'category_path' for f in fields) else 'unidentified'
    # A structured SKU table and all mandatory field controls must be readable.
    sku_fields = [f for f in fields if f['semantic'] == 'skus' and f['label'] == '价格库存']
    sku_rows = _scope(page, sku_fields[0]).locator('[data-row-key],tr[role="row"],tbody tr').filter(visible=True) if sku_fields else page.locator('[data-publish-sku-row]').filter(visible=True)
    sku_contract = sku_rows.count() > 0
    complete = bool(has_title and fields and observed.get('unidentified', 0) == 0 and sku_contract)
    return {'complete': complete, 'stage': stage, 'fields': fields,
            'source_url': url, 'observed_at': int(time.time()),
            'message': '已读取当前商品页面要求' if complete else '已读取当前阶段；填写并回填后继续读取后续必填项'}


def quality(page, platform, fingerprint):
    """Only platform-rendered diagnosis text can provide a numerical score."""
    areas = page.locator('aside,[role="complementary"]').filter(
        has_text=re.compile('实时诊断结果|商品信息诊断|商品质量分|商品评分'))
    texts = []
    for i in range(min(areas.count(), 5)):
        if areas.nth(i).is_visible():
            texts.append(areas.nth(i).inner_text()[:5000])
    scores = set()
    evidence = ''
    for text in texts:
        if re.search('计算中|诊断中|正在生成|评分中', text):
            continue
        found = re.findall(r'(?<![\d.])(100(?:\.0+)?|[0-9]{1,2}(?:\.\d+)?)\s*分(?!钟)', text)
        if len(set(found)) == 1:
            scores.add(float(found[0]))
            evidence = text[:500]
    result = {'source': 'platform_dom', 'score': None, 'fingerprint': fingerprint,
              'observed_at': int(time.time()), 'source_url': source_url(platform, page.url)}
    if len(scores) == 1:
        result.update(score=scores.pop(), label='平台商品质量分', evidence=evidence)
    else:
        result['label'] = '尚未读取到唯一、已完成的平台评分'
    return result


def _open_update(runner, external):
    configured = runner.config.get('edit_url')
    if runner.platform == 'kuaishou':
        try:
            from .kuaishou_submission import EDIT_URL
        except ImportError:
            from kuaishou_submission import EDIT_URL
        runner.navigate(EDIT_URL + '?' + urlencode({'itemId': external}))
        return
    if isinstance(configured, str) and '{external_id}' in configured:
        runner.navigate(configured.replace('{external_id}', quote(str(external), safe='')))
        return
    # Last resort: the linked row must expose exactly one edit control.
    row = runner.page.locator('tr,li,[role="row"]').filter(has_text=external).filter(visible=True)
    if row.count() != 1:
        raise Failure('blocked', '未唯一找到已关联商品的编辑行；请配置 edit_url 或手动核对后台')
    control = row.get_by_role('link', name=re.compile('编辑|修改')).or_(
        row.get_by_role('button', name=re.compile('编辑|修改'))).filter(visible=True)
    if control.count() != 1:
        raise Failure('blocked', '已关联商品行没有唯一编辑入口，未改写任何商品')
    control.click(timeout=15000)
    runner.page.wait_for_load_state('domcontentloaded')


def open_editor(runner):
    runner.navigate(PROFILES[runner.platform]['portal'])
    deadline = time.monotonic() + 25
    while not runner.logged_in():
        if time.monotonic() >= deadline:
            raise Failure('session', '请先在目标店铺完成电商后台登录')
        runner.page.wait_for_timeout(500)
    mode = runner.payload.get('mode', 'create')
    if mode == 'update':
        external = str(runner.payload.get('external_id') or
                       (runner.payload.get('listing') or {}).get('external_id') or '')
        if not external:
            raise Failure('blocked', '更新商品缺少平台商品 ID')
        _open_update(runner, external)
        return
    if runner.platform == 'kuaishou':
        try:
            from .kuaishou_commerce import CREATE_URL
        except ImportError:
            from kuaishou_commerce import CREATE_URL
        runner.navigate(CREATE_URL)
        runner.page.get_by_text('添加商品', exact=True).wait_for(state='visible', timeout=20000)
        return
    configured = runner.config.get('create_url')
    if configured:
        runner.navigate(configured)
        return
    # Use actual portal controls, not guessed platform API or create routes.
    for name in ('新增商品', '发布商品', '商品发布'):
        options = runner.page.get_by_role('link', name=name, exact=True).or_(
            runner.page.get_by_role('button', name=name, exact=True)).filter(visible=True)
        if options.count() != 1:
            continue
        before = list(runner.context.pages)
        options.click(timeout=15000)
        runner.page.wait_for_timeout(500)
        new = [p for p in runner.context.pages if p not in before]
        if len(new) == 1:
            runner.page = new[0]
            runner.page.wait_for_load_state('domcontentloaded')
        runner.assert_origin()
        return
    raise Failure('blocked', '当前平台尚未识别到唯一的商品发布入口，请先在后台完成页面接入')


def _values_by_semantic(payload):
    values = payload.get('values', {})
    result = {}
    for f in payload.get('schema', {}).get('fields', []):
        semantic = f.get('semantic') or f.get('canonical')
        if semantic and f.get('key', f.get('id')) in values:
            result[semantic] = values[f.get('key', f.get('id'))]
    return result


def fill(page, schema, values, *, media, upload_images, guard, handled_semantics=()):
    missing = []
    for f in schema.get('fields', []):
        # A platform adapter may already have uploaded or filled this field.
        # Replaying uploads appends duplicate images in the merchant SPA.
        if f.get('semantic') in handled_semantics:
            continue
        key, typ = f['key'], f['type']
        value = values.get(key)
        if value in (None, '', []):
            if f.get('required'):
                missing.append({'key': key, 'label': f['label'], 'message': '请在本地补充此平台要求'})
            continue
        if typ == 'skus':
            missing.append({'key': key, 'label': f['label'],
                            'message': '当前平台规格回填与回读尚未适配，不能跳过价格和规格直接提交'})
            continue
        if f.get('semantic') == 'category_path':
            missing.append({'key': key, 'label': f['label'],
                            'message': '当前平台类目选择尚未适配，请核对完整类目路径'})
            continue
        guard()
        try:
            area = _scope(page, f)
            if typ == 'images':
                if not upload_images:
                    missing.append({'key': key, 'label': f['label'], 'message': '尚未确认上传商品图片'})
                    continue
                if not isinstance(value, list) or any(mid not in media for mid in value):
                    raise Failure('blocked', '图片只能选用本地商品仓库已上传的素材')
                control = area.locator('input[type="file"][multiple]')
                if control.count() != 1:
                    raise Failure('blocked', '图片控件不能唯一识别')
                control.set_input_files([media[mid] for mid in value])
            elif typ == 'select':
                if f.get('locator', {}).get('radios'):
                    checked = area.locator('input[type="radio"]:checked')
                    if checked.count() == 1 and checked.evaluate("e=>(e.closest('label')?.textContent||e.getAttribute('aria-label')||e.value).replace(/\\s+/g,' ').trim()") == str(value):
                        continue
                    radio = _one(area.get_by_role('radio', name=str(value), exact=True))
                    if not radio.is_checked():
                        radio.check()
                    continue
                if f.get('locator', {}).get('chips'):
                    choice = _one(area.locator('[title]').filter(has_text=re.compile('^' + re.escape(str(value)) + '$')))
                    if '__checked' not in (choice.get_attribute('class') or ''):
                        choice.click()
                    if '__checked' not in (choice.get_attribute('class') or ''):
                        raise Failure('blocked', '平台选项未能读回选中状态')
                    continue
                control = area if f['locator']['kind'] == 'label' else area.get_by_role('combobox')
                control = _one(control)
                if control.evaluate('e=>e.tagName') == 'SELECT':
                    control.select_option(str(value))
                else:
                    control.click()
                    option = next((o for o in f.get('options', []) if str(o['value']) == str(value)), None)
                    _one(page.get_by_role('option', name=(option or {}).get('label', str(value)), exact=True)).click()
            elif typ == 'boolean':
                control = area if f['locator']['kind'] == 'label' else area.get_by_role('checkbox')
                _one(control).set_checked(bool(value))
            else:
                control = area if f['locator']['kind'] == 'label' else area.locator('input:not([type="hidden"]):not([type="file"]),textarea')
                _one(control).fill(str(value))
        except Failure:
            raise
        except Exception:
            missing.append({'key': key, 'label': f['label'], 'message': '页面控件变化或平台未接受，请重新读取当前要求'})
    return missing


def run(runner, action):
    payload = runner.payload
    mode = payload.get('mode', 'create')
    open_editor(runner)
    if action == 'collect_requirements':
        return collect(runner.page, runner.platform)
    runner.assert_identity()
    semantic = _values_by_semantic(payload)
    product = copy.deepcopy(payload['product'])
    for key in ('title', 'description', 'image_ids', 'detail_image_ids', 'skus'):
        if key in semantic:
            product[key] = semantic[key]
    if mode == 'create':
        for sku in product.get('skus', []):
            sku['stock'] = 0
    missing = []
    handled = set()
    workflow_action = 'product_update' if mode == 'update' else 'product_create'
    workflows = runner.config.get('workflows', {})
    if not isinstance(workflows, dict):
        raise Failure('blocked', '网页工作流配置必须是 JSON 对象')
    workflow = workflows.get(workflow_action)
    result_config = workflow.get('result') if isinstance(workflow, dict) else None
    has_receipt_workflow = isinstance(result_config, dict) and bool(result_config.get('external_id'))
    if has_receipt_workflow:
        handled.update(('skus', 'category_path'))  # Explicit workflow owns these structural controls.
    if runner.platform == 'kuaishou' and mode == 'create':
        try:
            from .kuaishou_commerce import prepare, PrepareBlocked
            from .kuaishou_submission import fill_skus
        except ImportError:
            from kuaishou_commerce import prepare, PrepareBlocked
            from kuaishou_submission import fill_skus
        config = copy.deepcopy(runner.config)
        defaults = payload.get('business_defaults', {})
        config['business'] = {**defaults, **{k: v for k, v in semantic.items() if k in ('category_path', 'brand', 'material', 'closure', 'freight_template')}}
        # The observed-column SKU adapter replaces the preview-only positional fallback.
        config['defer_skus'] = True
        try:
            prepared = prepare(runner.page, {**payload, 'product': product}, runner.request.get('media', {}), config)
            handled.update(prepared.get('filled', []))
            missing.extend(prepared.get('missing', []))
            sku_result = fill_skus(runner.page, product, runner.assert_identity)
            handled.update(sku_result.get('filled', []))
            missing.extend(sku_result.get('missing', []))
        except PrepareBlocked as exc:
            raise Failure('blocked', str(exc)) from None
    schema = collect(runner.page, runner.platform)
    missing.extend(fill(runner.page, schema, payload.get('values', {}),
                        media=runner.request.get('media', {}), upload_images=payload.get('upload_images') is True,
                        guard=runner.assert_identity, handled_semantics=handled))
    if action == 'prepare_workbench':
        checker = runner.page.get_by_role('button', name='填写检查', exact=True).filter(visible=True)
        if checker.count() == 1:
            checker.click()
            runner.page.wait_for_timeout(750)
        schema = collect(runner.page, runner.platform)
        observed_quality = quality(runner.page, runner.platform, payload['fingerprint'])
        if (observed_quality['score'] is None and not payload.get('require_platform_quality')
                and not (runner.platform == 'kuaishou' and mode == 'create')):
            # An absent platform score is different from invalid score evidence.
            # Local quality remains separately enforced by the draft service.
            observed_quality = {}
        return {'schema': schema, 'quality': observed_quality,
                'missing': missing, 'message': '已按本地资料回填并读取平台要求；尚未提交审核'}
    # Other platforms require a configured receipt workflow. Kuaishou has a
    # built-in adapter below; the generic form scanner alone cannot establish
    # remote product identity.
    review = quality(runner.page, runner.platform, payload['fingerprint'])
    if review['score'] is not None and review['score'] <= 90:
        raise Failure('blocked', '平台真实评分未高于90分，已停止')
    require_quality = (payload.get('require_platform_quality') is True
                       or (runner.platform == 'kuaishou' and mode == 'create')
                       or review['score'] is not None)
    if require_quality and review['score'] is None:
        raise Failure('blocked', '提交前未读取到当前商品的平台真实评分，已停止')
    unresolved = [x for x in missing if x.get('field') not in ('merchant_review',)]
    if unresolved or not schema['complete']:
        raise Failure('blocked', '平台表单仍有未完成或未识别的必填项，未提交审核')
    if not has_receipt_workflow:
        if runner.platform == 'kuaishou' and mode == 'create':
            try:
                from .kuaishou_submission import submit as submit_kuaishou
            except ImportError:
                from kuaishou_submission import submit as submit_kuaishou
            # Built-in Kuaishou receipt verification; no user DOM workflow is needed.
            return submit_kuaishou(
                runner.page, runner.context, {**payload, 'product': product}, runner.assert_identity,
                lambda: quality(runner.page, runner.platform, payload['fingerprint']),
                on_commit=lambda: setattr(runner, 'write_started', True))
        try:
            from .generic_submission import submit as submit_generic
        except ImportError:
            from generic_submission import submit as submit_generic
        return submit_generic(runner.page, {**payload, 'product': product,
                                           'require_platform_quality': require_quality}, runner.assert_identity,
                              read_quality=lambda: quality(runner.page, runner.platform, payload['fingerprint']),
                              on_commit=lambda: setattr(runner, 'write_started', True))
    # Reuse the existing receipt-verified execution path, rechecking the score
    # in Runner.steps immediately before the marked commit click.
    from decimal import Decimal
    for sku in product.get('skus', []):
        sku['price'] = format(Decimal(sku['price_cents']) / 100, '.2f')
        if mode == 'update':
            sku.pop('stock', None)  # Warehouse balances never overwrite remote stock.
    runner.payload = {**payload, 'product': product, 'publish_config': payload.get('publish_config', {}),
                      'quality_gate': {'minimum': 90, 'fingerprint': payload['fingerprint']}}
    original_action = runner.action
    try:
        runner.action = workflow_action
        result = runner.operate()
    finally:
        runner.action = original_action
    return {**result, 'quality': review}
