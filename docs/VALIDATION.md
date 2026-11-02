# v0.3.0 验证记录

2026-11-02，Windows 宿主 + 现有 ShopDock 完整 Docker 镜像。测试挂载新版源码，关闭外网并使用临时 SQLite / 浏览器目录；没有连接或改变真实店铺。原 8080 服务及其数据卷未替换。

## 已通过

- 完整 pytest：**136 passed**，耗时 27.66 秒，无跳过。包含 Playwright 1.64.1 与真实系统 Chrome 的本地 HTML 交互测试。
- JavaScript 语法检查；jsdom 界面流程：**3 passed**。
- `compose.yaml` 与 `compose.core.yaml` 配置校验。
- 临时 8081 站点真实 Edge 验收：创建临时管理员、添加拼多多店铺、查看电商 / 视频独立会话卡片、无电商 API 凭据表单、六平台管理能力表；控制台无错误。测试容器、浏览器测试标签已清理。

后端覆盖：六平台统一商品任务、商品 / 映射 / 网页规则变更后阻断、独立登录状态和账号队列、同操作及并发幂等去重、不同账号操作冲突提示、旧 API 开关禁用、增量迁移保留视频状态、网页订单来源保护、重复订单与库存扣账、库存未确认持续占额及确认后释放、未知结果防止盲目重发。

浏览器模块有 26 项测试，包括真实 Chrome 本地页面的异步成功提示、零库存创建、店铺身份不符写前阻断、库存回读不符、订单字段白名单、失效状态传播、Cookie / 凭据不回显、路径域名校验、进程超时清理。真实 DOM 用例通过路由提供虚构商家 HTML，不向真实平台提交商品。

## 复现

一般开发环境：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
npm ci
npm run check
npm run test:ui
```

浏览器用例自动检测 Playwright。使用本次已存在的完整镜像，在项目目录执行下列命令（先为测试环境安装 pytest；本次放在 `.testdeps`）：

```bash
docker run --rm --network none --entrypoint /opt/mpau-venv/bin/python \
  --mount type=bind,source=<项目绝对路径>,target=/work,readonly --workdir /work \
  -e APP_DATA=/tmp/shopdock-test -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/work/.testdeps:/work:/usr/local/lib/python3.12/site-packages \
  shopdock:local -m pytest tests -q -p no:cacheprovider
```

测试输出见 `tests/validation-output.txt`。仅有现有 FastAPI / Starlette 的 httpx 弃用提醒，未修改第三方依赖来压制提醒。

## 未验收范围

- 六个平台真实商家登录后的控件、类目必填项、身份显示位置、权限、扫码 / 短信 / 风控，以及真实商品创建 / 编辑 / 上下架、订单分页和库存锁定语义。每店需要配置并核验 `commerce_browser` 页面规则，缺失时预检阻断。
- 真实视频发布和商品挂载效果；视频通道保持独立。
- 本次未完整重建全套 Docker 镜像或替换原运行服务；验证使用原完整镜像运行新版源码。首次全量构建仍需下载第三方依赖。

测试证明统一任务和网页执行器的行为，不代表六个平台已经完成真实账号适配。当前没有无需配置的通用商家页面连接器。