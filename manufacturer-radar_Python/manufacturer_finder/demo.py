"""Offline UI fixtures. Every company, clue, and link is fictional."""
from .models import Company, Evidence, RunResult, SearchHit, now
from .providers import ENGINES
from .scoring import score_company


DEMO_NOTICE = "演示数据：以下厂家、电话、评分和负面线索均为虚构，未发起任何网络请求。"


def make_demo_run() -> RunResult:
    query = "演示数据 · 虚构厂家"
    first_hit = SearchHit(
        engine="baidu", query=query, page=1,
        title="【虚构演示】演示甲号机械制造有限公司 - 工厂介绍",
        url="https://factory-a.example/about", snippet="纯演示的厂家信息，非真实商业主体。",
        search_url="https://search.example/demo/baidu?page=1", ad_status="广告标识（演示）",
    )
    repeat_hit = SearchHit(
        engine="bing", query=query, page=2,
        title="【虚构演示】演示甲号机械制造有限公司",
        url="https://factory-a.example/products", snippet="同一家虚构厂家在另一引擎出现。",
        search_url="https://search.example/demo/bing?page=2",
    )
    second_hit = SearchHit(
        engine="sogou", query=query, page=1,
        title="【虚构演示】演示乙号设备有限公司",
        url="https://factory-b.example/", snippet="演示缺少电话和存在待核实线索时的评分。",
        search_url="https://search.example/demo/sogou?page=1",
    )
    third_hit = SearchHit(
        engine="so360", query=query, page=3,
        title="【虚构演示】演示丙号包装机械厂 - 主营产品",
        url="https://factory-c.example/products", snippet="虚构包装机械生产厂家，演示从详情页取得主营与地区。",
        search_url="https://search.example/demo/so360?page=3",
    )
    companies = [
        Company(
            name="演示甲号机械制造有限公司（虚构）", name_confirmed=True,
            phones=["138****0001（演示，无法拨打）"],
            urls=[first_hit.url, repeat_hit.url], hits=[first_hit, repeat_hit],
            negative_status="本轮未发现", notes=DEMO_NOTICE + "；‘本轮未发现’也不表示不存在风险。",
            page_verified=True, verification_url=first_hit.url,
            products=["自动化装配设备", "机械零件"], business_scope="机械零件加工、自动化装配设备生产与销售（虚构）",
            region="江苏省苏州市（虚构）", address="苏州市演示工业园甲号路（虚构，不存在）",
            admission_reasons=["演示：已读取虚构企业介绍页并识别厂家全称", "演示：页面说明自产机械零件与装配设备，匹配制造业务"],
        ),
        Company(
            name="演示乙号设备有限公司（虚构）", name_confirmed=True,
            urls=[second_hit.url], hits=[second_hit],
            evidence=[Evidence(
                kind="负面线索", url="https://feedback.example/fictional-complaint",
                excerpt="【虚构演示】同名厂家存在一条模拟投诉，仅用于展示待核实线索。",
                engine="sogou", query='"演示乙号设备有限公司（虚构）" 投诉',
                page=1, status="待核实",
            )],
            negative_status="发现待核实线索", notes=DEMO_NOTICE,
            page_verified=True, verification_url=second_hit.url,
            products=["不锈钢水箱", "消防水箱"], business_scope="不锈钢水箱、消防水箱的生产、安装与销售（虚构）",
            region="广东省佛山市（虚构）", address="佛山市演示大道乙号（虚构，不存在）",
            admission_reasons=["演示：已读取虚构厂家详情页并识别全称", "演示：详情页明确列出水箱生产业务与产品"],
        ),
        Company(
            name="演示丙号包装机械厂（虚构）", name_confirmed=True,
            urls=[third_hit.url], hits=[third_hit], negative_status="检查受限",
            page_verified=True, verification_url=third_hit.url,
            products=["自动封箱机", "喷码设备"], business_scope="包装机械生产、加工与销售（虚构）",
            region="山东省济南市（虚构）", address="济南市演示路丙号（虚构，不存在）",
            admission_reasons=["演示：已读取虚构产品详情页，页面展示厂家名称", "演示：自产包装机械业务及产品信息可回溯"],
            notes=DEMO_NOTICE + "；未公开电话，模拟负面检索受限，均据实影响完整度评分。",
        ),
    ]
    for company in companies:
        page = company.verification_url
        company.evidence.extend([
            Evidence(kind="详情页核验", url=page, excerpt="【虚构演示】模拟读取详情页，识别公司身份、制造业务和产品；未发起真实网络请求。", status="虚构演示"),
            Evidence(kind="名称", url=page, excerpt=f"【虚构演示】厂家名称：{company.name}", status="虚构演示"),
            Evidence(kind="主营产品", url=page, excerpt=f"【虚构演示】主营产品：{'、'.join(company.products)}", status="虚构演示"),
            Evidence(kind="经营范围", url=page, excerpt=f"【虚构演示】{company.business_scope}", status="虚构演示"),
            Evidence(kind="地区地址", url=page, excerpt=f"【虚构演示】地址：{company.address}", status="虚构演示"),
        ])
        score_company(company)
    engine_status = {}
    for engine in ENGINES:
        engine_status[engine] = {
            "status": "demo", "pages": 0,
            "hits": sum(hit.engine == engine for company in companies for hit in company.hits),
            "requested_pages": 3, "message": "离线虚构演示，未进行真实搜索",
        }
    return RunResult(
        query=query, companies=companies, engine_status=engine_status,
        finished_at=now(), candidates=[first_hit, repeat_hit, second_hit, third_hit],
        qualification_stats={"candidates": 4, "visited": 4, "accepted": 4, "rejected": 0, "unvisited": 0, "failed": 0},
        collection_log=[{"status": "accepted", "url": source.url, "title": source.title,
                         "reason": "虚构演示：详情页身份、主营与地区证据完整",
                         "source_count": 1, "final_url": source.url, "contact_pages": []}
                        for company in companies for source in company.hits],
    )
