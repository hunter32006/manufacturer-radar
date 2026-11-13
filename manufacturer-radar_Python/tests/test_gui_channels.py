import sys
from types import SimpleNamespace
import tkinter as tk

import pytest

from manufacturer_finder import gui_channels
from manufacturer_finder.bootstrap import configure_tcl
from manufacturer_finder.providers import ENGINES

configure_tcl()


def app_state(monkeypatch):
    interpreter = tk.Tcl()
    app = SimpleNamespace(_root=interpreter._root, tk=interpreter.tk)
    presets = [{"label": "公开名录", "url": "https://example.com/list"}]
    monkeypatch.setitem(sys.modules, "manufacturer_finder.discovery", SimpleNamespace(CATALOG_PRESETS=presets))
    gui_channels.init_channels(app)
    return app


def test_default_channel_options_keys_and_map_is_opt_in(monkeypatch):
    app = app_state(monkeypatch)
    options = gui_channels.channel_options(app)
    assert options["discovery_channels"] == ["b2b", "catalog"]
    assert options["b2b_sources"] == list(gui_channels.B2B_SOURCES)
    assert options["map_key"] == "" and options["map_authorized"] is False
    assert options["catalog_urls"] == ["https://example.com/list"]


def test_custom_list_url_validation_and_map_memory_options(monkeypatch):
    app = app_state(monkeypatch)
    app.channel_maps.set(True)
    app.map_key_var.set("test-memory-secret")
    app.map_city_var.set("郑州")
    app.map_authorized_var.set(True)
    app.map_provider_var.set("百度地图 API")
    options = gui_channels.channel_options(app)
    assert options["map_provider"] == "baidu_map" and "maps" in options["discovery_channels"]
    assert options["map_key"] == "test-memory-secret"
    app.catalog_custom_urls.set("http://127.0.0.1/private")
    with pytest.raises(ValueError, match="公开"):
        gui_channels.channel_options(app)


def test_empty_enabled_channels_need_explicit_source(monkeypatch):
    app = app_state(monkeypatch)
    for variable in app.b2b_source_vars.values():
        variable.set(False)
    with pytest.raises(ValueError, match="B2B"):
        gui_channels.channel_options(app)


def test_import_without_query_is_guidance_not_admission(monkeypatch):
    app = SimpleNamespace(running=False, query=SimpleNamespace(get=lambda: ""))
    notices = []
    monkeypatch.setattr(gui_channels.messagebox, "showinfo", lambda *a, **kw: notices.append(a))
    gui_channels.import_leads(app)
    assert notices and "关键词" in notices[0][0]


def test_channel_and_import_windows_build_and_preview_before_search(monkeypatch):
    monkeypatch.setattr(gui_channels, "_presets", lambda: [{"label": "公开名录", "url": "https://example.com/list"}])
    app = tk.Tk()
    app.withdraw()
    app.running = False
    app.query = tk.StringVar(master=app, value="水处理")
    app.engine_vars = {key: tk.BooleanVar(master=app, value=True) for key in ENGINES}
    app.budget = tk.StringVar(master=app, value="120")
    app.enrich = tk.BooleanVar(master=app, value=True)
    app.negative = tk.BooleanVar(master=app, value=True)
    calls = []
    app.start_search = lambda **kwargs: calls.append(kwargs)
    gui_channels.init_channels(app)

    def all_widgets(widget):
        result = [widget]
        for child in widget.winfo_children():
            result.extend(all_widgets(child))
        return result

    try:
        settings = gui_channels.settings(app)
        settings.withdraw()
        assert any(widget.winfo_class() == "TNotebook" for widget in all_widgets(settings))
        window = gui_channels.import_leads(app)
        window.withdraw()
        widgets = all_widgets(window)
        text = next(widget for widget in widgets if isinstance(widget, tk.Text))
        text.insert("1.0", "河南清泉水处理设备有限公司 https://example.com/")
        preview_button = next(widget for widget in widgets if widget.winfo_class() == "TButton" and widget.cget("text") == "读取粘贴内容并预览")
        preview_button.invoke()
        assert not calls
        identify_button = next(widget for widget in widgets if widget.winfo_class() == "TButton" and widget.cget("text") == "进入网页识别厂家 →")
        assert str(identify_button.cget("state")) == "normal"
        identify_button.invoke()
        assert calls and calls[0]["seed_hits"] == []
        assert calls[0]["seed_leads"][0].name == "河南清泉水处理设备有限公司"
    finally:
        app.destroy()
