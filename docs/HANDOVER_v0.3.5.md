# ShopDock Local v0.3.5 · 交接文档

日期：2026-11-03
适用代码：`D:/store/shopdock`，版本 `0.3.6`（登录流程为 v0.3.5，v0.3.6 仅修复新建商品空对象）
上一版：v0.3.4（本地质量评分、通用内置提交、更新模式、失败重试、审核回查）

---

## 1. 这份文档解决什么问题

v0.3.4 已具备“统一发布 + 本地质量评分 + 多平台内置提交”的主体能力，但真实账号登录环节有三个阻塞：

1. 抖音登录按钮打开的是招商首页，不是登录页；
2. 登录成功后浏览器 / 扫码窗口不关闭，任务一直显示“正在执行”；
3. 卡住的任务长期占住店铺，后续登录被“当前账号任务正在排队或执行”挡住。

v0.3.5 修复以上问题，并对其他平台的登录判定、任务看门狗和自动订单同步做了配套修正。本文档说明改动、当前系统状态、部署验收步骤和仍未完成的事项。

---

## 2. 本轮改动摘要

| 问题 | 原因 | 修复 |
| --- | --- | --- |
| 抖音打开招商页 | 所有平台统一用 `portal`，抖音门户已改成招商落地页 | 增加独立 `login` 地址，抖音使用 `/login/common` |
| 登录成功不关窗 | 只认固定菜单文字；多店账号先到选店铺页；等待上限 600 秒 | 登录页/工作区路径判定；选店铺页算成功；上限降为 300 秒；成功后立即关闭 |
| 扫码窗口不关闭 | 前端弹窗没有任务轮询 | 每 3 秒查询登录任务，成功/失败都自动关闭并刷新店铺卡片 |
| 平台不跳转时无法结束 | 没有手动完成入口 | 新增“我已完成登录”，先核对页面再保存；仍在登录页则拒绝 |
| 卡住任务占锁 | `running` 记录没有超时回收 | 任务看门狗按类型回收，标记“结果待核对”并释放登录锁 |
| 其他平台登录失败 | `login.*` / `passport.*` 未被识别为登录页，检查又依赖菜单文字 | 统一登录域名识别；检查改为打开需登录的商家页面作为探针 |
| 订单同步任务刷屏 | 缺少订单规则仍每分钟入队 | 自动同步前先校验 `workflows.orders`，缺失则跳过 |

无数据库迁移，无新增第三方依赖。

---

## 3. 登录流程（接手人先看）

### 3.1 三个动作

| 动作 | 接口 | 打开的页面 | 成功条件 |
| --- | --- | --- | --- |
| 登录电商后台 | `POST /api/stores/{sid}/commerce/login` | 平台真正登录页 | 离开登录页 + 商家工作区/选店铺页 + 有效 Cookie |
| 检查电商登录 | `POST /api/stores/{sid}/commerce/check` | 需要登录的商家探针页 | 未被重定向到登录页，且满足同上条件 |
| 我已完成登录 | `POST /api/stores/{sid}/commerce/login/confirm` | 不打开页面 | 登录任务正在运行；写入确认标记后由浏览器进程核对页面 |

### 3.2 平台地址

| 平台 | 登录地址 | 检查探针 | 登录后典型落点 |
| --- | --- | --- | --- |
| 抖音小店 | `https://fxg.jinritemai.com/login/common` | `/ffa/mshop/homepage/index` | `/ffa/mshop/...`（可能先到选店铺页） |
| 快手小店 | `https://s.kwaixiaodian.com/` | `/zone/goods/list` | `s.kwaixiaodian.com/zone/...` |
| 微信小店 | `https://store.weixin.qq.com/` | `/shop` | 小店后台 |
| 淘宝 / 天猫 | `https://myseller.taobao.com/` | `/home.htm` | 千牛卖家中心 |
| 京东 | `https://shop.jd.com/` | `/` | 京东商家后台 |
| 拼多多 | `https://mms.pinduoduo.com/` | `/` | 商家后台首页 |

以上地址定义在 `app/commerce_profiles.py` 的 `PROFILES`，新增平台或平台改版时只改这里。

### 3.3 判定规则

`Runner.logged_in()` 的顺序：

1. URL 主机是 `login.*` / `passport.*` / `loginmyseller.*`，或路径含 `/login`、`/signin`、`/passport` → **未登录**。
2. 配置了 `login_required` 且该元素可见 → **未登录**。
3. 配置了 `logged_in` 探针 → 必须唯一且可见。
4. 未配置探针时：
   - 页面仍显示“手机登录 / 扫码登录 / 密码登录 / 扫码进入我的小店”等强登录文案 → **未登录**；
   - URL 命中 `app_paths`（如 `/ffa/`、`/zone/`、`/shop`）或出现“商品管理 / 订单管理 / 店铺管理”等文字 → **已登录**。
5. 最后校验店铺身份（如已配置 `identity`）和至少一个未过期 Cookie。

这样做的原因：抖音多店账号登录后先进入“选择店铺”，没有侧边菜单；仅靠菜单文字会永远判定失败。

### 3.4 关闭与超时

- 登录成功：写入 `session.json` → `context.close()` → 浏览器窗口关闭 → 前端轮询到 `succeeded` → 扫码弹窗关闭。
- 用户点“我已完成登录”：FastAPI 在店铺 profile 写入 `login.confirm`，浏览器进程每秒检查；页面确实离开登录页才成功。
- 登录等待上限 300 秒；`commerce_browser._run` 子进程超时 360 秒。
- 任务看门狗：`commerce_login` 420 秒、`account_login` 900 秒、`commerce_prepare_product` 900 秒、其他 1200 秒；超时标记 `uncertain`，释放账号锁。

---

## 4. 代码地图

| 文件 | 职责 |
| --- | --- |
| `app/commerce_profiles.py` | 平台域名、登录地址、探针地址、登录页/工作区判定、`trusted_url` |
| `app/commerce_browser.py` | 浏览器子进程边界、profile 锁、超时、`confirm_login` 标记 |
| `app/commerce_worker.py` | 子进程内的 Playwright 执行器；登录状态机、商品表单、提交回执、审核回读 |
| `app/jobs.py` | 任务队列、去重、账号组互斥、看门狗、自动订单同步 |
| `app/main.py` | FastAPI 路由、认证、状态接口、登录确认接口 |
| `app/connectors.py` | 平台元数据（名称、颜色、门户、登录入口、能力） |
| `app/static/app.js` | 店铺卡片、扫码窗口、任务轮询、自动关闭 |
| `app/publish_workbench.py` | 统一发布草稿、质量门槛、逐店提交、重试、审核回查 |
| `app/quality_checker.py` | 本地 100 分制预评分与硬性阻断 |
| `app/generic_submission.py` | 无自定义工作流时的通用内置提交 |
| `app/platform_feedback.py` | 平台错误码保留与本地化提示 |
| `app/commerce_readiness.py` | 店铺接入检查（只读） |
| `tests/test_commerce_login.py` | 本轮新增：登录地址、状态判定、手动确认、看门狗 |

数据目录（`APP_DATA`）：

```text
/data/
  shopdock.sqlite3          业务数据与任务
  master.key                凭据加密密钥，必须备份
  browser/commerce/<店铺ID>/ 独立 Chrome profile、Cookie、session.json
  media/                    图片与视频
```

---

## 5. 部署与验收

### 5.1 当前临时验收实例

- 地址：`http://localhost:18081`
- 容器：`shopdock-accept`
- 数据卷：`shopdock-accept-data`（独立于正式 8080 服务）
- 环境：`COMMERCE_HEADLESS=0`、`COMMERCE_BROWSER_CHANNEL=chrome`、`DISPLAY=:99`
- 管理员密码：由部署人设置；本轮由用户设置为 `<管理员自设密码>7890`（仅临时实例）

> 注意：该实例挂载了源码目录 `/app`，重启容器即可加载 v0.3.5；正式部署应重新构建镜像。

重启命令：

```bash
docker restart shopdock-accept
# 或彻底重建（保留数据卷）
docker rm -f shopdock-accept
docker run -d --name shopdock-accept -p 127.0.0.1:18081:8080 \
  -v "D:/store/shopdock:/app:ro" -v shopdock-accept-data:/data \
  -e APP_DATA=/data -e TZ=Asia/Shanghai \
  -e ALLOWED_HOSTS=localhost,127.0.0.1 \
  -e COMMERCE_BROWSER_CHANNEL=chrome -e COMMERCE_HEADLESS=0 \
  -e COOKIE_SECURE=0 --shm-size=1gb shopdock:local
```

### 5.2 正式部署

```bash
cd D:/store/shopdock
docker compose up -d --build
```

升级前：

1. 停止旧容器；
2. 备份完整数据卷（含 `master.key`、数据库、`browser/commerce/`）；
3. 不要删除数据卷；浏览器 Cookie 与密钥都在里面。

### 5.3 登录验收步骤

以抖音为例：

1. 打开 `http://localhost:18081`，登录管理员。
2. 左侧“店铺连接”→ 对应店铺“登录电商后台”。
3. 扫码桌面应显示 `fxg.jinritemai.com/login/common` 的手机登录 / 邮箱登录 / 扫码入口。
4. 手机扫码并在抖音确认。
5. 浏览器离开登录页后，任务应在数秒内变为“已完成”，扫码窗口自动关闭，店铺状态变为“登录有效”。
6. 若页面停在原处，点“我已完成登录”。
7. 再点“检查电商登录”，应通过；点“店铺接入检查”确认会话与适配状态分开显示。

其他平台同理。微信小店登录页与后台同域，若扫码后仍不跳转，用“我已完成登录”。

### 5.4 验收时不要做的事

- 不要在未核对类目、资质、图片和 SKU 前点击统一发布的“提交审核”。
- 不要把“登录有效”当成“商品创建适配已完成”；二者是不同检查项。
- 不要在正式数据卷上测试删除、清空或重建。

---

## 6. 验证记录

- 完整 pytest（Docker 隔离、断网、临时数据目录）：**284 passed**，1 条既有 httpx 弃用提醒。
- jsdom 界面测试：**28 passed**（登录自动关闭扫码窗口 + v0.3.6 新建商品空对象）。
- v0.3.6 真实验收：新建商品弹窗、图片选择器、填写并保存、列表出现均正常，测试商品已删除。
- `node --check app/static/app.js / library.js / publishing.js`：通过。
- `docker compose config --quiet`：通过。
- 真实 Chrome 用例：
  - 抖音登录请求命中 `/login/common`；
  - 登录后进入 `/ffa/mshop/shop/select`，即使没有菜单文字也能识别；
  - Cookie 写入、`session.json` 生成；
  - 页面仍在登录页时点“我已完成登录”会被拒绝。
- 临时实例真实界面验收（独立数据卷）：
  - 店铺卡片“打开登录页”指向 `https://fxg.jinritemai.com/login/common`；
  - 点击“登录电商后台”后扫码桌面显示抖音手机登录页；
  - 点“我已完成登录”后任务明确失败为“页面仍停留在登录页”，弹窗自动关闭，Chrome 进程全部退出，页面无脚本错误。
- 真实网络探测（匿名）：
  - 抖音 `/ffa/mshop/homepage/index` 会重定向到 `/login/common`；
  - 快手 `/zone/goods/list` 会重定向到 `login.kwaixiaodian.com`；
  - 淘宝 → `loginmyseller.taobao.com`；京东 → `passport.shop.jd.com`；拼多多 → `/login/`；
  - 微信小店 `/shop` 显示“扫码进入我的小店”。

未验证：

- 真实抖音 / 快手 / 微信 / 淘宝 / 京东 / 拼多多账号的扫码成功路径；
- 平台登录后的真实风控、验证码、子账号、多店选择差异；
- 登录后商品创建 / 更新 / 订单页面的真实 DOM。

---

## 7. 当前能力与边界

### 已具备

- 六平台店铺、独立电商 / 视频会话、加密凭据、CSRF、审计日志。
- 商品、素材、SKU 映射、统一库存、订单同步与 CSV。
- 统一发布：读取页面要求 → 公共字段合并 → 本地质量评分（≥95）→ 逐店任务 → 提交回执 → 失败重试 → 审核回查。
- 快手内置创建 / 更新；抖音、微信通用内置创建；已关联商品更新。
- 平台错误码保留与本地化提示。

### 仍需完成

1. **真实账号验收**：六个平台的登录、类目、资质、图片审核、平台评分、提交回执、审核状态。
2. **官方规则接口**：当前仍以商家页面 DOM 为主，没有抖店 / 快手类目属性与资质接口，也没有规则版本缓存和提交前强制刷新。
3. **商品仓库**：ZIP / Excel 导入、回收站、原图 / 生成图 / 平台适配图三版本、按图片哈希去重、拖拽排序与裁剪、待完善 / 已发布 / 审核失败筛选。
4. **质量体系**：本地 95 分已实现；品牌授权、真实材质、资质文件仍只能人工确认，程序不编造。
5. **审核自动化**：审核回查是手动触发，没有定时轮询；非快手平台需要配置 `commerce_browser.audit_url`。
6. **更新与审核地址**：非快手平台的更新需要 `commerce_browser.edit_url`，审核需要 `audit_url`，未配置时明确阻断。
7. **通用提交 SKU 回读**：抖音 / 微信通用内置提交暂不读取平台 SKU ID，需要后续补齐或手工关联。
8. **多店选择**：抖音多店账号登录后可能停在选店铺页；当前识别为已登录，但后续页面操作默认进入平台首页，若平台要求先选店，需要在页面规则中补一步。

---

## 8. 常见问题

**登录后仍显示“尚未登录”**

先点“检查电商登录”。若失败，查看任务详情中的提示；确认扫码的是电商后台，不是抖音 / 快手创作者中心。电商与视频账号是两套会话。

**提示“此店铺正在执行「登录电商后台」”**

说明同一店铺已有登录任务。等待其完成，或在扫码窗口点“我已完成登录”。超过 7 分钟仍未结束时，看门狗会回收；也可以重启容器，启动时会把遗留 `running` 标记为“结果待核对”。

**浏览器窗口没有出现**

确认容器使用完整镜像、`COMMERCE_HEADLESS=0`、`DISPLAY=:99`，并已打开扫码桌面。`compose.core.yaml` 不含浏览器。

**登录成功但商品仍“待配置”**

登录只证明会话可用。商品创建 / 更新 / 订单 / 库存各自需要页面适配；在“店铺接入检查”中逐项查看。

**抖音打开后是招商页**

确认代码为 v0.3.5 且容器已重启。登录地址应为 `/login/common`，不是门户 `/`。

---

## 9. 接手检查清单

- [ ] `docker compose config --quiet` 通过。
- [ ] `docker compose up -d --build` 后 `/healthz` 返回 `0.3.5`。
- [ ] 打开扫码桌面，逐平台点“登录电商后台”，确认打开的是登录页。
- [ ] 至少完成一个平台的真实扫码，确认任务自动结束、窗口自动关闭、状态变为“登录有效”。
- [ ] 点“检查电商登录”，确认不会因为门户首页误判。
- [ ] 用测试商品走一次“读取要求 → 回填 → 质量检查”，先不提交审核。
- [ ] 确认 `master.key`、数据库、`browser/commerce/` 已纳入备份。
- [ ] 阅读 `docs/PUBLISHING_HELP.md` 与 `docs/RELEASE_v0.3.5.md`。

---

## 10. 回滚

代码回滚到 v0.3.4 不会破坏数据库：本版没有数据库迁移。回滚后登录仍会回到旧行为（抖音招商页、登录后不自动关闭），不建议在真实账号验收阶段回滚。若必须回滚，保留 `browser/commerce/` 与 `session.json`，已登录店铺的会话仍可复用。
