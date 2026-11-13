"""Bounded discovery on public supplier directories and official company lists.

Discovery yields unverified detail-page candidates, never admitted factories.
Only published links/forms are used; challenges, login walls and TLS errors are
reported without bypassing them. No search-engine ``site:`` queries hide here.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
import json
import re
import threading
import time
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from .extraction import _names, normalized_name, normalized_url
from .models import SearchHit, SearchOptions
from .network import (DeadlineExceeded, FetchCancelled, FetchError, _decode_html,
                      HTTPFetchError, check_control, fetch_bytes, fetch_text, safe_url)
from .providers import SearchBlocked, _explicit_ad, _page_block
from .source_types import DiscoveryBatch, LeadSeed, SOURCE_LABELS


B2B_SOURCES = ("madeinchina", "b2b168", "gys", "hc360", "aicaigou", "1688")
CATALOG_PRESETS = [
    {"label": "工信部：2025 年度绿色工厂官方名单", "url": "https://www.miit.gov.cn/jgsj/jns/lszz/art/2026/art_83c4f4f9652846c1b24f1f250f4a3591.html"},
    {"label": "cippe 2026 上海石化展商名录", "url": "https://sh.cippe.com.cn/exdb/"},
]
MAX_CATALOG_SOURCES = 8
MAX_SOURCE_HITS = 100
REQUEST_INTERVAL = 0.8
_NAV_LABELS = {"首页", "下一页", "上一页", "末页", "尾页", "登录", "注册", "联系我们", "产品", "公司", "供应商", "免费注册", "查看更多", "查看全部", "官网首页"}
_FOOTER_CLASS = re.compile(r"(?:^|[-_])(?:nav|footer|header|menu|bottom|topbar)(?:$|[-_])", re.I)
_CARD_CLASS = re.compile(r"(?:info-list-item|company-item|supplier|offer|product|prod-item|result-item|list-item|card|^item$)", re.I)
_BLOCK_TAGS = {"address", "article", "aside", "br", "dd", "div", "dl", "dt", "footer", "h1", "h2", "h3", "h4", "h5", "header", "li", "nav", "ol", "p", "section", "table", "td", "th", "tr", "ul"}


def discovery_keyword(query: str) -> str:
    """Remove only common search intent; preserve product and company identity."""
    value = query.strip()
    if len(_names(value, allow_generic_factory=True)) == 1 and normalized_name(_names(value, allow_generic_factory=True)[0]) == normalized_name(value):
        return value
    value = re.sub(r"^(?:帮我)?(?:查找|寻找|搜索|找)(?:一下|一批|一些)?", "", value)
    value = re.sub(r"(?:的)?(?:生产厂家|生产厂商|制造厂家|厂家|厂商|供应商|制造商|生产商)(?:信息|有哪些|名单)?$", "", value)
    return value.strip() or query.strip()


def build_b2b_url(source: str, query: str, page: int = 1) -> str:
    """Return a real public platform search/directory URL, never a web engine."""
    if source not in B2B_SOURCES:
        raise ValueError("不支持的 B2B 渠道")
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= 3:
        raise ValueError("每个渠道只允许第 1 至 3 页")
    query = discovery_keyword(query)
    if not query or len(query) > 100:
        raise ValueError("渠道搜索关键词应为 1 至 100 个字符")
    if source == "madeinchina":
        # This published legacy form reads GBK/GB18030, not UTF-8.
        return "https://cn.made-in-china.com/companysearch.do?" + urlencode({
            "subaction": "hunt", "style": "b", "mode": "and", "code": 0,
            "newCn": 1, "word": query, "page": page,
        }, encoding="gb18030")
    if source == "b2b168":
        from pypinyin import lazy_pinyin
        slug = "".join(lazy_pinyin(query)).lower()
        slug = re.sub(r"[^a-z0-9_-]", "", slug)
        if not slug:
            raise ValueError("八方资源网目录关键词无法转换")
        base = "https://www.b2b168.com/k-" + slug + "/"
        # A later page is fetched only when this URL is actually published as
        # a next-page link. Unknown pagination must never be invented.
        return base if page == 1 else base + "?" + urlencode({"page": page})
    if source == "gys":
        # This route is returned by the platform's public search form. discover
        # still submits that form so changes in its destination are respected.
        base = "https://product.cn.gys.cn/suppliers/" + quote(query, encoding="gb18030") + "/"
        return base if page == 1 else base + "?" + urlencode({"page": page})
    if source == "hc360":
        return "https://s.hc360.com/?" + urlencode({"w": query, "mc": "seller", "page": page}, encoding="gb18030")
    if source == "aicaigou":
        return "https://b2b.baidu.com/s?" + urlencode({"q": query, "from": "search", "page": page})
    return "https://s.1688.com/selloffer/offer_search.htm?" + urlencode({"keywords": query, "beginPage": page}, encoding="gb18030")


def _clean(node: Tag, _depth: int = 0) -> str:
    # Preserve block boundaries. Joining all adjacent Chinese text turns e.g.
    # a price's unit “吨” + the next company name into a false legal identity.
    if _depth > 80:
        return node.get_text(" ", strip=True)
    parts = []
    for child in node.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag):
            boundary = " " if child.name in _BLOCK_TAGS else ""
            parts.extend([boundary, _clean(child, _depth + 1), boundary])
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _relevant(query: str, text: str) -> bool:
    # Share the product aliases used by the factory qualifier. Relevance must
    # come from the supplier's own card/row, never the list publisher's heading.
    from .qualification import _query_plan
    groups, _regions, identity = _query_plan(query)
    text = normalized_name(text)
    if identity:
        return normalized_name(identity) in text
    return bool(groups) and all(any(normalized_name(term) in text for term in alternatives) for alternatives in groups)


def _navigation(anchor: Tag) -> bool:
    for node in [anchor, *anchor.parents]:
        if not isinstance(node, Tag):
            continue
        if node.name in {"nav", "footer", "header", "template", "noscript"}:
            return True
        if _FOOTER_CLASS.search(" ".join(node.get("class", []))):
            return True
        if str(node.get("role", "")).lower() in {"navigation", "contentinfo"}:
            return True
    return False


def _card(anchor: Tag) -> Tag:
    best = anchor.parent if isinstance(anchor.parent, Tag) else anchor
    for parent in anchor.parents:
        if not isinstance(parent, Tag) or parent.name in {"body", "html"}:
            break
        if len(_clean(parent)) > 2200:
            break
        if parent.name in {"li", "article", "tr"}:
            return parent
        if _CARD_CLASS.search(" ".join(parent.get("class", []))):
            best = parent
    return best


def _detail_url(source: str, url: str, title: str) -> bool:
    if not safe_url(url) or len(title) < 3 or title in _NAV_LABELS or title.isdigit():
        return False
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    path = p.path.rstrip("/")
    if source == "b2b168":
        return bool((host in {"www.b2b168.com", "info.b2b168.com"} and re.fullmatch(r"/(?:c168|s168)-\d+\.html", path))
                    or (host.endswith(".b2b168.com") and host.split(".")[0] not in {"www", "m", "info", "i", "s", "my", "bill", "cn", "help"}
                        and (not path or re.fullmatch(r"/s\d+\.html|/shop/(?:supply|profile|home)/?(?:\d+\.html)?", path))))
    if source == "madeinchina":
        return bool((host.endswith(".cn.made-in-china.com") and host.split(".")[0] not in {"membercenter", "captcha", "caigou"}
                     and (not path or re.search(r"/(?:product|gongying|showroom|company|contact|profile)(?:/|\.)", path)))
                    or (host == "3g.made-in-china.com" and re.match(r"/(?:company-|goods-)", path)))
    if source == "gys":
        return bool((host.endswith(".gys.cn") and host.split(".")[0] not in {"www", "product", "res", "my", "help", "mall"}
                     and (not path or path.endswith(".html")))
                    or (host == "www.gys.cn" and re.fullmatch(r"/[a-z0-9-]+/\d+\.html", path)))
    if source == "hc360":
        return bool((host == "b2b.hc360.com" and re.fullmatch(r"/supplyself/\d+\.html", path))
                    or (host.endswith(".b2b.hc360.com") and (not path or path.endswith(".html"))))
    if source == "aicaigou":
        return host == "b2b.baidu.com" and bool(re.match(r"/(?:land|detail|shop)(?:/|$)", path))
    return bool((host == "detail.1688.com" and re.fullmatch(r"/offer/\d+\.html", path))
                or (host.endswith(".1688.com") and host.split(".")[0] not in {"www", "s", "login", "work", "air", "sale", "detail"} and not path)
                or (host == "winport.m.1688.com" and path == "/page/index.html" and "memberId" in parse_qs(p.query)))


def _block(soup: BeautifulSoup, final_url: str = "", have_hits: bool = False):
    title = _clean(soup.title) if soup.title else ""
    if re.search(r"captcha|antispider|/verification|/punish|login\.1688|passport", final_url, re.I):
        raise SearchBlocked("平台要求验证或登录，请使用浏览器辅助处理", reason="captcha")
    if re.search(r"^(?:请验证|安全验证|访问验证|验证中心|人机验证|阿里巴巴登录|登录)(?:\s|$)|404页面|页面不存在|not found", title, re.I):
        if "404" in title or "不存在" in title or "not found" in title.lower():
            raise SearchBlocked("平台返回失效页面，未收录导航或推荐链接", reason="unparsed")
        raise SearchBlocked("平台要求人工安全验证或登录", reason="captcha")
    if error := _page_block(soup, have_hits=have_hits):
        raise error


def parse_b2b_html(source: str, html: str, query: str, page: int, search_url: str) -> list[SearchHit]:
    """Read actual supplier/product detail hrefs from visible public HTML."""
    if source not in B2B_SOURCES or not 1 <= page <= 3:
        raise ValueError("无效渠道或页码")
    if not html.strip():
        raise SearchBlocked("平台返回空页面，需要浏览器辅助查看", reason="needs_browser")
    soup = BeautifulSoup(html[:4_000_000], "html.parser")
    _block(soup, search_url, have_hits=True)
    had_scripts = bool(soup.find("script"))
    for hidden in soup.select("script,style,template,noscript,[hidden],[aria-hidden='true']"):
        hidden.decompose()
    hits = []
    seen = set()
    for anchor in soup.select("a[href]")[:5000]:
        if _navigation(anchor):
            continue
        title = _clean(anchor) or str(anchor.get("title", "")).strip()
        url = urljoin(search_url, str(anchor.get("href", "")).strip())
        if not _detail_url(source, url, title):
            continue
        card = _card(anchor)
        excerpt = _clean(card)[:2000]
        # Avoid unrelated homepage advertisements/recommendations being
        # mistaken for keyword-search results.
        if not _relevant(query, title + " " + excerpt):
            continue
        key = normalized_url(url)
        if key in seen:
            continue
        seen.add(key)
        label_names = _names(title, allow_generic_factory=True)
        exact_labels = []
        for label in card.select("a, .company-name, .company, .supplier-name, [itemprop='name']"):
            value = _clean(label)
            names = _names(value, allow_generic_factory=True)
            if len(names) == 1 and normalized_name(names[0]) == normalized_name(value):
                exact_labels.append(names[0])
        exact_labels = list(dict.fromkeys(exact_labels))
        hint = label_names[0] if len(label_names) == 1 and normalized_name(label_names[0]) == normalized_name(title) else (exact_labels[0] if len(exact_labels) == 1 else "")
        hits.append(SearchHit(engine=source, query=query, page=page, title=title[:500], url=url,
                              snippet=excerpt[:1800], search_url=search_url, source_url=search_url,
                              company_hint=hint, source_excerpt=excerpt[:1800],
                              ad_status="广告" if _explicit_ad(card) else "未标识"))
        if len(hits) >= MAX_SOURCE_HITS:
            break
    if hits:
        return hits
    _block(soup, search_url)
    visible = _clean(soup)
    if re.search(r"共找到\s*0\s*条|没有找到|未找到|暂无(?:相关)?(?:结果|产品|信息)|no results", visible, re.I):
        return []
    if not soup.select("a[href]") or (len(visible) < 200 and had_scripts):
        raise SearchBlocked("平台由 JavaScript 加载结果或需要登录，请使用浏览器辅助", reason="needs_browser")
    raise SearchBlocked("未识别到与关键词相关的厂家详情，页面可能需要浏览器加载或结构已变化", reason="unparsed")


def parse_discovery_html(source: str, html: str, url: str, query: str, page: int = 1) -> list[SearchHit]:
    return parse_b2b_html(source, html, query, page, url)


def _next_url(html: str, url: str) -> str:
    host = urlsplit(url).hostname
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.select("a[href]")[:5000]:
        label = _clean(anchor).lower()
        rel = anchor.get("rel", [])
        if label not in {"下一页", "下页", "下一页 >", "下一页>", "next", "next page", ">", "›", "»"} and "next" not in rel:
            continue
        target = urljoin(url, anchor["href"])
        if safe_url(target) and urlsplit(target).hostname == host and normalized_url(target) != normalized_url(url):
            return target
    return ""


class _Pacer:
    def __init__(self, cancel, deadline):
        self.cancel, self.deadline = cancel, deadline
        self.last = 0.0

    def wait(self):
        while time.monotonic() - self.last < REQUEST_INTERVAL:
            check_control(self.cancel, self.deadline)
            pause = min(0.05, REQUEST_INTERVAL - (time.monotonic() - self.last))
            if self.cancel is not None:
                self.cancel.wait(max(0.001, pause))
            else:
                threading.Event().wait(max(0.001, pause))
        check_control(self.cancel, self.deadline)
        self.last = time.monotonic()


def _status(source: str, label: str = "") -> dict:
    return {"status": "running", "label": label or SOURCE_LABELS.get(source, source),
            "pages": 0, "attempted_pages": 0, "parsed_pages": 0,
            "hits": 0, "seeds": 0, "message": "正在读取公开页面"}


def _finish_error(status: dict, error: Exception):
    if isinstance(error, FetchCancelled):
        state = "cancelled"
    elif isinstance(error, DeadlineExceeded):
        state = "timeout"
    elif isinstance(error, SearchBlocked):
        state = "needs_browser" if error.reason == "needs_browser" else ("unparsed" if error.reason in {"unparsed", "repeated"} else "blocked")
    elif isinstance(error, HTTPFetchError) and error.status_code in {401, 403, 418, 429, 451, 503}:
        state = "blocked"
    else:
        state = "error"
    status.update(status=state, message=str(error)[:600])


def _emit(emit, source: str, status: dict):
    if emit:
        emit({"type": "source", "source": source, **dict(status)})


def _b2b_worker(source, options, cancel, deadline, emit):
    batch = DiscoveryBatch()
    status = _status(source)
    batch.statuses[source] = status
    pacer = _Pacer(cancel, deadline)
    seen = set()
    fingerprints = set()
    try:
        search_url = build_b2b_url(source, options.query, 1)
        if source == "gys":
            pacer.wait()
            router, _ = fetch_text("https://www.gys.cn/common/search.php", cancel, deadline,
                                   timeout=options.request_timeout, max_bytes=64_000,
                                   method="POST", data={"key": discovery_keyword(options.query), "entType": "1"})
            try:
                target = json.loads(router).get("url", "")
            except (ValueError, AttributeError) as exc:
                raise SearchBlocked("供应商网公开搜索表单未返回可识别的详情目录", reason="unparsed") from exc
            if not safe_url(target) or urlsplit(target).hostname not in {"www.gys.cn", "product.gys.cn", "product.cn.gys.cn"}:
                raise SearchBlocked("供应商网搜索未给出有效公开目录，需要浏览器辅助", reason="needs_browser")
            search_url = target
        for page in range(1, min(3, options.pages) + 1):
            pacer.wait()
            status["attempted_pages"] += 1
            html, final = fetch_text(search_url, cancel, deadline, timeout=options.request_timeout)
            status["pages"] += 1
            _block(BeautifulSoup(html, "html.parser"), final, have_hits=True)
            hits = parse_b2b_html(source, html, options.query, page, search_url)
            status["parsed_pages"] += 1
            fingerprint = tuple(sorted(normalized_url(hit.url) for hit in hits))
            if fingerprint and fingerprint in fingerprints:
                raise SearchBlocked("平台返回重复页面，已停止翻页，未重复统计", reason="repeated")
            fingerprints.add(fingerprint)
            for hit in hits:
                key = normalized_url(hit.url)
                if key not in seen:
                    batch.hits.append(hit)
                    seen.add(key)
            status["hits"] = len(batch.hits)
            status["message"] = f"已读取 {status['pages']} 页，找到 {len(batch.hits)} 个待核实详情链接"
            _emit(emit, source, status)
            # Only published next links are followed. Dynamic search shells
            # cannot truthfully be reported as three completed result pages.
            next_url = _next_url(html, final)
            if not hits or not next_url or len(batch.hits) >= MAX_SOURCE_HITS:
                break
            search_url = next_url
        status.update(status="ok", message=f"实际读取 {status['pages']} 页；{len(batch.hits)} 个详情候选，需进入页面识别厂家")
    except Exception as error:
        _finish_error(status, error)
    _emit(emit, source, status)
    return batch


def parse_catalog_html(html: str, url: str, query: str) -> list[LeadSeed]:
    from .ingestion import read_text_leads
    # Filter records before the importer's cap, so a match after 100 unrelated
    # exhibitors is still discoverable. Relevance is never copied from h1/nav.
    soup = BeautifulSoup(html[:4_000_000], "html.parser")
    for tag in soup.select("script,style,noscript,iframe,object,embed,footer,nav,header,[role='contentinfo']"):
        tag.decompose()
    selected = []
    for table in soup.select("table")[:30]:
        rows = table.find_all("tr")[:1000]
        if not rows:
            continue
        header = str(rows[0])
        for row in rows:
            if _relevant(query, _clean(row)):
                selected.extend(read_text_leads("<table>" + header + str(row) + "</table>", query=query, source_url=url))
    for row in soup.select("li,article,p,div,section")[:5000]:
        if row.find_parent("table"):
            continue
        text = _clean(row)
        if len(text) <= 2200 and _relevant(query, text) and len(_names(text, allow_generic_factory=True)) == 1:
            selected.extend(read_text_leads(str(row), query=query, source_url=url))
    # Also support directories whose rows are plain name anchors.
    selected.extend(read_text_leads(str(soup), query=query, source_url=url))
    seeds = selected
    relevant = []
    seen = set()
    for seed in seeds:
        if not seed.name or not _relevant(query, " ".join([seed.name, seed.excerpt, *seed.products])):
            continue
        identity = (normalized_name(seed.name), normalized_url(seed.url))
        if identity in seen:
            continue
        seen.add(identity)
        seed.source = "catalog"
        seed.source_url = url
        relevant.append(seed)
    return relevant[:MAX_SOURCE_HITS]


def _pdf_catalog(data: bytes, url: str, query: str, cancel, deadline) -> list[LeadSeed]:
    """Filter per printed row before the importer's 100-lead cap is applied."""
    from pypdf import PdfReader
    from .ingestion import read_text_leads
    check_control(cancel, deadline)
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF 名录已加密，无法读取")
        if len(reader.pages) > 100:
            raise ValueError("PDF 名录超过 100 页，请下载后拆分导入")
        selected = []
        total = 0
        have_text = False
        for page in reader.pages:
            check_control(cancel, deadline)
            text = page.extract_text() or ""
            total += len(text)
            have_text |= bool(text.strip())
            if total > 4_000_000:
                raise ValueError("PDF 名录文本过长，请拆分导入")
            # Do not combine adjacent rows: a publisher's phone cannot flow
            # into the preceding manufacturer's record.
            for line in text.splitlines():
                if _relevant(query, line):
                    selected.extend(read_text_leads(line, source_url=url, query=query))
            if len(selected) >= MAX_SOURCE_HITS:
                break
        if not have_text:
            raise ValueError("PDF 无可提取文字，可能是扫描图片，请先 OCR 后导入")
        for seed in selected:
            seed.source = "catalog"
        return selected[:MAX_SOURCE_HITS]
    except (FetchError, ValueError):
        raise
    except Exception as exc:
        raise ValueError("PDF 名录无法解析，请使用正常的文字 PDF") from exc


def _catalog_links(html: str, url: str) -> list[str]:
    """Only same-host next pages, clearly labelled directories and PDF files."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlsplit(url).hostname
    links = []
    have_rows = any(_names(row.get_text(" ", strip=True), allow_generic_factory=True)
                    for row in soup.select("table tr")[:1000])
    if next_url := _next_url(html, url):
        links.append(next_url)
    for node in soup.select("a[href],iframe[src]")[:5000]:
        raw = str(node.get("href") or node.get("src") or "")
        candidate = urljoin(url, raw)
        # Official government notices often embed their attachment through a
        # pdf.js viewer. Read only its literal same-host file parameter.
        if node.name == "iframe":
            files = parse_qs(urlsplit(candidate).query).get("file", [])
            if len(files) == 1:
                candidate = urljoin(url, files[0])
        label = _clean(node)
        if (not safe_url(candidate) or urlsplit(candidate).hostname != host
                or normalized_url(candidate) == normalized_url(url)):
            continue
        pdf = urlsplit(candidate).path.lower().endswith(".pdf")
        directory = not have_rows and re.search(r"展商名录|参展商名单|参展企业|工厂名单|企业名录|exhibitor.*directory", label, re.I)
        if pdf or directory:
            links.append(candidate)
    return list(dict.fromkeys(links))[:10]


def _catalog_worker(source, entry, options, cancel, deadline, emit):
    batch = DiscoveryBatch()
    status = _status(source, entry["label"])
    status["url"] = entry["url"]
    batch.statuses[source] = status
    queue = [entry["url"]]
    seen_pages, seen_seeds = set(), set()
    pacer = _Pacer(cancel, deadline)
    failures = []
    recognized = 0
    try:
        while queue and status["attempted_pages"] < min(3, options.pages):
            check_control(cancel, deadline)
            url = queue.pop(0)
            key = normalized_url(url)
            if key in seen_pages:
                continue
            seen_pages.add(key)
            pacer.wait()
            status["attempted_pages"] += 1
            try:
                data = None
                kwargs = {}
                if urlsplit(url).hostname == "sh.cippe.com.cn" and urlsplit(url).path.rstrip("/") == "/exdb":
                    # This official directory visibly publishes this ordinary
                    # search form. Query its company-name field directly.
                    url = "https://sh.cippe.com.cn/exdb/index.php/Index/index"
                    kwargs = {"method": "POST", "data": {"gongsimingcheng": discovery_keyword(options.query), "zhanweihao": "", "tijiao": "查询"}}
                    html, final = fetch_text(url, cancel, deadline, timeout=options.request_timeout, max_bytes=4_000_000, **kwargs)
                else:
                    # PDF download URLs do not always end in .pdf. Detect the
                    # magic bytes without a second network request.
                    data, final = fetch_bytes(url, cancel, deadline, timeout=options.request_timeout, max_bytes=20_000_000)
                status["pages"] += 1
                if data is not None and data.lstrip().startswith(b"%PDF-"):
                    seeds = _pdf_catalog(data, final, options.query, cancel, deadline)
                    recognized += 1
                else:
                    if data is not None:
                        if len(data) > 4_000_000:
                            raise ValueError("名录 HTML 超过 4 MB，请选择更小的名单页")
                        html = _decode_html(data, None)
                    soup = BeautifulSoup(html, "html.parser")
                    _block(soup, final)
                    seeds = parse_catalog_html(html, final, options.query)
                    queue.extend(link for link in _catalog_links(html, final) if normalized_url(link) not in seen_pages)
                    # A meaningful rendered list or a notice with attachments
                    # is valid even when this keyword matches no row.
                    from .ingestion import read_text_leads
                    if seeds or read_text_leads(html, source_url=final) or queue:
                        recognized += 1
                    elif len(_clean(soup)) < 200 and soup.find("script"):
                        raise SearchBlocked("名录需要浏览器加载，请使用浏览器辅助或导入已下载名单", reason="needs_browser")
                status["parsed_pages"] += 1
                for seed in seeds:
                    identity = (normalized_name(seed.name), normalized_url(seed.url))
                    if identity not in seen_seeds:
                        batch.seeds.append(seed)
                        seen_seeds.add(identity)
                status["seeds"] = len(batch.seeds)
                status["message"] = f"已读取 {status['pages']} 个页面/附件，找到 {len(batch.seeds)} 条相关企业线索"
                _emit(emit, source, status)
            except (FetchCancelled, DeadlineExceeded):
                raise
            except (FetchError, ValueError, ImportError) as error:
                failures.append(error)
        if failures:
            _finish_error(status, failures[0])
            if recognized:
                status["status"] = "partial"
                status["message"] = f"已读取 {status['pages']} 个页面/附件、{len(batch.seeds)} 条线索；部分附件失败：{failures[0]}"
        elif not recognized:
            status.update(status="unparsed", message="名录页面没有可识别的企业行或公开附件，请检查链接或使用浏览器辅助")
        else:
            status.update(status="ok", message=f"实际读取 {status['pages']} 个页面/附件；{len(batch.seeds)} 条相关线索，仍需核实厂家")
    except Exception as error:
        _finish_error(status, error)
    _emit(emit, source, status)
    return batch


def discover(options: SearchOptions, cancel, deadline: float, emit=None) -> DiscoveryBatch:
    """Discover public candidates, max four workers and three pages per source."""
    combined = DiscoveryBatch()
    tasks = []
    channels = set(options.discovery_channels)
    if "b2b" in channels:
        for source in dict.fromkeys(options.b2b_sources):
            if source in B2B_SOURCES:
                tasks.append((_b2b_worker, (source, options, cancel, deadline, emit)))
            else:
                combined.notes.append(f"未知 B2B 渠道已跳过：{source}")
    if "catalog" in channels:
        configured = [{"label": "自定义名录 " + str(index + 1), "url": url.strip()}
                      for index, url in enumerate(options.catalog_urls) if isinstance(url, str) and url.strip()]
        entries = configured or CATALOG_PRESETS
        if len(entries) > MAX_CATALOG_SOURCES:
            combined.notes.append(f"单轮最多读取 {MAX_CATALOG_SOURCES} 个名录来源，超出部分已跳过")
        for index, entry in enumerate(entries[:MAX_CATALOG_SOURCES]):
            source = f"catalog:{index + 1}"
            if not safe_url(entry["url"]):
                status = _status(source, entry["label"])
                status.update(status="error", url=entry["url"], message="名录只接受有效的公网 HTTP/HTTPS 链接")
                combined.statuses[source] = status
                _emit(emit, source, status)
                continue
            tasks.append((_catalog_worker, (source, entry, options, cancel, deadline, emit)))
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="discovery") as pool:
        futures = [pool.submit(function, *args) for function, args in tasks]
        for future in as_completed(futures):
            batch = future.result()
            combined.hits.extend(batch.hits)
            combined.seeds.extend(batch.seeds)
            combined.statuses.update(batch.statuses)
            combined.notes.extend(batch.notes)
    return combined
