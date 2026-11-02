from typing import Annotated, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

Platform = Literal["douyin", "kuaishou", "wechat", "tmall", "jd", "pdd"]
Text = Annotated[str, Field(min_length=1, max_length=200)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(StrictModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=256)


class StoreInput(StrictModel):
    name: Text
    platform: Platform
    api_enabled: bool = False
    commerce_enabled: bool = True
    video_enabled: bool = True
    credentials: dict[str, str] = Field(default_factory=dict)
    templates: dict = Field(default_factory=dict)

    @field_validator("credentials")
    @classmethod
    def credential_keys(cls, v):
        if set(v) - {"app_key", "app_secret", "access_token", "sign_secret", "video_app_id", "video_access_token"}:
            raise ValueError("不支持的凭据字段")
        if any(len(s) > 5000 for s in v.values()):
            raise ValueError("凭据过长")
        return v


class SKU(StrictModel):
    code: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    price_cents: int = Field(strict=True, ge=0, le=100_000_000)
    stock: int = Field(strict=True, ge=0, le=100_000_000)


class ProductInput(StrictModel):
    title: Text
    description: str = Field(default="", max_length=10000)
    category: str = Field(default="", max_length=100)
    skus: list[SKU] = Field(min_length=1, max_length=200)
    images: list[str] = Field(default_factory=list, max_length=30)
    image_ids: list[str] = Field(default_factory=list, max_length=30)
    detail_image_ids: list[str] = Field(default_factory=list, max_length=30)
    cover_id: str = ""
    extra: dict = Field(default_factory=dict)
    version: int | None = None

    @model_validator(mode="after")
    def validate_data(self):
        codes = [s.code for s in self.skus]
        if len(set(codes)) != len(codes):
            raise ValueError("同一商品内的 SKU 编码不能重复")
        if any(not x.startswith("https://") or len(x) > 2000 for x in self.images):
            raise ValueError("商品图片 URL 必须以 https:// 开头")
        return self


class ListingInput(StrictModel):
    external_id: str = Field(default="", max_length=100)
    product_link: str = Field(default="", max_length=2000)
    publish_config: dict = Field(default_factory=dict)
    sku_map: dict[str, str] = Field(default_factory=dict)
    owned_product: bool = False

    @field_validator("product_link")
    @classmethod
    def valid_link(cls, value):
        if value and not value.startswith("https://"):
            raise ValueError("商品链接必须以 https:// 开头")
        return value


class Batch(StrictModel):
    kind: Literal["product_create", "product_update", "product_list", "product_delist", "order_sync", "video_publish", "inventory_sync"]
    store_ids: list[str] = Field(min_length=1, max_length=30)
    product_ids: list[str] = Field(default_factory=list, max_length=100)
    media_id: str = ""
    product_id: str = ""
    title: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=2000)
    tags: str = Field(default="", max_length=300)
    attach_product: bool = True
    cover_id: str = ""
    sales_paused: bool = False
    scheduled: int | None = None
    since: int | None = None
    until: int | None = None


class Note(StrictModel):
    note: str = Field(max_length=2000)


class JobAction(StrictModel):
    action: Literal["cancel", "retry", "confirm", "mark_failed"]
    checked_remote: bool = False
    evidence: str = Field(default="", max_length=2000)


class Resolve(StrictModel):
    code: str = Field(pattern=r"^\d{6}$")


class Allocation(StrictModel):
    version: int
    quantities: dict[str, dict[str, Annotated[int, Field(strict=True, ge=0, le=100_000_000)]]]


class InventoryAdjustment(StrictModel):
    version: int
    sku_code: str = Field(min_length=1, max_length=80)
    delta: int = Field(strict=True, ge=-100_000_000, le=100_000_000)
    reason: str = Field(min_length=1, max_length=300)
