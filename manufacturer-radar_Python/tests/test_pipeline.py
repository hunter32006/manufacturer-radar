"""No-network tests of search discovery -> actual visit -> factory admission."""
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from manufacturer_finder.models import Company, Evidence, SearchHit, SearchOptions
from manufacturer_finder.pipeline import SearchRunner, manufacturer_search_query
from manufacturer_finder.providers import FetchError, SearchBlocked
from manufacturer_finder.network import DeadlineExceeded
from manufacturer_finder.scoring import is_page_qualified
from manufacturer_finder.qualification import qualify_page as real_qualify_page


NAMES = ["测试甲机械制造有限公司", "测试乙机械制造有限公司"]
QUERY = "测试关键词"
EXECUTED_QUERY = manufacturer_search_query(QUERY)


def hit(index=0, engine="bing", page=1, title=None):
    return SearchHit(engine, QUERY, page, title or NAMES[index % 2] + " 官网",
                     "https://factory-%d.example/page-%d" % (index, page),
                     "摘要可能声称有厂家和电话，但仅凭摘要不允许入库", "https://search.example/" + engine)


def qualified_company(record, url, name=NAMES[0]):
    return Company(name=name, name_confirmed=True, page_verified=True, verification_url=url,
                   urls=[url], hits=[record], products=["测试机械"],
                   admission_reasons=["实际页面存在主体、生产业务和产品关联"],
                   evidence=[Evidence("详情页核验", url, "已实际读取页面及厂家生产业务"),
                             Evidence("名称", url, name)])


def qualify(record, html, url):
    # The double obtains the company from fetched content, never a snippet.
    if not html.startswith("PAGE FOR "):
        return SimpleNamespace(company=None, reason="实际页面没有相关厂家主体", contact_urls=[])
    return SimpleNamespace(company=qualified_company(record, url, html[9:]),
                           reason="实际页面厂家与业务已识别", contact_urls=[])


def fake_page(url, cancel, deadline, timeout=8, max_bytes=2_000_000):
    name = NAMES[1] if "factory-1." in url else NAMES[0]
    return "PAGE FOR " + name, url


class FakeProvider:
    def __init__(self, blocked=(), two=False, negative=False):
        self.blocked, self.two, self.negative = set(blocked), two, negative
        self.calls, self.lock = [], threading.Lock()

    def fetch(self, engine, query, page, cancel, deadline, timeout=8):
        with self.lock:
            self.calls.append((engine, query, page, time.monotonic()))
        if engine in self.blocked:
            raise SearchBlocked("测试验证码")
        if query != EXECUTED_QUERY:
            return [SearchHit(engine, query, page, NAMES[0] + " 投诉", "https://complaint.example/clue",
                              NAMES[0] + " 存在投诉，待核实")] if self.negative else []
        return [replace(hit(index, engine, page), query=query) for index in range(2 if self.two else 1)]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        for item in [patch("manufacturer_finder.pipeline.network.fetch_text", fake_page),
                     patch("manufacturer_finder.pipeline.qualification.qualify_page", qualify)]:
            item.start()
            self.addCleanup(item.stop)

    def options(self, **kwargs):
        defaults = dict(query=QUERY, engines=["baidu", "bing"], enrich=False, check_negative=False)
        defaults.update(kwargs)
        return SearchOptions(**defaults)

    def test_manufacturer_intent_is_added_only_for_plain_products(self):
        for original, expected in [("水处理", "水处理 厂家"), ("豆腐机", "豆腐机 厂家"),
                                   ("水处理厂家", "水处理厂家"), ("水处理设备公司", "水处理设备公司"),
                                   ("测试机械厂", "测试机械厂"), ("豆腐机供应商", "豆腐机供应商"),
                                   ("豆腐机制造商", "豆腐机制造商")]:
            with self.subTest(original=original):
                self.assertEqual(expected, manufacturer_search_query(original))

    def test_executed_intent_query_is_preserved_in_hits_and_original_run_query(self):
        class IntentProvider(FakeProvider):
            def fetch(self, engine, query, page, cancel, deadline, timeout=8):
                self.calls.append((engine, query, page))
                return [replace(hit(), engine=engine, query=query, page=page)]

        provider = IntentProvider()
        options = self.options(query="水处理", engines=["bing"], pages=1)
        result = SearchRunner(options, provider=provider).run()
        self.assertEqual("水处理", result.query)
        self.assertEqual([("bing", "水处理 厂家", 1)], provider.calls)
        self.assertEqual("水处理 厂家", result.candidates[0].query)
        self.assertEqual("水处理 厂家", result.companies[0].hits[0].query)

    def test_seed_accepts_original_and_derived_query_but_rejects_unrelated_query(self):
        options = self.options(query="豆腐机", engines=["bing"])
        seeds = [replace(hit(), query="豆腐机"), replace(hit(1), query="豆腐机 厂家")]
        result = SearchRunner(options, seed_hits=seeds).run()
        self.assertEqual(2, len(result.candidates))
        with self.assertRaises(ValueError):
            SearchRunner(options, seed_hits=[replace(hit(), query="水处理 厂家")]).run()

    def test_three_page_cap_parallel_engines_and_throttle(self):
        provider = FakeProvider()
        result = SearchRunner(self.options(pages=99), provider=provider).run()
        self.assertEqual(6, len(provider.calls))
        for engine in ("baidu", "bing"):
            calls = [call for call in provider.calls if call[0] == engine]
            self.assertEqual([1, 2, 3], [call[2] for call in calls])
            self.assertTrue(all(b[3] - a[3] >= 0.59 for a, b in zip(calls, calls[1:])))
            self.assertEqual("complete", result.engine_status[engine]["status"])
        self.assertEqual(1, len(result.companies))
        self.assertEqual(6, len(result.companies[0].hits))
        self.assertEqual(6, len(result.candidates))
        self.assertEqual(3, result.qualification_stats["visited"])
        self.assertTrue(is_page_qualified(result.companies[0]))

    def test_blocked_engine_does_not_prevent_factory_from_another_engine(self):
        result = SearchRunner(self.options(pages=1), provider=FakeProvider(blocked=["baidu"])).run()
        self.assertEqual("blocked", result.engine_status["baidu"]["status"])
        self.assertEqual("complete", result.engine_status["bing"]["status"])
        self.assertEqual(1, len(result.companies))

    def test_snippets_never_create_factories_when_target_is_unavailable(self):
        with patch("manufacturer_finder.pipeline.network.fetch_text", side_effect=FetchError("目标网页无法访问")):
            result = SearchRunner(self.options(pages=1), provider=FakeProvider(two=True)).run()
        self.assertEqual([], result.companies)
        self.assertEqual(4, len(result.candidates))
        self.assertEqual(2, result.qualification_stats["failed"])
        self.assertTrue(all(row["status"] == "failed" for row in result.collection_log))

    def test_search_events_remain_empty_until_actual_page_has_been_read(self):
        fetched, invalid, events = threading.Event(), [], []

        def read(*args, **kwargs):
            fetched.set()
            return fake_page(*args, **kwargs)

        def callback(event):
            events.append(event["type"])
            if event["type"] == "results" and event["companies"] and not fetched.is_set():
                invalid.append(event)

        with patch("manufacturer_finder.pipeline.network.fetch_text", read):
            result = SearchRunner(self.options(pages=1), callback, FakeProvider()).run()
        self.assertFalse(invalid)
        self.assertTrue(result.companies)
        self.assertTrue({"candidates", "stats", "phase", "engine", "results", "done"} <= set(events))

    def test_unrecognized_name_and_articles_are_audited_not_admitted(self):
        with patch("manufacturer_finder.pipeline.network.fetch_text", return_value=("article", "https://factory-0.example/")):
            result = SearchRunner(self.options(pages=1), provider=FakeProvider()).run()
        self.assertEqual([], result.companies)
        self.assertEqual(1, result.qualification_stats["rejected"])
        self.assertIn("没有相关厂家", result.collection_log[0]["reason"])

    def test_toggling_verified_flag_without_page_evidence_does_not_admit(self):
        invalid = Company(name=NAMES[0], name_confirmed=True, page_verified=True)
        with patch("manufacturer_finder.pipeline.qualification.qualify_page",
                   return_value=SimpleNamespace(company=invalid, reason="incorrect parser", contact_urls=[])):
            result = SearchRunner(self.options(pages=1), provider=FakeProvider()).run()
        self.assertEqual([], result.companies)
        self.assertEqual(1, result.qualification_stats["rejected"])

    def test_cancel_during_search_preserves_only_audit_candidates(self):
        runner = None

        def callback(event):
            if event["type"] == "candidates" and event["count"]:
                runner.cancel()

        runner = SearchRunner(self.options(), callback, FakeProvider())
        result = runner.run()
        self.assertTrue(result.cancelled)
        self.assertTrue(result.candidates)
        self.assertEqual([], result.companies)
        self.assertEqual(result.qualification_stats["candidates"], result.qualification_stats["unvisited"])

    def test_cancel_after_admission_preserves_factories_without_late_callbacks(self):
        events, runner = [], None

        def callback(event):
            events.append(event)
            if event["type"] == "results" and event["companies"]:
                runner.cancel()

        runner = SearchRunner(self.options(pages=1), callback, FakeProvider(two=True))
        result = runner.run()
        self.assertTrue(result.cancelled)
        self.assertTrue(result.companies)
        count = len(events)
        time.sleep(0.06)
        self.assertEqual(count, len(events))
        self.assertTrue(all(is_page_qualified(company) for company in result.companies))

    def test_deadline_returns_partial_admitted_factories_and_explicit_unvisited(self):
        calls = []

        def slow_read(url, cancel, deadline, timeout=8, max_bytes=2_000_000):
            calls.append(url)
            if len(calls) == 1:
                return fake_page(url, cancel, deadline, timeout, max_bytes)
            while time.monotonic() < deadline:
                time.sleep(0.005)
            raise DeadlineExceeded("deadline")

        options = self.options(engines=["bing"])
        options.budget_seconds = 0.10
        with patch("manufacturer_finder.pipeline.network.fetch_text", slow_read), patch.object(SearchRunner, "MAX_WORKERS", 1):
            result = SearchRunner(options, seed_hits=[hit(index) for index in range(5)]).run()
        self.assertTrue(result.timed_out)
        self.assertEqual(1, len(result.companies))
        self.assertEqual(1, result.qualification_stats["accepted"])
        self.assertEqual(1, result.qualification_stats["failed"])
        self.assertEqual(3, result.qualification_stats["unvisited"])
        self.assertTrue(all(row["reason"] for row in result.collection_log))

    def test_seed_hits_skip_search_and_duplicate_urls_preserve_all_sources(self):
        provider, fetched = FakeProvider(), []

        def read(*args, **kwargs):
            fetched.append(args[0])
            return fake_page(*args, **kwargs)

        with patch("manufacturer_finder.pipeline.network.fetch_text", read):
            result = SearchRunner(self.options(), provider=provider,
                                  seed_hits=[hit(engine="bing"), hit(engine="baidu")]).run()
        self.assertEqual([], provider.calls)
        self.assertEqual(1, len(fetched))
        self.assertEqual(2, len(result.candidates))
        self.assertEqual(2, len(result.companies[0].hits))
        self.assertEqual(1, result.qualification_stats["candidates"])

    def test_candidate_limit_prioritizes_manufacturers_without_admitting_unvisited(self):
        records = [hit(0, title="知识百科文章"), hit(1), hit(2, title="设备生产厂家官网")]
        fetched = []

        def read(*args, **kwargs):
            fetched.append(args[0])
            return fake_page(*args, **kwargs)

        with patch("manufacturer_finder.pipeline.network.fetch_text", read), patch.object(SearchRunner, "MAX_CANDIDATE_URLS", 2):
            result = SearchRunner(self.options(), seed_hits=records).run()
        self.assertNotIn(records[0].url, fetched)
        self.assertEqual(3, len(result.candidates))
        self.assertEqual(1, result.qualification_stats["unvisited"])
        self.assertIn("上限", next(row for row in result.collection_log if row["url"] == records[0].url)["reason"])
        self.assertEqual(2, result.qualification_stats["accepted"])

    def test_contact_followups_are_same_host_max_two_and_never_recursive(self):
        first = hit()
        fetched = []

        def read(url, *args, **kwargs):
            fetched.append(url)
            return "PAGE FOR " + NAMES[0], url

        def contacts(record, html, url):
            return SimpleNamespace(company=qualified_company(record, url), reason="厂家识别",
                contact_urls=["/contact1", "/contact2", "/contact3", "https://other.example/contact"])

        with patch("manufacturer_finder.pipeline.network.fetch_text", read), \
             patch("manufacturer_finder.pipeline.qualification.qualify_page", contacts):
            result = SearchRunner(self.options(enrich=True), seed_hits=[first]).run()
        self.assertEqual(3, len(fetched))
        self.assertTrue(all("factory-0.example" in url for url in fetched))
        self.assertEqual(2, result.collection_log[0]["contact_pages"])
        self.assertTrue(is_page_qualified(result.companies[0]))

    def test_negative_only_admitted_firms_page_one_with_unchecked_limit(self):
        provider = FakeProvider(two=True, negative=True)
        with patch.object(SearchRunner, "ENGINE_DELAY", 0):
            result = SearchRunner(self.options(engines=["bing"], pages=1, check_negative=True, negative_limit=1), provider=provider).run()
        by_name = {company.name: company for company in result.companies}
        self.assertEqual("发现待核实线索", by_name[NAMES[0]].negative_status)
        self.assertEqual("未检查", by_name[NAMES[1]].negative_status)
        supplemental = [call for call in provider.calls if call[1] != EXECUTED_QUERY]
        self.assertEqual(1, len(supplemental))
        self.assertEqual(1, supplemental[0][2])

    def test_rejected_article_can_supply_same_name_negative_without_becoming_factory(self):
        article = SearchHit("bing", QUERY, 1, NAMES[0] + " 投诉", "https://complaint.example/article",
                            NAMES[0] + " 因质量不合格受到投诉")

        def read(url, *args, **kwargs):
            if "complaint.example" in url:
                return "article", url
            return fake_page(url, *args, **kwargs)

        with patch("manufacturer_finder.pipeline.network.fetch_text", read):
            result = SearchRunner(self.options(), seed_hits=[hit(), article]).run()
        self.assertEqual(1, len(result.companies))
        self.assertEqual("发现待核实线索", result.companies[0].negative_status)
        self.assertTrue(any(e.kind == "负面线索" and e.url == article.url for e in result.companies[0].evidence))
        self.assertEqual(1, result.qualification_stats["rejected"])
        self.assertNotIn(article.url, result.companies[0].urls)

    def test_negative_captcha_stops_later_queued_checks(self):
        class BlockedNegative(FakeProvider):
            def fetch(self, engine, query, page, cancel, deadline, timeout=8):
                if query != EXECUTED_QUERY:
                    self.calls.append((engine, query, page, time.monotonic()))
                    raise SearchBlocked("补充查询验证码")
                return super().fetch(engine, query, page, cancel, deadline, timeout)

        provider = BlockedNegative(two=True)
        with patch.object(SearchRunner, "ENGINE_DELAY", 0):
            result = SearchRunner(self.options(engines=["bing"], pages=1, check_negative=True), provider=provider).run()
        self.assertEqual(1, len([call for call in provider.calls if call[1] != EXECUTED_QUERY]))
        self.assertTrue(all(company.negative_status == "检查受限" for company in result.companies))

    def test_callbacks_are_independent_snapshots(self):
        def callback(event):
            if event["type"] == "results" and event["companies"]:
                event["companies"][0].name = "外部修改"
            if event["type"] == "candidates":
                event["candidates"].clear()
            if event["type"] == "done":
                event["result"].companies.clear()

        result = SearchRunner(self.options(pages=1), callback, FakeProvider()).run()
        self.assertEqual(NAMES[0], result.companies[0].name)
        self.assertEqual(2, len(result.candidates))

    def test_interrupted_snapshot_preserves_admitted_factories_audit_without_side_effects(self):
        events = []
        runner = SearchRunner(self.options(pages=1), callback=events.append, provider=FakeProvider(two=True))
        original = runner._publish

        def interrupted_publish():
            original()
            if runner._result.companies:
                raise RuntimeError("测试识别协调器中断")

        with patch.object(runner, "_publish", interrupted_publish):
            with self.assertRaises(RuntimeError):
                runner.run()
        # A stale in-flight record must not become an admitted factory.
        key = next(key for key, row in runner._records.items() if row["status"] != "accepted")
        runner._records[key]["status"] = "visiting"
        before = runner._result.to_dict()
        event_count = len(events)
        partial = runner.snapshot()
        self.assertTrue(partial.cancelled)
        self.assertTrue(partial.finished_at)
        self.assertTrue(partial.companies)
        self.assertTrue(all(is_page_qualified(company) for company in partial.companies))
        self.assertEqual(1, partial.qualification_stats["failed"])
        self.assertEqual(event_count, len(events))
        self.assertEqual(before, runner._result.to_dict())
        self.assertEqual("visiting", runner._records[key]["status"])

    def test_snapshot_before_start_is_empty_and_uses_original_query(self):
        runner = SearchRunner(self.options(query="水处理"))
        partial = runner.snapshot()
        self.assertEqual("水处理", partial.query)
        self.assertEqual([], partial.companies)
        self.assertTrue(partial.cancelled)

    def test_integration_real_page_qualification_rejects_article_and_ignores_snippet_phone(self):
        query = "水处理设备厂家"
        html = """<title>山东清源环保设备有限公司 - 水处理设备</title>
        <h1>山东清源环保设备有限公司</h1>
        <p>山东清源环保设备有限公司专业生产水处理设备、反渗透设备。</p>
        <p>主营产品：水处理设备、纯净水设备</p>
        <p>联系电话：13800138000</p><p>地址：山东省青岛市工业园18号</p>"""
        candidates = [SearchHit("bing", query, 1, "水处理设备厂家", url,
                               "搜索摘要电话：13911112222")
                      for url in ["https://factory.example/", "https://baike.baidu.com/item/water"]]
        options = SearchOptions(query, engines=["bing"], enrich=False, check_negative=False)
        with patch("manufacturer_finder.pipeline.qualification.qualify_page", real_qualify_page), \
             patch("manufacturer_finder.pipeline.network.fetch_text", side_effect=lambda url, *a, **kw: (html, url)):
            result = SearchRunner(options, seed_hits=candidates).run()
        self.assertEqual(1, len(result.companies))
        self.assertTrue(is_page_qualified(result.companies[0]))
        self.assertEqual(["13800138000"], result.companies[0].phones)
        self.assertEqual(1, result.qualification_stats["accepted"])
        self.assertEqual(1, result.qualification_stats["rejected"])


if __name__ == "__main__":
    unittest.main()
