"""Public HTML result adapters. Search depth is always limited to three pages.

The exact result href is retained, including an engine redirect when that is
what the engine publishes. A result without an explicit ad label stays unknown;
keywords such as “推广服务” in an ordinary result are not proof of advertising.
"""
from __future__ import annotations

import re
import threading
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .models import SearchHit
from .network import (DeadlineExceeded, FetchCancelled, FetchError, HTTPFetchError,
                      check_control, fetch_text, safe_url)


ENGINES = {
    "baidu": "百度", "sogou": "搜狗", "so360": "360 搜索",
    "bing": "Bing", "toutiao": "头条搜索", "shenma": "神马搜索",
}


class SearchBlocked(FetchError):
    def __init__(self, message: str, *, reason: str = "blocked"):
        super().__init__(message)
        self.reason = reason


def build_search_url(engine: str, query: str, page: int) -> str:
    if engine not in ENGINES:
        raise ValueError(f"不支持的搜索引擎：{engine}")
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= 3:
        raise ValueError("每个搜索引擎仅允许搜索第 1 至 3 页")
    query = query.strip()
    if not query or len(query) > 300:
        raise ValueError("搜索关键词应为 1 至 300 个字符")
    if engine == "baidu":
        return "https://www.baidu.com/s?" + urlencode({"wd": query, "pn": (page - 1) * 10, "rn": 10})
    if engine == "sogou":
        return "https://www.sogou.com/web?" + urlencode({"query": query, "page": page})
    if engine == "so360":
        return "https://www.so.com/s?" + urlencode({"q": query, "pn": page})
    if engine == "bing":
        return "https://www.bing.com/search?" + urlencode({"q": query, "first": (page - 1) * 10 + 1, "count": 10})
    if engine == "toutiao":
        return "https://so.toutiao.com/search?" + urlencode({"keyword": query, "pd": "synthesis", "dvpf": "pc", "page_num": page - 1})
    return "https://m.sm.cn/s?" + urlencode({"q": query, "page": page})


_SELECTORS = {
    "baidu": "#content_left h3 a[href], #content_left h2.c-title a[href], .result h3 a[href], .result-op h3 a[href], .EC_result h3 a[href]",
    "sogou": ".results h3 a[href], .vrwrap h3 a[href], .rb h3 a[href]",
    "so360": "li.res-list h3 a[href], li.res-list h2 a[href], #m-spread-left h3 a[href], #m-spread-left h2 a[href], #m-spread-bottom h3 a[href], .e_result h3 a[href], .e_result h2 a[href]",
    "bing": "#b_results li.b_algo h2 a[href], #b_results .b_ad h2 a[href], #b_results .b_ad h3 a[href], #b_results .b_ad .b_adTitle a[href]",
    "toutiao": ".result-content .cs-header a[href], .s-result-list .cs-header a[href], .result-content .cs-title a[href], .result-content h3 a[href], .result-content h2 a[href]",
    "shenma": "#results .qk-title a[href], #results a[href]:has(.qk-title-text), #results .c-title a[href], #results .res-title a[href], #results h3 a[href], #results a.cpc-a[href]",
}

_CARD_CLASSES = {
    "baidu": {"result", "result-op", "c-container", "EC_result"},
    "sogou": {"vrwrap", "rb", "results-promotion", "result"},
    "so360": {"res-list", "e-idea", "e-item", "e-idea-item"},
    "bing": {"b_algo", "b_ad"},
    "toutiao": {"cs-card", "result-item"},
    "shenma": {"qk-card", "cpc-card", "result", "sc"},
}

_SNIPPET_SELECTORS = {
    "baidu": ".c-abstract, [class*='summary'], [class*='content-right']",
    "sogou": "[id^='cacheresult_summary'], .str_info, .str-text-info",
    "so360": ".res-desc, .res-comm-con, .res-summary",
    "bing": ".b_caption p, .b_snippet, .b_adDescription",
    "toutiao": ".cs-content .cs-text, .cs-paragraph, .cs-summary",
    "shenma": ".qk-paragraph-text, .c-abstract, .c-summary",
}

_AD_LABELS = {"广告", "品牌广告", "商业推广", "推广", "赞助", "赞助商", "ad", "ads", "advertisement", "sponsored", "sponsored result"}
_NAV_TITLES = {"下一页", "上一页", "首页", "尾页", "相关搜索", "更多", "搜索设置", "反馈", "帮助", "登录", "注册", "百度首页"}
_SEARCH_HOSTS = {"baidu.com", "www.baidu.com", "m.baidu.com", "sogou.com", "www.sogou.com", "m.sogou.com", "so.com", "www.so.com", "m.so.com", "bing.com", "www.bing.com", "cn.bing.com", "so.toutiao.com", "sou.toutiao.com", "m.sm.cn", "sm.cn", "www.sm.cn"}
_NAV_HOSTS = {"image.so.com", "wenku.so.com", "map.so.com", "map.360.cn", "video.so.com", "news.so.com", "e.so.com", "e.baidu.com", "image.baidu.com", "map.baidu.com", "news.baidu.com", "video.baidu.com", "fanyi.baidu.com", "pic.sogou.com", "weixin.sogou.com", "v.sogou.com", "news.sogou.com", "account.sogou.com"}


def _clean_text(element: Tag) -> str:
    # Joining inline <em> with spaces turns “有限<em>公司</em>” into a
    # different company name. Join Chinese inline spans naturally instead.
    value = element.get_text(" ", strip=True)
    value = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _card_for(anchor: Tag, engine: str) -> Tag:
    for parent in anchor.parents:
        if not isinstance(parent, Tag):
            continue
        if _CARD_CLASSES[engine].intersection(parent.get("class", [])):
            return parent
        if engine == "so360" and parent.name == "li" and str(parent.get("id", "")).startswith("e_"):
            return parent
        if parent.get("id") in {"content_left", "b_results", "results", "main"}:
            break
    return anchor.parent if isinstance(anchor.parent, Tag) else anchor


def _explicit_ad(card: Tag) -> bool:
    for element in card.find_all(["span", "i", "em", "a", "div", "label"]):
        # Hidden templates and script data are never ad evidence.
        ancestry = [element]
        for parent in element.parents:
            if not isinstance(parent, Tag):
                break
            ancestry.append(parent)
            if parent is card:
                break
        if any(node.name in {"template", "script", "style"}
               or node.get("hidden") is not None
               or str(node.get("aria-hidden", "")).lower() == "true"
               or re.search(r"display:none|visibility:hidden", str(node.get("style", "")).replace(" ", "").lower())
               for node in ancestry):
            continue
        label = _clean_text(element).lower()
        if label in _AD_LABELS:
            return True
        aria = str(element.get("aria-label", "")).strip().lower()
        if aria in _AD_LABELS:
            return True
    return False


def _is_result_url(url: str, title: str) -> bool:
    if not safe_url(url) or title.strip() in _NAV_TITLES or re.fullmatch(r"\d{1,3}", title.strip()):
        return False
    if any(phrase in title for phrase in ("想在此推广", "进一步探索更多相关内容")):
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if host in _NAV_HOSTS:
        return False
    if host in _SEARCH_HOSTS:
        path = parts.path.rstrip("/").lower()
        # Engine result redirectors are real provenance and must be kept.
        allowed = ("/link", "/url", "/baidu.php", "/other.php", "/ck/a", "/aclick", "/search/jump", "/adclick", "/webtrans")
        return any(path == prefix or path.startswith(prefix + "/") for prefix in allowed)
    return True


def _page_block(soup: BeautifulSoup, have_hits: bool = False) -> SearchBlocked | None:
    title = _clean_text(soup.title) if soup.title else ""
    if re.search(r"^百度安全验证$|^搜狗.*(?:安全|访问)验证$|^(?:安全验证|访问验证|验证中心|access\s+denied|robot\s+check|just\s+a\s+moment|captcha|too\s+many\s+requests)(?:\s*[-|:]|$)", title, re.I):
        return SearchBlocked("搜索引擎要求安全验证或限制访问，请在浏览器检查后稍后重试", reason="captcha")
    if have_hits:
        return None
    visible = _clean_text(soup)[:8000]
    if re.search(r"请输入验证码|请完成(?:安全)?验证|点击完成验证|请先完成验证|访问过于频繁|请求过于频繁|异常流量|检测到.*异常请求|访问被拒绝|请求已被拦截|unusual\s+traffic|verify\s+you\s+are\s+human|enable\s+javascript.*(?:continue|proceed)", visible, re.I):
        return SearchBlocked("搜索引擎返回验证码、访问限制或浏览器验证页面", reason="captcha")
    if soup.select_one("form[action*='captcha'], form[action*='antispider'], #captcha, #verify-wrap, #waf-captcha"):
        return SearchBlocked("搜索引擎要求验证码，已停止该引擎", reason="captcha")
    return None


def _url_key(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def parse_search_html(engine: str, html: str, query: str, page: int, search_url: str) -> list[SearchHit]:
    build_search_url(engine, query, page)  # validate the contract even for cached HTML
    if not html.strip():
        raise FetchError("搜索引擎返回了空页面")
    soup = BeautifulSoup(html, "html.parser")
    if blocked := _page_block(soup, have_hits=True):
        raise blocked
    hits: list[SearchHit] = []
    seen = set()
    for anchor in soup.select(_SELECTORS[engine]):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("#", "javascript:", "data:", "mailto:", "tel:")):
            continue
        url = urljoin(search_url, href)
        card = _card_for(anchor, engine)
        title = _clean_text(anchor)
        explicit_ad = _explicit_ad(card)
        image_only = engine == "shenma" and "cpc-a" in anchor.get("class", []) and anchor.find("img")
        if image_only and (not title or title in _AD_LABELS):
            image = anchor.find("img")
            title = str(image.get("alt", "")).strip() or "图片广告（未提供文字名称）"
        if len(title) < 3 or not _is_result_url(url, title):
            continue
        key = _url_key(url)
        if key in seen:
            continue
        seen.add(key)
        summaries = card.select(_SNIPPET_SELECTORS[engine])
        snippet = " ".join(dict.fromkeys(_clean_text(e) for e in summaries)) if summaries else _clean_text(card)
        if snippet.startswith(title):
            snippet = snippet[len(title):].strip()
        if image_only and not snippet.replace("广告", "").strip():
            snippet = "图片广告无可读取的厂家名称或电话，需人工核实。"
        hits.append(SearchHit(
            engine=engine, query=query.strip(), page=page, title=title[:500],
            url=url, snippet=snippet[:1800], search_url=search_url,
            ad_status="广告" if explicit_ad else "未标识",
        ))
    if hits:
        return hits
    if blocked := _page_block(soup):
        raise blocked
    visible = _clean_text(soup)
    if re.search(r"没有找到(?:相关)?(?:的)?(?:结果|网页|内容)|未找到.*(?:相关结果|相关网页)|找不到.*相关结果|抱歉.*没有.*相关|没有与.*相关.*结果|no\s+results\s+(?:found|for)|there\s+are\s+no\s+results", visible, re.I):
        return []
    reason = "JavaScript 渲染或页面结构已变化" if soup.find("script") else "页面结构无法识别"
    raise SearchBlocked(f"{ENGINES[engine]}未返回可解析的搜索结果（{reason}），请使用浏览器查看原始搜索页", reason="unparsed")


class SearchProvider:
    """One instance per run; independent engines may be fetched concurrently."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str], set[str]] = {}
        self._page_titles: dict[tuple[str, str], list[set[str]]] = {}

    def fetch(self, engine: str, query: str, page: int, cancel=None,
              deadline: float | None = None, timeout: float = 8) -> list[SearchHit]:
        check_control(cancel, deadline)
        search_url = build_search_url(engine, query, page)
        try:
            html, final_url = fetch_text(search_url, cancel, deadline, timeout=timeout)
        except HTTPFetchError as exc:
            if exc.status_code in {401, 403, 407, 418, 429, 451, 503}:
                raise SearchBlocked(f"{ENGINES[engine]}限制本次访问（HTTP {exc.status_code}），已停止该引擎", reason="access") from exc
            raise
        check_control(cancel, deadline)
        # Detect challenge redirects even if their page text is empty or changed.
        if re.search(r"(?:^|[/.?=&_-])(?:captcha|antispider|verify|waf)(?:[/.?=&_-]|$)", final_url, re.I):
            raise SearchBlocked(f"{ENGINES[engine]}跳转到验证页面，已停止该引擎", reason="captcha")
        hits = parse_search_html(engine, html, query, page, search_url)
        check_control(cancel, deadline)
        key = (engine, query.strip())
        # Ads can recur on every page. Only unmarked titles participate in the
        # repeated-page check, avoiding an ad campaign causing a false stop.
        titles = {re.sub(r"\s+", "", hit.title).lower() for hit in hits if hit.ad_status != "广告"}
        with self._lock:
            if page == 1:
                self._seen[key] = set()
                self._page_titles[key] = []
            previous = self._page_titles.setdefault(key, [])
            for earlier in previous:
                if titles and (titles == earlier or (len(titles) >= 3 and len(titles & earlier) / len(titles) >= 0.85)):
                    raise SearchBlocked(f"{ENGINES[engine]}返回了重复的搜索页，已停止继续翻页", reason="repeated")
            previous.append(titles)
            seen = self._seen.setdefault(key, set())
            fresh = [hit for hit in hits if _url_key(hit.url) not in seen]
            seen.update(_url_key(hit.url) for hit in hits)
        return fresh
