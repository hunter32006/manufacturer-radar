"""Channel configuration and unverified-list import windows for the desktop UI."""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from .ingestion import MAX_SEEDS, read_leads, read_text_leads, write_import_template
from .network import safe_url
from .providers import ENGINES
from .source_types import SOURCE_LABELS


BG, INK, MUTED, GREEN = "#F8FAF5", "#173D34", "#71836E", "#2B604A"
B2B_SOURCES = ("madeinchina", "b2b168", "gys", "hc360", "aicaigou", "1688")
MAP_PROVIDERS = {"高德地图 API": "amap", "百度地图 API": "baidu_map"}


def _presets():
    from .discovery import CATALOG_PRESETS
    return list(CATALOG_PRESETS)


def init_channels(app):
    """All options remain in this application instance; API keys are not saved."""
    if not hasattr(app, "_ui_variables"):
        app._ui_variables = []
    app.channel_b2b = tk.BooleanVar(master=app, value=True)
    app.channel_catalog = tk.BooleanVar(master=app, value=True)
    app.channel_maps = tk.BooleanVar(master=app, value=False)
    app.b2b_source_vars = {source: tk.BooleanVar(master=app, value=True) for source in B2B_SOURCES}
    app.catalog_custom_urls = tk.StringVar(master=app, value="")
    app.catalog_preset_vars = {preset["url"]: tk.BooleanVar(master=app, value=True) for preset in _presets()}
    app.map_provider_var = tk.StringVar(master=app, value="高德地图 API")
    app.map_key_var = tk.StringVar(master=app, value="")
    app.map_city_var = tk.StringVar(master=app, value="")
    app.map_authorized_var = tk.BooleanVar(master=app, value=False)
    app._channels_window = None
    app._leads_window = None


def channel_options(app) -> dict:
    channels = []
    if app.channel_b2b.get():
        channels.append("b2b")
    if app.channel_catalog.get():
        channels.append("catalog")
    if app.channel_maps.get():
        channels.append("maps")
    custom_urls = [line.strip() for line in app.catalog_custom_urls.get().splitlines() if line.strip()]
    if len(custom_urls) > 10:
        raise ValueError("自定义名录网址最多 10 个，请分批采集")
    if any(not safe_url(url) for url in custom_urls):
        raise ValueError("名录网址须为完整的公开 HTTP/HTTPS 链接，不能填写本机或内网地址")
    urls = [url for url, variable in app.catalog_preset_vars.items() if variable.get()] + custom_urls
    if app.channel_catalog.get() and not urls:
        raise ValueError("请至少选择一个公开名录，或填写一个名录网址")
    b2b = [source for source, variable in app.b2b_source_vars.items() if variable.get()]
    if app.channel_b2b.get() and not b2b:
        raise ValueError("请至少选择一个 B2B 平台")
    return {"discovery_channels": channels, "b2b_sources": b2b, "catalog_urls": list(dict.fromkeys(urls))[:12],
            "map_provider": MAP_PROVIDERS.get(app.map_provider_var.get(), "amap"),
            "map_key": app.map_key_var.get().strip(), "map_city": app.map_city_var.get().strip(),
            "map_authorized": app.map_authorized_var.get()}


def _label(parent, text, *, bold=False, wraplength=680):
    label = tk.Label(parent, text=text, bg=BG, fg=INK if bold else MUTED,
                     font=("Microsoft YaHei UI", 10, "bold" if bold else "normal"),
                     anchor="w", justify="left", wraplength=wraplength)
    label.pack(fill="x", pady=(5, 8))
    return label


def _check(parent, text, variable):
    checkbox = tk.Checkbutton(parent, text=text, variable=variable, bg=BG, fg=GREEN,
                              activebackground=BG, anchor="w", justify="left")
    checkbox.pack(anchor="w", pady=4)
    return checkbox


def settings(app):
    if getattr(app, "_channels_window", None) is not None:
        try:
            app._channels_window.lift()
            return app._channels_window
        except tk.TclError:
            app._channels_window = None
    dialog = tk.Toplevel(app)
    app._channels_window = dialog
    dialog.title("采集渠道与搜索设置")
    dialog.geometry("790x660")
    dialog.minsize(720, 600)
    dialog.configure(bg=BG)
    dialog.transient(app)
    shell = tk.Frame(dialog, bg=BG)
    shell.pack(fill="both", expand=True, padx=20, pady=14)
    _label(shell, "每条线索都需进入企业页面识别，确认相关生产业务后才收录为厂家。", bold=True)
    notebook = ttk.Notebook(shell)
    notebook.pack(fill="both", expand=True)

    def tab(title):
        frame = tk.Frame(notebook, bg=BG, padx=18, pady=12)
        notebook.add(frame, text=title)
        return frame

    engines = tab("搜索引擎")
    _label(engines, "通用搜索引擎 · 每个引擎最多前 3 页", bold=True)
    for source, name in ENGINES.items():
        _check(engines, name, app.engine_vars[source])
    _label(engines, "遇到登录或验证码会在采集记录中显示受限。可通过主界面的“浏览器采集”使用自己的浏览器会话。")

    b2b = tab("B2B 平台")
    _check(b2b, "启用 B2B 厂家与供应商发现", app.channel_b2b)
    _label(b2b, "选择需要查找的平台", bold=True)
    for source in B2B_SOURCES:
        _check(b2b, SOURCE_LABELS[source], app.b2b_source_vars[source])
    _label(b2b, "平台公开页可直接读取；需要验证或登录时会记录受限，可使用浏览器辅助采集。贸易商和生产厂家仍需分别核实。")

    catalog = tab("公开名录")
    _check(catalog, "启用公开展商 / 政府工厂名单", app.channel_catalog)
    for preset in _presets():
        _check(catalog, preset["label"], app.catalog_preset_vars[preset["url"]])
    _label(catalog, "补充名录网页 / 文本 PDF 链接（每行一个，最多 10 个）", bold=True)
    text = tk.Text(catalog, height=7, wrap="word", relief="solid", bd=1,
                   highlightthickness=0, fg=INK, font=("Microsoft YaHei UI", 9))
    text.pack(fill="x", pady=4)
    text.insert("1.0", app.catalog_custom_urls.get())
    _label(catalog, "每个名录入口最多读取 3 个页面或附件。只按当前产品关键词筛选线索，扫描图片 PDF 需先 OCR；Excel / PDF 本地名单请用“导入名单/链接”。")

    maps = tab("地图企业")
    _check(maps, "启用地图官方 API 企业线索", app.channel_maps)
    _label(maps, "使用自己的平台账户，最多 3 页、每页 20 条。地图商户仍需核实生产身份。")
    form = tk.Frame(maps, bg=BG)
    form.pack(fill="x", pady=8)
    for row, title in enumerate(("地图平台", "Web 服务密钥", "城市 / 行政区代码")):
        tk.Label(form, text=title, bg=BG, fg=INK, anchor="w").grid(row=row, column=0, sticky="w", pady=8, padx=(0, 20))
    ttk.Combobox(form, textvariable=app.map_provider_var, values=list(MAP_PROVIDERS), state="readonly", width=34).grid(row=0, column=1, sticky="ew")
    key_entry = ttk.Entry(form, textvariable=app.map_key_var, show="•", width=38)
    key_entry.grid(row=1, column=1, sticky="ew")
    ttk.Entry(form, textvariable=app.map_city_var, width=38).grid(row=2, column=1, sticky="ew")
    form.columnconfigure(1, weight=1)
    _check(maps, "我已获得平台对本次采集、存储和导出用途的授权", app.map_authorized_var)
    _label(maps, "密钥只在本次软件运行期间保留，不写入历史或导出。拥有密钥不代表已获得上述用途授权；接口额度、费用和数据使用范围以平台账户授权为准。")
    links = tk.Frame(maps, bg=BG)
    links.pack(fill="x", pady=5)
    for title, url in (("高德接口与权限", "https://lbs.amap.com/api/webservice/guide/api/search/"),
                       ("百度接口与权限", "https://lbsyun.baidu.com/docs/webapi?title=placev3%2Fguide%2Fwebservice-placeapiV3%2FinterfaceDocumentV2")):
        ttk.Button(links, text=title, command=lambda value=url: webbrowser.open(value)).pack(side="left", padx=(0, 8))

    general = tab("采集时间")
    _label(general, "整轮搜索与网页核实的时间上限", bold=True)
    row = tk.Frame(general, bg=BG)
    row.pack(fill="x", pady=10)
    tk.Label(row, text="总时间（秒）", bg=BG, fg=INK).pack(side="left", padx=(0, 15))
    ttk.Combobox(row, textvariable=app.budget, values=["60", "120", "180", "300", "600"], state="readonly", width=12).pack(side="left")
    _check(general, "补读厂家联系页，寻找公开电话", app.enrich)
    _check(general, "补查同名负面线索（最多 12 家）", app.negative)
    _label(general, "渠道越多，固定时间内留给详情核实的时间越少。可增加时间，或只勾选本轮需要的平台。未访问完成的候选可继续识别。")
    _label(general, "负面内容保留来源和“待核实”状态；资料缺失会影响完整度评分。地图商户评分不会转成厂家评分。")

    def close():
        app.catalog_custom_urls.set(text.get("1.0", "end").strip())
        try:
            channel_options(app)
        except ValueError as exc:
            messagebox.showerror("请检查渠道设置", str(exc), parent=dialog)
            return
        app._channels_window = None
        dialog.destroy()

    footer = tk.Frame(shell, bg=BG)
    footer.pack(fill="x", pady=(12, 0))
    tk.Label(footer, text="设置只对本次运行生效", bg=BG, fg=MUTED).pack(side="left")
    ttk.Button(footer, text="完成", command=close).pack(side="right")
    dialog.protocol("WM_DELETE_WINDOW", close)
    return dialog


def import_leads(app):
    if app.running:
        messagebox.showinfo("正在采集中", "请等待当前采集完成，或先停止本轮采集。", parent=app)
        return
    query = app.query.get().strip()
    if not query:
        messagebox.showinfo("先填写产品关键词", "请先在主搜索框填写要找的产品，例如“水处理设备”，再导入名单。", parent=app)
        return
    if getattr(app, "_leads_window", None) is not None:
        try:
            app._leads_window.lift()
            return app._leads_window
        except tk.TclError:
            app._leads_window = None
    dialog = tk.Toplevel(app)
    app._leads_window = dialog
    dialog.title("导入名单与公开链接 · 先预览再识别")
    dialog.geometry("1000x700")
    dialog.minsize(800, 600)
    dialog.configure(bg=BG)
    dialog.transient(app)
    shell = tk.Frame(dialog, bg=BG)
    shell.pack(fill="both", expand=True, padx=20, pady=15)
    _label(shell, f"当前查找：{query}。导入的是待核实线索，软件会访问企业网页后再生成厂家卡片。", bold=True, wraplength=930)
    controls = tk.Frame(shell, bg=BG)
    controls.pack(fill="x", pady=5)
    leads = []
    count_text = tk.StringVar(master=dialog, value="还没有导入线索")
    # Keep Tk variables with the main-window lifetime. A later background search
    # must not collect a closed dialog's variable on a worker thread.
    app._ui_variables.append(count_text)
    preview = ttk.Treeview(shell, columns=("name", "url", "phone", "source"), show="headings", height=9)
    for key, title, width in (("name", "厂家全称", 240), ("url", "待访问网址", 275), ("phone", "名单公开电话", 130), ("source", "线索来源", 245)):
        preview.heading(key, text=title)
        preview.column(key, width=width, minwidth=90)
    scroll = ttk.Scrollbar(shell, orient="vertical", command=preview.yview)
    preview.configure(yscrollcommand=scroll.set)

    def show_leads(values):
        leads[:] = values[:MAX_SEEDS]
        preview.delete(*preview.get_children())
        for index, lead in enumerate(leads):
            preview.insert("", "end", iid=str(index), values=(lead.name or "待网页识别", lead.url or "将按完整名称找官网", "、".join(lead.phones), lead.source_url))
        count_text.set(f"已读取 {len(leads)} 条待核实线索 · 每次最多 100 条 / 1000 行 / 20 MB")
        identify.configure(state="normal" if leads else "disabled")
        if not leads:
            messagebox.showinfo("没有找到可导入线索", "请提供完整公司名称或公开网址。纯电话、内网网址和普通词语不会作为企业线索。", parent=dialog)

    def choose_file():
        path = filedialog.askopenfilename(parent=dialog, title="选择厂家名单", filetypes=[
            ("支持的名单", "*.csv *.xlsx *.txt *.pdf *.html *.htm *.json"),
            ("Excel 表格", "*.xlsx"), ("CSV 表格", "*.csv"), ("文本 PDF", "*.pdf"), ("所有文件", "*.*")])
        if path:
            try:
                show_leads(read_leads(path, query=query))
            except (ValueError, ImportError, OSError) as exc:
                messagebox.showerror("名单读取失败", str(exc), parent=dialog)

    def template():
        path = filedialog.asksaveasfilename(parent=dialog, title="保存名单模板", defaultextension=".xlsx", initialfile="厂家名单导入模板.xlsx", filetypes=[("Excel", "*.xlsx")])
        if path:
            try:
                write_import_template(path)
                count_text.set("模板已保存；删除示例行并填写真实厂家后再导入")
            except OSError as exc:
                messagebox.showerror("模板保存失败", str(exc), parent=dialog)

    ttk.Button(controls, text="选择名单文件…", command=choose_file).pack(side="left", padx=(0, 8))
    ttk.Button(controls, text="下载 Excel 模板", command=template).pack(side="left")
    _label(shell, "或在下面粘贴完整公司名称、公开链接，支持一行一家；表格可带公司名称、官网、电话、主营和地区等列。", wraplength=930)
    paste = tk.Text(shell, height=6, wrap="word", relief="solid", bd=1, highlightthickness=0, font=("Microsoft YaHei UI", 9))
    paste.pack(fill="x", pady=(0, 5))

    def parse_paste():
        try:
            show_leads(read_text_leads(paste.get("1.0", "end"), query=query, source_url="用户粘贴名单"))
        except (ValueError, ImportError) as exc:
            messagebox.showerror("文本读取失败", str(exc), parent=dialog)

    ttk.Button(shell, text="读取粘贴内容并预览", command=parse_paste).pack(anchor="w", pady=(0, 6))
    table = tk.Frame(shell, bg=BG)
    table.pack(fill="both", expand=True)
    # Keep all list controls within the table frame for correct resize behavior.
    preview.pack(in_=table, side="left", fill="both", expand=True)
    scroll.pack(in_=table, side="right", fill="y")
    tk.Label(shell, textvariable=count_text, bg=BG, fg=MUTED, anchor="w").pack(fill="x", pady=8)
    _label(shell, "名称线索会按完整名称找官网；有网址的线索直接读取。名单中的电话先保留为线索，不能代替网页核实。扫描图片 PDF 请先 OCR。", wraplength=930)
    footer = tk.Frame(shell, bg=BG)
    footer.pack(fill="x")

    def close():
        app._leads_window = None
        dialog.destroy()

    def begin():
        if not leads:
            return
        selected = list(leads)
        close()
        app.start_search(seed_hits=[], seed_leads=selected)

    def remove_selected():
        selected = {int(key) for key in preview.selection()}
        if selected:
            show_leads([lead for index, lead in enumerate(leads) if index not in selected])

    ttk.Button(footer, text="移除所选线索", command=remove_selected).pack(side="left")
    ttk.Button(footer, text="关闭", command=close).pack(side="right", padx=(8, 0))
    identify = ttk.Button(footer, text="进入网页识别厂家 →", command=begin, state="disabled")
    identify.pack(side="right")
    dialog.protocol("WM_DELETE_WINDOW", close)
    return dialog
