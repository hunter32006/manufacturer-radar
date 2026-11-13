"""Import saved search pages as candidates, never as unvisited manufacturers."""
from copy import deepcopy

from .extraction import normalized_url
from .models import RunResult, SearchHit, now
from .pipeline import manufacturer_search_query
from .scoring import is_page_qualified


def _key(hit):
    return hit.engine, hit.query, hit.page, hit.url, hit.title, hit.snippet


def refresh_candidate_audit(result: RunResult) -> None:
    """Reconcile candidate URL counts after imports or combining identified runs."""
    groups = {}
    for hit in result.candidates:
        groups.setdefault(normalized_url(hit.url) or hit.url, []).append(hit)
    records = {normalized_url(row.get("url", "")) or row.get("url", ""): dict(row)
               for row in result.collection_log}
    result.collection_log = []
    for key, hits in groups.items():
        row = records.get(key, {"url": hits[0].url, "title": hits[0].title, "status": "unvisited",
            "reason": "已导入搜索候选；尚未实际访问目标网页，不能作为厂家入库", "contact_pages": 0})
        row["source_count"] = len(hits)
        result.collection_log.append(row)
    statuses = [row["status"] for row in result.collection_log]
    result.qualification_stats = {
        "candidates": len(groups), "visited": sum(status != "unvisited" for status in statuses),
        "accepted": statuses.count("accepted"), "rejected": statuses.count("rejected"),
        "unvisited": statuses.count("unvisited"), "failed": statuses.count("failed"),
    }


def merge_imported_page(result: RunResult, hits: list[SearchHit], engine: str, page: int) -> None:
    if page not in (1, 2, 3):
        raise ValueError("仅可导入前 3 页")
    if any(h.engine != engine or h.page != page or h.query not in {result.query, manufacturer_search_query(result.query)} for h in hits):
        raise ValueError("导入页与当前引擎、页码或关键词不一致")
    # Old histories may hold snippet-only records. They must not become visible
    # factories merely because another saved search page has been imported.
    result.companies = [company for company in result.companies if is_page_qualified(company)]
    keys = {_key(hit) for hit in result.candidates}
    for hit in hits:
        if _key(hit) not in keys:
            result.candidates.append(deepcopy(hit))
            keys.add(_key(hit))
    previous = result.engine_status.get(engine, {})
    prior_pages = previous.get("page_numbers")
    if prior_pages is None:
        prior_pages = list(range(1, int(previous.get("pages", 0)) + 1))
    page_numbers = sorted(set(prior_pages) | {page})
    imported_pages = sorted(set(previous.get("imported_pages", [])) | {page})
    unique_hits = {_key(hit) for hit in result.candidates if hit.engine == engine}
    result.engine_status[engine] = {
        "status": "手动导入", "pages": len(page_numbers), "page_numbers": page_numbers,
        "imported_pages": imported_pages, "hits": len(unique_hits),
        "message": "导入第 " + "、".join(map(str, imported_pages)) + " 页为候选；请点击“识别候选厂家”实际访问后入库",
    }
    refresh_candidate_audit(result)
    result.finished_at = now()
