"""Read user-supplied/public lists as unverified leads, never company records.

No workbook formula, HTML script, PDF action, remote resource or embedded file
is executed. Contacts remain confined to the record that supplied the identity.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import re
from urllib.parse import urljoin
import zipfile

from bs4 import BeautifulSoup

from .extraction import _names, extract_phones, normalized_name, normalized_url
from .network import safe_url
from .source_types import LeadSeed


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 4_000_000
MAX_ROWS = 1000
MAX_SEEDS = 100
MAX_PDF_PAGES = 100
MAX_EXCERPT = 800
_URL = re.compile(r"https?://[^\s<>\"'，。；、）)\]}]+", re.I)
_BARE_URL = re.compile(r"(?:www\.)?[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?::\d{1,5})?(?:/[^\s<>\"']*)?", re.I)
_EN_NAME = re.compile(r"[A-Za-z][A-Za-z0-9&.,()' -]{3,100}(?:Ltd\.?|Limited|Inc\.?|Corporation|Company|Factory|Co\.)", re.I)
_FOOTER = re.compile(r"(?:主办单位|承办单位|主办方|展会咨询|展会热线|组委会|会务组|会务联系|网站客服|网站电话|客服电话|客服热线|技术支持|备案号|版权所有|Copyright)", re.I)
_FIELDS = {
    "name": {"厂家名称", "公司名称", "企业名称", "工厂名称", "厂家", "公司", "企业", "名称", "name", "company", "companyname", "factoryname"},
    "url": {"厂家网址", "公司网址", "企业网址", "官网", "官方网站", "网址", "网站", "链接", "website", "url", "homepage", "companyurl"},
    "phones": {"电话", "公开电话", "联系电话", "联系电话号码", "手机", "手机号", "联系电话/手机", "联系方式", "phones", "phone", "tel", "telephone", "mobile"},
    "products": {"主营", "主营产品", "主要产品", "经营范围", "产品", "products", "product", "businessscope"},
    "address": {"地区", "地址", "公司地址", "企业地址", "所在地区", "地区地址", "address", "region", "city"},
    "source_url": {"来源", "来源链接", "收录链接", "来源网址", "source", "sourceurl", "source_url"},
}


def _value(value) -> str:
    if value is None or isinstance(value, (dict, bool)):
        return ""
    if isinstance(value, (list, tuple)):
        return "；".join(_value(v) for v in value[:20])
    return str(value).strip()[:4000]


def _field(key) -> str:
    key = re.sub(r"[\s_（）():：]", "", str(key)).casefold()
    return next((field for field, variants in _FIELDS.items() if key in variants), "")


def _full_name(value: str) -> str:
    value = _value(value).strip(" \t\r\n,，;；|｜")
    value = re.sub(r"^(?:厂家名称|公司名称|企业名称|工厂名称|名称)\s*[:：]\s*", "", value)
    for name in _names(value, allow_generic_factory=True):
        if normalized_name(name) == normalized_name(value):
            return name
    if _EN_NAME.fullmatch(value):
        return value
    return ""


def _public_urls(value: str, base: str = "") -> list[str]:
    value = _value(value)
    options = _URL.findall(value)
    if not options and _BARE_URL.fullmatch(value):
        options = ["https://" + value]
    if not options and base and value.startswith(("/", "./", "../")):
        options = [urljoin(base, value)]
    return list(dict.fromkeys(url for url in options if safe_url(url)))[:5]


def _dedupe(seeds: list[LeadSeed]) -> list[LeadSeed]:
    found: dict[tuple, LeadSeed] = {}
    for seed in seeds:
        if not seed.name and not seed.url:
            continue
        key = (normalized_name(seed.name), normalized_url(seed.url))
        if key in found:
            existing = found[key]
            existing.phones = list(dict.fromkeys(existing.phones + seed.phones))[:10]
            existing.products = list(dict.fromkeys(existing.products + seed.products))[:10]
        else:
            found[key] = seed
        if len(found) >= MAX_SEEDS:
            break
    return list(found.values())


def _record(record: dict, source_url: str, excerpt: str = "") -> list[LeadSeed]:
    fields = {}
    for key, value in list(record.items())[:100]:
        field = _field(key)
        if field:
            fields[field] = (fields.get(field, "") + "；" + _value(value)).strip("；")
    name = _full_name(fields.get("name", ""))
    urls = _public_urls(fields.get("url", ""), source_url)
    if not name and not urls:
        return []
    supplied_source = _public_urls(fields.get("source_url", ""))
    proof = excerpt or "；".join(f"{k}：{_value(v)}" for k, v in list(record.items())[:20])
    return [LeadSeed(name=name, url=url, phones=extract_phones(fields.get("phones", "")),
                     address=fields.get("address", "")[:300], source="import",
                     source_url=(supplied_source[0] if supplied_source else source_url),
                     excerpt=proof[:MAX_EXCERPT],
                     products=[v.strip() for v in re.split(r"[；;、|\n]", fields.get("products", "")) if v.strip()][:10])
            for url in (urls or [""])]


def _line_leads(text: str, source_url: str, allow_contacts: bool = True) -> list[LeadSeed]:
    """Treat one short printed row/card as a record; do not share contacts."""
    text = text.strip()
    if not text or len(text) > 2500:
        return []
    names = list(dict.fromkeys(_names(text, allow_generic_factory=True)))
    if not names and _full_name(text):
        names = [_full_name(text)]
    urls = _public_urls(text, source_url)
    # An index with several identities cannot safely own a phone or website.
    if len(names) > 1:
        return [LeadSeed(name=name, source="import", source_url=source_url,
                         excerpt=name) for name in names[:MAX_SEEDS]]
    name = names[0] if names else ""
    if not name and not urls:
        return []
    phones = extract_phones(text) if name and allow_contacts and not _FOOTER.search(text) else []
    return [LeadSeed(name=name, url=url, phones=phones, source="import",
                     source_url=source_url, excerpt=text[:MAX_EXCERPT]) for url in (urls or [""])]


def _read_html(text: str, source_url: str) -> list[LeadSeed]:
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup.select("script,style,noscript,iframe,object,embed,footer,nav,header,[role='contentinfo']"):
        tag.decompose()
    for tag in soup.find_all(attrs={"class": re.compile(r"(?:^|[-_\s])footer(?:$|[-_\s])", re.I)}):
        tag.decompose()
    for tag in soup.find_all(id=re.compile(r"(?:^|[-_])footer(?:$|[-_])", re.I)):
        tag.decompose()
    seeds = []
    processed = set()
    # Explicit table headers preserve phones, addresses and product columns.
    for table in soup.find_all("table")[:30]:
        rows = table.find_all("tr")[:MAX_ROWS + 1]
        if not rows:
            continue
        headers = [cell.get_text(" ", strip=True) for cell in rows[0].find_all(["td", "th"], recursive=False)]
        mapped = any(_field(value) for value in headers)
        for row in rows[1:] if mapped else rows:
            cells = row.find_all(["td", "th"], recursive=False)
            values = [cell.get_text(" ", strip=True) for cell in cells]
            if mapped:
                record = dict(zip(headers, values))
                for header, cell in zip(headers, cells):
                    if _field(header) == "url":
                        anchor = cell.find("a", href=True)
                        if anchor:
                            record[header] = urljoin(source_url, anchor["href"]) if source_url else anchor["href"]
                seeds.extend(_record(record, source_url, row.get_text(" ", strip=True)))
            else:
                seeds.extend(_html_row(row, source_url))
            processed.add(id(row))
            if len(seeds) >= MAX_SEEDS:
                return _dedupe(seeds)
    # Smallest cards keep a platform footer or adjacent company's contacts out.
    candidates = soup.find_all(["li", "article", "p", "div", "section"])
    for row in reversed(candidates[:5000]):
        if row.find_parent("table") or any(id(parent) in processed for parent in [row, *row.parents]):
            continue
        # Never grow a previously isolated company's row into a larger page
        # container that also happens to contain a publisher's contact details.
        if any(id(child) in processed for child in row.descendants):
            continue
        text_value = row.get_text(" ", strip=True)
        if not text_value or len(text_value) > 1800 or _FOOTER.search(text_value):
            continue
        if len(_names(text_value, allow_generic_factory=True)) != 1:
            continue
        # Prefer a row including a name and contacts to a bare child heading.
        parent = row.parent
        record_classes = {"card", "item", "company-card", "company-item", "supplier-card", "supplier-item", "exhibitor-card", "exhibitor-item", "exhibitor", "supplier", "member-card"}
        explicit_parent = parent and (parent.name in {"li", "article"} or bool(record_classes.intersection(parent.get("class", []))))
        if explicit_parent:
            parent_text = parent.get_text(" ", strip=True)
            if len(parent_text) <= 1800 and len(_names(parent_text, allow_generic_factory=True)) == 1 and not _FOOTER.search(parent_text):
                row = parent
        additions = _html_row(row, source_url)
        if additions:
            seeds.extend(additions)
            processed.add(id(row))
        if len(seeds) >= MAX_SEEDS:
            break
    # Plain company-name anchors in a list remain useful even without cards.
    for anchor in soup.find_all("a", href=True)[:MAX_ROWS]:
        if any(id(parent) in processed for parent in anchor.parents):
            continue
        name = _full_name(anchor.get_text(" ", strip=True))
        if name:
            url = urljoin(source_url, anchor["href"]) if source_url else anchor["href"]
            seeds.append(LeadSeed(name=name, url=url if safe_url(url) else "", source_url=source_url,
                                  excerpt=anchor.get_text(" ", strip=True)))
    if not seeds:
        for line in soup.get_text("\n", strip=True).splitlines()[:MAX_ROWS]:
            seeds.extend(_line_leads(line, source_url, allow_contacts=False))
    return _dedupe(seeds)


def _html_row(row, source_url):
    text = row.get_text(" ", strip=True)
    additions = _line_leads(text, source_url)
    if len({seed.name for seed in additions if seed.name}) != 1:
        return additions
    urls = []
    for anchor in row.find_all("a", href=True)[:20]:
        href = urljoin(source_url, anchor["href"]) if source_url else anchor["href"]
        label = anchor.get_text(" ", strip=True)
        if safe_url(href) and (_full_name(label) or re.search("官网|官方网站|企业详情|公司详情|查看详情", label)):
            urls.append(href)
    if urls:
        template = additions[0]
        return [LeadSeed(name=template.name, url=url, phones=template.phones[:],
                         source_url=source_url, excerpt=template.excerpt) for url in dict.fromkeys(urls)]
    return additions


def _read_delimited(text: str, source_url: str, delimiter: str | None = None) -> list[LeadSeed]:
    try:
        dialect = csv.Sniffer().sniff(text[:16000], delimiters=",\t;，") if delimiter is None else None
    except csv.Error:
        return []
    reader = csv.reader(io.StringIO(text), dialect=dialect) if dialect else csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        headers = next(reader)
    except StopIteration:
        return []
    if not any(_field(header) for header in headers):
        return []
    seeds = []
    for index, row in enumerate(reader):
        if index >= MAX_ROWS or len(seeds) >= MAX_SEEDS:
            break
        seeds.extend(_record(dict(zip(headers, row)), source_url))
    return _dedupe(seeds)


def read_text_leads(text: str, query: str = "", source_url: str = "") -> list[LeadSeed]:
    """Parse explicit company names/public URLs from HTML or line-based text.

    ``query`` does not invent a product claim or filter user-supplied names.
    The pipeline must resolve each name, visit pages and qualify manufacturers.
    """
    if not isinstance(text, str):
        raise ValueError("导入内容必须是文本")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("文本过长，请拆分为每份不超过 400 万字符")
    if re.search(r"<(?:html|body|table|div|li|article|a|p)(?:\s|>)", text, re.I):
        return _read_html(text, source_url)
    delimited = _read_delimited(text, source_url)
    if delimited:
        return delimited
    seeds = []
    # Labelled multiline records are explicit ownership, unlike a nearby footer
    # phone in a general document. Unlabelled lines remain separate records.
    rows = text.splitlines()[:MAX_ROWS]
    current = {}

    def flush():
        if current:
            seeds.extend(_record(current, source_url))
            current.clear()

    for line in rows:
        labelled = re.fullmatch(r"\s*([^:：\t]{1,20})\s*[:：]\s*(.*?)\s*", line)
        field = _field(labelled.group(1)) if labelled else ""
        if field:
            if field == "name" and any(_field(key) == "name" for key in current):
                flush()
            current[labelled.group(1)] = labelled.group(2)
        else:
            flush()
            seeds.extend(_line_leads(line, source_url))
        if len(seeds) >= MAX_SEEDS:
            break
    flush()
    return _dedupe(seeds)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    raise ValueError("无法识别文件编码，请另存为 UTF-8 或 GB18030")


def parse_pdf_bytes(data: bytes, source_url: str = "", query: str = "") -> list[LeadSeed]:
    if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
        raise ValueError("PDF 必须为不超过 20 MB 的文件")
    if not data.lstrip().startswith(b"%PDF-"):
        raise ValueError("文件不是有效 PDF")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ImportError("缺少 PDF 文本解析组件 pypdf") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF 已加密，请先提供可读取的公开名单")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("PDF 超过 100 页，请拆分后导入")
        texts = []
        size = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            size += len(text)
            if size > MAX_TEXT_CHARS:
                raise ValueError("PDF 提取文本过长，请拆分后导入")
            texts.append(text)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF 无法解析，请提供正常的文本 PDF 或名单表格") from exc
    if not any(text.strip() for text in texts):
        raise ValueError("PDF 没有可提取文字，可能是扫描图片；请先 OCR 识别后导入")
    return read_text_leads("\n".join(texts), query=query, source_url=source_url)


def _read_xlsx(path: Path, source_url: str) -> list[LeadSeed]:
    from openpyxl import load_workbook
    try:
        with zipfile.ZipFile(path) as archive:
            if sum(item.file_size for item in archive.infolist()) > 100 * 1024 * 1024:
                raise ValueError("Excel 解压内容过大，请拆分文件")
        workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Excel 无法读取，请使用 .xlsx 格式") from exc
    seeds = []
    count = 0
    try:
        for sheet in workbook.worksheets[:20]:
            rows = sheet.iter_rows(max_row=MAX_ROWS + 1, max_col=40, values_only=True)
            headers = next(rows, ())
            mapped = any(_field(value) for value in headers)
            if not mapped:
                seeds.extend(_line_leads(" ".join(_value(v) for v in headers), source_url))
            for row in rows:
                count += 1
                if count > MAX_ROWS or len(seeds) >= MAX_SEEDS:
                    return _dedupe(seeds)
                if mapped:
                    seeds.extend(_record(dict(zip(headers, row)), source_url))
                else:
                    seeds.extend(_line_leads(" ".join(_value(v) for v in row), source_url))
    finally:
        workbook.close()
    return _dedupe(seeds)


def read_leads(path, query: str = "") -> list[LeadSeed]:
    """Import at most 100 unverified leads, reading at most 1000 data rows."""
    path = Path(path)
    if not path.is_file():
        raise ValueError("导入文件不存在")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("导入文件超过 20 MB，请拆分后导入")
    suffix = path.suffix.lower()
    source_url = str(path.resolve())
    if suffix == ".xlsx":
        return _read_xlsx(path, source_url)
    if suffix not in {".csv", ".txt", ".html", ".htm", ".pdf", ".json"}:
        raise ValueError("支持 CSV、XLSX、TXT、PDF、HTML 和 JSON 名单")
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("导入文件超过 20 MB，请拆分后导入")
    if suffix == ".pdf":
        return parse_pdf_bytes(data, source_url=source_url, query=query)
    text = _decode(data)
    if suffix == ".json":
        try:
            value = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise ValueError("JSON 文件格式错误") from exc
        if isinstance(value, dict):
            value = next((value[key] for key in ("companies", "leads", "data", "results", "企业", "厂家")
                          if isinstance(value.get(key), list)), [value])
        if not isinstance(value, list):
            raise ValueError("JSON 必须是企业对象或企业对象数组")
        seeds = []
        for row in value[:MAX_ROWS]:
            if isinstance(row, dict):
                seeds.extend(_record(row, source_url))
            elif isinstance(row, str):
                seeds.extend(_line_leads(row, source_url))
            if len(seeds) >= MAX_SEEDS:
                break
        return _dedupe(seeds)
    return read_text_leads(text, query=query, source_url=source_url)


def write_import_template(path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "厂家线索"
    sheet.append(["厂家名称", "厂家网址", "电话", "主营", "地区", "来源链接"])
    sheet.append(["河南示例机械有限公司", "https://www.example.com/", "", "水处理设备", "河南省", ""])
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2C624D")
    for column, width in {"A": 35, "B": 45, "C": 28, "D": 32, "E": 25, "F": 45}.items():
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"
    notes = workbook.create_sheet("填写说明")
    notes.append(["请删除示例行，填写真实公开的企业线索。"])
    notes.append(["名称或公开 HTTP/HTTPS 网址至少填写一项；纯电话号码不导入。"])
    notes.append(["名单每次最多读取 1000 行、导入 100 条线索、文件不超过 20 MB。"])
    notes.append(["所有线索都需进入真实企业页面核验后，才会成为厂家卡片。"])
    notes.append(["表格仅解析值，不运行公式；请把公式结果粘贴为值。"])
    notes.column_dimensions["A"].width = 100
    workbook.save(path)
