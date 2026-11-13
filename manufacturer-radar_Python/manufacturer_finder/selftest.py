"""Checks the actual desktop executable, with opt-in live network verification."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def run():
    from .bootstrap import data_directory
    from .gui import Application, admitted
    from .models import Company, SearchHit, SearchOptions
    from .storage import Storage, export_csv, export_xlsx, export_json
    report = {"ok": False, "checks": [], "frozen": bool(getattr(sys, "frozen", False)), "mode": "offline fictional fixtures"}
    target = Path(sys.argv[sys.argv.index("--report") + 1]) if "--report" in sys.argv else data_directory() / "self-test.json"
    app = None
    scratch = tempfile.TemporaryDirectory(prefix="radar-ui-v11-")
    try:
        app = Application()
        app.store = Storage(Path(scratch.name) / "ui-history.sqlite3")
        app.load_demo()
        app.update()
        assert len(app.card_widgets) == 3
        assert all(admitted(c) and c.products and c.verification_url for c in app.visible)
        report["checks"].append("manufacturer cards show only page-qualified identities with products")
        for company in app.visible:
            details = app.details(company)
            details.update()
            details.destroy()
        report["checks"].append("manufacturer contact, page proof, original links and negative evidence dialogs")
        before = list(app.result.companies)
        app.result.companies.append(Company(name="这只是搜索标题", name_confirmed=True, urls=["https://fixture.example/unvisited"]))
        app.refresh_cards()
        assert len(app.visible) == 3
        app.result.companies = before
        report["checks"].append("unvisited search titles never become manufacturer cards")
        app.filter_mode.set("有负面线索")
        app.refresh_cards()
        assert len(app.visible) >= 1
        app.filter_mode.set("全部厂家")
        app.refresh_cards()
        root = Path(scratch.name)
        app.store.save_run(app.result)
        loaded = app.store.load_run(app.result.id)
        assert loaded.to_dict() == app.result.to_dict()
        assert app.store.list_runs()[0]["count"] == len(app.result.companies)
        export_csv(app.result.companies, root / "result.csv")
        export_xlsx(app.result.companies, root / "result.xlsx")
        export_json(app.result, root / "result.json")
        assert all((root / name).stat().st_size > 100 for name in ("result.csv", "result.xlsx", "result.json"))
        report["checks"].append("qualified manufacturer filters, SQLite audit round-trip, CSV/XLSX/JSON exports")
        from . import gui, network
        from .pipeline import SearchRunner as RealRunner
        class OfflineProvider:
            def fetch(self, engine, query, page, cancel, deadline, timeout=8):
                return [SearchHit(engine=engine, query=query, page=page,
                    title="离线测试夹具机械有限公司", url="https://fixture.example/about",
                    snippet="仅凭摘要不应入库", search_url=f"https://search.example/{page}")]
        html = """<html><head><title>离线测试夹具机械有限公司 - 水处理设备厂家</title></head>
        <body><h1>离线测试夹具机械有限公司</h1><section>公司简介：本公司专业生产制造水处理设备、净水设备。
        主营产品：水处理设备、净水设备。生产厂家，拥有加工生产车间。</section>
        <section>联系我们 公司名称：离线测试夹具机械有限公司 地址：山东省济南市演示工业园 联系电话：400-000-0000</section>
        <footer>版权所有：离线测试夹具机械有限公司</footer></body></html>"""
        original_runner, original_fetch = gui.SearchRunner, network.fetch_text
        def offline_runner(options, callback=None, seed_hits=None, **kwargs):
            options.discovery_channels = []
            return RealRunner(options, callback, provider=OfflineProvider(), seed_hits=seed_hits, **kwargs)
        gui.SearchRunner = offline_runner
        network.fetch_text = lambda url, *args, **kwargs: (html, url)
        try:
            app.query.set("水处理")
            app.enrich.set(False)
            app.negative.set(False)
            for key, value in app.engine_vars.items():
                value.set(key == "sogou")
            app.start_search()
            stop = time.monotonic() + 12
            while app.running and time.monotonic() < stop:
                app.update()
                time.sleep(0.02)
            assert not app.running
            assert len(app.result.companies) == 1 and admitted(app.result.companies[0]), app.result.to_dict()
            assert app.result.engine_status["sogou"]["pages"] == 3
            assert app.store.load_run(app.result.id) is not None
            assert app.result.collection_log[0]["status"] == "accepted"
            report["checks"].append("GUI worker -> search3pages -> fetch actual HTML fixture -> qualify -> card -> local history")
        finally:
            gui.SearchRunner, network.fetch_text = original_runner, original_fetch
        app.load_demo()
        for value in app.engine_vars.values():
            value.set(True)
        app.enrich.set(True)
        app.negative.set(True)
        from .ingestion import read_text_leads
        from .discovery import build_b2b_url
        assert '/k-shuichuli/' in build_b2b_url('b2b168','水处理')
        from .map_sources import discover_maps
        import threading
        imported = read_text_leads('公司名称,官网,电话\n离线测试夹具机械有限公司,https://fixture.example/about,0531-85808866', source_url='离线测试名单.csv')
        assert len(imported) == 1
        fixture = RealRunner(SearchOptions(query='水处理', engines=[], enrich=False, check_negative=False),
            seed_hits=[], seed_leads=imported, captured_pages={'https://fixture.example/about':html}).run()
        assert len(fixture.companies)==1 and admitted(fixture.companies[0])
        assert any(e.kind=='外部公开电话' for e in fixture.companies[0].evidence)
        maps = discover_maps(SearchOptions(query='水处理', map_key=''), threading.Event(), time.monotonic()+3)
        assert maps.statuses['amap']['status']=='configuration_required'
        dialogs = [app.settings(), app.import_leads(), app.browser_collect()]
        app.update()
        assert all(dialog.winfo_exists() for dialog in dialogs)
        for dialog in dialogs:
            dialog.destroy()
        import gc
        gc.collect()
        report['checks'].append('multi-source settings/import/browser windows; list -> captured page -> qualified factory; map configuration gate')
        if '--browser-smoke' in sys.argv:
            from .browser_assist import BrowserAssistant, installed_browser_channels
            report['browser']={'installed':list(installed_browser_channels())}
            assert report['browser']['installed'], '当前电脑没有安装受支持的浏览器'
            assistant=BrowserAssistant(root/'browser-profile')
            try:
                actual=assistant.open('https://example.com/').result(timeout=40)
                captured=assistant.snapshot('水处理').result(timeout=15)
                assert captured.current_url and not captured.batch.hits
                report['browser'].update(ok=True,url=actual,captured_pages=len(captured.pages))
            finally:
                assistant.close().result(timeout=15)
            report['checks'].append('bundled Playwright driver opens installed browser, reads current page and closes dedicated context')
        if "--online" in sys.argv:
            start = time.monotonic()
            keyword = os.getenv("RADAR_TEST_QUERY", "水处理设备厂家")
            channels=['b2b','catalog'] if '--multisource' in sys.argv else []
            online = RealRunner(SearchOptions(query=keyword, budget_seconds=120, discovery_channels=channels)).run()
            report["online"] = {
                "query": keyword, "started_at": online.started_at, "finished_at": online.finished_at,
                "elapsed_seconds": round(time.monotonic() - start, 2), "engine_status": online.engine_status,
                "source_status": online.source_status, "discovery_seeds": len(online.discovery_seeds),
                "admitted_sources": {source:sum(any(h.engine==source or h.discovery_source==source for h in c.hits) for c in online.companies)
                    for source in sorted({h.discovery_source or h.engine for c in online.companies for h in c.hits})},
                "manufacturers": len(online.companies), "with_phone": sum(bool(c.phones) for c in online.companies),
                "qualification_stats": online.qualification_stats, "collection_log": online.collection_log,
                "samples": [{"name": c.name, "products": c.products, "url": c.verification_url, "admission_reasons": c.admission_reasons} for c in online.companies[:8]],
            }
            assert online.candidates, "本轮引擎均未返回候选，详见访问状态"
            assert online.companies, "本轮未识别到符合规则的厂家，详见采集记录"
            assert all(admitted(c) for c in online.companies)
            assert all(h.url and (h.search_url or h.source_url) and 1 <= h.page <= 3 for c in online.companies for h in c.hits)
            report["checks"].append("packaged live: fetch destination pages, identify manufacturer business, exclude irrelevant/unread links")
            report["mode"] += " plus live manufacturer qualification"
        if "--screenshot" in sys.argv:
            from PIL import ImageGrab
            app.update()
            app.after(600, app.quit)
            app.mainloop()
            app.update()
            path = Path(sys.argv[sys.argv.index("--screenshot") + 1])
            try:
                screenshot = ImageGrab.grab(bbox=(app.winfo_rootx(), app.winfo_rooty(), app.winfo_rootx() + app.winfo_width(), app.winfo_rooty() + app.winfo_height()))
            except OSError:
                import ctypes
                handle = ctypes.windll.user32.GetParent(app.winfo_id()) or app.winfo_id()
                screenshot = ImageGrab.grab(window=handle)
            screenshot.save(path)
            report["checks"].append("card interface screenshot captured from actual desktop executable")
        report["ok"] = True
    except Exception as exc:
        import traceback
        report["error"], report["traceback"] = str(exc), traceback.format_exc()
    finally:
        if app is not None:
            app.destroy()
        scratch.cleanup()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not report["ok"]:
        raise SystemExit(1)
