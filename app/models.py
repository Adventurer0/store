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


class KuaishouPreviewBusiness(StrictModel):
    category_path: list[Text] = Field(default_factory=list, max_length=8)
    brand: str = Field(default='', max_length=100)
    material: str = Field(default='', max_length=100)
    closure: str = Field(default='', max_length=100)
    freight_template: str = Field(default='', max_length=100)


class CommercePrepareInput(StrictModel):
    product_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    upload_images: bool = Field(default=False, strict=True)
    business: KuaishouPreviewBusiness | None = None


MediaId = Annotated[str, Field(pattern=r'^[a-f0-9]{32}$')]
MediaTag = Annotated[str, Field(min_length=1, max_length=80)]


class MediaMetadataPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=240)
    category: str | None = Field(default=None, max_length=100)
    tags: list[MediaTag] | None = Field(default=None, max_length=30)

    @model_validator(mode='after')
    def valid_patch(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError('请提供要更新的素材名称、分类或标签，字段不能为 null')
        if self.name and any(ch in self.name for ch in ('/', '\\', '\x00', '\r', '\n')):
            raise ValueError('素材名称不能包含路径或控制字符')
        if self.tags is not None:
            self.tags = list(dict.fromkeys(self.tags))
        return self


class MediaBatchMetadata(StrictModel):
    ids: list[MediaId] = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    tags: list[MediaTag] | None = Field(default=None, max_length=30)

    @model_validator(mode='after')
    def valid_patch(self):
        if not ({'category', 'tags'} & self.model_fields_set):
            raise ValueError('请提供要更新的分类或标签')
        if any(getattr(self, key) is None for key in self.model_fields_set - {'ids'}):
            raise ValueError('素材分类或标签不能为 null')
        self.ids = list(dict.fromkeys(self.ids))
        if self.tags is not None:
            self.tags = list(dict.fromkeys(self.tags))
        return self


class MediaBatchDelete(StrictModel):
    ids: list[MediaId] = Field(min_length=1, max_length=200)

    @field_validator('ids')
    @classmethod
    def unique_ids(cls, value):
        return list(dict.fromkeys(value))


class Allocation(StrictModel):
    version: int
    quantities: dict[str, dict[str, Annotated[int, Field(strict=True, ge=0, le=100_000_000)]]]


class InventoryAdjustment(StrictModel):
    version: int
    sku_code: str = Field(min_length=1, max_length=80)
    delta: int = Field(strict=True, ge=-100_000_000, le=100_000_000)
    reason: str = Field(min_length=1, max_length=300)
