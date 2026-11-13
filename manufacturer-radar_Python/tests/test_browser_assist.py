from concurrent.futures import Future
from pathlib import Path
import threading

import pytest

from manufacturer_finder.browser_assist import (
    BrowserActionRequired, BrowserAssistant, BrowserAssistError, _prepare_profile,
    _is_search_page, _search_page_number, browser_search_url, parse_browser_html, social_identity_matches,
)


COMPANY = "山东清源环保设备有限公司"
FACTORY_HTML = f"""<html><head><title>{COMPANY}官网</title></head><body>
<h1>{COMPANY}</h1><p>公司名称：{COMPANY}</p><p>{COMPANY}是一家专业生产水处理设备的生产企业。</p>
<p>主营产品：水处理设备、反渗透设备</p><p>联系电话：0531-85808866</p></body></html>"""


@pytest.mark.parametrize("source", ["baidu", "sogou", "so360", "bing", "toutiao", "shenma"])
def test_web_browser_urls_keep_three_page_limit(source):
    url = browser_search_url(source, "水处理 设备", 3)
    assert url.startswith("https://")
    assert "%" in url
    assert _search_page_number(source, url) == 3
    with pytest.raises(ValueError):
        browser_search_url(source, "水处理", 4)
    with pytest.raises(ValueError):
        browser_search_url(source, "水处理", True)


def test_social_urls_are_normal_public_routes_without_fake_pagination():
    assert browser_search_url("douyin", "水处理") == "https://www.douyin.com/search/%E6%B0%B4%E5%A4%84%E7%90%86"
    assert browser_search_url("kuaishou", "水处理") == "https://www.kuaishou.com/search/video?searchKey=%E6%B0%B4%E5%A4%84%E7%90%86"
    for source in ("douyin", "kuaishou"):
        with pytest.raises(ValueError):
            browser_search_url(source, "水处理", 2)


@pytest.mark.parametrize("source", ["madeinchina", "b2b168", "gys", "hc360", "aicaigou", "1688"])
def test_native_b2b_builder_pages_are_recognized_and_limited(source):
    for page in (1, 2, 3):
        url = browser_search_url(source, "水处理", page)
        assert _is_search_page(source, url)
        assert _search_page_number(source, url) == page
    with pytest.raises(ValueError):
        browser_search_url(source, "水处理", 4)


def test_b2b_detail_product_route_is_not_mistaken_for_search_list():
    assert not _is_search_page("madeinchina", "https://factory.cn.made-in-china.com/product/abc/details.html")
    assert not _is_search_page("b2b168", "https://factory.b2b168.com/shop/profile/")


def test_profile_is_owned_and_never_adopts_personal_profile(tmp_path):
    personal = tmp_path / "Microsoft" / "Edge" / "User Data" / "Default"
    with pytest.raises(BrowserAssistError):
        _prepare_profile(personal)
    occupied = tmp_path / "unrelated"
    occupied.mkdir()
    (occupied / "existing.txt").write_text("personal data")
    with pytest.raises(BrowserAssistError):
        _prepare_profile(occupied)
    own = tmp_path / "radar-browser"
    assert _prepare_profile(own) == own.resolve()
    (own / "Default").mkdir()
    assert _prepare_profile(own) == own.resolve()


def test_selected_factory_dom_is_captured_for_qualification():
    result = parse_browser_html(FACTORY_HTML, "https://factory.example/", "水处理")
    assert len(result.batch.hits) == 1
    hit = result.batch.hits[0]
    assert hit.company_hint == COMPANY
    assert hit.source_url == "https://factory.example/"
    assert result.pages[hit.url]


def test_capture_removes_credentials_hidden_payloads_and_scripts():
    html = FACTORY_HTML.replace("</body>", '<input value="password-secret"><textarea>account-secret</textarea><script>cookie-secret</script><p hidden>hidden-secret</p></body>')
    result = parse_browser_html(html, "https://factory.example/", "水处理")
    captured = result.pages["https://factory.example/"]
    for value in ("password-secret", "account-secret", "cookie-secret", "hidden-secret"):
        assert value not in captured


def test_nested_hidden_and_style_nodes_do_not_break_live_social_dom():
    html = FACTORY_HTML + '<div hidden><input value="secret"><div style="display:none">hidden</div></div><div style="display:none"><p style="color:red">child</p></div>'
    result = parse_browser_html(html, "https://factory.example/", "水处理")
    assert result.batch.hits
    assert "child" not in result.pages[result.current_url]


def test_challenge_requires_user_action_and_is_not_cached():
    result = parse_browser_html("<title>安全验证</title><p>请完成验证</p>", "https://www.baidu.com/s?wd=test", "水处理")
    assert result.batch.statuses["baidu"]["status"] == "needs_user_action"
    assert not result.batch.hits
    assert not result.pages


def test_web_engine_uses_rendered_dom_and_exact_published_links():
    html = '<title>水处理 - 搜索</title><ol id="b_results"><li class="b_algo"><h2><a href="https://factory.example/">山东清源环保有限公司</a></h2><div class="b_caption"><p>水处理设备生产企业</p></div></li></ol>'
    result = parse_browser_html(html, "https://www.bing.com/search?q=test&first=11", "水处理", "browser")
    assert len(result.batch.hits) == 1
    hit = result.batch.hits[0]
    assert hit.engine == "bing"
    assert hit.page == 2
    assert hit.url == "https://factory.example/"
    assert hit.source_url == result.current_url
    assert "https://factory.example/" not in result.pages


def test_fourth_search_page_is_never_collected():
    result = parse_browser_html(FACTORY_HTML, "https://www.bing.com/search?q=test&first=31", "水处理")
    assert result.batch.statuses["bing"]["status"] == "page_limit"
    assert not result.pages


def test_social_nickname_and_video_are_not_manufacturer_identity():
    html = '<title>清源水处理设备厂家 - 抖音</title><h1>清源水处理设备厂家</h1><p>专业生产水处理设备，看看我的车间视频</p><a href="/video/123">山东清源环保有限公司专业生产水处理设备</a>'
    result = parse_browser_html(html, "https://www.douyin.com/user/abc", "水处理")
    assert not result.batch.hits
    assert not result.batch.seeds
    assert result.batch.statuses["douyin"]["status"] == "needs_user_action"


def test_social_enterprise_label_and_production_body_can_enter_qualification():
    html = FACTORY_HTML.replace("公司名称：", "企业认证：")
    result = parse_browser_html(html, "https://www.douyin.com/user/abc", "水处理")
    assert len(result.batch.hits) == 1
    assert result.batch.hits[0].company_hint == COMPANY
    assert result.batch.seeds[0].name == COMPANY
    assert result.batch.seeds[0].source_url == result.current_url


def test_social_full_company_nickname_is_not_an_enterprise_binding():
    html = f'<title>{COMPANY}</title><h1>{COMPANY}</h1><p>专业生产水处理设备</p>'
    assert not social_identity_matches(html, COMPANY)
    assert social_identity_matches(html + f'<p>企业认证：{COMPANY}</p>', COMPANY)
    assert not social_identity_matches(html + '<p>企业认证：山东另外环保有限公司</p>', COMPANY)
    assert not social_identity_matches(html + f'<p hidden>企业认证：{COMPANY}</p>', COMPANY)
    assert not social_identity_matches(html + f'<footer>企业认证：{COMPANY}</footer>', COMPANY)


def test_social_ambiguous_entity_labels_are_not_assigned_to_either_company():
    html = f'<p>企业名称：{COMPANY}</p><p>企业名称：山东另外环保有限公司</p>'
    assert not social_identity_matches(html, COMPANY)


def test_social_search_only_collects_named_profile_candidates():
    html = f'<title>搜索</title><a href="/user/abc">{COMPANY}</a><a href="/video/123">山东另外环保有限公司</a><a href="/user/nick">水处理老王</a>'
    result = parse_browser_html(html, "https://www.douyin.com/search/test", "水处理")
    assert [hit.url for hit in result.batch.hits] == ["https://www.douyin.com/user/abc"]
    assert result.batch.hits[0].company_hint == COMPANY
    assert not result.batch.seeds


def test_social_external_official_link_preserves_public_source():
    html = f'<title>工厂主页</title><p>{COMPANY} <a href="https://factory.example/">企业官网</a></p>'
    result = parse_browser_html(html, "https://www.kuaishou.com/profile/abc", "水处理")
    assert len(result.batch.hits) == 1
    assert result.batch.hits[0].url == "https://factory.example/"
    assert result.batch.hits[0].source_url == result.current_url


def test_generic_platform_navigation_without_named_entity_is_not_collected():
    html = '<title>门户</title><a href="https://www.example.com/about">关于我们</a><a href="https://www.example.com/product">水处理设备</a>'
    result = parse_browser_html(html, "https://catalog.example/", "水处理")
    assert not result.batch.hits


def test_candidate_capture_is_bounded():
    html = "<title>公开列表</title>" + "".join(f'<a href="https://factory{i}.example/">山东清源{i}环保有限公司</a>' for i in range(150))
    assert len(parse_browser_html(html, "https://catalog.example/", "水处理").batch.hits) == 100


def test_large_public_dom_requires_narrower_page():
    with pytest.raises(BrowserActionRequired):
        parse_browser_html("<p>" + "水" * 700_000 + "</p>", "https://factory.example/", "水处理")


@pytest.mark.parametrize("url", ["file:///C:/private.txt", "chrome://settings", "http://127.0.0.1/", "https://user:secret@example.com/"])
def test_only_public_http_targets_can_be_read(url):
    with pytest.raises(BrowserActionRequired):
        parse_browser_html(FACTORY_HTML, url, "水处理")


class FakePage:
    def __init__(self, url="https://factory.example/", html=FACTORY_HTML, *, visible=True, focused=False):
        self.url, self.html = url, html
        self.visible, self.focused = visible, focused
        self.reads = 0
        self.threads = []
        self.closed = False

    def is_closed(self):
        return self.closed

    def evaluate(self, expression):
        self.threads.append(threading.get_ident())
        return self.visible if "visibilityState" in expression else self.focused

    def content(self):
        self.threads.append(threading.get_ident())
        self.reads += 1
        return self.html

    def bring_to_front(self):
        self.threads.append(threading.get_ident())

    def goto(self, url, **kwargs):
        self.threads.append(threading.get_ident())
        self.url = url


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.closed = False
        self.close_thread = None

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True
        self.close_thread = threading.get_ident()


class FakeDriver:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def assistant_with_pages(tmp_path, monkeypatch, pages):
    assistant = BrowserAssistant(tmp_path / "profile")
    context, driver = FakeContext(pages), FakeDriver()
    monkeypatch.setattr(assistant, "_launch_context", lambda: (driver, context))
    return assistant, context, driver


def test_browser_operations_use_one_worker_and_cleanup_owned_context(tmp_path, monkeypatch):
    page = FakePage()
    assistant, context, driver = assistant_with_pages(tmp_path, monkeypatch, [page])
    main_thread = threading.get_ident()
    opened = assistant.open("https://factory.example/")
    assert isinstance(opened, Future)
    assert opened.result(timeout=5) == page.url
    result = assistant.snapshot("水处理").result(timeout=5)
    assert result.batch.hits
    assistant.close().result(timeout=5)
    assert context.closed and driver.stopped
    assert len(set(page.threads + [context.close_thread])) == 1
    assert main_thread not in page.threads
    assert assistant.close().done()
    with pytest.raises(BrowserAssistError):
        assistant.snapshot("水处理").result(timeout=5)


def test_only_current_selected_page_content_is_read(tmp_path, monkeypatch):
    background = FakePage("https://unrelated.example/", "private unrelated data", visible=False)
    selected = FakePage()
    assistant, _, _ = assistant_with_pages(tmp_path, monkeypatch, [background, selected])
    try:
        result = assistant.snapshot("水处理").result(timeout=5)
        assert result.current_url == selected.url
        assert selected.reads == 1
        assert background.reads == 0
    finally:
        assistant.close().result(timeout=5)


def test_ambiguous_multiple_visible_tabs_require_selection_without_reading_html(tmp_path, monkeypatch):
    pages = [FakePage(), FakePage("https://other.example/")]
    assistant, _, _ = assistant_with_pages(tmp_path, monkeypatch, pages)
    try:
        with pytest.raises(BrowserActionRequired):
            assistant.snapshot("水处理").result(timeout=5)
        assert sum(page.reads for page in pages) == 0
    finally:
        assistant.close().result(timeout=5)


def test_close_before_first_open_is_immediate_without_starting_browser(tmp_path):
    assistant = BrowserAssistant(tmp_path / "profile")
    assert assistant.close().result(timeout=1) is None
    assert not assistant.is_running


def test_navigation_race_never_caches_html_under_previous_url(tmp_path, monkeypatch):
    page = FakePage()
    original = page.content
    def navigating_content():
        page.url = "https://different.example/"
        return original()
    page.content = navigating_content
    assistant, _, _ = assistant_with_pages(tmp_path, monkeypatch, [page])
    try:
        with pytest.raises(BrowserActionRequired):
            assistant.snapshot("水处理").result(timeout=5)
    finally:
        assistant.close().result(timeout=5)


def test_user_closed_browser_error_clears_context_for_next_open(tmp_path, monkeypatch):
    page = FakePage()
    assistant, context, driver = assistant_with_pages(tmp_path, monkeypatch, [page])
    TargetClosedError = type("TargetClosedError", (Exception,), {})
    def closed_content():
        raise TargetClosedError("closed")
    page.content = closed_content
    try:
        with pytest.raises(BrowserAssistError, match="重新打开"):
            assistant.snapshot("水处理").result(timeout=5)
        assert context.closed and driver.stopped
        assert assistant._context is None
        assert not assistant.is_closed
    finally:
        assistant.close().result(timeout=5)


def test_each_source_query_captures_at_most_three_unique_search_pages(tmp_path, monkeypatch):
    html = '<ol id="b_results"><li class="b_algo"><h2><a href="https://factory.example/">山东清源环保有限公司</a></h2></li></ol>'
    page = FakePage("https://www.bing.com/search?q=test&first=1", html)
    assistant, _, _ = assistant_with_pages(tmp_path, monkeypatch, [page])
    try:
        for extra in range(3):
            page.url = f"https://www.bing.com/search?q=test&first=1&filter={extra}"
            assert assistant.snapshot("水处理").result(timeout=5).batch.hits
        page.url = "https://www.bing.com/search?q=test&first=1&filter=4"
        result = assistant.snapshot("水处理").result(timeout=5)
        assert result.batch.statuses["bing"]["status"] == "page_limit"
        assert page.reads == 3
        assistant.reset_limits().result(timeout=5)
        assert assistant.snapshot("水处理").result(timeout=5).batch.hits
    finally:
        assistant.close().result(timeout=5)
