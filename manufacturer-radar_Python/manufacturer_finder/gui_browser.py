"""Non-blocking Tk controls for the application-owned attended browser."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from .bootstrap import data_directory
from .browser_assist import BROWSER_SOURCES, BrowserAssistant, browser_search_url


def browser_dialog(app):
    existing = getattr(app, "_browser_dialog", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.lift()
                return existing
        except tk.TclError:
            pass
    dialog = tk.Toplevel(app)
    app._browser_dialog = dialog
    dialog.title("浏览器辅助采集 · 先查看再识别")
    dialog.geometry("820x600")
    dialog.minsize(700, 520)
    dialog.transient(app)
    shell = ttk.Frame(dialog, padding=18)
    shell.pack(fill="both", expand=True)
    ttk.Label(shell, text="在专用浏览器打开搜索或企业详情页", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
    ttk.Label(shell, text="正常登录或完成网站验证后，点击“读取并识别当前页”。仅采集当前标签页，厂家仍需经过名称、主营与生产依据核实。",
              wraplength=760, justify="left").pack(anchor="w", pady=(8, 12))

    labels = {label: source for source, label in BROWSER_SOURCES.items()}
    custom_label = "自定义公开网页 URL"
    source = tk.StringVar(dialog, value=next(iter(labels)))
    query = tk.StringVar(dialog, value=app.query.get())
    page = tk.StringVar(dialog, value="1")
    url = tk.StringVar(dialog)
    state = tk.StringVar(dialog, value="打开专用窗口后可正常操作网站；软件不会读取个人浏览器资料。")
    # Keep Tk variables alive with the application. A destroyed dialog's local
    # callback cycles could otherwise be garbage-collected by a later network
    # worker, and Tk Variable.__del__ must run on the UI thread.
    if not hasattr(app, "_ui_variables"):
        app._ui_variables = []
    app._ui_variables.extend((source, query, page, url, state))
    busy = {"value": False}
    actions = []

    form = ttk.Frame(shell)
    form.pack(fill="x")
    form.columnconfigure(1, weight=1)
    ttk.Label(form, text="来源").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
    picker = ttk.Combobox(form, textvariable=source, values=[*labels, custom_label], state="readonly", width=24)
    picker.grid(row=0, column=1, sticky="ew", pady=5)
    ttk.Label(form, text="页码").grid(row=0, column=2, padx=(18, 6))
    pages = ttk.Spinbox(form, from_=1, to=3, textvariable=page, width=4)
    pages.grid(row=0, column=3, sticky="e")
    ttk.Label(form, text="关键词").grid(row=1, column=0, sticky="w", pady=5)
    query_entry = ttk.Entry(form, textvariable=query)
    query_entry.grid(row=1, column=1, columnspan=3, sticky="ew", pady=5)
    ttk.Label(form, text="网页地址").grid(row=2, column=0, sticky="w", pady=5)
    address = ttk.Entry(form, textvariable=url)
    address.grid(row=2, column=1, columnspan=3, sticky="ew", pady=5)
    ttk.Label(shell, text="搜索来源最多前 3 页；抖音、快手为当前已加载页面，不自动滚动。企业全称缺失时请进入企业认证或公司资料页。",
              wraplength=760, foreground="#6C7D70").pack(anchor="w", pady=(8, 10))
    bar = ttk.Frame(shell)
    bar.pack(fill="x", pady=(0, 12))
    ttk.Label(shell, textvariable=state, wraplength=760, justify="left").pack(fill="x", anchor="w", pady=(0, 8))
    frame = ttk.Frame(shell)
    frame.pack(fill="both", expand=True)
    output = tk.Text(frame, height=12, wrap="word", font=("Microsoft YaHei UI", 9), state="disabled", relief="solid", borderwidth=1)
    scroll = ttk.Scrollbar(frame, orient="vertical", command=output.yview)
    output.configure(yscrollcommand=scroll.set)
    output.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    ttk.Label(shell, text="专用会话保存在软件 data/browser-profile，可在此窗口关闭浏览器。关闭本对话框会保留浏览器，退出软件时一并关闭。",
              wraplength=760, foreground="#6C7D70").pack(anchor="w", pady=(10, 0))

    def write(text):
        output.configure(state="normal")
        output.insert("end", text + "\n")
        output.see("end")
        output.configure(state="disabled")

    def assistant():
        current = getattr(app, "browser_assistant", None)
        if current is None or current.is_closed:
            current = BrowserAssistant(data_directory() / "browser-profile")
            app.browser_assistant = current
        return current

    def set_busy(value):
        busy["value"] = value
        for button in actions:
            button.configure(state="disabled" if value else "normal")

    def watch(future, callback):
        # A Future is polled only on Tk's thread. No result() call here can wait.
        if not dialog.winfo_exists():
            return
        if not future.done():
            dialog.after(100, lambda: watch(future, callback))
            return
        set_busy(False)
        try:
            callback(future.result())
        except Exception as exc:
            state.set("未完成：" + str(exc)[:450])
            write("未完成：" + str(exc)[:450])

    def proposed_url():
        if source.get() == custom_label:
            return url.get().strip()
        return browser_search_url(labels[source.get()], query.get(), int(page.get()))

    def update_url(_event=None):
        source_key = labels.get(source.get(), "browser")
        if source_key in {"douyin", "kuaishou"}:
            page.set("1")
            pages.configure(state="disabled")
        else:
            pages.configure(state="normal")
        if source.get() != custom_label:
            try:
                url.set(proposed_url())
            except (ValueError, KeyError) as exc:
                state.set(str(exc))

    def open_page():
        if busy["value"]:
            return
        try:
            target = proposed_url()
            if not target:
                raise ValueError("请填写公开网页地址")
            url.set(target)
            set_busy(True)
            state.set("正在打开专用浏览器窗口…")
            watch(assistant().open(target), lambda actual: (state.set("窗口已打开。请在网站正常操作，准备好后读取当前页。"), write("已打开：" + actual)))
        except Exception as exc:
            set_busy(False)
            state.set(str(exc))

    def collect_page():
        if busy["value"]:
            return
        if getattr(app, "running", False):
            messagebox.showinfo("正在识别厂家", "请等待当前任务完成后，再读取浏览器页面。", parent=dialog)
            return
        keyword = query.get().strip()
        if not keyword:
            state.set("请填写产品或行业关键词，用于核实企业主营是否匹配。")
            return
        set_busy(True)
        state.set("正在读取当前标签页…")

        def collected(result):
            batch = result.batch
            write("当前页：" + result.current_url)
            write(f"读取到 {len(batch.hits)} 个页面候选、{len(batch.seeds)} 条企业名称线索。")
            for status in batch.statuses.values():
                write(str(status.get("message", status.get("reason", status.get("status", "")))))
            for hit in batch.hits[:12]:
                write("• " + (hit.company_hint or hit.title) + "\n  " + hit.url)
            if not batch.hits and not batch.seeds:
                state.set("本页没有可识别的厂家候选。请按上方采集说明进入具体企业资料页后重试。")
                return
            app.query.set(keyword)
            app.start_search(seed_hits=batch.hits, seed_leads=batch.seeds,
                             captured_pages=result.pages, source_status=batch.statuses)
            state.set("已送入厂家识别。识别通过后显示在主界面的厂家卡片中。")

        watch(assistant().snapshot(keyword, labels.get(source.get(), "browser")), collected)

    def reset_job():
        if busy["value"]:
            return
        set_busy(True)
        watch(assistant().reset_limits(), lambda _: (state.set("已开始新一轮浏览器采集；每个来源仍仅采集前 3 页。"), write("已重置本轮浏览器采集计数。")))

    def close_browser():
        current = getattr(app, "browser_assistant", None)
        if current is None:
            state.set("专用浏览器尚未打开。")
            return
        set_busy(True)
        state.set("正在关闭专用浏览器…")
        watch(current.close(), lambda _: (state.set("专用浏览器已关闭。可重新打开；专用会话资料保存在本机。"), write("已关闭专用浏览器。")))

    for title, callback in (("打开搜索页", open_page), ("读取并识别当前页", collect_page),
                            ("新一轮采集", reset_job), ("关闭专用浏览器", close_browser)):
        button = ttk.Button(bar, text=title, command=callback)
        button.pack(side="left", padx=(0, 6))
        actions.append(button)
    picker.bind("<<ComboboxSelected>>", update_url)
    pages.bind("<FocusOut>", update_url)
    query_entry.bind("<FocusOut>", update_url)
    update_url()
    write("使用步骤：打开搜索页 → 正常登录/验证并进入目标页 → 读取并识别当前页。")
    write("浏览器辅助只提供线索，企业昵称、短视频或平台客服电话不会直接作为厂家信息入库。")
    return dialog
