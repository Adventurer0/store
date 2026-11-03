# v0.3.7 验证记录

2026-11-03，Windows + 现有完整 Docker 镜像。商家自动化测试使用本地模拟 HTML，禁止外网，不向真实平台提交商品。

- 完整 pytest：**301 passed**，125.74 秒，无跳过；包含真实 Chrome 的本地页面交互。
- jsdom 界面测试：**30 passed**。
- 三个前端脚本语法检查与 `docker compose config --quiet` 通过。
- 正式浏览器上线检查发现旧脚本缓存后，增加版本资源与缓存校验；相关 API 回归 **25 passed**（包含新增缓存用例），6.20 秒。
- v0.3.7 镜像构建通过。
- 独立内存数据目录验收实例：真实 Edge 创建管理员、新建并保存本地商品，确认商品弹窗及空素材选择器可用，检查素材仓库菜单和筛选界面。
- 正式 8080 服务：从 v0.3.1 升级至 v0.3.7，确认原商品和店铺保留、商品主图加载成功、悬浮产生 260×260 放大面板、新版菜单与统一发布页面正常；未向平台提交。

新增回归覆盖：重复图片上传、创建 / 更新工作流分派、更新库存保护、无效自定义工作流写前阻断、草稿保存与审核提交区分、提交前评分跌至90或缺失 / 错误指纹 / 过期 / NaN、平台错误码保留、未适配结构字段阻断、没有可选平台评分时的证据格式、审核回查 / 重试迟到响应切换批次。

后端存在一项现有 FastAPI / Starlette 对 httpx 的弃用提醒，测试通过；本次没有变更第三方依赖。

## 复现

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
npm ci
npm run check
npm run test:ui
```

完整镜像中的浏览器测试可按以下方式隔离运行（`.testdeps` 仅为本机 pytest 安装目录，不进入发行包）：

```bash
docker run --rm --network none --entrypoint /opt/mpau-venv/bin/python \
  --mount type=bind,source=<项目绝对路径>,target=/work,readonly --workdir /work \
  -e APP_DATA=/tmp/shopdock-test -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/work/.testdeps:/work:/usr/local/lib/python3.12/site-packages \
  shopdock:local -m pytest tests -q -p no:cacheprovider
```

## 未验收范围

六个平台真实账号的类目、属性、资质、图片审核、评分、提交回执与审核状态没有完成全面实店验收。没有适配的类目和 SKU 不会被静默跳过；读取失败或提交结果不明时阻断 / 待核对，不能将这些测试视为真实平台发布成功证明。

本地评分 ≥95 是预检；快手新建另需实际平台评分 >90。其他平台提供实际评分时也须 >90，评分缺失、过期、指纹变化不可绕过此前的真实评分检查。
