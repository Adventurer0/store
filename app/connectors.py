"""Explicit platform contracts; no mock success and no arbitrary HTTP targets."""
import copy
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx
from . import db
from .security import redact

PLATFORMS = {
    "douyin": {"name": "抖音小店", "video_name": "抖音", "color": "#303644", "commerce": ["product_create", "product_update", "product_list", "product_delist", "order_sync"], "link": True, "portal": "https://fxg.jinritemai.com/"},
    "kuaishou": {"name": "快手小店", "video_name": "快手", "color": "#f47632", "commerce": ["product_create", "product_update", "order_sync"], "link": True, "portal": "https://s.kwaixiaodian.com/"},
    "wechat": {"name": "微信小店", "video_name": "视频号", "color": "#20a772", "commerce": ["product_create", "product_update", "product_list", "product_delist", "order_sync"], "link": True, "portal": "https://store.weixin.qq.com/"},
    "tmall": {"name": "淘宝 / 天猫", "video_name": "淘宝光合", "color": "#f4832b", "commerce": [], "link": True, "portal": "https://creator.guanghe.taobao.com/"},
    "jd": {"name": "京东", "video_name": "京东京麦", "color": "#e75353", "commerce": [], "link": True, "portal": "https://shop.jd.com/"},
    "pdd": {"name": "拼多多", "video_name": "多多视频", "color": "#e64752", "commerce": [], "link": True, "portal": "https://mms.pinduoduo.com/"},
}
# Merchant control always uses an isolated browser session. Video portals and
# sessions are separate even when the platform lets a user share one account.
_COMMERCE_PORTALS = {
    'douyin': 'https://fxg.jinritemai.com/',
    'kuaishou': 'https://s.kwaixiaodian.com/',
    'wechat': 'https://store.weixin.qq.com/',
    'tmall': 'https://myseller.taobao.com/',
    'jd': 'https://shop.jd.com/',
    'pdd': 'https://mms.pinduoduo.com/',
}
_VIDEO_PORTALS = {
    'douyin': 'https://creator.douyin.com/',
    'kuaishou': 'https://cp.kuaishou.com/',
    'wechat': 'https://channels.weixin.qq.com/',
    'tmall': 'https://creator.guanghe.taobao.com/',
    'jd': 'https://jmai.jd.com/',
    'pdd': 'https://ddsp.pinduoduo.com/',
}
for _platform, _meta in PLATFORMS.items():
    _meta.update(portal=_COMMERCE_PORTALS[_platform],
                 video_portal=_VIDEO_PORTALS[_platform],
                 commerce_mode='browser_cookie',
                 commerce=['product_create', 'product_update', 'product_list',
                           'product_delist', 'order_sync'])
METHODS = {
    "wechat": {"product_create": "channels/ec/product/add", "product_update": "channels/ec/product/update", "product_list": "channels/ec/product/listing", "product_delist": "channels/ec/product/delisting", "orders": "channels/ec/order/list/get", "order": "channels/ec/order/get"},
    "douyin": {"product_create": "product.addV2", "product_update": "product.editV2", "product_list": "product.launchProduct", "product_delist": "product.setOffline", "orders": "order.searchList"},
    "kuaishou": {"product_create": "open.item.new", "product_update": "open.item.edit", "orders": "open.order.cursor.list"},
}


class Blocked(Exception):
    """No write was attempted; setup or input is incomplete."""


class Rejected(Exception):
    """The upstream explicitly rejected the request."""


class Uncertain(Exception):
    """An external side effect may have happened. Do not automatically retry."""


def canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\\b", "\\u0008")


def douyin_params(credentials, method, payload, timestamp):
    params = {"app_key": credentials["app_key"], "method": method, "param_json": canonical(payload), "timestamp": timestamp, "v": "2"}
    raw = credentials["app_secret"] + "".join(k + str(params[k]) for k in sorted(params)) + credentials["app_secret"]
    params["sign"] = hmac.new(credentials["app_secret"].encode(), raw.encode(), hashlib.sha256).hexdigest()
    params["sign_method"] = "hmac-sha256"
    params["access_token"] = credentials["access_token"]
    return params


def kuaishou_params(credentials, method, payload, timestamp):
    # The official MD5 variant is fully specified; sign_secret != app_secret.
    params = {"appkey": credentials["app_key"], "method": method, "param": db.dumps(payload), "version": "1", "access_token": credentials["access_token"], "timestamp": str(timestamp), "signMethod": "MD5"}
    raw = "&".join(f"{k}={params[k]}" for k in sorted(params)) + "&signSecret=" + credentials["sign_secret"]
    params["sign"] = hashlib.md5(raw.encode()).hexdigest()
    return params


def check_credentials(store):
    p = store["platform"]
    if not store["api_enabled"]:
        raise Blocked("店铺尚未启用电商 API；请先完成商家授权")
    fields = ["access_token"]
    if p == "douyin":
        fields += ["app_key", "app_secret"]
    elif p == "kuaishou":
        fields += ["app_key", "sign_secret"]
    missing = [k for k in fields if not store["credentials"].get(k)]
    if missing:
        raise Blocked("缺少店铺授权凭据：" + "、".join(missing))


class Commerce:
    def __init__(self, store, transport=None):
        check_credentials(store)
        self.store = store
        self.platform = store["platform"]
        self.creds = store["credentials"]
        self.transport = transport

    def call(self, method, payload, write=False, files=None, query=None):
        try:
            with httpx.Client(timeout=45, follow_redirects=False, transport=self.transport) as client:
                if self.platform == "wechat":
                    response = client.post("https://api.weixin.qq.com/" + method, params={"access_token": self.creds["access_token"], **(query or {})}, **({"files": files} if files else {"json": payload}))
                elif self.platform == "douyin":
                    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                    params = douyin_params(self.creds, method, payload, stamp)
                    body = params.pop("param_json")
                    response = client.post("https://openapi-fxg.jinritemai.com/" + method.replace(".", "/"), params=params, content=body.encode(), headers={"Content-Type": "application/json"})
                elif self.platform == "kuaishou":
                    params = kuaishou_params(self.creds, method, payload, int(time.time() * 1000))
                    url = "https://openapi.kwaixiaodian.com/" + method.replace(".", "/")
                    if write:
                        body = params.pop("param")
                        response = client.post(url, params=params, data={"param": body}, **({"files": files} if files else {}))
                    else:
                        response = client.get(url, params=params)
                else:
                    raise Blocked("此平台尚未提供内置电商连接器，请使用 CSV 管理")
                if not 200 <= response.status_code < 300:
                    raise (Uncertain if write else Rejected)(f"平台 HTTP {response.status_code}；请检查平台后台记录")
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("not an object")
        except (httpx.HTTPError, ValueError):
            # Never include exception URLs, which may contain access tokens.
            raise (Uncertain if write else Rejected)("平台连接中断或响应无法解析；请核对平台后台后再决定是否重试") from None
        success_field, success_value = {"wechat": ("errcode", "0"), "douyin": ("code", "10000"), "kuaishou": ("result", "1")}[self.platform]
        if success_field not in result:
            raise (Uncertain if write else Rejected)("平台响应缺少业务状态码，无法确认结果")
        if str(result[success_field]) != success_value:
            msg = result.get("sub_msg") or result.get("errmsg") or result.get("error_msg") or result.get("msg", "请求被拒绝")
            raise Rejected(redact(f"平台错误 {result[success_field]}：{msg}", self.creds))
        return result

    def mutate_product(self, action, payload):
        return self.call(METHODS[self.platform][action], payload, write=True)

    def orders(self, start, end):
        if end <= start or end - start > 7 * 86400:
            raise Blocked("订单查询范围须大于零且不超过 7 天")
        cursor, page, seen = "", 0, set()
        while page < 500:
            if self.platform == "wechat":
                result = self.call(METHODS[self.platform]["orders"], {"update_time_range": {"start_time": start, "end_time": end}, "page_size": 100, "next_key": cursor})
                if "order_id_list" not in result:
                    raise Rejected("微信返回结构变化：缺少 order_id_list")
                ids = result["order_id_list"]
                for oid in ids:
                    detail = self.call(METHODS[self.platform]["order"], {"order_id": str(oid)})
                    if not isinstance(detail.get("order"), dict):
                        raise Rejected("微信订单详情缺少 order 字段")
                    yield normalize_order(self.platform, detail["order"], self.store.get("templates", {}).get("inventory_status_map", {}))
                more, nxt = result.get("has_more", False), result.get("next_key", "")
            elif self.platform == "douyin":
                result = self.call(METHODS[self.platform]["orders"], {"update_time_start": start, "update_time_end": end, "page": page, "size": 100, "order_by": "update_time", "order_asc": True})
                data = result.get("data", {})
                if "shop_order_list" not in data or "total" not in data:
                    raise Rejected("抖店返回结构变化：缺少 shop_order_list / total")
                rows = data["shop_order_list"]
                for item in rows:
                    yield normalize_order(self.platform, item, self.store.get("templates", {}).get("inventory_status_map", {}))
                more = (page + 1) * 100 < int(data["total"])
                if more and not rows:
                    raise Rejected("抖店返回空页但 total 显示还有订单，请缩小时间范围")
                nxt = str(page + 1)
            else:
                result = self.call(METHODS[self.platform]["orders"], {"orderViewStatus": 1, "pageSize": 50, "sort": 2, "queryType": 2, "beginTime": start * 1000, "endTime": end * 1000, "cursor": cursor})
                data = result.get("data", {})
                if "orderList" not in data or "cursor" not in data:
                    raise Rejected("快手返回结构变化：缺少 orderList / cursor")
                for item in data["orderList"]:
                    yield normalize_order(self.platform, item, self.store.get("templates", {}).get("inventory_status_map", {}))
                nxt = data["cursor"]
                more = nxt != "nomore"
            if not more:
                return
            if not nxt or nxt in seen:
                raise Rejected("平台分页游标未推进，已停止同步；已获取记录仍保留")
            seen.add(nxt)
            cursor = nxt
            page += 1
            time.sleep(0.15)
        raise Rejected("达到 500 页上限，请缩小时间范围；已获取记录仍保留")


def normalize_order(platform, raw, status_map=None):
    # Preserve only operational fields, not buyer names/addresses/tokens.
    if platform == "wechat":
        detail = raw.get("order_detail", {})
        oid = raw.get("order_id")
        status = {10: "待付款", 20: "待发货", 21: "部分发货", 30: "待收货", 100: "已完成", 200: "售后取消", 250: "已取消"}.get(raw.get("status"), f"平台状态 {raw.get('status')}")
        items = [{"title": x.get("title", ""), "product_id": str(x.get("product_id", "")), "sku": str(x.get("sku_id", "")), "quantity": int(x.get("sku_cnt", 0))} for x in detail.get("product_infos", [])]
        amount = detail.get("price_info", {}).get("order_price")
        created = raw.get("create_time", 0)
    elif platform == "douyin":
        oid = raw.get("order_id")
        status = raw.get("order_status_desc") or f"平台状态 {raw.get('order_status')}"
        items = [{"title": x.get("product_name", ""), "product_id": str(x.get("product_id", "")), "sku": str(x.get("sku_id", "")), "quantity": int(x.get("item_num", 0))} for x in raw.get("sku_order_list", [])]
        amount = raw.get("pay_amount")
        created = raw.get("create_time", 0)
    else:
        base, item = raw.get("orderBaseInfo", {}), raw.get("orderItemInfo", {})
        oid = base.get("oid")
        status = base.get("statusDesc") or f"平台状态 {base.get('status')}"
        items = [{"title": item.get("itemTitle", ""), "product_id": str(item.get("itemId", "")), "sku": str(item.get("skuId", "")), "quantity": int(item.get("num", 0))}]
        amount = base.get("totalFee")
        created = int(base.get("createTime", 0)) // 1000
    if oid is None:
        raise Rejected("平台订单缺少唯一订单号，已停止同步")
    raw_status = str(raw.get('status') if platform == 'wechat' else raw.get('order_status') if platform == 'douyin' else base.get('status'))
    inventory_state = (status_map or {}).get(raw_status)
    if inventory_state not in ('reserve', 'sold', 'void', 'unknown'):
        if platform == 'wechat':
            inventory_state = {'10': 'reserve', '20': 'sold', '21': 'sold', '30': 'sold', '100': 'sold', '200': 'void', '250': 'void'}.get(raw_status, 'unknown')
        elif any(x in status for x in ('关闭', '取消', '退款')):
            inventory_state = 'void'
        elif any(x in status for x in ('待付款', '待支付')):
            inventory_state = 'reserve'
        elif any(x in status for x in ('待发货', '已支付', '备货', '待收货', '已发货', '已完成', '交易成功', '已收货')):
            inventory_state = 'sold'
        else:
            inventory_state = 'unknown'
    updated = raw.get('update_time', 0) if platform != 'kuaishou' else int(base.get('updateTime', 0)) // 1000
    return {'external_id': str(oid), 'status': status, 'raw_status': raw_status, 'inventory_state': inventory_state, 'remote_updated': int(updated or 0), 'amount_cents': int(amount) if amount is not None else None, 'items': items, 'created': created}


PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


def render_template(template, context):
    def lookup(path):
        cur = context
        try:
            for key in path.split("."):
                cur = cur[int(key)] if isinstance(cur, list) else cur[key]
        except (KeyError, IndexError, ValueError, TypeError):
            raise Blocked(f"发布模板缺少数据：{path}") from None
        return copy.deepcopy(cur)
    if isinstance(template, dict):
        if set(template) == {"$int"}:
            raw = lookup(template["$int"])
            if isinstance(raw, bool) or not re.fullmatch(r"-?\d+", str(raw)):
                raise Blocked(f"模板字段 {template['$int']} 不能转换为整数")
            return int(raw)
        if set(template) == {"$json"}:
            return db.dumps(lookup(template["$json"]))
        if "$map" in template:
            values = lookup(template["$map"])
            if not isinstance(values, list) or len(values) > 200:
                raise Blocked("模板 $map 目标必须是最多 200 项的列表")
            alias = template.get("as", "item")
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", alias) or alias in context:
                raise Blocked("模板 $map 别名无效或覆盖已有变量")
            return [render_template(template["template"], {**context, alias: v}) for v in values]
        return {k: render_template(v, context) for k, v in template.items()}
    if isinstance(template, list):
        return [render_template(v, context) for v in template]
    if isinstance(template, str):
        match = PATTERN.fullmatch(template)
        if match:
            return lookup(match[1])
        return PATTERN.sub(lambda m: str(lookup(m[1])), template)
    return template


def product_payload(store, product, listing, action):
    if action not in PLATFORMS[store["platform"]]["commerce"]:
        raise Blocked("此平台的该电商操作尚未接入；请到商家后台处理")
    check_credentials(store)
    if action != "product_create" and not listing.get("external_id"):
        raise Blocked("尚未关联该店铺的平台商品 ID")
    if action == "product_create" and listing.get("external_id"):
        raise Blocked("此商品已经关联平台 ID，请选择更新以避免重复创建")
    if action in ("product_list", "product_delist"):
        return {"product_id": listing["external_id"]}
    from .publishing import merged_config, built_in_payload, protect_stock
    if merged_config(store, listing):
        return protect_stock(built_in_payload(store, product, listing, action), action == 'product_create')
    template = store["templates"].get(action)
    if not isinstance(template, dict) or not template:
        raise Blocked("请先在店铺设置中配置此操作的商品发布模板（类目 / 运费 / SKU / 图片）")
    context = {"product": product, "listing": listing, "store": {"id": store["id"], "name": store["name"]}}
    payload = render_template(template, context)
    encoded = db.dumps(payload)
    if "__REQUIRED__" in encoded or "{{" in encoded:
        raise Blocked("模板还含有未填写的 __REQUIRED__ 或变量")
    if len(encoded) > 500_000:
        raise Blocked("商品请求过大")
    return protect_stock(payload, action == 'product_create')


def mpau_platform(platform):
    return "tencent" if platform == "wechat" else platform


def mpau_available():
    return bool(shutil.which("mpau"))


def mpau_args(store, payload, media_path):
    platform = store["platform"]
    if not store["video_enabled"]:
        raise Blocked("此店铺未启用视频发布")
    if not mpau_available():
        raise Blocked("视频引擎未安装；请使用默认 Docker 完整镜像")
    if payload.get("attach_product") and platform == "kuaishou":
        raise Blocked("快手挂商品请使用官方视频通道，需配置 Video App ID / Video Access Token")
    args = ["mpau", mpau_platform(platform), "upload-video", "--account", store["id"], "--file", str(media_path), "--headed"]
    title = payload["title"]
    if platform == "jd" and not 5 <= len(title) <= 27:
        raise Blocked("京东标题须为 5–27 个字符")
    if platform == "tmall" and len(title) > 30:
        raise Blocked("淘宝光合标题最多 30 个字符")
    if platform != "pdd":
        args += ["--title", title]
    if platform != "jd":
        description = payload.get("description", "")
        args += ["--desc", description or title, "--tags", payload.get("tags", "")]
    if payload.get("attach_product"):
        listing = payload.get("listing", {})
        if platform == "douyin":
            link = listing.get("product_link", "")
            if not link.startswith("https://"):
                raise Blocked("请在商品的店铺关联中填写该抖音店铺的商品分享链接")
            args += ["--product-link", link, "--product-title", title[:10]]
        else:
            goods_id = listing.get("external_id", "")
            if not str(goods_id).isdigit():
                raise Blocked("请在商品的店铺关联中填写此平台的数字商品 ID")
            args += ["--goods-id", str(goods_id)]
    return args


def run_mpau(args, job_id, timeout=1800):
    logfile = db.DATA / "logs" / (job_id + ".log")
    env = dict(os.environ, PYTHONUNBUFFERED="1", MPAU_DEBUG="false")
    # No shell. The web API cannot select an executable or inject extra flags.
    with logfile.open("wb") as log:
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise Uncertain("视频引擎超时，可能正在等待扫码、验证码或已提交；请核对后台") from None
    with logfile.open("rb") as f:
        f.seek(max(0, logfile.stat().st_size - 24000))
        output = redact(f.read().decode("utf-8", "replace"))
    if code != 0:
        raise Uncertain(f"视频引擎退出码 {code}；请查看任务日志并核对平台后台")
    return {"message": "视频引擎已执行完成，请在平台后台核对发布和商品挂载结果", "log_tail": output}
