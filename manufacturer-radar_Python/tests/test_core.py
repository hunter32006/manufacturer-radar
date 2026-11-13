import csv
import json
from pathlib import Path
from threading import Thread

from openpyxl import load_workbook
import pytest

from manufacturer_finder.extraction import (
    _names, company_from_hit, enrich_company, extract_phones, merge_companies, negative_evidence,
)
from manufacturer_finder.models import Company, Evidence, RunResult, SearchHit
from manufacturer_finder.scoring import is_page_qualified, score_company
from manufacturer_finder.storage import Storage, export_csv, export_json, export_xlsx


def hit(title="山东蓝天机械有限公司 - 官方网站", snippet="联系电话：13800138000", **kwargs):
    options = dict(engine="baidu", query="机械厂家", page=1, title=title,
                   url="https://example.com/contact", snippet=snippet)
    options.update(kwargs)
    return SearchHit(**options)


def qualified_company():
    company = company_from_hit(hit())
    company.page_verified = True
    company.verification_url = "https://example.com/about"
    company.products = ["工业机械", "输送设备"]
    company.business_scope = "工业机械与输送设备的生产、加工与销售"
    company.region = "山东省济南市"
    company.address = "山东省济南市工业园"
    company.admission_reasons = ["已进入企业详情页并识别全称", "页面有与关键词相关的机械生产业务"]
    for kind, excerpt in [
        ("详情页核验", "已读取详情页，山东蓝天机械有限公司从事工业机械与输送设备生产"),
        ("名称", company.name), ("主营产品", "工业机械、输送设备"),
        ("经营范围", company.business_scope), ("地区地址", company.address),
        ("电话", "联系电话：13800138000"),
    ]:
        company.evidence.append(Evidence(kind=kind, url=company.verification_url, excerpt=excerpt))
    return company


def test_name_phone_and_evidence_attribution():
    company = company_from_hit(hit())
    assert company.name == "山东蓝天机械有限公司"
    assert company.name_confirmed
    assert company.phones == ["13800138000"]
    assert {e.kind for e in company.evidence} == {"收录", "名称", "电话"}
    assert all(e.url == "https://example.com/contact" for e in company.evidence)
    assert all(e.engine == "baidu" and e.page == 1 for e in company.evidence)


def test_unknown_name_is_retained_as_title_without_claiming_identity():
    company = company_from_hit(hit(title="机械供应厂家大全 - 批发报价", snippet="数百家供应商 13800138000"))
    assert not company.name_confirmed
    assert company.name == "机械供应厂家大全 - 批发报价"
    assert not company.phones
    assert any(e.kind == "页面电话（未归属）" for e in company.evidence)


def test_unknown_name_explicit_contact_is_retained_with_attribution_warning():
    company = company_from_hit(hit(title="机械供应信息", snippet="联系电话：13800138000"))
    assert not company.name_confirmed
    assert company.phones == ["13800138000"]
    assert "归属" in company.notes
    assert any(e.kind == "页面电话（未归属）" for e in company.evidence)


def test_factories_and_multiple_supplier_snippets():
    company = company_from_hit(hit(title="潍坊精工机械厂_官网", snippet="联系潍坊精工机械厂或潍坊飞扬机械有限公司，电话13800138000"))
    assert company.name == "潍坊精工机械厂"
    assert not company.phones


@pytest.mark.parametrize("prose", ["如果出厂", "设备返厂", "产品原厂", "我们已经建厂", "全国十大机械厂", "知名机械厂排行榜", "如何选择工厂", "国内一线大厂"])
def test_prose_ending_factory_is_not_a_manufacturer_identity(prose):
    assert _names(prose, allow_generic_factory=True) == []
    company = company_from_hit(hit(title=prose, snippet=""))
    assert not company.name_confirmed


def test_body_factory_requires_contact_or_identity_context_before_adoption():
    company = company_from_hit(hit(title="设备出厂流程介绍", snippet=""))
    enrich_company(company, "<title>机械行业新闻</title><p>如果出厂之前没有检查，将有风险。</p><p>来自山东精工机械厂的介绍</p>", "https://news.example/article")
    assert not company.name_confirmed
    assert company.name == "设备出厂流程介绍"
    enrich_company(company, "<title>企业介绍</title><p>厂家名称：山东精工机械厂</p><p>联系电话：0531-88888888</p>", "https://factory.example/about")
    assert company.name == "山东精工机械厂"
    assert company.name_confirmed


def test_phone_formats_and_no_long_identity_number():
    text = "手机 +86 138 0013 8000；固话(021)55556666转123；热线400-123-4567；证号913701381234567890"
    assert extract_phones(text) == ["13800138000", "021-55556666 转123", "400-123-4567"]
    assert extract_phones("证号 913801380001380001") == []


def test_enrichment_confirms_name_and_keeps_page_provenance():
    company = company_from_hit(hit(title="齿轮制造商官网", snippet=""))
    enrich_company(company, "<title>山东蓝天机械有限公司</title><h1>山东蓝天机械有限公司</h1><p>联系我们：0531-88888888</p>", "https://example.com/about")
    assert company.name_confirmed
    assert company.name == "山东蓝天机械有限公司"
    assert company.phones == ["0531-88888888"]
    phone = next(e for e in company.evidence if e.kind == "电话")
    assert phone.url == "https://example.com/about"
    assert "0531-88888888" in phone.excerpt


def test_enrichment_will_not_take_a_competitors_phone():
    company = company_from_hit(hit(snippet=""))
    enrich_company(company, "<title>山东蓝天机械有限公司</title><h1>山东蓝天机械有限公司</h1><div>合作企业：潍坊飞扬机械有限公司，电话13900139000</div>", "https://example.com/")
    assert company.phones == []
    assert any(e.kind == "页面电话（未归属）" for e in company.evidence)
    company = Company(name="蓝天机械有限公司", name_confirmed=True)
    enrich_company(company, "<title>山东蓝天机械有限公司</title><p>联系电话：13900139000</p>", "https://example.com/")
    assert not company.phones


def test_merging_uses_exact_names_and_urls_not_domains_phones_or_platforms():
    a = company_from_hit(hit(snippet="电话13800138000", url="https://market.example/vendor/a"))
    duplicate = company_from_hit(hit(engine="bing", url="https://other.example/a"))
    b = company_from_hit(hit(title="潍坊飞扬机械有限公司", snippet="电话13800138000", url="https://market.example/vendor/b"))
    merged = merge_companies([a, duplicate, b])
    assert len(merged) == 2
    assert len(merged[0].hits) == 2
    assert len(merged[0].urls) == 2
    assert len(a.hits) == 1  # input records remain independent
    # Different full identities must survive even on an identical directory page.
    b.urls = a.urls[:]
    assert len(merge_companies([a, b])) == 2
    unknown1 = company_from_hit(hit(title="供应商目录", snippet="", url="https://a.example:443/contact?b=2&a=1&utm_source=test"))
    unknown2 = company_from_hit(hit(title="其他标题", snippet="", url="https://A.example/contact?a=1&b=2"))
    assert len(merge_companies([unknown1, unknown2])) == 1
    unknown2.urls = ["https://a.example/contact?a=1&b=3"]
    assert len(merge_companies([unknown1, unknown2])) == 2


@pytest.mark.parametrize("text", [
    "山东蓝天机械有限公司无投诉记录", "山东蓝天机械有限公司投诉电话0531-88888888",
    "山东蓝天机械有限公司非诈骗企业", "山东蓝天机械有限公司没有质量问题",
    "未发现山东蓝天机械有限公司有行政处罚", "山东蓝天机械有限公司反诈骗宣传",
    "山东蓝天机械有限公司投诉举报热线12315", "山东蓝天机械有限公司无失信被执行人信息",
    "山东蓝天机械有限公司未收到投诉", "山东蓝天机械有限公司投诉处理制度",
    "山东蓝天机械有限公司是否存在质量问题", "山东蓝天机械有限公司诈骗吗",
    "山东蓝天机械有限公司未被投诉", "山东蓝天机械有限公司并未被行政处罚",
    "山东蓝天机械有限公司未列入失信名单", "山东蓝天机械有限公司投诉为零",
    "山东蓝天机械有限公司否认存在欺诈", "山东蓝天机械有限公司不涉及诉讼",
    "山东蓝天机械有限公司欺诈指控不属实",
])
def test_negative_neutral_or_denied_text_is_not_an_allegation(text):
    company = company_from_hit(hit())
    assert negative_evidence(company, hit(title=text, snippet="")) is None


def test_negative_needs_same_full_name_and_same_sentence():
    company = company_from_hit(hit())
    assert negative_evidence(company, hit(title="蓝天机械公司拖欠货款", snippet="")) is None
    assert negative_evidence(company, hit(title="山东新蓝天机械有限公司拖欠货款", snippet="")) is None
    assert negative_evidence(company, hit(title="山东蓝天机械有限公司。另一供应商被投诉", snippet="")) is None
    assert negative_evidence(company, hit(title="山东蓝天机械有限公司官网", snippet="", query="山东蓝天机械有限公司 诈骗")) is None
    result = negative_evidence(company, hit(title="网友投诉山东蓝天机械有限公司拖欠货款", snippet=""))
    assert result is not None
    assert result.kind == "负面线索"
    assert "待核实" in result.status
    assert "拖欠货款" in result.excerpt


def test_score_is_reproducible_source_deduplicated_and_explained():
    company = qualified_company()
    company.hits.append(hit(engine="bing"))
    company.negative_status = "本轮未发现"
    score_company(company)
    assert company.score == 100
    score_company(company)
    assert company.score == 100
    e = Evidence(kind="负面线索", url="https://news.example/1", excerpt="待核实投诉")
    company.evidence.extend([e, Evidence(kind="负面线索", url=e.url, excerpt="同来源重复收录")])
    score_company(company)
    assert company.score == 85
    assert company.negative_status == "发现待核实线索"
    company.evidence.extend([Evidence(kind="负面线索", url=f"https://news.example/{i}", excerpt="待核实") for i in (2, 3, 4)])
    score_company(company)
    assert company.score == 70
    assert any("上限 30" in reason for reason in company.score_reasons)


def test_search_snippet_cannot_receive_admitted_manufacturer_score():
    candidate = company_from_hit(hit())
    candidate.hits.append(hit(engine="bing"))
    candidate.negative_status = "本轮未发现"
    score_company(candidate)
    assert not is_page_qualified(candidate)
    assert candidate.score <= 25
    assert any("不计入正式厂家" in reason for reason in candidate.score_reasons)
    # A flag alone cannot claim that a detail page was read and identified.
    candidate.page_verified = True
    candidate.verification_url = "https://example.com/about"
    candidate.admission_reasons = ["只有一个声明，没有页面原文证据"]
    score_company(candidate)
    assert not is_page_qualified(candidate)
    assert candidate.score <= 25
    verified = qualified_company()
    verified.hits.append(hit(engine="bing"))
    verified.negative_status = "本轮未发现"
    score_company(verified)
    assert is_page_qualified(verified)
    assert verified.score == 100
    assert any("已进入详情页" in reason for reason in verified.score_reasons)


def test_missing_phone_reduces_completeness_without_rejecting_factory():
    company = qualified_company()
    company.hits.append(hit(engine="bing"))
    company.negative_status = "本轮未发现"
    company.phones = []
    company.evidence = [e for e in company.evidence if e.kind != "电话"]
    score_company(company)
    assert is_page_qualified(company)
    assert company.score == 85
    assert any("电话" in reason and reason.startswith("+0") for reason in company.score_reasons)


def test_contact_phone_requires_visited_same_identity_page_proof():
    company = qualified_company()
    company.hits.append(hit(engine="bing"))
    company.negative_status = "本轮未发现"
    company.evidence = [e for e in company.evidence if e.kind != "电话"]
    contact = "https://example.com/contact-us"
    company.evidence.append(Evidence(kind="电话", url=contact, excerpt="联系电话：13800138000"))
    score_company(company)
    assert company.score == 85
    company.evidence.extend([
        Evidence(kind="联系页核验", url=contact, excerpt="已进入联系页，页面为该同主体厂家联系方式"),
        Evidence(kind="名称", url=contact, excerpt=company.name),
    ])
    score_company(company)
    assert company.score == 85  # no visited-URL record yet
    company.urls.append(contact)
    score_company(company)
    assert company.score == 100
    company.evidence[-1].excerpt = "山东飞扬机械有限公司"
    score_company(company)
    assert company.score == 85  # a different firm's contact page is insufficient


def test_sqlite_roundtrip_and_atomic_failed_update(tmp_path):
    storage = Storage(tmp_path / "data" / "runs.sqlite")
    result = RunResult(
        query="机械厂家", companies=[qualified_company()], finished_at="2026-11-11T13:00:00+08:00",
        candidates=[hit(), hit(title="仅搜索到的行业文章", snippet="没有厂家身份")],
        qualification_stats={"candidates": 2, "visited": 2, "accepted": 1, "rejected": 1},
        collection_log=[{"url": "https://example.com/about", "status": "accepted", "name": "山东蓝天机械有限公司"}],
    )
    result.companies[0].negative_status = "检查受限"
    score_company(result.companies[0])
    storage.save_run(result)
    assert storage.load_run(result.id).to_dict() == result.to_dict()
    assert storage.list_runs()[0]["count"] == 1
    assert storage.list_runs()[0]["legacy_unverified_count"] == 0
    assert storage.load_run(result.id).companies[0].products == ["工业机械", "输送设备"]
    assert storage.load_run(result.id).companies[0].verification_url == "https://example.com/about"
    assert len(storage.load_run(result.id).candidates) == 2
    assert storage.load_run(result.id).qualification_stats["accepted"] == 1
    assert storage.load_run("missing") is None
    result.engine_status = {"bad": {"value": float("nan")}}
    with pytest.raises(ValueError):
        storage.save_run(result)
    assert storage.load_run(result.id).engine_status == {}
    # Worker and GUI calls can use one Storage instance without connection affinity.
    errors = []
    def worker():
        try:
            assert storage.load_run(result.id).query == "机械厂家"
        except Exception as exc:
            errors.append(exc)
    thread = Thread(target=worker)
    thread.start()
    thread.join()
    assert errors == []
    storage.close()


def test_history_does_not_count_legacy_search_links_as_factories(tmp_path):
    storage = Storage(tmp_path / "legacy.sqlite")
    old = RunResult(query="旧版原始搜索线索", companies=[company_from_hit(hit())])
    payload = old.to_dict()
    for attribute in ("page_verified", "verification_url", "products", "business_scope", "region", "address", "admission_reasons"):
        payload["companies"][0].pop(attribute)
    for attribute in ("candidates", "qualification_stats", "collection_log"):
        payload.pop(attribute)
    raw_payload = json.dumps(payload, ensure_ascii=False)
    with storage._connection() as connection:
        connection.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (old.id, old.query, old.started_at, old.finished_at, 1, 0, 0, raw_payload))
    summary = storage.list_runs()[0]
    assert summary["count"] == 0
    assert summary["old_count"] == 1
    assert summary["legacy_unverified_count"] == 1
    restored = storage.load_run(old.id)
    assert not restored.companies[0].page_verified
    assert restored.companies[0].name == "山东蓝天机械有限公司"
    with storage._connection() as connection:
        assert connection.execute("SELECT payload FROM runs WHERE id=?", (old.id,)).fetchone()[0] == raw_payload
    storage.close()


def test_exports_keep_links_evidence_and_neutralize_formulas(tmp_path):
    company = company_from_hit(hit(title="=HYPERLINK(\"https://bad.example\",\"x\")", snippet=""))
    company.notes = " \t=1+1"
    company.phones = ["+8613800138000"]
    company.evidence = [Evidence(kind="负面线索", url="https://source.example/report", excerpt="@SUM(1,2)", query="-10+20")]
    company.score_reasons = ["说明：仅参考"]
    run = RunResult(query="机械厂家", companies=[company])
    csv_path, xlsx_path, json_path = (tmp_path / name for name in ("结果.csv", "结果.xlsx", "结果.json"))
    export_csv(run.companies, csv_path)
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    with csv_path.open(encoding="utf-8-sig", newline="") as file:
        row = list(csv.DictReader(file))[0]
    assert row["厂家名称"].startswith("'=")
    assert row["备注"].startswith("' \t=")
    assert row["电话"].startswith("'+")
    assert "https://source.example/report" in row["证据详情JSON"]
    export_xlsx(run.companies, xlsx_path)
    workbook = load_workbook(xlsx_path)
    assert {"厂家汇总", "搜索记录", "证据明细", "收录核验", "评分说明"} <= set(workbook.sheetnames)
    assert all(cell.data_type != "f" for sheet in workbook for row in sheet for cell in row)
    assert workbook["证据明细"]["D2"].value == "https://source.example/report"
    assert workbook["证据明细"]["E2"].value == "'@SUM(1,2)"
    assert workbook["证据明细"]["G2"].value == "'-10+20"
    workbook.close()
    export_json(run, json_path)
    assert RunResult.from_dict(json.loads(json_path.read_text(encoding="utf-8"))).to_dict() == run.to_dict()


def test_exports_include_detail_qualification_products_region_and_raw_links(tmp_path):
    company = qualified_company()
    csv_path = tmp_path / "qualified.csv"
    export_csv([company], csv_path)
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["厂家名称"] == company.name
    assert row["厂家详情页"] == company.verification_url
    assert "已进入详情页" in row["详情来源验证"]
    assert row["主营"] == "工业机械、输送设备"
    assert row["地区"] == company.region
    assert row["地址"] == company.address
    assert company.admission_reasons[0] in row["收录依据"]
    assert company.hits[0].url in row["原始链接"]
    path = tmp_path / "qualified.xlsx"
    export_xlsx([company], path)
    workbook = load_workbook(path)
    verification = workbook["收录核验"]
    assert verification["C2"].value == "是"
    assert verification["D2"].value == company.verification_url
    assert verification["E2"].value == "工业机械、输送设备"
    workbook.close()


def test_demo_contains_three_fictional_page_qualified_manufacturers_only():
    from manufacturer_finder.demo import make_demo_run
    run = make_demo_run()
    assert len(run.companies) == 3
    assert all(is_page_qualified(company) for company in run.companies)
    assert all(company.products and company.region for company in run.companies)
    assert all("虚构" in company.name for company in run.companies)
    assert all(".example" in company.verification_url for company in run.companies)
    assert len(run.candidates) == 4


def test_excel_long_evidence_is_preserved_in_continuation_sheet(tmp_path):
    company = company_from_hit(hit())
    excerpt = "证据原文" * 12000
    company.evidence = [Evidence(kind="名称", url="https://source.example/report", excerpt=excerpt)]
    path = tmp_path / "long.xlsx"
    export_xlsx([company], path)
    workbook = load_workbook(path)
    continuation = workbook["长文本续页"]
    chunks = [row[4].value for row in continuation.iter_rows(min_row=2) if row[0].value == "证据明细" and row[1].value == "E2"]
    assert "".join(chunks) == excerpt
    assert "长文本续页" in workbook["证据明细"]["E2"].value
    workbook.close()
