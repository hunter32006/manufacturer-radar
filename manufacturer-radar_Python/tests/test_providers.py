"""Adapter contracts and failure handling; no live web traffic in unit tests."""
import socket
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from manufacturer_finder.models import SearchHit
from manufacturer_finder.network import (
    DeadlineExceeded, FetchCancelled, FetchError, HTTPFetchError, UnsafeURL,
    _decode_html, _known_search_wrapper_target, _resolve_public, _transparent_html_redirect, fetch_text, safe_url,
)
from manufacturer_finder.providers import (
    ENGINES, SearchBlocked, SearchProvider, build_search_url, parse_search_html,
)


class ProviderTests(unittest.TestCase):
    def parse(self, engine, html, page=1):
        return parse_search_html(engine, html, "水箱厂家", page, build_search_url(engine, "水箱厂家", page))

    def test_engine_paging_is_explicit_and_capped(self):
        expected = {
            "baidu": ("pn", ["0", "10", "20"]),
            "sogou": ("page", ["1", "2", "3"]),
            "so360": ("pn", ["1", "2", "3"]),
            "bing": ("first", ["1", "11", "21"]),
            "toutiao": ("page_num", ["0", "1", "2"]),
            "shenma": ("page", ["1", "2", "3"]),
        }
        self.assertEqual(set(ENGINES), set(expected))
        for engine, (parameter, values) in expected.items():
            for page, value in enumerate(values, 1):
                with self.subTest(engine=engine, page=page):
                    url = build_search_url(engine, "水箱 & 厂家", page)
                    self.assertEqual(parse_qs(urlsplit(url).query)[parameter], [value])
            for invalid in (0, -1, 4, 99, True):
                with self.assertRaises(ValueError):
                    build_search_url(engine, "水箱", invalid)

    def test_baidu_preserves_redirect_and_marks_only_explicit_ads(self):
        html = '''<div id="content_left">
          <div class="c-container result"><h3><a href="/link?url=opaque&amp;src=search">华兴水箱有限<em>公司</em></a></h3><div class="c-abstract">电话 13800138000</div><span>广告</span></div>
          <div class="c-container result"><h3><a href="https://example.com/about?campaign=ad#intro">广告服务设备制造厂</a></h3><div class="c-abstract">我们提供广告印刷设备和推广服务。</div></div>
          <div class="result"><h3><a href="/s?wd=其他厂家">相关搜索</a></h3></div>
          <div class="result"><h3><a href="https://e.baidu.com/">想在此推广您的厂家吗</a></h3></div>
        </div>'''
        hits = self.parse("baidu", html)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].url, "https://www.baidu.com/link?url=opaque&src=search")
        self.assertIn("有限公司", hits[0].title)
        self.assertEqual(hits[0].ad_status, "广告")
        self.assertIn("13800138000", hits[0].snippet)
        self.assertEqual(hits[1].ad_status, "未标识")
        self.assertEqual(hits[1].url, "https://example.com/about?campaign=ad#intro")
        self.assertEqual(hits[0].search_url, build_search_url("baidu", "水箱厂家", 1))

    def test_all_six_realistic_card_structures(self):
        fixtures = {
            "baidu": '<div id="content_left"><div class="result c-container"><h3><a href="https://example.com/factory">华兴水箱制造有限公司</a></h3><div class="c-abstract">电话 13800138000</div></div></div>',
            "sogou": '<div class="results"><div class="vrwrap"><h3 class="vr-title"><a href="/link?url=opaque">华兴水箱制造有限公司</a></h3><div id="cacheresult_summary_1">电话 13800138000</div></div></div>',
            "so360": '<ul class="result"><li class="res-list"><h3 class="res-title"><a href="/link?m=opaque">华兴水箱制造有限公司</a></h3><p class="res-desc">电话 13800138000</p></li></ul>',
            "bing": '<ol id="b_results"><li class="b_algo"><h2><a href="https://example.com/factory">华兴水箱制造有限公司</a></h2><div class="b_caption"><p>电话 13800138000</p></div></li></ol>',
            "toutiao": '<div class="result-content"><div class="cs-card"><div class="cs-header"><a href="https://sou.toutiao.com/search/jump?url=https%3A%2F%2Fexample.com%2Ffactory">华兴水箱制造有限公司</a></div><div class="cs-summary">电话 13800138000</div></div></div>',
            "shenma": '<div id="results"><div class="sc"><div class="qk-card"><div class="qk-title"><a href="https://example.com/factory"><div class="qk-title-text">华兴水箱制造有限公司</div></a></div><div class="qk-paragraph-text">电话 13800138000</div></div></div></div>',
        }
        for engine, html in fixtures.items():
            with self.subTest(engine=engine):
                hits = self.parse(engine, html)
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0].title, "华兴水箱制造有限公司")
                self.assertIn("13800138000", hits[0].snippet)
                self.assertEqual(hits[0].ad_status, "未标识")

    def test_bing_sponsored_label_and_navigation_excluded(self):
        html = '''<ol id="b_results"><li class="b_ad"><h2><a href="https://www.bing.com/aclick?ld=opaque">华兴制造厂</a></h2><span>Sponsored</span></li>
        <li class="b_algo"><h2><a href="/search?q=more&amp;first=11">下一页</a></h2></li>
        <li class="b_algo"><h2><a href="https://127.0.0.1/">内网页面</a></h2></li></ol>'''
        hits = self.parse("bing", html)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].ad_status, "广告")

    def test_360_vertical_search_widgets_are_not_supplier_pages(self):
        html = '''<ul class="result"><li class="res-list"><h3><a href="https://example.com/factory">真正制造厂</a></h3></li>
        <li class="res-list"><h3><a href="https://image.so.com/i?q=water">水箱厂家 - 360图片</a></h3></li>
        <li class="res-list"><h3><a href="https://wenku.so.com/s?q=water">水箱厂家 - 360文库</a></h3></li>
        <li class="res-list"><h3><a href="http://map.360.cn/?k=water">水箱厂家 - 360地图</a></h3></li></ul>'''
        self.assertEqual([h.title for h in self.parse("so360", html)], ["真正制造厂"])

    def test_shenma_image_only_ad_keeps_link_and_unknown_name(self):
        html = '<div id="results"><div class="cpc-card"><a class="cpc-a" href="http://m.sm.cn/adclick?disguise_click_url=opaque"><img src="https://example.com/pic.jpg"><div class="cpc-img-ad">广告</div></a></div></div>'
        hits = self.parse("shenma", html)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].title, "图片广告（未提供文字名称）")
        self.assertEqual(hits[0].url, "http://m.sm.cn/adclick?disguise_click_url=opaque")
        self.assertEqual(hits[0].ad_status, "广告")

    def test_hidden_ad_template_does_not_mark_real_result(self):
        html = '<div class="results"><div class="vrwrap"><h3><a href="https://example.com/">华兴制造有限公司</a></h3><div style="display: none"><span>广告</span></div></div></div>'
        self.assertEqual(self.parse("sogou", html)[0].ad_status, "未标识")

    def test_duplicate_links_with_fragment_are_deduplicated(self):
        html = '<ol id="b_results"><li class="b_algo"><h2><a href="https://example.com/a#top">华兴制造厂</a></h2></li><li class="b_algo"><h2><a href="https://example.com/a#footer">华兴制造厂</a></h2></li></ol>'
        hits = self.parse("bing", html)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].url, "https://example.com/a#top")

    def test_captcha_and_js_shell_are_explicit_errors(self):
        for engine in ENGINES:
            with self.subTest(engine=engine), self.assertRaises(SearchBlocked) as ctx:
                self.parse(engine, '<html><title>百度安全验证</title><p>请输入验证码</p></html>')
            self.assertEqual(ctx.exception.reason, "captcha")
        with self.assertRaises(SearchBlocked) as ctx:
            self.parse("toutiao", '<html><div id="app"></div><script src="/bundle.js"></script></html>')
        self.assertEqual(ctx.exception.reason, "unparsed")
        self.assertIn("JavaScript", str(ctx.exception))

    def test_captcha_phrase_in_real_search_result_is_not_block(self):
        html = '<title>验证码设备_百度搜索</title><div id="content_left"><div class="result"><h3><a href="https://example.com/a">验证码设备有限公司</a></h3><div class="c-abstract">提示“请输入验证码”的设备说明</div></div></div>'
        self.assertEqual(len(self.parse("baidu", html)), 1)

    def test_explicit_empty_result_is_empty_not_failure(self):
        self.assertEqual(self.parse("bing", '<html><title>搜索</title><p>There are no results for this query.</p></html>'), [])

    @patch("manufacturer_finder.providers.fetch_text")
    def test_fetch_stops_repeated_pages(self, fetch):
        html = '<ol id="b_results"><li class="b_algo"><h2><a href="https://example.com/a">华兴制造有限公司</a></h2></li></ol>'
        fetch.return_value = (html, "https://www.bing.com/search?q=water")
        provider = SearchProvider()
        self.assertEqual(len(provider.fetch("bing", "水箱", 1)), 1)
        with self.assertRaises(SearchBlocked) as ctx:
            provider.fetch("bing", "水箱", 2)
        self.assertEqual(ctx.exception.reason, "repeated")
        # A new run's first page can be refreshed normally.
        self.assertEqual(len(provider.fetch("bing", "水箱", 1)), 1)

    @patch("manufacturer_finder.providers.fetch_text")
    def test_fetch_http_429_is_block_not_empty_success(self, fetch):
        fetch.side_effect = HTTPFetchError("too many requests", 429, "https://www.bing.com/")
        with self.assertRaises(SearchBlocked) as ctx:
            SearchProvider().fetch("bing", "水箱", 1)
        self.assertEqual(ctx.exception.reason, "access")

    @patch("manufacturer_finder.providers.fetch_text")
    def test_fetch_detects_challenge_redirect(self, fetch):
        fetch.return_value = ("<html></html>", "https://wappass.baidu.com/static/captcha/tuxing_v2.html")
        with self.assertRaises(SearchBlocked) as ctx:
            SearchProvider().fetch("baidu", "水箱", 1)
        self.assertEqual(ctx.exception.reason, "captcha")


class NetworkTests(unittest.TestCase):
    def test_only_valid_public_http_urls(self):
        valid = ["https://www.baidu.com/s?wd=test", "http://example.com/a?q=1", "https://8.8.8.8/", "https://[2606:4700:4700::1111]/"]
        invalid = ["file:///C:/Windows/win.ini", "javascript:alert(1)", "ftp://example.com/a", "https://user:pass@example.com/", "http://127.0.0.1/", "http://10.1.2.3/", "http://192.168.1.2/", "http://169.254.169.254/", "http://localhost/", "http://machine.local/", "http://2130706433/", "http://0177.0.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "https://example.com:0/", "https://example.com:99999/", "https://examp le.com/", "https://example.com\\@127.0.0.1/", "https://example.com/\r\nHost:local"]
        for url in valid:
            with self.subTest(url=url):
                self.assertTrue(safe_url(url))
        for url in invalid:
            with self.subTest(url=url):
                self.assertFalse(safe_url(url))

    def test_private_or_mixed_dns_answers_rejected(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        for answers in [[private], [public, private]]:
            with patch("manufacturer_finder.network.socket.getaddrinfo", return_value=answers):
                with self.assertRaises(UnsafeURL):
                    _resolve_public("example.com", 443, None, time.monotonic()+1, 1)

    def test_cancel_and_deadline_checked_before_any_request(self):
        cancel = threading.Event()
        cancel.set()
        with patch("manufacturer_finder.network.requests.Session") as session:
            with self.assertRaises(FetchCancelled):
                fetch_text("https://example.com/", cancel)
            with self.assertRaises(DeadlineExceeded):
                fetch_text("https://example.com/", deadline=time.monotonic()-1)
            session.assert_not_called()

    def response(self, url="https://example.com/", status=200, headers=None, chunks=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = status
        response.headers = headers or {"Content-Type": "text/html; charset=UTF-8"}
        response.url = url
        response.encoding = "utf-8"
        response.raw._connection = None
        response.raw.read1.side_effect = (chunks or [b"<html>ok</html>", b""])
        return response

    @patch("manufacturer_finder.network.requests.Session")
    def test_redirect_to_localhost_rejected_before_second_request(self, session_cls):
        session = session_cls.return_value.__enter__.return_value
        session.get.return_value = self.response(status=302, headers={"Location": "http://127.0.0.1/private"})
        with self.assertRaises(UnsafeURL):
            fetch_text("https://example.com/")
        self.assertEqual(session.get.call_count, 1)

    @patch("manufacturer_finder.network.requests.Session")
    def test_decompressed_size_is_bounded(self, session_cls):
        session = session_cls.return_value.__enter__.return_value
        session.get.return_value = self.response(chunks=[b"12345", b"67890", b""])
        with self.assertRaises(FetchError) as ctx:
            fetch_text("https://example.com/", max_bytes=8)
        self.assertIn("大小上限", str(ctx.exception))

    @patch("manufacturer_finder.network.requests.Session")
    def test_valid_utf8_and_final_url_returned(self, session_cls):
        session = session_cls.return_value.__enter__.return_value
        session.get.return_value = self.response(url="https://example.com/result", chunks=["<html>华兴制造厂</html>".encode(), b""])
        html, url = fetch_text("https://example.com/")
        self.assertIn("华兴制造厂", html)
        self.assertEqual(url, "https://example.com/result")

    def test_legacy_chinese_charset(self):
        self.assertIn("华兴制造厂", _decode_html('<meta charset="gb2312">华兴制造厂'.encode("gb2312"), "ISO-8859-1"))

    def test_sogou_literal_wrapper_is_transparent_navigation(self):
        wrapper = '<meta content="always" name="referrer"><script>window.location.replace("https://factory.example/about")</script><noscript><META http-equiv="refresh" content="0;URL=\'https://factory.example/about\'"></noscript>'
        self.assertEqual(_transparent_html_redirect(wrapper, "https://www.sogou.com/link?url=opaque"), "https://factory.example/about")
        self.assertEqual(_transparent_html_redirect('<script>location.href="../about";</script>', "https://factory.example/redirect/go"), "https://factory.example/about")
        self.assertEqual(_transparent_html_redirect('<meta http-equiv="refresh" content="1; URL=/about">', "https://factory.example/"), "https://factory.example/about")

    def test_real_pages_dynamic_js_and_conflicting_targets_are_not_followed(self):
        pages = [
            '<html><body><h1>华兴机械有限公司</h1><p>主营机械设备的生产制造</p><script>location.replace("https://other.example/")</script></body></html>',
            '<script>if (navigator.userAgent) location.replace("https://other.example/");</script>',
            '<script>location.replace("https://" + location.host)</script>',
            '<script>location.replace(atob("aHR0cHM6Ly9leGFtcGxlLmNvbQ=="))</script>',
            '<meta http-equiv="refresh" content="0;URL=https://one.example/"><script>location.replace("https://two.example/")</script>',
            '<meta http-equiv="refresh" content="30;URL=https://other.example/">',
            '<p>请输入验证码</p><meta http-equiv="refresh" content="0;URL=https://other.example/">',
        ]
        for page in pages:
            with self.subTest(page=page):
                self.assertIsNone(_transparent_html_redirect(page, "https://example.com/"))

    @patch("manufacturer_finder.network.requests.Session")
    def test_fetch_enters_literal_html_redirect_destination(self, session_cls):
        session = session_cls.return_value.__enter__.return_value
        session.get.side_effect = [
            self.response(url="https://www.sogou.com/link?url=opaque", chunks=[b'<script>window.location.replace("https://factory.example/")</script>', b""]),
            self.response(url="https://factory.example/", chunks=["<h1>华兴机械有限公司</h1>".encode(), b""]),
        ]
        html, final_url = fetch_text("https://www.sogou.com/link?url=opaque")
        self.assertEqual(final_url, "https://factory.example/")
        self.assertIn("华兴机械有限公司", html)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args_list[1].args[0], "https://factory.example/")

    @patch("manufacturer_finder.network.requests.Session")
    def test_html_redirect_to_private_url_rejected(self, session_cls):
        session = session_cls.return_value.__enter__.return_value
        session.get.return_value = self.response(chunks=[b'<meta http-equiv="refresh" content="0;URL=http://127.0.0.1/private">', b""])
        with self.assertRaises(UnsafeURL):
            fetch_text("https://example.com/")
        self.assertEqual(session.get.call_count, 1)

    @patch("manufacturer_finder.network.requests.Session")
    def test_http_and_html_redirects_share_one_bounded_budget(self, session_cls):
        session = session_cls.return_value.__enter__.return_value
        session.get.side_effect = [
            self.response(url=f"https://example.com/{i}", status=302, headers={"Location": f"/{i+1}"}) if i % 2 == 0 else
            self.response(url=f"https://example.com/{i}", chunks=[f'<script>location.replace("/{i+1}")</script>'.encode(), b""])
            for i in range(6)
        ]
        with self.assertRaises(FetchError) as ctx:
            fetch_text("https://example.com/0")
        self.assertIn("重定向次数", str(ctx.exception))
        self.assertEqual(session.get.call_count, 6)

    def test_only_exact_known_wrapper_domains_and_paths_publish_targets(self):
        for url in [
            "https://example.com/search/jump?url=https://factory.example/",
            "https://sou.toutiao.com.evil.example/search/jump?url=https://factory.example/",
            "https://link.wtturl.cn/other?target=https://factory.example/",
            "https://sou.toutiao.com/search/jump?url=https://one.example/&url=https://two.example/",
        ]:
            with self.subTest(url=url):
                self.assertIsNone(_known_search_wrapper_target(url))
        target = "https://factory.example/products?keyword=a+b&source=search"
        source = "https://link.wtturl.cn/middle-page?" + urlencode({"target": target})
        self.assertEqual(_known_search_wrapper_target(source), target)

    @patch("manufacturer_finder.network.requests.Session")
    def test_nested_toutiao_wrappers_fetch_actual_destination(self, session_cls):
        target = "https://factory.example/products?source=search"
        middle = "https://link.wtturl.cn/middle-page?" + urlencode({"target": target})
        nested = "https://sou.toutiao.com/search/jump?" + urlencode({"url": middle})
        source = "https://sou.toutiao.com/search/jump?" + urlencode({"url": nested})
        session = session_cls.return_value.__enter__.return_value
        session.get.return_value = self.response(url=target, chunks=["<h1>华兴机械有限公司</h1>".encode(), b""])
        html, final_url = fetch_text(source)
        self.assertEqual(final_url, target)
        self.assertIn("华兴机械有限公司", html)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(session.get.call_args.args[0], target)

    @patch("manufacturer_finder.network.requests.Session")
    def test_known_wrapper_private_target_never_requested(self, session_cls):
        source = "https://link.wtturl.cn/middle-page?" + urlencode({"target": "http://169.254.169.254/latest"})
        session = session_cls.return_value.__enter__.return_value
        with self.assertRaises(UnsafeURL):
            fetch_text(source)
        session.get.assert_not_called()

    @patch("manufacturer_finder.network.requests.Session")
    def test_nested_wrapper_budget_includes_http_redirects(self, session_cls):
        target = "https://factory.example/"
        source = target
        for _ in range(5):
            source = "https://sou.toutiao.com/search/jump?" + urlencode({"url": source})
        session = session_cls.return_value.__enter__.return_value
        session.get.return_value = self.response(url=target, status=302, headers={"Location": "https://factory.example/about"})
        with self.assertRaises(FetchError) as ctx:
            fetch_text(source)
        self.assertIn("重定向次数", str(ctx.exception))
        self.assertEqual(session.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
