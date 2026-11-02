# Third-party notices

The ShopDock application is original code licensed under MIT. The Docker build installs separate open-source dependencies and retains their licenses in the installed packages / repositories.

| Component | Source | Role / license |
| --- | --- | --- |
| MPAU | https://github.com/cjccd/multi-platform-auto-upload | Optional full-image publishing engine, MIT. Pinned commit 4c8b6465c2473aac3cfe064c8c6dfbadd87b2238. Upstream LICENSE remains in /opt/mpau. |
| social-auto-upload | https://github.com/dreammis/social-auto-upload | Architectural reference, MIT. No uploader source copied into this package. |
| wxshop-go-sdk | https://github.com/ealink1/wxshop-go-sdk | Protocol reference; no implementation copied. |
| FastAPI | https://github.com/fastapi/fastapi | Web API, MIT |
| Starlette | https://github.com/Kludex/starlette | ASGI / responses / WebSocket, BSD-3-Clause |
| Uvicorn | https://github.com/Kludex/uvicorn | ASGI server, BSD-3-Clause |
| HTTPX | https://github.com/encode/httpx | HTTP client, BSD-3-Clause |
| Pydantic | https://github.com/pydantic/pydantic | Validation, MIT |
| cryptography | https://github.com/pyca/cryptography | Encrypted credentials, Apache-2.0 or BSD-3-Clause |
| python-multipart | https://github.com/Kludex/python-multipart | Upload parsing, Apache-2.0 |
| noVNC | https://github.com/novnc/noVNC | Authenticated browser desktop; MPL-2.0. Full image contains package notices. |
| x11vnc / Xvfb / Openbox / Chromium / Tini | Installed distribution or upstream packages | Full-image runtime; see package-specific copyright files, including /usr/share/doc. |

The ZIP is source-only. It does not contain a prebuilt third-party browser, real cookies, merchant credentials, or the full MPAU source tree. Third-party dependencies are fetched during image construction. When redistributing a built image, retain applicable source / notice obligations of its component licenses.


## v0.2 协议与案例参考

新增 Python 连接器按官方文档与公开案例独立实现，未打包这些项目的源文件。参考 WxJava 的微信图片上传字段、crywolfx/kuaishou-merchant-open-api 的图片上传方式、loong-135/doudian-sdk 的二进制素材协议。whichmen/commerce-multi-platform-publisher 只作为商品关联与回查思路参考。完整链接与验证限制见 docs/V2_GUIDE.md。
