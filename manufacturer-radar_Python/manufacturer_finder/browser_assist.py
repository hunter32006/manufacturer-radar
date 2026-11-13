"""Attended collection in a visible, application-owned Edge/Chrome window.

Every browser operation runs on the same worker thread. Public methods return
Futures so Tk callers can poll them with ``after``; never wait on Tk's thread.
The user completes ordinary site logins/challenges in this dedicated profile.
There is no connection to a running personal browser and no cookie import.

Playwright references (checked 2026-11-11):
https://playwright.dev/python/docs/browsers#google-chrome--microsoft-edge
https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context
"""
from __future__ import annotations

import atexit
from concurrent.futures import Future
from dataclasses import dataclass, field
import os
from pathlib import Path
import queue
import re
import shutil
import threading
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .extraction import _names, extract_phones, normalized_name, normalized_url
from .models import SearchHit
from .network import safe_url
from .providers import ENGINES, SearchBlocked, build_search_url, parse_search_html
from .qualification import qualify_page
from .source_types import DiscoveryBatch, LeadSeed, SOURCE_LABELS


MAX_HTML_BYTES = 2_000_000
MAX_CANDIDATES = 100
B2B_SOURCES = ("madeinchina", "b2b168", "gys", "hc360", "aicaigou", "1688")
BROWSER_SOURCES = {**ENGINES, **{key: SOURCE_LABELS[key] for key in B2B_SOURCES},
                   "douyin": "抖音", "kuaishou": "快手"}
_PROFILE_MARKER = ".manufacturer-radar-browser-profile"
_FULL_COMPANY = re.compile(r"(?:股份有限公司|有限责任公司|有限公司|集团公司|总公司)$")
_IDENTITY_LABEL = re.compile(r"(?:企业认证|认证企业|企业名称|公司名称|企业主体|经营主体|营业执照)\s*[:：]?\s*([^\n]{4,100})")
_PRODUCTION = re.compile(r"专业生产|生产加工|生产企业|生产型|制造|工厂|主营产品|主营业务")
_CHALLENGE = re.compile(r"安全验证|人机验证|请输入验证码|请完成验证|请先完成验证|访问过于频繁|访问被拒绝|verify you are human|access denied|just a moment", re.I)
_LOGIN_TITLE = re.compile(r"^(?:登录|登陆|用户登录|账号登录|扫码登录|login|sign in)(?:\s*[-_|—].*)?$", re.I)
_REMOVE_DOM = "script:not([type='application/ld+json']),style,noscript,template,iframe,svg,[hidden],[aria-hidden='true'],input,textarea,select"


class BrowserAssistError(RuntimeError):
    pass


class BrowserActionRequired(BrowserAssistError):
    pass


@dataclass
class CollectResult:
    batch: DiscoveryBatch = field(default_factory=DiscoveryBatch)
    pages: dict[str, str] = field(default_factory=dict)
    current_url: str = ""


def browser_search_url(source: str, query: str, page: int = 1) -> str:
    """Build public search URLs, never an undocumented/private API request."""
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 3:
        raise ValueError("只允许打开前 3 页搜索结果")
    query = str(query).strip()
    if not query or len(query) > 300:
        raise ValueError("搜索关键词应为 1 至 300 个字符")
    if source in ENGINES:
        return build_search_url(source, query, page)
    if source in B2B_SOURCES:
        from .discovery import build_b2b_url
        return build_b2b_url(source, query, page)
    # These sites load results when the user scrolls. We do not invent page
    # parameters, auto-scroll or call the platforms' internal search APIs.
    if source == "douyin":
        if page != 1:
            raise ValueError("抖音采用滚动列表，请在专用浏览器查看后采集当前页")
        return "https://www.douyin.com/search/" + quote(query, safe="")
    if source == "kuaishou":
        if page != 1:
            raise ValueError("快手采用滚动列表，请在专用浏览器查看后采集当前页")
        return "https://www.kuaishou.com/search/video?" + urlencode({"searchKey": query})
    raise ValueError("不支持的浏览器搜索来源")


def installed_browser_channels() -> tuple[str, ...]:
    """Inspect only installed executables, never browser profiles/history."""
    roots = [os.environ.get(key, "") for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
    candidates = {
        "msedge": [Path(root) / "Microsoft/Edge/Application/msedge.exe" for root in roots if root],
        "chrome": [Path(root) / "Google/Chrome/Application/chrome.exe" for root in roots if root],
    }
    candidates["msedge"] += [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    candidates["chrome"] += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    found = []
    for channel, paths in candidates.items():
        if any(path.is_file() for path in paths) or shutil.which("msedge" if channel == "msedge" else "google-chrome"):
            found.append(channel)
    return tuple(found)


def _prepare_profile(profile_dir: str | Path) -> Path:
    profile = Path(profile_dir).expanduser().resolve()
    normalized = profile.as_posix().casefold()
    # Reject the normal User Data directory and all of its profiles, even if a
    # caller accidentally supplies one. An existing unrelated directory also
    # cannot be adopted as an application profile.
    if re.search(r"/(?:google/chrome|microsoft/edge|chromium)/(?:user data|user-data)(?:/|$)", normalized):
        raise BrowserAssistError("请使用软件专用浏览器目录，不能使用个人浏览器的 User Data 目录")
    if profile.exists() and not profile.is_dir():
        raise BrowserAssistError("专用浏览器目录无效")
    marker = profile / _PROFILE_MARKER
    if profile.exists() and any(profile.iterdir()) and not marker.is_file():
        raise BrowserAssistError("该目录已有其他资料，请为软件选择新的专用浏览器目录")
    profile.mkdir(parents=True, exist_ok=True)
    marker.write_text("Manufacturer Radar dedicated attended browser profile.\n", encoding="utf-8")
    return profile


def _actual_source(url: str, fallback: str = "browser") -> str:
    host = (urlsplit(url).hostname or "").lower()
    domains = {
        "baidu": ("baidu.com",), "sogou": ("sogou.com",), "so360": ("so.com",),
        "bing": ("bing.com",), "toutiao": ("so.toutiao.com", "sou.toutiao.com"),
        "shenma": ("sm.cn",), "madeinchina": ("made-in-china.com",),
        "b2b168": ("b2b168.com", "b2b168.net"), "gys": ("gys.cn", "china.cn"),
        "hc360": ("hc360.com",), "aicaigou": ("b2b.baidu.com",),
        "1688": ("1688.com",), "douyin": ("douyin.com",), "kuaishou": ("kuaishou.com",),
    }
    # More specific marketplace hosts must be checked before baidu.com.
    if host == "b2b.baidu.com" or host.endswith(".b2b.baidu.com"):
        return "aicaigou"
    for source, names in domains.items():
        if any(host == domain or host.endswith("." + domain) for domain in names):
            return source
    return "browser"


def _is_search_page(source: str, url: str) -> bool:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/").lower()
    host = (parsed.hostname or "").lower()
    if source in ENGINES:
        return path in {"/s", "/web", "/search"}
    if source in {"douyin", "kuaishou"}:
        return path.startswith("/search")
    if source == "madeinchina":
        return host == "cn.made-in-china.com" and "search" in path
    if source == "b2b168":
        return host in {"www.b2b168.com", "m.b2b168.com"} and bool(re.match(r"^/(?:k-|company/|search/)", path))
    if source == "gys":
        return host.startswith("product.") and "/suppliers/" in path + "/"
    if source == "hc360":
        return host == "s.hc360.com"
    if source == "aicaigou":
        return host == "b2b.baidu.com" and path == "/s"
    if source == "1688":
        return host == "s.1688.com" and "offer_search" in path
    return False


def _search_page_number(source: str, url: str) -> int:
    params = parse_qs(urlsplit(url).query)
    def value(*keys, default=1):
        for key in keys:
            if key in params:
                try:
                    return int(params[key][0])
                except (TypeError, ValueError):
                    raise BrowserActionRequired("搜索页页码无法识别，请打开前 3 页再采集")
        return default
    if source == "baidu":
        return value("pn", default=0) // 10 + 1
    if source == "bing":
        return (value("first", default=1) - 1) // 10 + 1
    if source == "toutiao":
        return value("page_num", default=0) + 1
    return value("page", "pn", "pageNo", "pageIndex", "pageNum", "pageindex", "beginPage", default=1)


def _clean_dom(html: str) -> str:
    """Keep rendered public content; omit form values and executable payloads."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select(_REMOVE_DOM):
        if node.parent is not None:
            node.decompose()
    for node in soup.select("[style]"):
        if node.parent is None:
            continue
        style = str(node.get("style", "")).lower().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style:
            node.decompose()
    sanitized = str(soup)
    if len(sanitized.encode("utf-8")) > MAX_HTML_BYTES:
        raise BrowserActionRequired("当前页面内容超过 2 MB，请进入具体企业详情页再采集")
    return sanitized


def _visible_text(soup: BeautifulSoup) -> str:
    for node in soup.select("head,nav,aside,footer,header,form,script,style"):
        if node.parent is not None:
            node.decompose()
    return soup.get_text("\n", strip=True)


def _full_company_names(text: str) -> list[str]:
    return [name for name in _names(text) if _FULL_COMPANY.search(name)]


def _enterprise_names(text: str) -> list[str]:
    return list(dict.fromkeys(name for match in _IDENTITY_LABEL.finditer(text)
                             for name in _full_company_names(match.group(1))))


def social_identity_matches(html: str, name: str) -> bool:
    """An explicit visible enterprise label, not a nickname, binds the entity."""
    if not name or not _FULL_COMPANY.search(name):
        return False
    text = _visible_text(BeautifulSoup(_clean_dom(html), "html.parser"))
    owners = _enterprise_names(text)
    return len(owners) == 1 and normalized_name(owners[0]) == normalized_name(name)


def _generic_discovery(source: str, html: str, query: str, current_url: str) -> DiscoveryBatch:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else "浏览器公开页面"
    body = _visible_text(BeautifulSoup(html, "html.parser"))
    batch = DiscoveryBatch()
    social = source in {"douyin", "kuaishou"}
    own_names = _enterprise_names(body) if social else []
    is_search = _is_search_page(source, current_url)
    # A video, nickname or production footage does not establish a legal
    # enterprise. Social collection needs an explicit displayed entity label.
    if not is_search and (not social or (len(own_names) == 1 and _PRODUCTION.search(body))):
        hit = SearchHit(source, query, 1, title[:500], current_url, snippet=body[:1800],
                        search_url=current_url, source_url=current_url, source_excerpt=body[:1800])
        qualified = qualify_page(hit, html, current_url)
        if qualified.company:
            hit.company_hint = qualified.company.name
            if not social or hit.company_hint == own_names[0]:
                batch.hits.append(hit)
    seen = {normalized_url(hit.url) for hit in batch.hits}
    for anchor in soup.select("a[href]"):
        if len(batch.hits) >= MAX_CANDIDATES:
            break
        href = str(anchor.get("href", "")).strip()
        url = urljoin(current_url, href)
        if not safe_url(url) or normalized_url(url) in seen:
            continue
        label = anchor.get_text(" ", strip=True)
        names = _full_company_names(label)
        context = anchor.parent.get_text(" ", strip=True)[:1200] if anchor.parent else label
        if not names and re.search(r"(?:官方网站|企业官网|公司官网|访问官网|官网链接)", label):
            names = _full_company_names(context)
        if len(names) != 1:
            continue
        destination_source = _actual_source(url)
        if social:
            # Search cards can supply only profile candidates for later entity
            # inspection. Never turn video titles or platform navigation into
            # manufacturer records.
            if destination_source == source and not re.search(r"/(?:user|profile)/[^/?#]+", urlsplit(url).path):
                continue
        if _is_search_page(destination_source, url):
            continue
        seen.add(normalized_url(url))
        batch.hits.append(SearchHit(source, query, 1, label[:500] or names[0], url,
                                    snippet=context, search_url=current_url,
                                    source_url=current_url, company_hint=names[0], source_excerpt=context))
    for name in own_names[:20]:
        start = body.find(name)
        excerpt = body[max(0, start - 50):start + 450]
        batch.seeds.append(LeadSeed(name=name, url=current_url, phones=extract_phones(excerpt),
                                    source=source, source_url=current_url, excerpt=excerpt))
    if social and not batch.hits and not batch.seeds:
        batch.statuses[source] = {"status": "needs_user_action", "message": "未读取到明确企业全称。请正常登录后进入企业主页或认证资料页，再采集当前页；昵称和视频不作为厂家依据。", "pages": 1, "hits": 0}
    return batch


def parse_browser_html(html: str, current_url: str, query: str, source: str = "browser") -> CollectResult:
    """Pure parser shared by the attended browser and regression tests."""
    if not safe_url(current_url):
        raise BrowserActionRequired("请在专用浏览器打开公开的 HTTP(S) 企业或搜索页面")
    query = query.strip()
    if not query or len(query) > 300:
        raise ValueError("搜索关键词应为 1 至 300 个字符")
    actual = _actual_source(current_url, source)
    clean_html = _clean_dom(html)
    soup = BeautifulSoup(clean_html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    body = _visible_text(BeautifulSoup(clean_html, "html.parser"))
    result = CollectResult(current_url=current_url)
    if _CHALLENGE.search(title) or (_CHALLENGE.search(body[:1800]) and len(body) < 5000) or _LOGIN_TITLE.search(title):
        result.batch.statuses[actual] = {"status": "needs_user_action", "message": "当前页要求登录或验证，请在专用浏览器正常完成后再采集。", "pages": 0, "hits": 0}
        return result
    search_page = _is_search_page(actual, current_url)
    page = _search_page_number(actual, current_url) if search_page else 1
    if not 1 <= page <= 3:
        result.batch.statuses[actual] = {"status": "page_limit", "message": "仅采集每个来源的前 3 页，当前页已超出范围。", "pages": 0, "hits": 0}
        return result
    try:
        if actual in ENGINES and search_page:
            result.batch.hits = parse_search_html(actual, clean_html, query, page, current_url)[:MAX_CANDIDATES]
        elif actual in B2B_SOURCES and search_page:
            from .discovery import parse_b2b_html
            result.batch.hits = parse_b2b_html(actual, clean_html, query, page, current_url)[:MAX_CANDIDATES]
        else:
            result.batch = _generic_discovery(actual, clean_html, query, current_url)
    except SearchBlocked as exc:
        result.batch.statuses[actual] = {"status": "needs_user_action", "message": str(exc), "reason": exc.reason, "pages": 1, "hits": 0}
        return result
    for hit in result.batch.hits:
        hit.source_url = hit.source_url or current_url
        hit.source_excerpt = hit.source_excerpt or hit.snippet[:1800]
    result.batch.statuses.setdefault(actual, {"status": "completed" if result.batch.hits or result.batch.seeds else "empty",
                                             "message": "已读取当前公开页面，候选还需进入详情核实" if result.batch.hits or result.batch.seeds else "当前页未发现可核实的厂家候选，请进入企业详情后再采集。",
                                             "pages": 1, "hits": len(result.batch.hits), "seeds": len(result.batch.seeds)})
    # A captured search list is provenance, not proof for all listed factories.
    # The runner uses this cache only when the candidate URL is this exact page.
    result.pages[current_url] = clean_html
    return result


class BrowserAssistant:
    """Owned worker and dedicated browser. ``open/snapshot/close`` return Futures."""

    def __init__(self, profile_dir: str | Path):
        self.profile_dir = Path(profile_dir)
        self._commands: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._context = None
        self._driver = None
        self._page = None
        self._focus_sessions = {}
        self._close_future: Future | None = None
        self._search_pages: dict[tuple[str, str], set[str]] = {}
        atexit.register(self._at_exit)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    @property
    def is_closed(self) -> bool:
        return self._stop.is_set()

    def _submit(self, operation: str, *args) -> Future:
        future = Future()
        with self._lock:
            if self._stop.is_set():
                future.set_exception(BrowserAssistError("专用浏览器已关闭，请重新打开浏览器辅助窗口"))
                return future
            self._commands.put((future, operation, args))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="radar-attended-browser", daemon=True)
                self._thread.start()
        return future

    def open(self, url: str) -> Future:
        if not safe_url(url):
            future = Future()
            future.set_exception(ValueError("只能打开公开的 HTTP(S) 页面"))
            return future
        return self._submit("open", url)

    def snapshot(self, query: str, source: str = "browser") -> Future:
        return self._submit("snapshot", query, source)

    def reset_limits(self) -> Future:
        """Call at an explicit new collection job; never resets automatically."""
        return self._submit("reset")

    def close(self) -> Future:
        with self._lock:
            if self._close_future is not None:
                return self._close_future
            self._stop.set()
            self._close_future = Future()
            if self._thread is None or not self._thread.is_alive():
                self._close_future.set_result(None)
            else:
                self._commands.put((self._close_future, "close", ()))
            return self._close_future

    def _at_exit(self) -> None:
        future = self.close()
        try:
            future.result(timeout=8)
        except Exception:
            pass

    def _launch_context(self):
        profile = _prepare_profile(self.profile_dir)
        channels = installed_browser_channels()
        if not channels:
            raise BrowserAssistError("未找到 Microsoft Edge 或 Google Chrome，请安装其中一种后重试；软件不会自动安装浏览器。")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserAssistError("当前程序缺少浏览器辅助组件，请使用完整便携版。") from exc
        driver = sync_playwright().start()
        last_error = None
        for channel in channels:
            try:
                context = driver.chromium.launch_persistent_context(str(profile), channel=channel,
                    headless=False, no_viewport=True, accept_downloads=False, timeout=20_000)
                context.set_default_timeout(10_000)
                context.set_default_navigation_timeout(20_000)
                return driver, context
            except Exception as exc:
                last_error = exc
        driver.stop()
        raise BrowserAssistError("无法启动已安装的 Edge/Chrome 专用窗口，请关闭该软件的其他浏览器辅助窗口后重试。" + (" " + type(last_error).__name__ if last_error else ""))

    def _ensure_context(self):
        if self._context is None:
            self._driver, self._context = self._launch_context()
            if hasattr(self._context, "on"):
                self._context.on("page", self._use_real_focus)
            pages = self._context.pages
            self._page = pages[-1] if pages else self._context.new_page()
            for page in self._context.pages:
                self._use_real_focus(page)
        return self._context

    def _use_real_focus(self, page):
        # Playwright normally makes every tab behave as focused. Restore the
        # browser's real focus/visibility for this owned context so "current
        # page" means the tab the user selected. This changes no site challenge,
        # automation identifier or personal-browser setting.
        if page in self._focus_sessions or not hasattr(self._context, "new_cdp_session"):
            return
        session = None
        try:
            session = self._context.new_cdp_session(page)
            session.send("Emulation.setFocusEmulationEnabled", {"enabled": False}, timeout=3000)
            self._focus_sessions[page] = session
        except Exception:
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass

    def _current_page(self):
        context = self._ensure_context()
        pages = [page for page in context.pages if not page.is_closed()]
        visible, focused = [], []
        for page in pages:
            try:
                if page.evaluate("document.visibilityState === 'visible'"):
                    visible.append(page)
                if page.evaluate("document.hasFocus()"):
                    focused.append(page)
            except Exception:
                continue
        if len(focused) == 1:
            return focused[0]
        if len(visible) == 1:
            return visible[0]
        if len(pages) == 1:
            return pages[0]
        raise BrowserActionRequired("无法确定当前标签页，请在专用浏览器只保留要采集的目标页后重试。")

    def _snapshot(self, query: str, source: str) -> CollectResult:
        page = self._current_page()
        url = page.url
        actual = _actual_source(url, source)
        is_search = _is_search_page(actual, url)
        key = (actual, query.strip())
        url_key = normalized_url(url)
        visited = self._search_pages.setdefault(key, set())
        if is_search and url_key not in visited and len(visited) >= 3:
            batch = DiscoveryBatch(statuses={actual: {"status": "page_limit", "message": "本次已采集该来源 3 个搜索页面，请开始新一轮搜索后再采集。", "pages": 0, "hits": 0}})
            return CollectResult(batch=batch, current_url=url)
        # Only this selected page's top-level DOM is read. No cookies, other
        # tabs' HTML, account exports, frame traversal or background crawling.
        html = page.content()
        if page.url != url:
            raise BrowserActionRequired("当前页正在跳转，请等待页面稳定后再采集，避免页面内容与来源链接不一致。")
        result = parse_browser_html(html, url, query, source)
        status = result.batch.statuses.get(actual, {}).get("status")
        if is_search and status not in {"needs_user_action", "page_limit"}:
            visited.add(url_key)
        self._page = page
        return result

    def _cleanup(self):
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._driver is not None:
            try:
                self._driver.stop()
            except Exception:
                pass
            self._driver = None
        self._focus_sessions.clear()

    def _run(self):
        try:
            while True:
                future, operation, args = self._commands.get()
                if operation == "close":
                    self._cleanup()
                    future.set_result(None)
                    break
                if self._stop.is_set() or not future.set_running_or_notify_cancel():
                    if not future.done():
                        future.set_exception(BrowserAssistError("浏览器辅助已停止"))
                    continue
                try:
                    if operation == "open":
                        self._ensure_context()
                        if self._page is None or self._page.is_closed():
                            self._page = self._context.new_page()
                        self._page.bring_to_front()
                        try:
                            self._page.goto(args[0], wait_until="domcontentloaded", timeout=20_000)
                        except Exception:
                            # An interactive challenge or slow asset must leave
                            # the actual window available for the user's action.
                            if not safe_url(self._page.url) or self._page.url == "about:blank":
                                raise
                        value = self._page.url
                    elif operation == "snapshot":
                        value = self._snapshot(*args)
                    elif operation == "reset":
                        self._search_pages.clear()
                        value = None
                    else:
                        raise BrowserAssistError("未知浏览器辅助操作")
                    future.set_result(value)
                except Exception as exc:
                    if type(exc).__name__ == "TargetClosedError":
                        # Closing the owned browser with its normal X button
                        # must allow the next explicit Open action to relaunch.
                        self._cleanup()
                        exc = BrowserAssistError("专用浏览器已关闭，请点击“打开搜索页”重新打开。")
                    future.set_exception(exc)
        finally:
            self._cleanup()
            while True:
                try:
                    future, _, _ = self._commands.get_nowait()
                except queue.Empty:
                    break
                if not future.done():
                    future.set_exception(BrowserAssistError("浏览器辅助已关闭"))

