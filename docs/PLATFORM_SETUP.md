# 平台连接 · v0.2.0（历史资料）

> v0.3.0 起电商后台改用独立网页登录 Cookie，以下电商 API 授权步骤已停用。当前流程见 [README](../README.md) 与 [网页规则](COMMERCE_BROWSER.md)。视频授权仍独立管理。

详细操作、教学和参考案例见 [V2_GUIDE.md](V2_GUIDE.md)，以 README 的当前范围为准。此包尚未实店联调。

| 平台 | 商品 / 订单凭据 | 视频发布凭据 |
| --- | --- | --- |
| 抖音 | app_key、app_secret、access_token | MPAU 独立扫码登录 |
| 微信 | 有效 access_token，须有相应小店接口权限 | MPAU 视频号扫码 |
| 快手 | app_key、sign_secret、access_token | 挂商品：video_app_id、video_access_token；普通视频可用 MPAU 扫码 |

快手 sign_secret 不是 app_secret。Video Token 不是电商 Token。用户授权、应用权限和类目资格须在各平台申请；本系统不会因扫码自动获取商品 / 订单 API 权限。

## 请求通道

- 抖音：`product.addV2 / product.editV2 / product.launchProduct / product.setOffline / order.searchList / sku.syncStock`；素材二进制上传走 `open.materialToken`，随后 `material.uploadImageSync / material.queryMaterialDetail`。图片注册可能异步审核。
- 微信：`channels/ec/product/add / update / listing / delisting`、`order/list/get / order/get`、`product/stock/update`；本地图片上传为 `shop/ec/basics/img/upload`，multipart 字段 `media`。
- 快手电商：`open.item.new / open.item.edit / open.item.get / open.item.image.upload / open.order.cursor.list`。库存校准使用 `open.item.edit` 的 SKU 设置库存字段；尚无独立上下架适配。
- 快手内容：`photo/start_upload / photo/publish / photo/info`；上传网关执行 `api/upload/fragment / complete`。商品字段 `merchant_product_id`，仅自建商品。

各平台版本、SKU 字段及类目必填项仍需按店铺权限实测。标准映射的附加 `native / sku_native` 不应包含库存，程序会将新建库存归零、剥离编辑库存；区域仓和批次库存不受支持。

## 订单状态与起算

微信使用已知订单状态归类。抖音优先根据订单状态描述识别；快手若响应只有数字状态码，必须在店铺 JSON 里添加 `inventory_status_map`，例如 `{"实际待付款状态码":"reserve","实际待发货状态码":"sold","实际关闭状态码":"void"}`，以你当前官方文档为准，不要照抄示例文字。

默认扣库起算时间是该店在 ShopDock 的创建时间。可在店铺模板 JSON 设置 Unix 秒 `inventory_start`，用来配合既有库存盘点。早于此时间的订单照常汇总，但不扣库。CSV 不扣库。已扣减的订单关闭时不会自动回补；退货入库要单独记账。

Token 目前手动取得与更新。核心镜像能使用商品 API、订单 API 与快手官方视频通道；其余 MPAU 视频依赖完整镜像。
