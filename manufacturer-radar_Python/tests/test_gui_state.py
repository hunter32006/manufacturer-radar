from copy import deepcopy
import pytest
from manufacturer_finder.bootstrap import configure_tcl
configure_tcl()
from manufacturer_finder.demo import make_demo_run
from manufacturer_finder.gui import Application
from manufacturer_finder.models import Company, Evidence, RunResult, SearchHit
from manufacturer_finder.storage import Storage


@pytest.fixture(scope="module")
def gui_window():
    window = Application()
    window.withdraw()
    yield window
    window.destroy()


@pytest.fixture
def app(tmp_path, gui_window):
    import queue
    gui_window.store = Storage(tmp_path / "history.sqlite3")
    gui_window.result = RunResult()
    gui_window._resume_base = None
    gui_window.events = queue.Queue()
    gui_window.running = False
    gui_window.filter_text.set("")
    gui_window.filter_mode.set("全部厂家")
    return gui_window


def test_search_titles_never_become_cards_even_with_company_name(app):
    app.result = make_demo_run()
    app.result.companies.append(Company(name="未访问网页测试机械有限公司", name_confirmed=True,
        phones=["13800138000"], urls=["https://fixture.example/unread"]))
    app.refresh_cards()
    assert len(app.visible) == len(app.card_widgets) == 3
    assert all(c.page_verified for c in app.visible)


def test_legacy_links_migrate_to_audit_instead_of_manufacturer_cards(app):
    hit = SearchHit("sogou", "豆腐机", 1, "某标题", "https://fixture.example/unread")
    app.result = RunResult(query="豆腐机", companies=[Company(name="未访问网页有限公司", hits=[hit])])
    app._remove_legacy_links()
    app.refresh_cards()
    assert app.visible == [] and app.result.companies == []
    assert app.result.candidates[0].url == hit.url
    assert app.result.qualification_stats["unvisited"] == 1


def test_resume_preserves_search_coverage_and_refreshes_score(app):
    base = make_demo_run()
    company = deepcopy(base.companies[0])
    company.phones = []
    company.evidence = [e for e in company.evidence if e.kind != "电话"]
    from manufacturer_finder.scoring import score_company
    score_company(company)
    old_score = company.score
    base.companies = [company]
    base.engine_status = {"sogou": {"pages": 3, "page_numbers": [1, 2, 3], "status": "complete", "hits": 30}}
    extra = deepcopy(company)
    extra.phones = ["13800138000"]
    extra.evidence.append(Evidence(kind="电话", url=extra.verification_url, excerpt="联系电话：13800138000"))
    partial = RunResult(query=base.query, companies=[extra],
        engine_status={"sogou": {"pages": 0, "status": "imported"}})
    app._resume_base = base
    app.finish(partial)
    assert app.result.engine_status["sogou"]["pages"] == 3
    assert app.result.companies[0].score > old_score
    assert "13800138000" in app.result.companies[0].phones
    assert app.store.load_run(base.id).companies[0].score == app.result.companies[0].score


def test_failure_snapshot_keeps_admitted_manufacturers_and_audit(app):
    partial = make_demo_run()
    partial.cancelled = True
    app.events.put({"type": "failed", "message": "测试异常", "result": partial})
    app._poll()
    assert len(app.result.companies) == 3
    assert app.result.collection_log
    assert app.store.load_run(partial.id).collection_log == app.result.collection_log
