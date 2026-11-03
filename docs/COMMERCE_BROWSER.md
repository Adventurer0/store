# 电商后台网页登录与页面工作流

普通使用者先看 [登录成功后为何仍显示待配置](PUBLISHING_HELP.md)。本文面向维护平台页面适配的技术人员，示例选择器不能直接用于真实店铺发布。

本版本的商品、订单、SKU 与库存路径使用商家网页和独立浏览器 Cookie，不再调用电商 Open API。视频发布仍使用其原有的独立账号通道。不要把视频创作者页面的扫码当成商家后台登录。

## 运行与登录

使用 Docker 完整镜像启动。店铺卡片的「电商后台登录」打开本平台商家网页，在扫码桌面完成扫码、短信或其他平台要求的人工验证，然后执行「检查电商登录」。登录有效后才能执行电商任务。「视频账号登录」是另一项操作。

六个平台入口为抖店 `fxg.jinritemai.com`、快手小店 `s.kwaixiaodian.com`、微信小店 `store.weixin.qq.com`、淘宝/天猫卖家中心 `myseller.taobao.com`、京东商家后台 `shop.jd.com`、拼多多商家后台 `mms.pinduoduo.com`。淘宝光合属于视频发布入口，不用于电商商品管理。

每个店铺保留独立 Chromium 用户目录：`APP_DATA/browser/commerce/<store-id>`。浏览器自行保存 HttpOnly Cookie、本地存储与平台会话；不会向前端导出 Cookie。视频的 `APP_DATA/mpau/cookies` 不受电商登录、失效和退出影响。退出电商登录会删除本店商家浏览器目录；如需撤销平台服务器上的设备会话，请使用平台账号管理。

在完整镜像以外运行，需要安装 Playwright 及 Chrome。默认复用 `/opt/mpau-venv/bin/python`；可通过 `COMMERCE_PYTHON` 指定已经安装 Playwright 的 Python。默认 Chrome 通道，可设置 `COMMERCE_BROWSER_CHANNEL=chromium` 使用已安装的 Playwright Chromium。手工扫码需要可见桌面，`COMMERCE_HEADLESS=1` 仅适合已登录后的自动检查或测试。

## 页面适配的实际边界

平台页面会按类目、账号权限、店铺类型和版本变化。项目提供真实的 DOM 控制引擎与六个平台独立登录，但不把未经账号验证的控件名称冒充已联调的平台连接器。每个店铺需在高级设置的 `commerce_browser` 中提供经过当前页面核对的声明式工作流；缺配置时预检会明确阻断，不会返回模拟成功。

首次使用测试店铺验证每个动作的目标控件、类目必填项、SKU 匹配及回执。下面是结构示例，包含 `__REQUIRED__` 的内容必须替换；示例不是六个平台的通用页面选择器。

## 配置结构

在完整模板 JSON 中使用 `commerce_browser` 键；如界面提供单独的网页规则输入框，填入该键对应的对象即可。店铺公共发布配置仍位于 `publish_defaults`，单个商品的配置位于 `listing.publish_config`，执行时合并为 `publish_config`。

```json
{
  "commerce_browser": {
    "logged_in": {"css": "#__REQUIRED_authenticated_navigation"},
    "login_required": {"css": "#__REQUIRED_login_panel"},
    "identity": {
      "target": {"css": "#__REQUIRED_current_shop_name"},
      "value": "实际商家后台显示的店铺名称"
    },
    "workflows": {
      "product_create": {
        "url": "/__REQUIRED_product_create_route",
        "steps": [
          {"op":"fill","target":{"label":"商品名称"},"value":"{{product.title}}"},
          {"op":"upload","target":{"css":"#__REQUIRED_main_image_input"},"value":"{{product.image_ids}}"},
          {"op":"each_sku","steps":[
            {"op":"fill","target":{"css":".__REQUIRED_sku_code","nth":"{{index}}"},"value":"{{sku.code}}"},
            {"op":"fill","target":{"css":".__REQUIRED_sku_price","nth":"{{index}}"},"value":"{{sku.price}}"},
            {"op":"fill","field":"stock","target":{"css":".__REQUIRED_sku_stock","nth":"{{index}}"},"value":"{{sku.stock}}"}
          ]},
          {"op":"click","target":{"role":"button","name":"保存"},"commit":true}
        ],
        "result": {
          "success":{"css":"#__REQUIRED_new_success_receipt"},
          "external_id":{"css":"#__REQUIRED_product_id","regex":"(\\d+)"},
          "rows":{"css":".__REQUIRED_saved_sku_row"},
          "fields": {
            "code":{"css":".__REQUIRED_external_sku_code"},
            "sku_id":{"css":".__REQUIRED_remote_sku_id"},
            "stock":{"css":".__REQUIRED_remote_stock","input":true}
          }
        }
      }
    }
  }
}
```

`logged_in` 指定已登录后才可见的控件。省略时使用商品/订单管理菜单的保守探针，同时要求实际有效 Cookie；自定义后台应显式配置此字段。`login_required` 是登录页可见的控件。`identity` 要求页面上唯一匹配的店铺名与 `value` 完全相同；所有写操作都必须配置它，在进入操作页和每次提交前再次核对，防止同账号切错店。

选择器可以是 CSS 字符串，或下列对象之一：

```json
{"css":".sku-input","nth":0}
{"role":"button","name":"保存","exact":true}
{"label":"商品名称"}
{"text":"保存成功"}
{"placeholder":"请输入商品名称"}
{"test_id":"product-title"}
```

默认必须唯一匹配。`nth` 从 0 开始，只在明确的 SKU 循环等场景使用，不能依靠行号猜测平台 SKU ID。读取字段可以增加 `attribute`（`data-*`、`value`、`href`、`title` 或 `aria-label`）、`input:true`（输入框当前值）或包含捕获组的 `regex`。配置只支持 DOM 交互，不支持任意 JavaScript、HTTP 请求或私有接口调用。跳转地址必须是当前平台的 HTTPS 域名。

可用步骤：`fill` 填表、`select` 选择下拉值、`check` 勾选布尔值、`upload` 给文件控件上传图片、`click` 点击、`wait` 等待控件可见，以及 `each_sku` 循环。提交按钮必须标记 `commit:true`，写入结果必须声明新出现的 `result.success` 控件；若提交前已有同一成功提示可见，任务会停止。

变量包括 `product.title`、`product.description`、`product.images`、`product.image_ids`、`product.detail_image_ids`、`listing.external_id`、`listing.sku_map`、`publish_config.category_id`、`publish_config.freight_id` 及其他已配置发布字段。`each_sku` 提供 `sku` 和 `index`；`sku.price` 是保留两位小数的元字符串，`sku.price_cents` 是整数分。上传步骤只能引用此商品已上传的图片素材 ID，不能引用任意本地路径。

新建商品的 `sku.stock` 永远为 0，必须逐项填入并在提交后逐 SKU 回读确认为零；成功回执必须包含商品 ID、外部 SKU 编码、平台 SKU ID 及库存。编辑商品不会提供仓库库存，且不能写库存控件；请使用独立库存校准工作流。

## 各操作的回执要求

| 工作流键 | 用途 | 额外要求 |
| --- | --- | --- |
| `product_create` | 新建商品 | `external_id`、`rows`、`fields.code/sku_id/stock`，完整匹配全部本地 SKU 并回读零库存 |
| `product_update` | 修改商品内容 | 已关联商品 ID、`success`；可配置 `external_id` 再次核对 |
| `product_list` | 上架 | 已关联商品 ID、`success` |
| `product_delist` | 下架 | 已关联商品 ID、`success` |
| `fetch_skus` | 读取 SKU 关联 | `rows`、`fields.code/sku_id`，必须完整、唯一对应全部本地编码 |
| `inventory_sync` | 校准店铺可售额度 | 逐 SKU 写 `sku.stock`、`success`、`rows`、`fields.sku_id/stock`，所有回读值必须与目标额度完全一致 |
| `orders` | 读取订单 | `rows`、`fields.external_id/status/created/remote_updated/product_id/sku/quantity`，以及分页规则 |

更新、上下架和库存页面地址可引用 `{{listing.external_id}}`。库存循环的 `sku.stock` 是该店分配额度；传给库存工作流的商品数据也只包含该店额度，不包含仓库总余额。库存同步前仍需暂停销售并核对占用。

订单字段的 `created`、`remote_updated` 须读取 10 位秒时间戳、13 位毫秒时间戳或完整 ISO 日期时间；没有时区的日期按北京时间解析。金额可以提供整数分 `amount_cents` 或十进制元 `amount`。可选 `title` 和 `raw_status`；程序仅保存运营字段，不保存买家姓名地址。

订单 `result.next` 是下一页控件，最后一页必须禁用，或设置 `aria-disabled=true`；缺失控件、内容不推进、空页却没有 `result.empty` 提示都会中止，避免漏单。确认为完整单页的工作流可显式设置 `result.single_page:true`。时间窗口最大 7 天，程序按更新时间过滤读取结果；每页最多 1000 行、最多 500 页，超出须缩小范围。同一订单出现重复 SKU 行时拒绝猜测数量，请将读取规则调整到完整且唯一的商品明细。

## 执行与异常

同一店铺浏览器有独立文件锁。所有网页操作均在隔离 Python 进程运行，不传入电商 API 凭据或视频 Cookie。任务提交前预检配置与登录；执行时重新核对商品版本、关联和库存修订。

登录失效会标记电商会话为 `expired`；视频登录状态保持独立。写入后遇到超时、页面变更、缺失回执或数量不匹配，任务为 `uncertain`，库存占用保留；必须核对平台实际结果后才能重试。`submitted` 表示读取到平台提交回执，审核和上架结果仍以商家后台为准。

备份时停止服务并保留整个数据卷，包括 `master.key`、商家浏览器目录与视频 Cookie。浏览器目录含登录凭据，应仅由应用运行用户访问。
