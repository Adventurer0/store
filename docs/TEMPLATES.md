# v0.2 API 模板（历史资料）

> v0.3.0 商品任务不再使用下述 API 请求模板。新配置是 `templates.commerce_browser` 声明式页面规则，见 [网页规则](COMMERCE_BROWSER.md)。`publish_defaults` / 商品 `publish_config` 作为页面变量保留。

优先使用店铺 `publish_defaults` 与商品 `publish_config` 的内置映射，见 [V2_GUIDE.md](V2_GUIDE.md)。下方是旧版高级模板机制；新建库存会归零、编辑库存字段会被剥离，统一通过库存模块提交。

# 商品模板与 CSV

## 为什么需要每店模板

不同平台甚至不同类目要求的类目 ID、品牌资质、运费、属性与 SKU 结构都不同。统一商品库负责公共资料，模板负责转换。设置一次同类模板后，可以批量复用。模板只是请求映射，不是平台规则校验器；本地预检通过仍可能被平台拒绝。

店铺设置里的模板 JSON 最外层使用以下操作名：

```json
{
  "product_create": {"title": "{{product.title}}"},
  "product_update": {"product_id": "{{listing.external_id}}", "title": "{{product.title}}"}
}
```

上例只展示语法，**不是完整可发布请求**。本版为所有店铺共用一份商品资料；多类目店铺可通过 `product.extra` 保存各商品差异，再在店铺模板引用。

## 支持的语法

| 写法 | 结果 |
| --- | --- |
| `"{{product.title}}"` | 商品标题字符串 |
| `"{{product.skus.0.price_cents}}"` | 第一 SKU 价格，保留整数类型 |
| `"新品：{{product.title}}"` | 拼接成字符串 |
| `"{{product.images}}"` | 图片 URL 列表，保留数组类型 |
| `"{{product.extra.category_id}}"` | 商品扩展字段 |
| `"{{listing.external_id}}"` | 该店铺的平台商品 ID 字符串 |
| `{"$int":"listing.external_id"}` | 在后端转整数，避免浏览器丢失 64 位 ID 精度 |
| `{"$json":"product.extra.spec_prices"}` | 将扩展对象 / 数组编码为 JSON 字符串，供要求字符串的字段使用 |

全长商品 ID 在界面里始终填字符串，不要在浏览器 JSON 文本里写大于 2^53 的裸整数。平台明确要求数值类型时，用 `$int` 后端转换。

多 SKU 映射示例：

```json
{
  "skus": {
    "$map": "product.skus",
    "as": "sku",
    "template": {
      "out_sku_id": "{{sku.code}}",
      "sale_price": "{{sku.price_cents}}",
      "stock_num": "{{sku.stock}}",
      "sku_attrs": [{"attr_key": "规格", "attr_value": "{{sku.name}}"}]
    }
  }
}
```

这是映射机制示例。具体属性名称、规格体系、SKU ID 的要求以平台对应类目为准。**编辑已有 SKU 时通常需要原平台 SKU ID，不能简单将新建 SKU 列表当作编辑参数。** 面板中的微信更新示例因此使用 `product.extra.wechat_update_skus`，快手使用 `product.extra.kuaishou_skus`，需要维护实际平台数据。

模板不会执行 Python、JavaScript、shell 或任意网络请求。缺失变量、`__REQUIRED__` 或格式不合法都会阻止任务入执行队列。队列存储已预检的请求快照；编辑商品或模板不会偷偷改变已经排队的请求。需要修改请求时取消旧任务，重新预检。

## 图片与素材

本地主图用于面板预览。`product.images` 是你提供的 HTTPS 图片地址，不是本机 `/data/media/...` 路径。部分平台要求先上传到本平台素材中心并使用其 URL，本版尚未自动完成这一步。不要把 localhost URL 填入平台图片字段。

抖音 `description` 常要求详情图片地址串，而非普通文字描述，因此示例使用 `product.extra.douyin_description_images`。同理，spec_prices 等复杂结构要按当前官方文档准备，不把公共 SKU 直接硬套成平台规格。

## 商品 CSV

UTF-8 编码，一行一个 SKU。价格单位是分。

| 列 | 含义 |
| --- | --- |
| product_key | 本地导入键，同键多行组成一个商品；重复导入更新本地商品 |
| title | 商品标题，同一 product_key 的每行需一致 |
| category | 本地分类 |
| description | 描述 |
| sku_code | 同一商品内唯一 SKU 编码 |
| sku_name | 规格名称 |
| price_cents | 非负整数，12900 = 129.00 元 |
| stock | 非负整数库存 |

商品 CSV 重新导入更新标题、分类、描述和 SKU，保留主图、平台素材 URL、扩展字段及店铺关联。不会自动改动平台商品。

## 订单 CSV

选择目标店铺后导入，列为 `order_id,status,amount_cents,title,quantity`。每个订单号一行；若原导出是同单多 SKU 行，请先合并到订单粒度。不同店铺相同订单号视为不同订单，同店订单重复导入更新记录。

同一店铺已有 API 来源订单时，CSV 不覆盖该记录。所有行先验证，任一行有问题就不写入本批次。订单本地备注在再次同步时保留。

CSV 导出对 `= + - @` 等公式前缀做文本转义，以避免表格软件执行公式。该转义用于导出安全；特殊编码的导出文件再次导入前，应检查前导单引号是否符合你的实际编码规则。
