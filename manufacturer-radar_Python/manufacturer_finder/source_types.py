"""Discovery source contract. Discovery never directly admits a manufacturer."""
from dataclasses import dataclass, field
from .models import SearchHit


SOURCE_LABELS = {
    "madeinchina": "中国制造网", "b2b168": "八方资源网", "gys": "供应商网",
    "hc360": "慧聪网", "aicaigou": "百度爱采购", "1688": "1688找工厂",
    "catalog": "公开展商/工厂名录", "amap": "高德地图API", "baidu_map": "百度地图API",
    "import": "名单与链接导入", "browser": "浏览器辅助", "douyin": "抖音企业主页", "kuaishou": "快手企业主页",
}


@dataclass
class LeadSeed:
    name: str = ""
    url: str = ""
    phones: list[str] = field(default_factory=list)
    address: str = ""
    source: str = "import"
    source_url: str = ""
    excerpt: str = ""
    products: list[str] = field(default_factory=list)


@dataclass
class DiscoveryBatch:
    hits: list[SearchHit] = field(default_factory=list)
    seeds: list[LeadSeed] = field(default_factory=list)
    statuses: dict[str, dict] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
