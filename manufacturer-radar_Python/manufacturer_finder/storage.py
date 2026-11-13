"""Atomic local search snapshots and portable, formula-safe spreadsheet exports."""
from __future__ import annotations

from contextlib import contextmanager
import csv
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import NamedTemporaryFile
from threading import RLock
import unicodedata

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .extraction import normalized_url
from .models import Company, RunResult
from .scoring import is_page_qualified


class Storage:
    """One transaction per run; file-backed calls use short independent connections.

    list_runs() includes the admitted count, old_count and legacy_unverified_count.
    load_run() returns None for an unknown ID. No network or cloud storage is used.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = RLock()
        self._memory = None
        if self.path == ":memory:":
            self._memory = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, query TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL, company_count INTEGER NOT NULL,
                cancelled INTEGER NOT NULL, timed_out INTEGER NOT NULL, payload TEXT NOT NULL
            )""")

    @contextmanager
    def _connection(self):
        with self._lock:
            connection = self._memory or sqlite3.connect(self.path, timeout=10)
            try:
                with connection:
                    yield connection
            finally:
                if connection is not self._memory:
                    connection.close()

    def save_run(self, result: RunResult) -> None:
        payload = json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False)
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO runs (id, query, started_at, finished_at, company_count, cancelled, timed_out, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
                query=excluded.query, started_at=excluded.started_at, finished_at=excluded.finished_at,
                company_count=excluded.company_count, cancelled=excluded.cancelled,
                timed_out=excluded.timed_out, payload=excluded.payload""",
                (result.id, result.query, result.started_at, result.finished_at, sum(is_page_qualified(company) for company in result.companies), int(result.cancelled), int(result.timed_out), payload),
            )

    def load_run(self, run_id: str) -> RunResult | None:
        with self._connection() as connection:
            row = connection.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        return RunResult.from_dict(json.loads(row[0])) if row else None

    def list_runs(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, query, started_at, finished_at, company_count, cancelled, timed_out, payload FROM runs ORDER BY started_at DESC, rowid DESC"
            ).fetchall()
        keys = ("id", "query", "started_at", "finished_at", "count", "cancelled", "timed_out")
        result = []
        for row in rows:
            summary = dict(zip(keys, row[:7]))
            # Older versions counted raw search hits as companies. Recompute
            # from the saved proof without rewriting historical payloads.
            payload = json.loads(row[7])
            saved_companies = payload.get("companies", [])
            summary["count"] = sum(is_page_qualified(Company.from_dict(company)) for company in saved_companies)
            summary["old_count"] = len(saved_companies)
            summary["legacy_unverified_count"] = len(saved_companies) - summary["count"]
            result.append(summary)
        for row in result:
            row["cancelled"] = bool(row["cancelled"])
            row["timed_out"] = bool(row["timed_out"])
        return result

    def close(self) -> None:
        if self._memory is not None:
            with self._lock:
                self._memory.close()
                self._memory = None


def spreadsheet_text(value) -> str:
    """Prefix formula-like text, including formulas hidden after whitespace/control."""
    text = "" if value is None else str(value)
    check = unicodedata.normalize("NFKC", text)
    while check and (check[0].isspace() or unicodedata.category(check[0]) in {"Cc", "Cf"}):
        check = check[1:]
    if check.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


_HEADERS = ["记录ID", "厂家名称", "详情来源验证", "厂家详情页", "主营", "经营范围", "地区", "地址", "收录依据", "原始链接", "名称状态", "电话", "收录链接", "搜索引擎", "广告标识", "信息完整度评分", "评分依据", "负面检查状态", "待核实负面线索", "来源详情JSON", "证据详情JSON", "备注", "更新时间"]


def _row(company: Company) -> list:
    negative = [item for item in company.evidence if item.kind in {"负面线索", "负面", "negative"}]
    return [
        company.id, company.name,
        "已进入详情页并识别厂家（未验证工商主体）" if is_page_qualified(company) else "尚未完成详情页厂家核验，不计入正式厂家",
        company.verification_url, "、".join(company.products), company.business_scope,
        company.region, company.address, "\n".join(company.admission_reasons),
        "\n".join(dict.fromkeys(hit.url for hit in company.hits)),
        "详情页文本提取全称，未验证工商主体" if is_page_qualified(company) else "尚未通过详情页确认",
        "\n".join(company.phones), "\n".join(company.urls), "、".join(dict.fromkeys(hit.engine for hit in company.hits)),
        "、".join(dict.fromkeys(hit.ad_status for hit in company.hits)), company.score,
        "\n".join(company.score_reasons), company.negative_status,
        "\n".join(f"{item.excerpt}\n{item.url}" for item in negative),
        json.dumps(company.to_dict()["hits"], ensure_ascii=False), json.dumps(company.to_dict()["evidence"], ensure_ascii=False),
        company.notes, company.updated_at,
    ]


@contextmanager
def _atomic_file(path: str | Path, suffix: str):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = NamedTemporaryFile(prefix=".export-", suffix=suffix, dir=destination.parent, delete=False)
    temporary.close()
    try:
        yield Path(temporary.name)
        os.replace(temporary.name, destination)
    finally:
        if os.path.exists(temporary.name):
            os.unlink(temporary.name)


def export_csv(companies: list[Company], path: str | Path) -> None:
    with _atomic_file(path, ".csv") as temporary:
        with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(_HEADERS)
            for company in companies:
                writer.writerow([value if isinstance(value, (int, float)) else spreadsheet_text(value) for value in _row(company)])


def _sheet_rows(sheet, headers: list[str], rows, overflow: list | None = None) -> None:
    sheet.append(headers)
    for row in rows:
        safe_row = []
        for column, value in enumerate(row, 1):
            if isinstance(value, (int, float, bool)):
                safe_row.append(value)
                continue
            value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", spreadsheet_text(value))
            if len(value) > 32767 and overflow is not None:
                from openpyxl.utils import get_column_letter
                reference = f"{get_column_letter(column)}{sheet.max_row + 1}"
                chunks = [value[offset:offset + 30000] for offset in range(0, len(value), 30000)]
                overflow.extend([sheet.title, reference, index, len(chunks), chunk] for index, chunk in enumerate(chunks, 1))
                value = value[:32000] + f"\n[全文见“长文本续页”，原单元格 {sheet.title}!{reference}]"
            safe_row.append(value)
        sheet.append(safe_row)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor="23445E")
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if isinstance(cell.value, str):
                cell.data_type = "s"
                if normalized_url(cell.value) and "\n" not in cell.value:
                    cell.hyperlink = cell.value
                    cell.style = "Hyperlink"
    for column in sheet.columns:
        letter = column[0].column_letter
        sheet.column_dimensions[letter].width = min(55, max(14, len(str(column[0].value)) * 2 + 2))


def export_xlsx(companies: list[Company], path: str | Path) -> None:
    workbook = Workbook()
    overflow = []
    summary = workbook.active
    summary.title = "厂家汇总"
    _sheet_rows(summary, _HEADERS, (_row(company) for company in companies), overflow)
    sources = workbook.create_sheet("搜索记录")
    _sheet_rows(sources, ["厂家记录ID", "厂家名称", "采集渠道", "关键词", "页码", "标题", "收录链接", "摘要", "搜索页链接", "广告标识", "采集时间", "发现来源", "名称补查实际查询", "原始渠道", "来源摘录"], (
        [company.id, company.name, hit.engine, hit.query, hit.page, hit.title, hit.url, hit.snippet, hit.search_url, hit.ad_status, hit.fetched_at, hit.source_url, hit.lookup_query, hit.discovery_source, hit.source_excerpt]
        for company in companies for hit in company.hits
    ), overflow)
    evidence = workbook.create_sheet("证据明细")
    _sheet_rows(evidence, ["厂家记录ID", "厂家名称", "证据类型", "来源链接", "原文摘录", "搜索引擎", "关键词", "页码", "状态", "采集时间"], (
        [company.id, company.name, item.kind, item.url, item.excerpt, item.engine, item.query, item.page, item.status, item.fetched_at]
        for company in companies for item in company.evidence
    ), overflow)
    verification = workbook.create_sheet("收录核验")
    _sheet_rows(verification, ["厂家记录ID", "厂家名称", "详情页已读取并识别厂家", "核验详情页", "主营产品", "经营范围", "地区", "地址", "收录依据", "原始搜索链接"], (
        [company.id, company.name, "是" if is_page_qualified(company) else "否", company.verification_url,
         "、".join(company.products), company.business_scope, company.region, company.address,
         "\n".join(company.admission_reasons), "\n".join(dict.fromkeys(hit.url for hit in company.hits))]
        for company in companies
    ), overflow)
    explanation = workbook.create_sheet("评分说明")
    _sheet_rows(explanation, ["项目", "分值或说明"], [
        ["正式收录前提", "已读取可回溯详情页，页面身份/厂家业务核验及名称证据齐全，并保留收录依据。仅搜索标题或摘要不计入正式厂家。"],
        ["进入详情页并识别厂家", "30，须有同一详情页的核验原文证据"],
        ["详情页厂家全称", "20；名称为页面文本提取，未验证工商主体"],
        ["主营产品或经营范围", "15，须有详情页原文证据"],
        ["地区或地址", "5，须有详情页原文证据"],
        ["公开电话", "10，须有厂家详情页或已核验同主体联系页的原文证据；电话归属仍待核实"],
        ["名称和电话来源证据", "各 5，共 10"], ["引擎、查询和页码来源", "5"],
        ["至少两个搜索引擎收录", "3，不代表独立事实验证"],
        ["已执行负面线索检查", "2；状态须为本轮未发现或发现待核实线索"],
        ["未完成详情页核验的候选线索", "不计入正式厂家，评分最多 25，仅用于候选审计，不得当作已找到的厂家"],
        ["待核实负面线索", "每个来源链接减 15，最多减 30；同来源重复结果只扣一次"],
        ["总分", "限制在 0 至 100。仅作信息完整度与线索筛选参考，不是信用、资质或事实认定。"],
        ["检查状态", "未检查 / 检查受限 / 本轮未发现 / 发现待核实线索；本轮未发现不等于企业没有负面。"],
        ["去重", "仅全称精确归一化相同或同一页面 URL；不按域名、电话、简称合并。"],
    ])
    if overflow:
        continuation = workbook.create_sheet("长文本续页")
        _sheet_rows(continuation, ["原工作表", "原单元格", "分段序号", "总段数", "文本分段"], overflow)
    with _atomic_file(path, ".xlsx") as temporary:
        workbook.save(temporary)
    workbook.close()


def export_json(result: RunResult, path: str | Path) -> None:
    with _atomic_file(path, ".json") as temporary:
        temporary.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
