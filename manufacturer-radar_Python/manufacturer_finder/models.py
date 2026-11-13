"""Shared, JSON-compatible domain models. All timestamps are local ISO-8601."""
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional
from uuid import uuid4


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class SearchHit:
    engine: str
    query: str
    page: int
    title: str
    url: str
    snippet: str = ""
    search_url: str = ""
    ad_status: str = "未标识"
    fetched_at: str = field(default_factory=now)
    source_url: str = ""
    company_hint: str = ""
    source_excerpt: str = ""
    lookup_query: str = ""
    discovery_source: str = ""


@dataclass
class Evidence:
    kind: str
    url: str
    excerpt: str
    engine: str = ""
    query: str = ""
    page: int = 1
    status: str = "待核实"
    fetched_at: str = field(default_factory=now)


@dataclass
class Company:
    id: str = field(default_factory=lambda: uuid4().hex)
    name: str = ""
    name_confirmed: bool = False
    phones: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    hits: list[SearchHit] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    negative_status: str = "未检查"
    score: int = 0
    score_reasons: list[str] = field(default_factory=list)
    notes: str = ""
    updated_at: str = field(default_factory=now)
    page_verified: bool = False
    verification_url: str = ""
    products: list[str] = field(default_factory=list)
    business_scope: str = ""
    region: str = ""
    address: str = ""
    admission_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        data = dict(data)
        data["hits"] = [SearchHit(**v) for v in data.get("hits", [])]
        data["evidence"] = [Evidence(**v) for v in data.get("evidence", [])]
        return cls(**data)


@dataclass
class SearchOptions:
    query: str
    engines: list[str] = field(default_factory=lambda: ["baidu", "sogou", "so360", "bing", "toutiao", "shenma"])
    pages: int = 3
    budget_seconds: int = 120
    enrich: bool = True
    check_negative: bool = True
    negative_limit: int = 12
    request_timeout: float = 8.0
    discovery_channels: list[str] = field(default_factory=list)
    b2b_sources: list[str] = field(default_factory=lambda: ["madeinchina", "b2b168", "gys", "hc360", "aicaigou", "1688"])
    catalog_urls: list[str] = field(default_factory=list)
    map_provider: str = "amap"
    map_key: str = ""
    map_city: str = ""
    map_authorized: bool = False

    def __post_init__(self):
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("请输入搜索关键词")
        self.pages = max(1, min(3, int(self.pages)))
        self.budget_seconds = max(30, min(600, int(self.budget_seconds)))
        self.negative_limit = max(0, min(50, int(self.negative_limit)))


@dataclass
class RunResult:
    id: str = field(default_factory=lambda: uuid4().hex)
    query: str = ""
    started_at: str = field(default_factory=now)
    finished_at: str = ""
    companies: list[Company] = field(default_factory=list)
    engine_status: dict[str, dict] = field(default_factory=dict)
    cancelled: bool = False
    timed_out: bool = False
    candidates: list[SearchHit] = field(default_factory=list)
    qualification_stats: dict[str, int] = field(default_factory=dict)
    collection_log: list[dict] = field(default_factory=list)
    source_status: dict[str, dict] = field(default_factory=dict)
    discovery_seeds: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        data = dict(data)
        data["companies"] = [Company.from_dict(v) for v in data.get("companies", [])]
        data["candidates"] = [SearchHit(**v) for v in data.get("candidates", [])]
        return cls(**data)
