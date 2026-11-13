from dataclasses import asdict
import threading
import time

import pytest

from manufacturer_finder import discovery
from manufacturer_finder.models import SearchOptions
from manufacturer_finder.network import DeadlineExceeded, FetchCancelled, HTTPFetchError
from manufacturer_finder.providers import SearchBlocked


def card(name="河南清源水处理设备有限公司", url="https://www.b2b168.com/c168-123.html", product="生产制造污水处理设备", phone="13812345678"):
    return f'<li class="info-list-item"><h2>{product}</h2><p class="company"><a href="{url}">{name}</a></p><p>主营：{product} 公开电话：{phone}</p></li>'


@pytest.mark.parametrize("source", discovery.B2B_SOURCES)
def test_direct_platform_urls_and_page_limits(source):
    from urllib.parse import urlsplit
    for page in (1, 2, 3):
        url = discovery.build_b2b_url(source, "水处理", page)
        assert url.startswith("https://")
        assert "site%3A" not in url and "site:" not in url
        assert urlsplit(url).hostname not in {"www.baidu.com", "www.so.com", "www.bing.com"}
    for page in (0, 4, True):
        with pytest.raises(ValueError):
            discovery.build_b2b_url(source, "水处理", page)


def test_real_directory_keyword_encoding():
    assert discovery.build_b2b_url("b2b168", "找水处理厂家") == "https://www.b2b168.com/k-shuichuli/"
    assert "word=%CB%AE%B4%A6%C0%ED" in discovery.build_b2b_url("madeinchina", "水处理")
    assert discovery.discovery_keyword("上海水处理设备有限公司") == "上海水处理设备有限公司"


def test_supplier_detail_only_provenance_and_exact_identity():
    url = "https://www.b2b168.com/k-shuichuli/"
    html = '<nav>' + card() + '</nav><ul>' + card() + card() + '</ul>'
    html += '<footer>' + card(url="https://www.b2b168.com/c168-999.html") + '</footer>'
    html += '<a href="https://www.b2b168.com/k-shuichuli/">水处理导航</a>'
    hits = discovery.parse_b2b_html("b2b168", html, "水处理", 1, url)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.company_hint == "河南清源水处理设备有限公司"
    assert hit.url == "https://www.b2b168.com/c168-123.html"
    assert hit.search_url == hit.source_url == url
    assert hit.engine == "b2b168" and hit.page == 1
    assert "13812345678" in hit.source_excerpt


@pytest.mark.parametrize("source,url", [
    ("madeinchina", "https://huanbao.cn.made-in-china.com/"),
    ("madeinchina", "https://3g.made-in-china.com/company-huanbao/"),
    ("gys", "https://qingyuan.gys.cn/"),
    ("gys", "https://www.gys.cn/wushuichuli/123456.html"),
    ("hc360", "https://b2b.hc360.com/supplyself/123456.html"),
    ("aicaigou", "https://b2b.baidu.com/land?id=123&u=https%3A%2F%2Fexample.com"),
    ("1688", "https://detail.1688.com/offer/123456.html"),
])
def test_each_platform_actual_detail_route(source, url):
    hits = discovery.parse_b2b_html(source, card(url=url), "水处理", 1, discovery.build_b2b_url(source, "水处理"))
    assert len(hits) == 1 and hits[0].url == url


def test_unrelated_recommendation_and_external_urls_never_admitted():
    html = card(name="河南食品机械有限公司", product="豆腐机", url="https://www.b2b168.com/c168-8.html")
    html += card(url="https://evil.example.com/advertising")
    with pytest.raises(SearchBlocked) as exc:
        discovery.parse_b2b_html("b2b168", html, "水处理", 1, "https://www.b2b168.com/k-shuichuli/")
    assert exc.value.reason == "unparsed"


def test_challenge_never_parses_recommendations_as_result():
    with pytest.raises(SearchBlocked) as exc:
        discovery.parse_b2b_html("madeinchina", '<title>请验证</title>' + card(url="https://abc.cn.made-in-china.com/"), "水处理", 1, "https://captcha.cn.made-in-china.com/verification.html")
    assert exc.value.reason == "captcha"


def test_dynamic_shell_not_reported_as_empty_success():
    with pytest.raises(SearchBlocked) as exc:
        discovery.parse_b2b_html("1688", '<title>阿里巴巴</title><div id="app"></div><script>load()</script>', "水处理", 1, discovery.build_b2b_url("1688", "水处理"))
    assert exc.value.reason == "needs_browser"


def test_zero_result_page_drops_unrelated_advertisements():
    html = '<p>共找到 0 条企业黄页信息</p>' + card(name="山东食品机械有限公司", product="豆腐机")
    assert discovery.parse_b2b_html("b2b168", html, "水处理", 1, discovery.build_b2b_url("b2b168", "水处理")) == []


def test_multiple_names_do_not_invent_single_hint():
    html = card().replace('公开电话', '合作单位：山东清水设备有限公司 公开电话')
    hit = discovery.parse_b2b_html("b2b168", html.replace('河南清源水处理设备有限公司</a>', '水处理设备厂家资料</a>'), "水处理", 1, discovery.build_b2b_url("b2b168", "水处理"))[0]
    assert not hit.company_hint


def test_only_published_same_host_next_url():
    html = '<a href="https://evil.example.com/2">下一页</a><a rel="next" href="/next?page=2">下一页</a>'
    assert discovery._next_url(html, "https://www.b2b168.com/k-shuichuli/") == "https://www.b2b168.com/next?page=2"


def options(**kwargs):
    values = dict(query="水处理", engines=[], discovery_channels=["b2b"], b2b_sources=["b2b168"], request_timeout=1)
    values.update(kwargs)
    return SearchOptions(**values)


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(discovery, "REQUEST_INTERVAL", 0)
    def binary(url, *args, **kwargs):
        html, final = discovery.fetch_text(url, *args, **kwargs)
        return html.encode("utf8"), final
    monkeypatch.setattr(discovery, "fetch_bytes", binary)


def test_crawl_follows_max_three_published_pages(no_wait, monkeypatch):
    requests = []
    def fetch(url, *args, **kwargs):
        requests.append(url)
        n = len(requests)
        return card(url=f"https://www.b2b168.com/c168-{n}.html") + f'<a href="/next?page={n+1}">下一页</a>', url
    monkeypatch.setattr(discovery, "fetch_text", fetch)
    events = []
    batch = discovery.discover(options(pages=99), threading.Event(), time.monotonic() + 5, events.append)
    assert len(requests) == len(batch.hits) == 3
    assert batch.statuses["b2b168"]["pages"] == 3
    assert batch.statuses["b2b168"]["status"] == "ok"
    assert all(event["type"] == "source" for event in events)


def test_no_published_next_link_stops_after_one_actual_page(no_wait, monkeypatch):
    calls = []
    monkeypatch.setattr(discovery, "fetch_text", lambda url, *args, **kwargs: (calls.append(url) or card(), url))
    batch = discovery.discover(options(), threading.Event(), time.monotonic() + 5)
    assert len(calls) == 1
    assert batch.statuses["b2b168"]["pages"] == 1


def test_repeated_page_is_explicit_and_not_counted_twice(no_wait, monkeypatch):
    monkeypatch.setattr(discovery, "fetch_text", lambda url, *args, **kwargs: (card() + '<a href="/next">下一页</a>', url))
    batch = discovery.discover(options(), threading.Event(), time.monotonic() + 5)
    assert len(batch.hits) == 1
    assert batch.statuses["b2b168"]["status"] == "unparsed"
    assert batch.statuses["b2b168"]["pages"] == 2


def test_http_block_is_zero_retrieved_pages(no_wait, monkeypatch):
    def blocked(url, *args, **kwargs):
        raise HTTPFetchError("HTTP 403", 403, url)
    monkeypatch.setattr(discovery, "fetch_text", blocked)
    batch = discovery.discover(options(), threading.Event(), time.monotonic() + 5)
    status = batch.statuses["b2b168"]
    assert status["status"] == "blocked" and status["pages"] == 0 and status["attempted_pages"] == 1


def test_cancelled_and_expired_runs_do_not_fetch(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("network must not be used")
    monkeypatch.setattr(discovery, "fetch_text", forbidden)
    cancel = threading.Event()
    cancel.set()
    assert discovery.discover(options(), cancel, time.monotonic() + 5).statuses["b2b168"]["status"] == "cancelled"
    assert discovery.discover(options(), threading.Event(), time.monotonic() - 1).statuses["b2b168"]["status"] == "timeout"


def test_catalog_record_relevance_and_no_publisher_phone_leak():
    html = '''<h1>水处理参展企业</h1><table><tr><th>公司名称</th><th>主营</th><th>电话</th><th>官网</th></tr>
    <tr><td>河南清源环保设备有限公司</td><td>生产制造污水处理设备</td><td>13812345678</td><td><a href="https://qingyuan.example.com">官网</a></td></tr>
    <tr><td>河南食品设备有限公司</td><td>生产豆腐机</td><td>13912345678</td><td></td></tr></table>
    <footer>主办单位：北京展览有限公司 展会咨询：010-12345678</footer>'''
    seeds = discovery.parse_catalog_html(html, "https://expo.example.com/list", "水处理")
    assert len(seeds) == 1
    assert seeds[0].name == "河南清源环保设备有限公司"
    assert seeds[0].url == "https://qingyuan.example.com"
    assert seeds[0].phones == ["13812345678"]
    assert seeds[0].source == "catalog"
    assert "010-12345678" not in asdict(seeds[0]).__str__()


def test_catalog_pdf_viewer_only_reads_literal_same_host_file():
    html = '<iframe src="/viewer.html?file=%2Ffiles%2Flist.pdf"></iframe><a href="https://outside.example.com/list.pdf">展商名录</a><a href="/news">新闻</a>'
    assert discovery._catalog_links(html, "https://expo.example.com/list") == ["https://expo.example.com/files/list.pdf"]


def test_catalog_invalid_private_url_not_fetched(no_wait, monkeypatch):
    monkeypatch.setattr(discovery, "fetch_text", lambda *a, **k: pytest.fail("unsafe fetch"))
    batch = discovery.discover(options(discovery_channels=["catalog"], catalog_urls=["http://127.0.0.1/list"]), threading.Event(), time.monotonic() + 5)
    assert batch.statuses["catalog:1"]["status"] == "error"
    assert batch.statuses["catalog:1"]["pages"] == 0


def test_catalog_caps_three_documents_and_preserves_source(no_wait, monkeypatch):
    calls = []
    def fetch(url, *args, **kwargs):
        calls.append(url)
        n = len(calls)
        html = f'<table><tr><td>河南清源水处理设备{n}有限公司</td></tr></table><a href="/list{n+1}">下一页</a>'
        return html, url
    monkeypatch.setattr(discovery, "fetch_text", fetch)
    batch = discovery.discover(options(discovery_channels=["catalog"], catalog_urls=["https://expo.example.com/list"]), threading.Event(), time.monotonic() + 5)
    assert len(calls) == 3 and len(batch.seeds) == 3
    assert batch.statuses["catalog:1"]["pages"] == 3
    assert batch.statuses["catalog:1"]["status"] == "ok"
    assert all(seed.source_url in calls for seed in batch.seeds)


def test_catalog_partial_attachment_failure_is_explicit(no_wait, monkeypatch):
    monkeypatch.setattr(discovery, "fetch_text", lambda url, *a, **k: ('<p>河南清源水处理设备有限公司</p><a href="/list.pdf">名录</a>', url))
    def failed(*args, **kwargs):
        if str(args[0]).endswith(".pdf"):
            raise ValueError("扫描附件无法读取")
        html, final = discovery.fetch_text(*args, **kwargs)
        return html.encode("utf8"), final
    monkeypatch.setattr(discovery, "fetch_bytes", failed)
    batch = discovery.discover(options(discovery_channels=["catalog"], catalog_urls=["https://expo.example.com/list"]), threading.Event(), time.monotonic() + 5)
    assert batch.statuses["catalog:1"]["status"] == "partial"
    assert batch.statuses["catalog:1"]["attempted_pages"] == 2
    assert batch.statuses["catalog:1"]["pages"] == 1
    assert len(batch.seeds) == 1


def test_pdf_filter_runs_before_hundred_seed_limit(monkeypatch):
    import pypdf
    class Page:
        def extract_text(self):
            return '\n'.join([f"河南食品设备{i}有限公司" for i in range(120)] + ["河南清源水处理设备有限公司 13812345678"])
    class Reader:
        is_encrypted = False
        pages = [Page()]
    monkeypatch.setattr(pypdf, "PdfReader", lambda stream: Reader())
    seeds = discovery._pdf_catalog(b"%PDF", "https://expo.example.com/list.pdf", "水处理", threading.Event(), time.monotonic() + 5)
    assert len(seeds) == 1
    assert seeds[0].name == "河南清源水处理设备有限公司"
    assert seeds[0].phones == ["13812345678"]


def test_pdf_checks_cancel_between_pages(monkeypatch):
    import pypdf
    cancel = threading.Event()
    class Page:
        def extract_text(self):
            cancel.set()
            return "河南清源水处理设备有限公司"
    class Reader:
        is_encrypted = False
        pages = [Page(), Page()]
    monkeypatch.setattr(pypdf, "PdfReader", lambda stream: Reader())
    with pytest.raises(FetchCancelled):
        discovery._pdf_catalog(b"%PDF", "https://expo.example.com/list.pdf", "水处理", cancel, time.monotonic() + 5)


def test_pacer_cannot_sleep_past_deadline():
    pacer = discovery._Pacer(threading.Event(), time.monotonic() + .03)
    pacer.last = time.monotonic()
    start = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        pacer.wait()
    assert time.monotonic() - start < .3


def test_html_query_filter_precedes_hundred_seed_cap():
    rows = ''.join(f'<tr><td>河南食品设备{i}有限公司</td><td>豆腐机</td></tr>' for i in range(120))
    rows += '<tr><td>河南清源环保设备有限公司</td><td>生产污水处理设备</td></tr>'
    seeds = discovery.parse_catalog_html('<table><tr><th>企业名称</th><th>主营</th></tr>' + rows + '</table>', 'https://expo.example.com/list', '水处理')
    assert len(seeds) == 1 and seeds[0].name == '河南清源环保设备有限公司'


def test_extensionless_pdf_is_detected_without_second_request(no_wait, monkeypatch):
    from manufacturer_finder.source_types import LeadSeed
    calls = []
    def binary(url, *args, **kwargs):
        calls.append(url)
        return b'%PDF-fake', url
    monkeypatch.setattr(discovery, 'fetch_bytes', binary)
    monkeypatch.setattr(discovery, '_pdf_catalog', lambda *a: [LeadSeed(name='河南清源水处理设备有限公司', source='catalog', source_url=a[1])])
    batch = discovery.discover(options(discovery_channels=['catalog'], catalog_urls=['https://expo.example.com/download?id=1']), threading.Event(), time.monotonic() + 5)
    assert len(calls) == 1 and len(batch.seeds) == 1
    assert batch.statuses['catalog:1']['status'] == 'ok'
