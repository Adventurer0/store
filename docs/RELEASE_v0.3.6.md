# ShopDock Local v0.3.6 · 新建商品报错修复

日期：2026-11-03。基于 v0.3.5 的前端修复，无数据库迁移、无后端接口变化。

## 问题

点击“新建商品”时提示：

```text
Cannot read properties of null (reading 'image_ids')
```

商品弹窗没有打开。

## 原因

`app/static/library.js` 的 `imageChoices(product = {}, media = [])` 只在参数为
`undefined` 时使用默认值；新建商品传入的是 `null`，默认值不会生效，随后直接读取
`product.image_ids` 触发 TypeError。

旧版没有加载素材库脚本时会走 `app.js` 里的 `p?.image_ids` 兜底，所以只在启用
`MediaLibrary` 的完整界面出现。

## 修复

- `imageChoices` 入口统一执行 `product = product || {}`，空商品也能渲染空的图片选择器。
- 新增界面回归用例：打开“新建商品”后弹窗存在、图片选择器存在、默认不勾选图片、标题为空。

## 涉及文件

- `app/static/library.js`
- `tests/test_library_ui.cjs`
- 版本号：`app/main.py`、`app/static/index.html`、`package.json`、`package-lock.json`、`tests/test_commerce_api.py`

## 验证

- 完整 pytest：**284 passed**（后端未改动）。
- jsdom 界面测试：**28 passed**（新增 1 项）。
- 验收实例真实 Chrome：登录 → 商品中心 → 新建商品，弹窗正常打开，图片选择器正常渲染、默认不勾选；填写临时商品并保存成功，列表出现该商品，随后已删除测试数据，控制台无错误。
- `node --check app/static/library.js`、`app.js`、`publishing.js`：通过。

## 边界

- 仅修复空商品对象的渲染；图片上传、商品保存字段未改动。
- 新建商品保存后仍按原逻辑进入本地商品库，不会自动发布到平台。
