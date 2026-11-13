from __future__ import annotations

import copy
import logging
import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

from .bootstrap import app_directory, data_directory
from .models import RunResult, SearchOptions, now
from .providers import ENGINES, build_search_url, parse_search_html
from .pipeline import SearchRunner, manufacturer_search_query
from .import_service import merge_imported_page, refresh_candidate_audit
from .storage import Storage, export_csv, export_xlsx, export_json
from .scoring import is_page_qualified, score_company
from .source_types import SOURCE_LABELS

ALL_SOURCES = {**ENGINES, **SOURCE_LABELS}

BG, WHITE, INK = "#F8FAF5", "#FFFFFF", "#173D34"
GREEN, MUTED, LINE, GOLD = "#2B604A", "#859481", "#DFE6D9", "#AF913D"
FONT = "Microsoft YaHei UI"


def admitted(company):
    return is_page_qualified(company)


class Application(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("厂商雷达 · 找到厂家与公开联系方式")
        self.geometry("1240x750")
        self.minsize(1040, 680)
        self.configure(bg=BG)
        self.store = Storage(data_directory() / "radar.sqlite3")
        self.result = RunResult()
        self.events = queue.Queue()
        self.runner = self.worker = None
        self.running = self.closing = False
        self.visible, self.logs, self.card_widgets = [], [], {}
        self._columns = 3
        self._resume_base = None
        self.query = tk.StringVar()
        self.filter_text = tk.StringVar()
        self.filter_mode = tk.StringVar(value="全部厂家")
        self.order = tk.StringVar(value="评分优先")
        self.engine_vars = {key: tk.BooleanVar(value=True) for key in ENGINES}
        self.budget = tk.StringVar(value="120")
        self.negative = tk.BooleanVar(value=True)
        self.enrich = tk.BooleanVar(value=True)
        self.browser_engine = tk.StringVar(value=list(ENGINES.values())[0])
        from .gui_channels import init_channels
        init_channels(self)
        self.browser_assistant = None
        self._ui_variables = []
        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll)
        self.bind("<Control-f>", lambda _: self.search_entry.focus_set())
        self.bind("<Control-e>", lambda _: self.export())
        self.bind("<MouseWheel>", self._wheel)
        self.filter_text.trace_add("write", lambda *_: self.refresh_cards())
        self.refresh_cards()

    def _style(self):
        self.option_add("*Font", (FONT, 10))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TButton", font=(FONT, 9), padding=(10, 6), background=BG, foreground=GREEN, borderwidth=0)
        style.map("TButton", background=[("active", "#EAF0E4")])
        style.configure("Primary.TButton", background=GREEN, foreground=WHITE, font=(FONT, 11, "bold"), padding=(22, 12))
        style.map("Primary.TButton", background=[("active", "#204C39"), ("disabled", "#BCCABD")])
        style.configure("TCombobox", padding=4, foreground=INK)
        style.configure("Treeview", font=(FONT, 9), rowheight=28, background=WHITE, fieldbackground=WHITE)
        style.configure("Treeview.Heading", font=(FONT, 9, "bold"), background="#EAF0E4")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 7), background="#EDF2E8", foreground=GREEN)

    def _build(self):
        shell = tk.Frame(self, bg=BG)
        shell.pack(fill="both", expand=True, padx=28, pady=(12, 8))
        toolbar = tk.Frame(shell, bg=BG)
        toolbar.pack(fill="x")
        tk.Label(toolbar, text="▣  厂商雷达", bg=BG, fg=GREEN, font=(FONT, 11, "bold")).pack(side="left")
        tk.Label(toolbar, text="v1.2", bg=BG, fg=MUTED, font=(FONT, 8)).pack(side="left", padx=10)
        self.toolbar_buttons = []
        for title, command in reversed([("采集渠道", self.settings), ("采集记录", self.audit), ("搜索历史", self.history), ("导出厂家", self.export)]):
            button = ttk.Button(toolbar, text=title, command=command)
            button.pack(side="right", padx=2)
            self.toolbar_buttons.append(button)
        tk.Label(shell, text="你想找什么厂家？", bg=BG, fg=INK, font=(FONT, 24, "bold")).pack(pady=(8, 4))
        tk.Label(shell, text="先读取厂家网页，再识别主体、主营与公开联系方式。", bg=BG, fg=MUTED, font=(FONT, 10)).pack()
        search = tk.Frame(shell, bg=WHITE, highlightbackground="#D6E1CC", highlightthickness=1)
        search.pack(fill="x", padx=130, pady=(18, 8))
        tk.Label(search, text="⌕", bg=WHITE, fg="#A0B291", font=("Segoe UI", 24)).pack(side="left", padx=(15, 6))
        self.search_entry = tk.Entry(search, textvariable=self.query, bg=WHITE, fg=INK, font=(FONT, 13), bd=0, insertbackground=GREEN)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=8, pady=15)
        self.search_entry.bind("<Return>", lambda _: self.start_search())
        self.start_button = ttk.Button(search, text="找厂家   →", style="Primary.TButton", command=self.start_search)
        self.start_button.pack(side="right", padx=7, pady=7)
        self.cancel_button = ttk.Button(search, text="停止", command=self.stop_search, state="disabled")
        self.cancel_button.pack(side="right")
        assist = tk.Frame(shell, bg=BG)
        assist.pack()
        tk.Label(assist, text="数据保存在本机   ·   每引擎最多前 3 页", bg=BG, fg="#A0AC9A", font=(FONT, 8)).pack(side="left", padx=8)
        ttk.Button(assist, text="导入搜索页", command=self.import_html).pack(side="left")
        ttk.Button(assist, text="导入名单/链接", command=self.import_leads).pack(side="left")
        ttk.Button(assist, text="浏览器采集", command=self.browser_collect).pack(side="left")
        ttk.Button(assist, text="查看演示", command=self.load_demo).pack(side="left")
        ttk.Button(assist, text="使用说明", command=self.help).pack(side="left")
        tk.Frame(shell, bg=LINE, height=1).pack(fill="x", pady=(12, 12))
        results_bar = tk.Frame(shell, bg=BG)
        results_bar.pack(fill="x")
        self.summary = tk.Label(results_bar, text="等待查找厂家", bg=BG, fg=INK, font=(FONT, 13, "bold"))
        self.summary.pack(side="left")
        self.resume_button = ttk.Button(results_bar, text="继续识别候选页", command=self.resume_candidates)
        self.resume_button.pack(side="right", padx=(6, 0))
        sortbox = ttk.Combobox(results_bar, textvariable=self.order, values=["评分优先", "有电话优先", "名称排序"], state="readonly", width=10)
        sortbox.pack(side="right", padx=4)
        sortbox.bind("<<ComboboxSelected>>", lambda _: self.refresh_cards())
        filterbox = ttk.Combobox(results_bar, textvariable=self.filter_mode, values=["全部厂家", "有公开电话", "有负面线索", "有广告标注"], state="readonly", width=12)
        filterbox.pack(side="right", padx=4)
        filterbox.bind("<<ComboboxSelected>>", lambda _: self.refresh_cards())
        ttk.Entry(results_bar, textvariable=self.filter_text, width=19).pack(side="right", padx=5)
        self.stats_label = tk.Label(shell, text="搜索链接只作为候选，读取并识别成功后才收录厂家。", fg=MUTED, bg=BG, anchor="w", font=(FONT, 8))
        self.stats_label.pack(fill="x", pady=(6, 2))
        self.coverage_label = tk.Label(shell, text="", fg=MUTED, bg=BG, anchor="w", font=(FONT, 8))
        self.coverage_label.pack(fill="x", pady=(0, 10))
        viewport = tk.Frame(shell, bg=BG)
        viewport.pack(fill="both", expand=True)
        self.card_canvas = tk.Canvas(viewport, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(viewport, orient="vertical", command=self.card_canvas.yview)
        scroll.pack(side="right", fill="y")
        self.card_canvas.configure(yscrollcommand=scroll.set)
        self.card_canvas.pack(side="left", fill="both", expand=True)
        self.cards_frame = tk.Frame(self.card_canvas, bg=BG)
        self.cards_window = self.card_canvas.create_window(0, 0, window=self.cards_frame, anchor="nw")
        self.cards_frame.bind("<Configure>", lambda _: self.card_canvas.configure(scrollregion=self.card_canvas.bbox("all")))
        self.card_canvas.bind("<Configure>", self._resize_cards)
        footer = tk.Frame(shell, bg=BG)
        footer.pack(fill="x", pady=(8, 0))
        self.status = tk.Label(footer, text="准备就绪", fg=MUTED, bg=BG, anchor="w", font=(FONT, 8))
        self.status.pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(footer, length=130, mode="determinate")
        self.progress.pack(side="right", padx=8)

    def _resize_cards(self, event):
        self.card_canvas.itemconfigure(self.cards_window, width=max(300, event.width - 4))
        columns = 3 if event.width >= 980 else 2
        if columns != self._columns:
            self._columns = columns
            self.refresh_cards()

    def _wheel(self, event):
        if self.card_canvas.winfo_exists():
            self.card_canvas.yview_scroll(-int(event.delta / 120), "units")

    def log(self, message):
        self.logs.append(time.strftime("%H:%M:%S ") + str(message))
        self.logs = self.logs[-2000:]
        logging.info(message)

    def set_running(self, value):
        self.running = value
        self.start_button.configure(state="disabled" if value else "normal")
        self.cancel_button.configure(state="normal" if value else "disabled")
        self.search_entry.configure(state="disabled" if value else "normal")
        self.resume_button.configure(state="disabled" if value else "normal")

    def start_search(self, seed_hits=None, seed_leads=None, captured_pages=None, source_status=None):
        if self.running:
            return
        query = self.query.get().strip()
        engines = [key for key, var in self.engine_vars.items() if var.get()]
        from .gui_channels import channel_options
        try:
            extra_options = channel_options(self)
        except ValueError as exc:
            messagebox.showerror('渠道设置有误', str(exc), parent=self)
            return
        if not query or (not engines and not extra_options['discovery_channels'] and seed_hits is None and not seed_leads):
            messagebox.showinfo("填写搜索条件", "请输入产品或行业关键词，并选择采集渠道或导入线索。", parent=self)
            return
        try:
            options = SearchOptions(query=query, engines=engines, budget_seconds=int(self.budget.get()), enrich=self.enrich.get(), check_negative=self.negative.get(), **extra_options)
        except ValueError as exc:
            messagebox.showerror("搜索条件有误", str(exc), parent=self)
            return
        preserve = seed_hits is not None and self.result.query == query
        self._resume_base = copy.deepcopy(self.result) if preserve else None
        if not preserve:
            self.result = RunResult(query=query)
        self.refresh_cards()
        self.status.configure(text="正在打开候选网页并识别厂家…" if seed_hits is not None else "正在检索候选网页；识别成功后显示厂家…")
        self.started_clock, self.current_budget = time.monotonic(), options.budget_seconds
        self.progress.configure(value=0)
        self.set_running(True)
        self.runner = SearchRunner(options, callback=self.events.put, seed_hits=seed_hits,
            seed_leads=seed_leads, captured_pages=captured_pages, source_status=source_status)
        self.log(f"{'继续识别' if seed_hits is not None else '开始查找'}：{query}；每引擎最多前 3 页。")
        def work():
            try:
                self.events.put({"type": "completed", "result": self.runner.run()})
            except Exception as exc:
                logging.exception("Manufacturer search failed")
                self.events.put({"type": "failed", "message": str(exc), "result": self.runner.snapshot()})
        self.worker = threading.Thread(target=work, daemon=True, name="factory-coordinator")
        self.worker.start()

    def stop_search(self):
        if self.runner and self.running:
            self.runner.cancel()
            self.status.configure(text="正在停止；保留已经读取并识别成功的厂家…")
            self.cancel_button.configure(state="disabled")

    def _combined_companies(self, current):
        if self._resume_base is None:
            return current
        from .extraction import merge_companies
        merged = merge_companies(copy.deepcopy(self._resume_base.companies) + current)
        for company in merged:
            score_company(company)
        return merged

    def _poll(self):
        changed = False
        for _ in range(150):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("type")
            if kind == "log":
                self.log(event.get("message", ""))
            elif kind == "phase":
                self.status.configure(text=event.get("message", "正在识别厂家…"))
                self.log(event.get("message", ""))
            elif kind == "engine":
                key = event.get("engine")
                self.result.engine_status[key] = {k: v for k, v in event.items() if k not in {"type", "engine"}}
                self._coverage()
            elif kind == 'source':
                key = event.get('source')
                self.result.source_status[key] = {k:v for k,v in event.items() if k not in {'type','source'}}
                self._coverage()
            elif kind == "candidates":
                if self._resume_base is None:
                    self.result.candidates = event.get("candidates", [])
            elif kind == "stats":
                self.result.qualification_stats = event.get("stats", {})
                self._stats()
            elif kind == "results":
                self.result.companies = self._combined_companies([c for c in event.get("companies", []) if admitted(c)])
                changed = True
            elif kind == "completed":
                self.finish(event["result"])
                changed = True
            elif kind == "failed":
                partial = event.get("result") or self.result
                partial.finished_at, partial.cancelled = now(), True
                self.finish(partial)
                self.log("运行失败：" + event.get("message", "未知错误"))
        if changed:
            self.refresh_cards()
        if self.running:
            self.progress.configure(value=min(99, (time.monotonic() - self.started_clock) / self.current_budget * 100))
        if self.closing and not self.running:
            self.destroy()
            return
        self.after(100, self._poll)

    def finish(self, result):
        if self._resume_base is not None:
            base = self._resume_base
            result.companies = self._combined_companies(result.companies)
            hit_keys = {(h.engine, h.query, h.page, h.url) for h in result.candidates}
            result.candidates += [h for h in base.candidates if (h.engine, h.query, h.page, h.url) not in hit_keys]
            new_urls = {v.get("url") for v in result.collection_log}
            result.collection_log += [v for v in base.collection_log if v.get("url") not in new_urls]
            # A seed run visits saved targets; it does not re-fetch search pages.
            result.engine_status = copy.deepcopy(base.engine_status)
            result.source_status = {**copy.deepcopy(base.source_status), **result.source_status}
            existing_seeds = {(v.get('name'), v.get('url'), v.get('source_url')) for v in result.discovery_seeds}
            result.discovery_seeds += [v for v in base.discovery_seeds if (v.get('name'), v.get('url'), v.get('source_url')) not in existing_seeds]
            result.id, result.started_at = base.id, base.started_at
            self._resume_base = None
        self.result = result
        self._remove_legacy_links()
        refresh_candidate_audit(self.result)
        self.set_running(False)
        self.progress.configure(value=100)
        self.refresh_cards()
        prefix = "已停止" if result.cancelled else ("本轮时间到" if result.timed_out else "本轮查找结束")
        self.status.configure(text=f"{prefix} · 已识别 {len(self.result.companies)} 家厂家 · 原始候选和未收录原因可在采集记录查看")
        try:
            self.store.save_run(self.result)
        except Exception as exc:
            self.log("历史保存失败：" + str(exc))

    def _remove_legacy_links(self):
        removed = [c for c in self.result.companies if not admitted(c)]
        if removed:
            keys = {(h.engine, h.page, h.url) for h in self.result.candidates}
            for company in removed:
                for hit in company.hits:
                    if (hit.engine, hit.page, hit.url) not in keys:
                        self.result.candidates.append(hit)
                        keys.add((hit.engine, hit.page, hit.url))
            self.result.companies = [c for c in self.result.companies if admitted(c)]
            refresh_candidate_audit(self.result)
            self.log(f"{len(removed)} 条旧版未读取网页的记录已移至候选，需识别后才展示为厂家。")

    def _stats(self):
        stats = self.result.qualification_stats
        if not stats:
            text = f"候选链接 {len({h.url for h in self.result.candidates})} 条；每家均需读取网页并识别主体与主营后收录。"
        else:
            text = ("本次续采：" if self._resume_base is not None else "") + f"候选 {stats.get('candidates', 0)} 条 · 已访问 {stats.get('visited', 0)} 页 · 不符合 {stats.get('rejected', 0)} 页 · 访问失败 {stats.get('failed', 0)} 页 · 未处理 {stats.get('unvisited', 0)} 页"
        self.stats_label.configure(text=text)

    def _coverage(self):
        labels = []
        for key, state in self.result.engine_status.items():
            if not isinstance(state, dict):
                continue
            limit = "（受限）" if state.get("status") in {"blocked", "error", "partial"} else ""
            if state.get("status") == "demo":
                continue
            labels.append(f"{ENGINES.get(key, key)} {state.get('pages', 0)}页{limit}")
        text = "已读取搜索页：" + "  /  ".join(labels) if labels else "识别依据、电话出处与评分理由均可在厂家资料中查看。"
        if self.result.source_status:
            read = sum(s.get('pages',0) for s in self.result.source_status.values())
            text += f"  ·  其他渠道 {len(self.result.source_status)} 项 / 已读 {read} 页（详见采集记录）"
        self.coverage_label.configure(text=text)

    def refresh_cards(self):
        companies = [c for c in self.result.companies if admitted(c)]
        term, mode = self.filter_text.get().strip().lower(), self.filter_mode.get()
        visible = []
        for company in companies:
            if term and term not in " ".join([company.name, company.business_scope, company.region, *company.products, *company.phones]).lower():
                continue
            if mode == "有公开电话" and not company.phones:
                continue
            if mode == "有负面线索" and not any(e.kind == "负面线索" for e in company.evidence):
                continue
            if mode == "有广告标注" and not any("广告" in h.ad_status or "推广" in h.ad_status for h in company.hits):
                continue
            visible.append(company)
        if self.order.get() == "名称排序":
            visible.sort(key=lambda c: c.name)
        elif self.order.get() == "有电话优先":
            visible.sort(key=lambda c: (not bool(c.phones), -c.score, c.name))
        else:
            visible.sort(key=lambda c: (-c.score, c.name))
        self.visible = visible
        self.summary.configure(text=f"为你找到 {len(companies)} 家厂家" + (f"  ·  当前显示 {len(visible)} 家" if len(visible) != len(companies) else ""))
        self._stats()
        self._coverage()
        for child in self.cards_frame.winfo_children():
            child.destroy()
        self.card_widgets = {}
        for index in range(3):
            self.cards_frame.grid_columnconfigure(index, weight=1 if index < self._columns else 0, uniform="cards")
        if not visible:
            text = "正在打开网页识别厂家，请稍候…" if self.running else ("本轮尚未识别到符合条件的厂家" if self.result.candidates else "输入产品或行业，开始查找厂家")
            empty = tk.Frame(self.cards_frame, bg=BG, height=220)
            empty.grid(row=0, column=0, columnspan=self._columns, sticky="ew")
            empty.pack_propagate(False)
            tk.Label(empty, text="▧", bg=BG, fg="#AEC0A1", font=(FONT, 30)).pack(pady=(30, 12))
            tk.Label(empty, text=text, bg=BG, fg=GREEN, font=(FONT, 12)).pack()
            tk.Label(empty, text="只有实际读取网页、识别厂家全称及相关主营后才会显示卡片。\n可在采集记录中查看访问失败、待识别和排除原因。", bg=BG, fg=MUTED, justify="center", font=(FONT, 9)).pack(pady=9)
            return
        for index, company in enumerate(visible):
            card = tk.Frame(self.cards_frame, bg=WHITE, highlightbackground=LINE, highlightthickness=1, height=310)
            card.grid(row=index // self._columns, column=index % self._columns, sticky="nsew", padx=(0, 14 if index % self._columns != self._columns - 1 else 0), pady=(0, 14))
            card.grid_propagate(False)
            self.card_widgets[company.id] = card
            self._card(card, company)

    def _card(self, card, company):
        body = tk.Frame(card, bg=WHITE)
        body.pack(fill="both", expand=True, padx=19, pady=17)
        row = tk.Frame(body, bg=WHITE)
        row.pack(fill="x")
        tk.Label(row, text="◇", fg="#8B9E62", bg="#F0F4E8", font=(FONT, 21), width=2).pack(side="left")
        names = list(dict.fromkeys(ALL_SOURCES.get(h.engine, h.engine) for h in company.hits))
        ad = "含广告标注" if any("广告" in h.ad_status or "推广" in h.ad_status for h in company.hits) else "广告未标识"
        source = (" / ".join(names) if len(names) <= 2 else f"{len(names)} 个引擎") + " · " + ad
        tk.Label(row, text=source, bg=WHITE, fg="#9DA993", font=(FONT, 7)).pack(side="right", padx=(5, 0))
        title = tk.Label(body, text=company.name, bg=WHITE, fg=INK, font=(FONT, 11, "bold"), anchor="nw", justify="left", wraplength=315, height=2)
        title.pack(fill="x", pady=(14, 3))
        body.bind("<Configure>", lambda e, w=title: w.configure(wraplength=max(180, e.width)))
        business = "、".join(company.products[:3]) or company.business_scope or "主营详见厂家页面"
        tk.Label(body, text="主营：" + business[:58], bg=WHITE, fg=MUTED, anchor="w", font=(FONT, 9)).pack(fill="x", pady=(0, 12))
        score_row = tk.Frame(body, bg=WHITE)
        score_row.pack(fill="x")
        filled = max(0, min(5, round(company.score / 20)))
        tk.Label(score_row, text="★" * filled + "☆" * (5 - filled), bg=WHITE, fg=GOLD, font=("Segoe UI", 14)).pack(side="left")
        tk.Label(score_row, text=str(company.score), bg=WHITE, fg=GREEN, font=(FONT, 13, "bold")).pack(side="left", padx=(10, 2))
        tk.Label(score_row, text="/ 100  参考评分", bg=WHITE, fg=MUTED, font=(FONT, 8)).pack(side="left")
        risk = any(e.kind == "负面线索" for e in company.evidence)
        tk.Button(body, text="负面信息：" + company.negative_status + "  ›", command=lambda: self.details(company, 2), bg=WHITE, fg="#A86A36" if risk else MUTED, activebackground=WHITE, relief="flat", bd=0, anchor="w", font=(FONT, 8), cursor="hand2").pack(fill="x", pady=(3, 7))
        phone = "、".join(company.phones[:2]) if company.phones else "未提取到公开电话"
        tk.Label(body, text="公开电话：" + phone, bg=WHITE, fg=GREEN if company.phones else MUTED, anchor="w", font=(FONT, 9)).pack(fill="x", pady=(2, 7))
        tk.Label(body, text="⌖ " + (company.region or "地区待确认") + "    ·    已读取厂家页", bg=WHITE, fg=MUTED, anchor="w", font=(FONT, 8)).pack(fill="x")
        bottom = tk.Frame(body, bg=WHITE)
        bottom.pack(side="bottom", fill="x")
        tk.Frame(bottom, bg="#EBEFE6", height=1).pack(fill="x", pady=(8, 10))
        actions = tk.Frame(bottom, bg=WHITE)
        actions.pack(fill="x")
        for label, action, side in [("查看厂家页面 ↗", lambda: self.open_url(company.verification_url), "left"), ("联系与资料", lambda: self.details(company), "right"), ("复制链接", lambda: self.copy(company.verification_url), "right")]:
            tk.Button(actions, text=label, command=action, bg=WHITE, fg=GREEN if side == "left" else MUTED, activebackground="#F4F7EF", relief="flat", bd=0, cursor="hand2", font=(FONT, 8)).pack(side=side, padx=(0, 4))

    def copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.configure(text="已复制到剪贴板")

    @staticmethod
    def open_url(url):
        if urlparse(url).scheme.lower() in {"http", "https"}:
            webbrowser.open(url)

    def details(self, company, tab=0):
        dialog = tk.Toplevel(self)
        dialog.title(company.name + " · 厂家资料")
        dialog.geometry("810x620")
        dialog.transient(self)
        dialog.configure(bg=BG)
        tk.Label(dialog, text=company.name, bg=BG, fg=INK, font=(FONT, 16, "bold"), wraplength=740, justify="left").pack(anchor="w", padx=22, pady=(20, 8))
        tk.Label(dialog, text="已读取网页并识别厂家主体与主营；工商资质、电话归属仍需核对。", bg=BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w", padx=22, pady=(0, 12))
        book = ttk.Notebook(dialog)
        book.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        basic = f"厂家名称：{company.name}\n主营：{'、'.join(company.products) or company.business_scope}\n地区：{company.region or '待确认'}\n地址：{company.address or '未提取'}\n公开电话：{' / '.join(company.phones) or '未提取'}\n\n收录依据：\n" + "\n".join(company.admission_reasons) + f"\n\n资料评分：{company.score}/100\n" + "\n".join(company.score_reasons) + f"\n\n备注：{company.notes or '暂无'}\n更新时间：{company.updated_at}"
        source = "厂家页面：\n" + company.verification_url + "\n\n"
        for hit in company.hits:
            source += f"{ALL_SOURCES.get(hit.engine, hit.engine)} · 第{hit.page}页 · {hit.ad_status}\n{hit.title}\n收录链接：{hit.url}\n搜索页：{hit.search_url}\n发现来源：{hit.source_url}\n实际查询：{hit.lookup_query or hit.query}\n采集时间：{hit.fetched_at}\n\n"
        for evidence in company.evidence:
            if evidence.kind != "负面线索":
                source += f"[{evidence.kind}] {evidence.excerpt}\n{evidence.url}\n{evidence.status}\n\n"
        negative = f"检查状态：{company.negative_status}\n负面内容是待核实线索，不是事实认定。本轮未发现不代表没有负面。\n\n"
        for evidence in company.evidence:
            if evidence.kind == "负面线索":
                negative += f"{evidence.excerpt}\n{evidence.url}\n查询：{evidence.query}\n{evidence.status} · {evidence.fetched_at}\n\n"
        for label, content in [("联系与主营", basic), ("收录依据与来源", source), ("负面线索", negative)]:
            frame = tk.Frame(book, bg=WHITE)
            book.add(frame, text=label)
            self._text(frame, content)
        book.select(tab)
        ttk.Button(dialog, text="复制联系资料", command=lambda: self.copy(company.name + "\n" + business_text(company) + "\n" + " / ".join(company.phones) + "\n" + company.verification_url)).pack(anchor="e", padx=20, pady=(0, 15))
        return dialog

    def _text(self, frame, content):
        text = tk.Text(frame, bg=WHITE, fg=INK, wrap="word", bd=0, padx=16, pady=13, font=(FONT, 10))
        scroll = ttk.Scrollbar(frame, command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.pack(fill="both", expand=True)
        text.insert("end", content)
        import re
        for index, match in enumerate(re.finditer(r"https?://[^\s]+", content)):
            tag = f"url{index}"
            text.tag_add(tag, f"1.0+{match.start()}c", f"1.0+{match.end()}c")
            text.tag_configure(tag, foreground=GREEN, underline=True)
            text.tag_bind(tag, "<Button-1>", lambda _, u=match.group(): self.open_url(u))
        text.configure(state="disabled")
        return text

    def settings(self):
        from .gui_channels import settings
        return settings(self)

    def import_leads(self):
        from .gui_channels import import_leads
        return import_leads(self)

    def browser_collect(self):
        from .gui_browser import browser_dialog
        return browser_dialog(self)

    def export(self):
        if not self.visible:
            messagebox.showinfo("暂无厂家", "识别到厂家后才可导出。候选链接可在采集记录查看。", parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, title="导出当前筛选的已识别厂家", defaultextension=".xlsx", initialfile="厂家资料_" + time.strftime("%Y%m%d_%H%M%S"), filetypes=[("Excel 工作簿", "*.xlsx"), ("CSV 表格", "*.csv"), ("JSON 含采集记录", "*.json")])
        if not path:
            return
        try:
            if Path(path).suffix.lower() == ".csv":
                export_csv(self.visible, path)
            elif Path(path).suffix.lower() == ".json":
                result = copy.deepcopy(self.result)
                result.companies = copy.deepcopy(self.visible)
                export_json(result, path)
            else:
                export_xlsx(self.visible, path)
            self.status.configure(text=f"已导出 {len(self.visible)} 家厂家：{Path(path).name}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)

    def load_demo(self):
        if self.running:
            return
        from .demo import make_demo_run
        self.result = make_demo_run()
        self.query.set(self.result.query)
        self.filter_text.set("")
        self.filter_mode.set("全部厂家")
        self.refresh_cards()
        self.status.configure(text="演示模式 · 以下厂家、电话及负面内容均为虚构")

    def history(self):
        if self.running:
            return
        dialog = tk.Toplevel(self)
        dialog.title("本地搜索历史")
        dialog.geometry("800x440")
        dialog.transient(self)
        table = ttk.Treeview(dialog, columns=("time", "query", "count", "state"), show="headings")
        for key, label, width in [("time", "时间", 195), ("query", "关键词", 280), ("count", "已识别厂家", 90), ("state", "状态", 160)]:
            table.heading(key, text=label)
            table.column(key, width=width)
        table.pack(fill="both", expand=True, padx=12, pady=12)
        for record in self.store.list_runs():
            state = "旧版记录，需重新识别" if record.get("legacy_unverified_count") else ("已停止" if record.get("cancelled") else "完成")
            table.insert("", "end", iid=record["id"], values=(record["started_at"], record["query"], record.get("count", 0), state))
        def load(_=None):
            selection = table.selection()
            if selection:
                result = self.store.load_run(selection[0])
                if result:
                    self.result = result
                    self._remove_legacy_links()
                    self.query.set(result.query)
                    self.filter_text.set("")
                    self.filter_mode.set("全部厂家")
                    self.refresh_cards()
                    self.status.configure(text="已载入历史；未识别的旧版链接可点击“继续识别候选页”。")
                dialog.destroy()
        table.bind("<Double-1>", load)
        ttk.Button(dialog, text="打开选中记录", command=load).pack(pady=(0, 12))

    def audit(self):
        dialog = tk.Toplevel(self)
        dialog.title("采集记录 · 候选链接不计作厂家")
        dialog.geometry("1000x590")
        dialog.transient(self)
        book = ttk.Notebook(dialog)
        book.pack(fill="both", expand=True, padx=12, pady=12)
        records = tk.Frame(book)
        book.add(records, text="网页识别记录")
        table = ttk.Treeview(records, columns=("status", "title", "reason", "url"), show="headings")
        for key, title, width in [("status", "处理状态", 95), ("title", "候选页标题", 230), ("reason", "识别或未收录原因", 330), ("url", "来源链接", 300)]:
            table.heading(key, text=title)
            table.column(key, width=width)
        horizontal = ttk.Scrollbar(records, orient="horizontal", command=table.xview)
        table.configure(xscrollcommand=horizontal.set)
        horizontal.pack(side="bottom", fill="x")
        table.pack(fill="both", expand=True)
        rows = self.result.collection_log or [{"title": h.title, "url": h.url, "status": "unvisited", "reason": "尚未打开网页识别"} for h in self.result.candidates]
        statuses = {"accepted": "已识别", "rejected": "未收录", "failed": "访问失败", "unvisited": "待识别", "legacy_unverified": "旧版待识别"}
        for row in rows:
            table.insert("", "end", values=(statuses.get(row.get("status"), row.get("status", "")), row.get("title", ""), row.get("reason", ""), row.get("final_url") or row.get("url", "")))
        table.bind("<Double-1>", lambda _: self.open_url(table.item(table.selection()[0], "values")[3]) if table.selection() else None)
        logs = tk.Frame(book)
        book.add(logs, text="运行日志")
        self._text(logs, "\n".join(self.logs) or "本轮暂无运行日志。")
        sources = tk.Frame(book)
        book.add(sources, text='渠道状态')
        statuses_text = []
        translations = {'ok':'已读取','complete':'已完成','empty':'无结果','blocked':'访问受限',
            'unparsed':'页面未能解析','configuration_required':'需要配置','auth_required':'需要授权',
            'quota_limited':'额度受限','error':'请求失败','timeout':'时间到','imported':'已导入',
            'partial':'部分完成','cancelled':'已停止','running':'采集中',
            'needs_browser':'需浏览器辅助','needs_user_action':'需手动处理','completed':'已读取','page_limit':'页数上限'}
        for key, state in {**self.result.engine_status, **self.result.source_status}.items():
            label=state.get('label',key) if key.startswith('catalog:') else ALL_SOURCES.get(key,key)
            statuses_text.append(f"{label}：{translations.get(state.get('status'),state.get('status',''))} · 已读 {state.get('pages',0)} 页 · 候选 {state.get('hits',0)} 条 · 名单 {state.get('seeds',0)} 条\n{state.get('message','')}")
        self._text(sources, '\n\n'.join(statuses_text) or '尚未执行采集。')
        leads = tk.Frame(book)
        book.add(leads, text='名单线索（未认定厂家）')
        self._text(leads, '\n\n'.join(f"{v.get('name') or '未提取名称'}\n{v.get('url','')}\n原始来源：{v.get('source_url','')}\n{v.get('excerpt','')}" for v in self.result.discovery_seeds) or '本轮暂无名单线索。')
        tk.Label(dialog, text="这里只是采集过程记录，未通过网页识别的链接不会显示在厂家卡片中。", fg=MUTED, font=(FONT, 9)).pack(pady=(0, 12))

    def resume_candidates(self):
        if self.running:
            return
        if not self.result.candidates:
            messagebox.showinfo("暂无候选", "先搜索，或导入浏览器保存的前 3 页搜索结果。", parent=self)
            return
        query = self.query.get().strip()
        if query != self.result.query:
            messagebox.showinfo("关键词已变化", "继续识别请保持本轮关键词；查找新的产品可点击“找厂家”。", parent=self)
            return
        rejected = {v.get("url") for v in self.result.collection_log if v.get("status") in {"accepted", "rejected"}}
        candidates = [h for h in self.result.candidates if h.url not in rejected]
        if not candidates:
            messagebox.showinfo("已处理完成", "本轮候选已识别完成，没有待处理网页。", parent=self)
            return
        self.start_search(seed_hits=candidates)

    def import_html(self):
        if self.running:
            return
        dialog = tk.Toplevel(self)
        dialog.title("导入浏览器保存的搜索页")
        dialog.geometry("610x340")
        dialog.transient(self)
        tk.Label(dialog, text="引擎需要验证时，可先在浏览器正常打开搜索页。", fg=INK, font=(FONT, 12, "bold")).pack(anchor="w", padx=22, pady=(22, 12))
        tk.Label(dialog, text="选择对应的引擎与前 1—3 页，用 Ctrl+S 将搜索页保存为 HTML 后导入。\n导入仅增加候选，随后会打开候选网页识别厂家；不会直接收录链接。", justify="left", fg=MUTED, font=(FONT, 9)).pack(anchor="w", padx=22)
        row = tk.Frame(dialog)
        row.pack(fill="x", padx=22, pady=22)
        ttk.Combobox(row, textvariable=self.browser_engine, values=list(ENGINES.values()), state="readonly", width=13).pack(side="left")
        page = tk.StringVar(value="1")
        ttk.Combobox(row, textvariable=page, values=["1", "2", "3"], state="readonly", width=4).pack(side="left", padx=8)
        def engine():
            return next(k for k, v in ENGINES.items() if v == self.browser_engine.get())
        def open_search():
            if self.query.get().strip():
                self.open_url(build_search_url(engine(), manufacturer_search_query(self.query.get().strip()), int(page.get())))
        ttk.Button(row, text="浏览器打开搜索页", command=open_search).pack(side="left", padx=8)
        def load():
            query = self.query.get().strip()
            if not query or self.result.query and self.result.query != query:
                messagebox.showinfo("检查关键词", "请先填写关键词，且与本轮记录保持一致。", parent=dialog)
                return
            filename = filedialog.askopenfilename(parent=dialog, filetypes=[("网页文件", "*.html *.htm")])
            if not filename:
                return
            try:
                from bs4 import UnicodeDammit
                if Path(filename).stat().st_size > 5_000_000:
                    raise ValueError("单页文件不能超过 5 MB")
                html = UnicodeDammit(Path(filename).read_bytes(), is_html=True).unicode_markup or ""
                executed_query = manufacturer_search_query(query)
                hits = parse_search_html(engine(), html, executed_query, int(page.get()), build_search_url(engine(), executed_query, int(page.get())))
                if not hits:
                    raise ValueError("未找到有效搜索结果链接，请确认保存的是已经加载的搜索结果页。")
                if not self.result.query:
                    self.result = RunResult(query=query)
                merge_imported_page(self.result, hits, engine(), int(page.get()))
                self.refresh_cards()
                dialog.destroy()
                self.resume_candidates()
            except Exception as exc:
                messagebox.showerror("导入失败", str(exc), parent=dialog)
        ttk.Button(dialog, text="导入 HTML 并识别厂家", style="Primary.TButton", command=load).pack(anchor="w", padx=22, pady=10)

    def help(self):
        dialog = tk.Toplevel(self)
        dialog.title("使用说明与收录规则")
        dialog.geometry("800x600")
        guide = app_directory() / "使用说明.txt"
        content = guide.read_text(encoding="utf-8-sig") if guide.exists() else "输入产品或行业，软件检索六个引擎前 3 页。\n\n候选链接必须实际读取网页，提取厂家名称和相关主营后才展示为厂家。\n未收录与未处理链接保留在采集记录中。\n\n点击厂家卡片的“联系与资料”查看收录依据、公开电话出处和评分。\n负面为待核实线索，评分不是信用评级。\n\n默认时限120秒，每轮负面补查最多12家。"
        self._text(dialog, content)

    def _close(self):
        if self.browser_assistant is not None:
            self.browser_assistant.close()
        if self.running:
            self.closing = True
            self.stop_search()
        else:
            self.store.close()
            self.destroy()


def business_text(company):
    return "主营：" + ("、".join(company.products) or company.business_scope)


def main():
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    Application().mainloop()
