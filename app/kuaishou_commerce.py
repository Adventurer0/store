"""Conservative preparation for the observed Kuaishou merchant create page.

This adapter prepares a reviewable form without clicking submission or draft
save actions; the platform may autosave its forms. Its default controls follow
the observed Nexus title, category, uploads, custom choices and selects. Optional
selectors are installed by the application after inspecting the actual UI,
rather than entered as a workflow by a merchant. Unsupported or ambiguous
controls remain explicit manual inputs.
"""
from decimal import Decimal
from pathlib import Path
import re
import time
from urllib.parse import urlsplit


CREATE_URL = 'https://s.kwaixiaodian.com/zone/goods/nexus/self/release/add'
TITLE_PLACEHOLDER = '最多输入30个汉字（60个字符）'
NEXT_BUTTON = '下一步，完善商品信息'
FORM_ITEM = '.kwaishop-goods-nexus-pc-formily-item'
BUSINESS_FIELDS = {
    'brand': '品牌', 'material': '主要材质', 'closure': '闭合方式',
    'freight_template': '运费模板',
}
FORBIDDEN_ACTION = re.compile(r'提交|保存|发布|审核')


class PrepareBlocked(Exception):
    kind = 'blocked'


def _target(root, spec):
    """The same declarative locator vocabulary as the merchant worker."""
    if isinstance(spec, str):
        return root.locator(spec)
    if not isinstance(spec, dict) or not spec:
        raise PrepareBlocked('快手控件适配格式无效')
    if 'css' in spec:
        return root.locator(spec['css'])
    if 'test_id' in spec:
        return root.get_by_test_id(spec['test_id'])
    if 'label' in spec:
        return root.get_by_label(spec['label'], exact=True)
    if 'placeholder' in spec:
        return root.get_by_placeholder(spec['placeholder'], exact=True)
    if 'role' in spec:
        return root.get_by_role(spec['role'], name=spec.get('name'), exact=True)
    if 'text' in spec:
        return root.get_by_text(spec['text'], exact=True)
    raise PrepareBlocked('快手控件适配缺少已核验的选择器')


def _resolved(locator, *, visible=True, timeout=15000):
    # DOMContentLoaded precedes Nexus's React header/form rendering. Wait for
    # the expected control, then enforce uniqueness among visible matches.
    # File inputs are intentionally hidden and use attached-only resolution.
    if visible:
        locator = locator.filter(visible=True)
    if locator.count() > 1:
        raise PrepareBlocked('快手控件未唯一匹配，需按当前页面更新内置适配')
    try:
        locator.wait_for(state='visible' if visible else 'attached', timeout=timeout)
    except Exception:
        raise PrepareBlocked('等待快手页面控件加载超时，请确认商家页面已打开') from None
    if locator.count() != 1:
        raise PrepareBlocked('快手控件未唯一匹配，需按当前页面更新内置适配')
    return locator


def _unique(root, spec, *, visible=True, timeout=15000):
    return _resolved(_target(root, spec), visible=visible, timeout=timeout)


class _Preparation:
    def __init__(self, page, payload, media, config):
        self.page, self.payload, self.media, self.config = page, payload, media, config
        self.product = payload.get('product', {})
        self.selectors = config.get('selectors', {})
        self.business = config.get('business', {})
        self.filled, self.missing = [], []
        if not isinstance(self.selectors, dict) or not isinstance(self.business, dict):
            raise PrepareBlocked('快手内置适配配置必须是对象')

    def guard(self):
        url = urlsplit(self.page.url)
        if (url.scheme != 'https' or url.hostname != 's.kwaixiaodian.com'
                or url.port not in (None, 443) or url.username or url.password
                or url.path.rstrip('/') != urlsplit(CREATE_URL).path):
            raise PrepareBlocked('快手填表预览只能操作当前商家新增商品页面')
        identity = self.config.get('identity')
        if (not isinstance(identity, dict) or not identity.get('target')
                or not isinstance(identity.get('value'), str) or not identity['value'].strip()):
            raise PrepareBlocked('请先核对并绑定当前快手店铺身份')
        value = _unique(self.page, identity['target'], timeout=20000).inner_text().strip()
        if value != identity['value'].strip():
            raise PrepareBlocked('当前快手店铺身份与目标店铺不一致，未继续填表')

    def need(self, field, label, reason):
        if field not in {item['field'] for item in self.missing}:
            self.missing.append({'field': field, 'label': label, 'reason': reason})

    def scope(self, label):
        # Verified Nexus form items include their own label at the beginning;
        # prefix anchoring excludes the outer 商品属性 container and its children.
        return self.page.locator(FORM_ITEM).filter(has_text=re.compile(r'^\s*\*?\s*' + re.escape(label)))

    def field(self, name, label, value, spec=None, *, upload=False, target=None):
        self.guard()
        try:
            if target is None:
                target = _unique(self.page, spec, visible=not upload)
            else:
                target = _resolved(target, visible=not upload)
        except PrepareBlocked:
            self.need(name, label, '当前页面控件尚未唯一识别，需人工核对')
            return False
        if upload:
            if self.payload.get('upload_images') is not True:
                self.need(name, label, '需要你明确允许将本地商品图片上传到快手，当前只做不上传图片的预览')
                return False
            if (target.get_attribute('type') or '').lower() != 'file':
                raise PrepareBlocked('商品图片只能上传到已核验的文件控件')
            try:
                target.set_input_files(value, timeout=30000)
            except Exception:
                self.need(name, label, '图片控件未完成接收，请在预览页核对上传状态')
                return False
        else:
            try:
                target.fill(str(value), timeout=15000)
            except Exception:
                self.need(name, label, '控件未完成填写，请在预览页核对')
                return False
        self.filled.append(name)
        return True

    def click(self, target):
        self.guard()
        target = _resolved(target)
        # Protect against a stale selector resolving to a final action button.
        if FORBIDDEN_ACTION.search(target.inner_text().strip()):
            raise PrepareBlocked('填表预览不能提交审核、发布商品或保存草稿')
        target.click(timeout=15000)

    def image_paths(self, key, minimum, maximum):
        ids = self.product.get(key, [])
        if not isinstance(ids, list) or not minimum <= len(ids) <= maximum:
            return None
        paths = []
        for mid in ids:
            raw = self.media.get(mid)
            if not isinstance(raw, str):
                raise PrepareBlocked('图片只能引用此商品已上传的素材')
            path = Path(raw)
            if not path.is_absolute() or not path.is_file() or path.is_symlink():
                raise PrepareBlocked('商品图片文件不存在或路径不安全')
            if path.suffix.lower() not in ('.jpg', '.jpeg', '.png') or path.stat().st_size > 5 * 1024 * 1024:
                raise PrepareBlocked('快手商品图片须为不超过 5MB 的 JPG / PNG')
            paths.append(str(path))
        return paths

    def images_and_category(self):
        title = _target(self.page, {'placeholder': TITLE_PLACEHOLDER}).filter(visible=True)
        if title.count() == 1:
            return True  # The visible page has already advanced to step two.
        ids = self.product.get('image_ids', [])
        if not isinstance(ids, list) or not 3 <= len(ids) <= 9:
            self.need('image_ids', '商品主图', '快手当前发布页要求 3–9 张商品主图')
            return False
        if self.payload.get('upload_images') is not True:
            self.need('image_ids', '商品主图', '需要你明确允许将本地商品图片上传到快手，当前停留在主图 / 类目步骤')
            return False
        images = self.image_paths('image_ids', 3, 9)
        image_spec = self.selectors.get('main_images')
        image_scope = self.scope('商品主图')
        if not image_spec:
            if image_scope.count() != 1:
                self.need('image_ids', '商品主图', '主图区域尚未唯一识别')
                return False
            existing = re.search(r'上传图片\(\s*(\d+)\s*/\s*9\s*\)', image_scope.inner_text())
            if not existing or int(existing[1]) != 0:
                self.need('image_ids', '商品主图', '当前页面已有主图或无法确认空白状态，未重复上传')
                return False
        image_target = None if image_spec else image_scope.locator('input[type="file"][multiple]')
        if not self.field('image_ids', '商品主图', images, image_spec,
                          upload=True, target=image_target):
            return False
        # set_input_files resolves before the SPA has uploaded and accepted its
        # images. Observe the actual Nexus upload count rather than a fixed sleep.
        if not image_spec:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                self.guard()
                try:
                    count = re.search(r'上传图片\(\s*(\d+)\s*/\s*9\s*\)', image_scope.inner_text(timeout=1000))
                    if count and int(count[1]) == len(images):
                        break
                except Exception:
                    pass
                self.page.wait_for_timeout(250)
            else:
                self.need('image_ids', '商品主图', '图片仍在上传或平台处理中，请等待上传完成后再进入下一步')
                return False
        category_path = self.business.get('category_path')
        if not isinstance(category_path, list) or not category_path or any(not isinstance(x, str) or not x.strip() for x in category_path):
            self.need('category_path', '商品类目', '请确认商品在快手后台的完整类目路径')
            return False
        # The observed create page exposes a recent category chip. Use it only
        # when unique, then verify the complete breadcrumb before advancing.
        try:
            category = _unique(self.page, self.selectors.get('category', {'text': category_path[-1]}))
            self.click(category)
            breadcrumb = self.selectors.get('category_selected')
            if breadcrumb:
                selected = _unique(self.page, breadcrumb).inner_text()
            else:
                selected_path = self.scope('商品类目').locator('[class^="selectedArea___"]')
                if selected_path.count() != 1:
                    raise PrepareBlocked('快手完整类目路径不能唯一识别，未进入下一步')
                selected = selected_path.inner_text()
            normalized = re.sub(r'\s+', '', selected).replace('＞', '>')
            if '>'.join(category_path) not in normalized:
                raise PrepareBlocked('快手当前类目路径与确认资料不一致')
            self.filled.append('category_path')
            self.click(_unique(self.page, {'role': 'button', 'name': NEXT_BUTTON}))
            _target(self.page, {'placeholder': TITLE_PLACEHOLDER}).wait_for(state='visible', timeout=20000)
            return True
        except PrepareBlocked:
            raise
        except Exception:
            self.need('category_path', '商品类目', '第一步尚未通过平台校验，请在预览页核对主图与类目')
            return False

    def business_fields(self):
        for name, label in BUSINESS_FIELDS.items():
            value = self.business.get(name)
            if not isinstance(value, str) or not value.strip():
                self.need(name, label, '请提供已确认的商品业务资料')
                continue
            spec = self.selectors.get(name)
            self.guard()
            try:
                scope = self.scope(label)
                if spec:
                    control = _unique(self.page, spec)
                elif name == 'closure':
                    if scope.count() != 1:
                        raise PrepareBlocked('闭合方式字段范围不唯一')
                    control = scope.locator('[title]').filter(has_text=re.compile('^' + re.escape(value) + '$'))
                    if control.count() != 1 or control.get_attribute('title') != value:
                        raise PrepareBlocked('闭合方式选项不唯一')
                    if '__checked' not in (control.get_attribute('class') or ''):
                        self.click(control)
                    if '__checked' not in (control.get_attribute('class') or ''):
                        raise PrepareBlocked('闭合方式没有读回选中状态')
                    self.filled.append(name)
                    continue
                else:
                    if scope.count() != 1:
                        raise PrepareBlocked('业务字段范围不唯一')
                    control = scope.get_by_role('combobox')
                    if control.count() != 1:
                        raise PrepareBlocked('业务字段控件不唯一')
                self.click(control)
                self.click(_unique(self.page, {'role': 'option', 'name': value}))
                selection = scope.locator('.kwaishop-goods-nexus-pc-select-selection-item')
                if (selection.count() != 1 or selection.inner_text().strip() != value
                        or selection.get_attribute('title') != value):
                    raise PrepareBlocked('下拉选项没有读回唯一选中值')
                self.filled.append(name)
            except Exception:
                self.need(name, label, '控件或所选值不能唯一确认，请人工核对')

    def skus(self):
        skus = self.product.get('skus', [])
        contract = self.selectors.get('sku_fields')
        if not isinstance(skus, list) or not skus:
            self.need('skus', '商品规格', '商品至少需要一个本地规格')
            return
        # Generic positional mapping is intentionally unsupported. A verified
        # single-row default specification may be installed by the adapter.
        if len(skus) != 1 or not isinstance(contract, dict) or not contract.get('single_default'):
            self.need('skus', '商品规格', '需核对平台颜色 / 尺寸与本地规格，不会按表格位置猜测')
            return
        if any(not contract.get(key) for key in ('code', 'price', 'stock')):
            self.need('skus', '商品规格', '单规格的编码、价格和库存控件尚未完整适配')
            return
        sku = skus[0]
        if not isinstance(sku.get('price_cents'), int) or isinstance(sku['price_cents'], bool) or sku['price_cents'] < 0:
            raise PrepareBlocked('商品价格必须是非负整数分')
        # Resolve all three controls before changing any; inventory is always 0.
        try:
            targets = {key: _unique(self.page, contract[key]) for key in ('code', 'price', 'stock')}
        except PrepareBlocked:
            self.need('skus', '商品规格', '规格控件未唯一匹配，未按位置猜测填写')
            return
        values = {'code': sku['code'], 'price': format(Decimal(sku['price_cents']) / 100, '.2f'), 'stock': '0'}
        for key, target in targets.items():
            self.guard()
            try:
                target.fill(str(values[key]), timeout=15000)
            except Exception:
                self.need('skus', '商品规格', '规格填写未完成，请人工核对价格、编码及零库存')
                return
        if targets['stock'].input_value().strip() != '0':
            raise PrepareBlocked('新建商品库存未能确认为零，不能继续预览')
        self.filled.append('skus')

    def run(self):
        self.guard()
        if self.payload.get('listing', {}).get('external_id'):
            raise PrepareBlocked('已有平台商品不能作为新建商品填表预览')
        title = self.product.get('title')
        if not isinstance(title, str) or not title.strip():
            raise PrepareBlocked('请先填写商品标题')
        if not self.images_and_category():
            return self.result()
        self.field('title', '商品标题', title, {'placeholder': TITLE_PLACEHOLDER})
        self.business_fields()
        details = (self.image_paths('detail_image_ids', 1, 50)
                   if self.payload.get('upload_images') is True else None)
        if details:
            detail_spec = self.selectors.get('detail_images')
            detail_target = None if detail_spec else self.scope('商品详情图').locator('input[type="file"][multiple]')
            self.field('detail_image_ids', '商品详情图', details, detail_spec, upload=True, target=detail_target)
        else:
            self.need('detail_image_ids', '商品详情图', '详情图上传控件尚未适配或没有商品详情图片')
        if not self.config.get('defer_skus'):
            self.skus()
        # A filled preview is deliberately not a statement that platform
        # qualifications, shipping obligations or final validation are complete.
        self.need('merchant_review', '平台最终检查', '请核对完整商品属性、规格、服务承诺和平台填写检查；本步骤不会提交审核')
        return self.result()

    def result(self):
        return {'status': 'needs_input' if self.missing else 'prepared', 'submitted': False,
                'filled': self.filled, 'missing': self.missing,
                'message': '已准备快手商品表单预览；程序未点击提交审核或保存草稿，平台可能自动暂存表单'}


def prepare(page, payload, media, config):
    """Fill verified create-page controls and return review requirements.

    ``media`` maps only this product's image IDs to absolute local image paths.
    ``config.identity`` binds the visible merchant name/ID; ``config.business``
    contains approved merchant facts. Optional ``config.selectors`` contains
    application-maintained selectors verified against the actual store UI.
    """
    if not isinstance(payload, dict) or not isinstance(media, dict) or not isinstance(config, dict):
        raise PrepareBlocked('快手填表预览参数无效')
    return _Preparation(page, payload, media, config).run()
