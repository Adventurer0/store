# 一份商品铺到三平台，以及快手视频挂商品（v0.2 历史资料）

> 当前 v0.3.0 电商控制已切换为六平台网页 Cookie 会话，不再使用本文的电商 API 发布 / 库存 / 订单授权。当前使用说明见 [README](../README.md) 与 [网页规则](COMMERCE_BROWSER.md)。快手官方视频授权仍作为独立视频通道保留。

编写于 2026-11-02。本文基于官方文档与公开源码案例；不把项目 README 的自述等同于真实店铺验证。

## 一次设置，后续复用

在每家店的设置填写默认类目、运费模板。商品“关联”里的发布配置可以单独覆盖：

```json
{
  "category_id": "本平台叶子类目ID",
  "freight_id": "本店运费模板ID",
  "spec_name": "颜色",
  "native": {},
  "sku_native": {}
}
```

不同店铺 / 平台只填差异字段，标题、描述、JPG/PNG 主图、详情图、价格和一个维度的规格从本地商品自动生成。`native` 放品牌 / 类目属性 / 服务规则；`sku_native` 按本地 SKU 编码放特殊规格字段。类目资格、品牌资质不能从照片可靠推断，需首次配置；多维 SKU 不属于标准映射的一键适配范围。

点击本地商品“多店铺货”，选择抖店、快手、微信的多家店。预检会逐店列出配置问题。确认一次后创建独立任务，按店上传素材和创建商品，保存返回的远端 ID。新商品初始为 0 库存，避免初始铺货重复占用仓库总量。

SKU ID 可在平台回执中自动匹配；若创建只返回商品 ID，保存关联后点击“按外部编码获取 SKU”。只能按明确的 out_sku_id / relSkuId 关联，不按规格位置猜测。已有非本系统创建商品没有外部编码时，需手工建立初次映射。

## 快手挂小黄车步骤

1. 电商商品：在 [快手电商开放平台](https://open.kwaixiaodian.com/)申请应用与店铺授权，取得商品、素材和订单权限。自用应用和第三方应用授权范围不同。
2. 视频账号：在 [快手开放平台](https://open.kuaishou.com/)申请内容能力，对正确账号获取视频 OAuth Token；需要 `user_video_publish`，回查需 `user_video_info`。
3. 将内容应用 ID / Token 填入 ShopDock 的 Video 字段。电商 Token 填电商字段，两者不可混用。
4. 商品“关联”填写快手小店商品 ID，并确认它是该视频账号的自建商品。官方接口不支持把分销等其他类型商品直接按此参数挂载。
5. 上传 MP4 与封面，在视频页选商品、封面和目标店，预检后提交。程序按官方流程上传分片、合并，发布 multipart 表单携带 `caption`、`cover` 与 `merchant_product_id`。
6. 回执保存 `photo_id`，点击“回查作品”。能查询到作品、pending=false 证明作品处理状态；详情接口没有提供足够的挂商品证明，因此仍需在快手后台确认小黄车。
7. 发布超时或程序中断时保持“结果待核对”，核对未成功前不要重发。

官方教学：[创建视频](https://open.kuaishou.com/platformDocs/openAbility/contentManagement/createAVideo.html)、[查询视频](https://open.kuaishou.com/platformDocs/openAbility/contentManagement/searchForVideos.html)。前者提供发起、分片、合并和带商品发布参数；后者提供按 photo_id 查询处理状态。

## 库存和订单

仓库 10 件不能给三店各 10 件。分配为 4/3/3 后，各店各卖自己的额度。减少一店再调拨给另一店，必须先确认远端减量成功；同步超时的额度继续被占用。

补货 / 校准时先暂停目标商品销售，核对待付款锁定库存，再提交全量可售额度。当前不是跨平台实时共享同一个库存池，不能在不断下单时把滞后的可售数字反复覆盖回平台。自动订单轮询与 SKU 账本负责汇总、占用扣减与异常提示。

订单初次接入须设定盘点起算边界。取消 / 退款不代表实物入库，需核对后用“出入库”登记。异常状态或缺失 SKU 时不猜测扣账。补齐映射后可点击“重新核算订单”，已扣过的订单不会再扣一次。

## 官方资料与参考案例

| 资料 | 采用的内容 | 局限 |
| --- | --- | --- |
| [快手视频官方教学](https://open.kuaishou.com/platformDocs/openAbility/contentManagement/createAVideo.html) | 小黄车字段、封面、上传流程 | 必须有内容授权；只支持自建商品 |
| [快手商品创建](https://open.kwaixiaodian.com/zone/new/docs/api?name=open.item.new&version=1) / [编辑](https://open.kwaixiaodian.com/zone/new/docs/api?name=open.item.edit&version=1) | 商品创建方法、SKU、图片和库存字段 | 部分文档依赖 JavaScript；需实际应用版本确认 |
| [快手订单](https://open.kwaixiaodian.com/docs/api?apiName=open.order.cursor.list&version=1) | 游标、毫秒时间、窗口限制 | 不同状态码须按官方定义配置 |
| [抖店素材教学](https://op.jinritemai.com/docs/guide/171) | 本地二进制 → URI → 素材注册 → 图片地址 | 上传域名与接口版本可能变化 |
| [抖店库存](https://op.jinritemai.com/docs/api-docs/34/155) | 普通 SKU 全量校准 | 增量模式与区域仓本版未启用 |
| [微信小店官方文档](https://developers.weixin.qq.com/doc/store/shop/API/) | 商品 / 订单 / 素材 / 库存权限与接口 | 本执行环境部分页面不可直接读取 |
| [MPAU](https://github.com/cjccd/multi-platform-auto-upload) | 抖音、视频号等视频发布参数和账号隔离 | 当前快手模块没有挂商品参数，因此快手挂链另接官方 API |
| [commerce-multi-platform-publisher](https://github.com/whichmen/commerce-multi-platform-publisher) | 快手商品关联与发布回查的公开自动化案例 | 项目自述覆盖这些能力；未在本包复制其连接器、未验证其生产效果 |
| [kuaishou-merchant-open-api](https://github.com/crywolfx/kuaishou-merchant-open-api) | 图片上传 multipart 示例 | 社区 SDK，非官方支持承诺 |
| [WxJava](https://github.com/binarywang/WxJava/tree/develop/weixin-java-channel) | 微信本地图片字段 media、接口路径及返回模型 | 社区 SDK，用于交叉核对 |
| [doudian-sdk](https://github.com/loong-135/doudian-sdk/blob/main/src/open/core/BinaryMaterialUpload.php) | 素材 Token、上传地址、CRC32 与 StoreUri 流程 | 社区 PHP 实现；本包独立 Python 适配尚未实店验证 |

## 首次实店验收

从一件低库存测试商品开始：读取订单 → 上传图片 → 创建商品 → 核对类目、规格、价格与 ID → 以小额度校准库存 → 一笔订单核对只扣一次 → 一条快手视频核对小黄车。当前执行环境没有你的应用授权，不能替你确认平台已开通权限或审核已通过。
