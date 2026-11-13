import pytest
from manufacturer_finder.import_service import merge_imported_page
from manufacturer_finder.models import Company, Evidence, RunResult, SearchHit


def hit(page=3, title="山东离线测试机械有限公司", engine="sogou", query="测试夹具"):
    return SearchHit(engine, query, page, title, "https://fixture.example/about",
                     search_url=f"https://search.example/{page}")


def test_import_page3_is_one_completed_page_and_reimport_is_idempotent():
    result = RunResult(query="测试夹具")
    merge_imported_page(result, [hit()], "sogou", 3)
    merge_imported_page(result, [hit()], "sogou", 3)
    assert result.engine_status["sogou"]["pages"] == 1
    assert result.engine_status["sogou"]["page_numbers"] == [3]
    assert result.engine_status["sogou"]["hits"] == 1
    assert len(result.candidates) == 1
    assert result.companies == []
    assert result.qualification_stats["unvisited"] == 1


def test_import_never_creates_factory_even_when_search_snippet_has_full_name_phone_and_negative():
    result = RunResult(query="测试夹具", engine_status={"sogou": {"pages": 1, "hits": 0, "status": "partial"}})
    record = hit(title="山东离线测试机械有限公司 质量不合格被处罚")
    record.snippet = "专业生产测试夹具，联系电话：13800138000"
    merge_imported_page(result, [record], "sogou", 3)
    assert result.engine_status["sogou"]["pages"] == 2
    assert result.engine_status["sogou"]["page_numbers"] == [1, 3]
    assert result.companies == []
    assert result.candidates == [record]
    assert result.collection_log[0]["status"] == "unvisited"
    assert "尚未实际访问" in result.collection_log[0]["reason"]


def test_import_preserves_verified_factories_and_existing_audit_outcomes():
    url = "https://fixture.example/about"
    verified = Company(name="山东离线测试机械有限公司", name_confirmed=True, page_verified=True,
        verification_url=url, admission_reasons=["实际详情页主体和生产业务"],
        evidence=[Evidence("详情页核验", url, "生产测试夹具"), Evidence("名称", url, "山东离线测试机械有限公司")])
    result = RunResult(query="测试夹具", companies=[verified], candidates=[hit()],
        collection_log=[{"url": url, "status": "accepted", "reason": "已核实", "title": verified.name}])
    merge_imported_page(result, [hit(page=2)], "sogou", 2)
    assert result.companies == [verified]
    assert result.qualification_stats["candidates"] == 1
    assert result.qualification_stats["accepted"] == 1
    assert result.collection_log[0]["source_count"] == 2


def test_import_does_not_restore_legacy_snippet_only_company():
    result = RunResult(query="测试夹具", companies=[Company(name="旧版摘要推测有限公司", name_confirmed=True)])
    merge_imported_page(result, [hit()], "sogou", 3)
    assert result.companies == []


def test_import_accepts_manufacturer_intent_query_and_preserves_executed_query():
    result = RunResult(query="水处理")
    record = hit(query="水处理 厂家")
    merge_imported_page(result, [record], "sogou", 3)
    assert result.query == "水处理"
    assert result.candidates[0].query == "水处理 厂家"
    assert result.companies == []


@pytest.mark.parametrize("page,record", [(4, hit()), (1, hit(3)), (3, hit(query="另一关键词"))])
def test_invalid_import_context_is_rejected_without_mutation(page, record):
    result = RunResult(query="测试夹具")
    with pytest.raises(ValueError):
        merge_imported_page(result, [record], "sogou", page)
    assert result.companies == []
    assert result.candidates == []
