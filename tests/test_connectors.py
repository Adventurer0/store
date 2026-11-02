import json
from urllib.parse import parse_qs
import httpx
import pytest
from app.connectors import (Commerce, Blocked, Rejected, Uncertain, canonical, douyin_params,
    kuaishou_params, normalize_order, product_payload, render_template, mpau_args)


def store(platform):
    return {"id": "abc123", "platform": platform, "api_enabled": True, "video_enabled": True,
            "credentials": {"app_key": "app", "app_secret": "secret", "access_token": "token", "sign_secret": "sign-secret"},
            "templates": {}, "name": "test"}


def test_known_signature_vectors():
    # Independently evaluated with Node crypto from the official signature specification.
    dy = douyin_params(store("douyin")["credentials"], "product.addV2", {"name": "测试&"}, "2026-11-02 10:00:00")
    assert dy["sign"] == "ec5ae09fd782408e34c84137d912f0211aa419a2bb7701b46c9716faee27a4cf"
    ks = kuaishou_params(store("kuaishou")["credentials"], "open.item.edit", {"title": "测试"}, 1793584800000)
    assert ks["sign"] == "4d0dfd7ee14223cd523a4f129ba913f6"
    assert "signSecret" not in ks


def test_templates_preserve_types_and_multi_sku():
    context = {"product": {"title": "箱包", "skus": [{"price": 12900, "stock": 0}, {"price": 13900, "stock": 7}]}}
    result = render_template({"title": "新品 {{product.title}}", "skus": {"$map": "product.skus", "as": "sku", "template": {"price": "{{sku.price}}", "stock": "{{sku.stock}}"}}}, context)
    assert result == {"title": "新品 箱包", "skus": [{"price": 12900, "stock": 0}, {"price": 13900, "stock": 7}]}
    with pytest.raises(Blocked, match="缺少数据"):
        render_template("{{product.not_here}}", context)


def test_large_ids_converted_server_side_without_rounding():
    ctx = {"listing": {"external_id": "90071992547409999"}, "list": [{"price": 12900}]}
    assert render_template({"$int": "listing.external_id"}, ctx) == 90071992547409999
    assert render_template({"$json": "list"}, ctx) == '[{"price":12900}]'


def test_no_create_without_authorization_or_mapping():
    s = store("wechat")
    with pytest.raises(Blocked, match="发布模板"):
        product_payload(s, {}, {}, "product_create")
    s["templates"] = {"product_create": {"title": "__REQUIRED__"}}
    with pytest.raises(Blocked, match="未填写"):
        product_payload(s, {}, {}, "product_create")
    s["templates"] = {"product_create": {"title": "test"}}
    with pytest.raises(Blocked, match="避免重复"):
        product_payload(s, {}, {"external_id": "already"}, "product_create")
    s["api_enabled"] = False
    with pytest.raises(Blocked, match="尚未启用"):
        product_payload(s, {}, {}, "product_create")


def test_douyin_exact_request_body_and_endpoint():
    def handle(req):
        assert req.url.host == "openapi-fxg.jinritemai.com"
        assert req.url.path == "/product/launchProduct"
        assert req.method == "POST"
        assert "param_json" not in req.url.params
        assert json.loads(req.content) == {"product_id": "123"}
        return httpx.Response(200, json={"code": 10000, "data": {}})
    result = Commerce(store("douyin"), httpx.MockTransport(handle)).mutate_product("product_list", {"product_id": "123"})
    assert result["code"] == 10000


def test_kuaishou_form_body():
    def handle(req):
        assert req.method == "POST" and req.url.path == "/open/item/edit"
        assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
        body = parse_qs(req.content.decode())
        assert json.loads(body["param"][0]) == {"itemId": "123", "title": "中文商品"}
        assert "signSecret" not in req.url.params
        return httpx.Response(200, json={"result": 1, "data": {"kwaiItemId": 123}})
    assert Commerce(store("kuaishou"), httpx.MockTransport(handle)).mutate_product("product_update", {"itemId": "123", "title": "中文商品"})["result"] == 1


@pytest.mark.parametrize("platform,result", [("wechat", {"errcode": 40001, "errmsg": "token invalid"}), ("douyin", {"code": 50002, "sub_msg": "rejected"}), ("kuaishou", {"result": 0, "error_msg": "rejected"})])
def test_business_error_never_counts_as_success(platform, result):
    api = Commerce(store(platform), httpx.MockTransport(lambda r: httpx.Response(200, json=result)))
    with pytest.raises(Rejected):
        api.call("irrelevant", {}, write=True)


def test_unexpected_200_and_timeout_are_uncertain():
    api = Commerce(store("wechat"), httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(Uncertain):
        api.call("channels/ec/product/add", {}, write=True)
    def timeout(req):
        raise httpx.ReadTimeout("secret URL containing token", request=req)
    api = Commerce(store("wechat"), httpx.MockTransport(timeout))
    with pytest.raises(Uncertain) as exc:
        api.call("channels/ec/product/add", {}, write=True)
    assert "secret URL" not in str(exc.value)


def test_wechat_order_cursor_and_details(monkeypatch):
    calls = []
    def handle(req):
        body = json.loads(req.content)
        calls.append((req.url.path, body))
        if req.url.path.endswith("list/get"):
            if body["next_key"] == "":
                return httpx.Response(200, json={"errcode": 0, "order_id_list": ["9007199254740999"], "has_more": True, "next_key": "next"})
            return httpx.Response(200, json={"errcode": 0, "order_id_list": [], "has_more": False})
        return httpx.Response(200, json={"errcode": 0, "order": {"order_id": body["order_id"], "status": 20, "order_detail": {"price_info": {"order_price": 12900}, "delivery_info": {"buyer_phone": "private"}}}})
    monkeypatch.setattr("app.connectors.time.sleep", lambda _: None)
    rows = list(Commerce(store("wechat"), httpx.MockTransport(handle)).orders(100, 200))
    assert len(rows) == 1 and rows[0]["external_id"] == "9007199254740999"
    assert rows[0]["status"] == "待发货" and rows[0]["amount_cents"] == 12900
    assert "private" not in json.dumps(rows)
    assert len(calls) == 3 and calls[2][1]["next_key"] == "next"


def test_order_pagination_fail_closed():
    api = Commerce(store("wechat"), httpx.MockTransport(lambda r: httpx.Response(200, json={"errcode": 0, "order_id_list": [], "has_more": True, "next_key": ""})))
    with pytest.raises(Rejected, match="游标"):
        list(api.orders(100, 200))


def test_douyin_starts_at_zero_and_uses_total(monkeypatch):
    pages = []
    def handle(req):
        page = json.loads(req.content)["page"]
        pages.append(page)
        return httpx.Response(200, json={"code": 10000, "data": {"shop_order_list": [{"order_id": str(page), "order_status_desc": "已发货", "pay_amount": 100}], "total": 101}})
    monkeypatch.setattr("app.connectors.time.sleep", lambda _: None)
    assert len(list(Commerce(store("douyin"), httpx.MockTransport(handle)).orders(100, 200))) == 2
    assert pages == [0, 1]


def test_kuaishou_terminates_at_nomore():
    def handle(req):
        body = json.loads(req.url.params["param"])
        assert body["beginTime"] == 100000 and body["cursor"] == ""
        return httpx.Response(200, json={"result": 1, "data": {"orderList": [{"orderBaseInfo": {"oid": 123, "status": 10, "totalFee": 12300, "createTime": 100000}, "orderItemInfo": {"itemTitle": "箱子", "num": 1}}], "cursor": "nomore"}})
    rows = list(Commerce(store("kuaishou"), httpx.MockTransport(handle)).orders(100, 200))
    assert rows[0]["amount_cents"] == 12300 and rows[0]["external_id"] == "123"


@pytest.mark.parametrize("platform,flag", [("douyin", "--product-link"), ("wechat", "--goods-id"), ("tmall", "--goods-id"), ("jd", "--goods-id"), ("pdd", "--goods-id")])
def test_video_arguments_are_platform_specific(platform, flag, monkeypatch):
    monkeypatch.setattr("app.connectors.mpau_available", lambda: True)
    payload = {"title": "六字测试标题", "description": "描述", "tags": "测试", "attach_product": True, "listing": {"product_link": "https://example.com/item", "external_id": "123456"}}
    args = mpau_args(store(platform), payload, "/data/media/test.mp4")
    assert args[:3] == ["mpau", "tencent" if platform == "wechat" else platform, "upload-video"]
    assert flag in args and "--headed" in args
    if platform == "pdd":
        assert "--title" not in args
    if platform == "jd":
        assert "--desc" not in args and "--tags" not in args


def test_kuaishou_link_never_silently_dropped(monkeypatch):
    monkeypatch.setattr("app.connectors.mpau_available", lambda: True)
    with pytest.raises(Blocked, match="快手挂商品"):
        mpau_args(store("kuaishou"), {"title": "title", "attach_product": True}, "/data/test.mp4")
