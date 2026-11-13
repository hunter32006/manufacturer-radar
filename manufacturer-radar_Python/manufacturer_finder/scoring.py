"""Detail-page information completeness scores, never company credit ratings."""
from .extraction import extract_phones, normalized_name, normalized_url
from .models import Company
from .providers import ENGINES


NEGATIVE_KINDS = {"负面线索", "负面", "negative"}


def _page_evidence(company: Company, kinds: set[str]) -> bool:
    page = normalized_url(company.verification_url)
    return bool(page) and any(
        item.kind in kinds and normalized_url(item.url) == page and item.excerpt.strip()
        for item in company.evidence
    )


def _identity_page_evidence(company: Company, url: str) -> bool:
    identity = normalized_name(company.name)
    page = normalized_url(url)
    return bool(identity and page) and any(
        item.kind == "名称" and normalized_url(item.url) == page
        and identity in normalized_name(item.excerpt)
        for item in company.evidence
    )


def is_page_qualified(company: Company) -> bool:
    """A search snippet or a manually toggled flag is insufficient for admission."""
    return bool(
        company.page_verified and company.name_confirmed and company.name.strip()
        and normalized_url(company.verification_url) and company.admission_reasons
        and _page_evidence(company, {"详情页核验"})
        and _identity_page_evidence(company, company.verification_url)
    )


def _phone_page_evidence(company: Company) -> bool:
    wanted = {phone for value in company.phones for phone in extract_phones(value)}
    if not wanted:
        return False
    admitted_pages = {normalized_url(company.verification_url)} - {""}
    visited_urls = {normalized_url(url) for url in company.urls} - {""}
    admitted_pages.update(
        normalized_url(item.url) for item in company.evidence
        if item.kind == "联系页核验" and item.excerpt.strip()
        and normalized_url(item.url) in visited_urls
        and _identity_page_evidence(company, item.url)
    )
    return any(
        item.kind == "电话" and normalized_url(item.url) in admitted_pages
        and wanted.intersection(extract_phones(item.excerpt))
        for item in company.evidence
    )


def score_company(company: Company) -> None:
    negative = [item for item in company.evidence if item.kind in NEGATIVE_KINDS]
    if negative:
        company.negative_status = "发现待核实线索"
    reasons: list[str] = []
    score = 0

    def add(points: int, text: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"{points:+d}：{text}")

    links = {normalized_url(value) for value in company.urls} - {""}
    phones = [number for number in company.phones if extract_phones(number)]
    provenance = any(hit.engine and hit.query and 1 <= hit.page <= 3 and normalized_url(hit.url) for hit in company.hits)
    engines = {hit.engine for hit in company.hits if hit.engine in ENGINES and normalized_url(hit.url)}
    qualified = is_page_qualified(company)
    if qualified:
        add(30, f"已进入详情页并依据页面内容识别厂家：{company.verification_url}")
        add(20, "厂家全称来自已读取的详情页（未验证工商主体）")
        products = bool(company.products or company.business_scope) and _page_evidence(company, {"主营产品", "经营范围"})
        add(15 if products else 0, "详情页有主营产品或经营范围及原文证据" if products else "主营产品或经营范围证据待补充")
        location = bool(company.region or company.address) and _page_evidence(company, {"地区地址", "地区", "地址"})
        add(5 if location else 0, "详情页有地区或地址证据" if location else "地区或地址待补充")
        phone_evidence = _phone_page_evidence(company)
        add(10 if phone_evidence else 0, "厂家详情页或同主体联系页提取到公开电话，归属仍待人工核实" if phone_evidence else "未取得已核验页面可回溯的公开电话")
        add(5, "详情页厂家名称证据可回溯")
        add(5 if phone_evidence else 0, "详情页或已核验联系页电话原文证据可回溯" if phone_evidence else "电话原文证据待补充")
        add(5 if provenance else 0, "保留原始采集渠道、关键词及页码" if provenance else "原始采集来源待补充")
        add(3 if len(engines) >= 2 else 0, "至少两个搜索引擎收录（不代表独立事实验证）" if len(engines) >= 2 else "仅单引擎或搜索来源不足")
        completed = company.negative_status in {"本轮未发现", "发现待核实线索"}
        add(2 if completed else 0, f"负面线索检查状态：{company.negative_status}")
    else:
        add(0, "尚未完成厂家详情页读取与身份、业务核验，不计入正式厂家结果；候选线索最高 25 分")
        add(5 if company.name_confirmed else 0, "搜索文本提取到名称，须进入页面确认" if company.name_confirmed else "缺少经详情页确认的厂家名称")
        add(10 if links else 0, "保留候选来源链接" if links else "缺少有效候选链接")
        add(5 if phones else 0, "搜索文本含电话，尚未通过详情页核验" if phones else "缺少公开电话")
        add(5 if provenance else 0, "保留原始搜索来源" if provenance else "原始搜索来源待补充")
    negative_sources = {normalized_url(item.url) or f"{item.engine}:{item.excerpt}" for item in negative}
    deduction = min(30, 15 * len(negative_sources))
    if deduction:
        add(-deduction, f"{len(negative_sources)} 个来源含待核实负面指称，扣分上限 30")
    company.score = max(0, min(100 if qualified else 25, score))
    reasons.append("评分仅反映已采集信息的完整度和待核实线索，不是企业信用、资质或事实认定。")
    company.score_reasons = reasons
