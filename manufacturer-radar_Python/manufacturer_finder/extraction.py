"""Conservative extraction and identity matching from public search results.

An extracted name is a text match, not an industrial registration verification.
We deliberately do not equate companies by a shared phone, domain or marketplace.
"""
from __future__ import annotations

from copy import deepcopy
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .models import Company, Evidence, SearchHit, now


_NAME_RE = re.compile(
    r"[\u3400-\u9fffA-Za-z0-9（）()·]{2,58}?"
    r"(?:股份有限公司|有限责任公司|有限公司|集团公司|总公司|公司|(?:工厂|厂)(?!有限公司|有限责任公司|股份有限公司|公司|家|商|房|区|长|址))"
)
_PREFIXES = (
    "欢迎访问", "欢迎来到", "欢迎光临", "欢迎咨询", "欢迎联系", "欢迎进入",
    "企业全称", "公司全称", "企业名称", "公司名称", "厂家名称", "生产厂家",
    "厂家直销", "官方网站", "官网", "供应商", "制造商", "经销商", "生产商",
    "主营厂家", "专业生产", "专业制造", "厂家", "首页", "关于我们", "关于",
    "联系", "联系我们", "走进", "网友投诉", "消费者投诉", "投诉对象",
    "被投诉人", "投诉", "举报对象", "举报", "被告", "原告", "被执行人",
    "据了解", "据悉", "涉及", "针对", "来自", "位于", "名称", "企业", "该企业",
)
_GENERIC_NAMES = {
    "本公司", "该公司", "贵公司", "我公司", "我们公司", "一家企业公司", "有限公司",
    "责任公司", "有限责任公司", "股份有限公司", "集团公司", "生产工厂", "本厂", "该厂",
    "生产厂家", "厂家", "工厂", "制造工厂", "你的公司", "您的公司", "其他公司",
}
_FACTORY_INDUSTRY = re.compile(
    r"(?:机械|机床|制品|加工|设备|生产|制造|五金|服装|纺织|电子|电器|食品|化工|建材|印刷|"
    r"配件|电机|铸造|铸件|门窗|包装|家具|水泥|陶瓷|电缆|塑料|模具|绣花|印染|砂轮|"
    r"石料|石材|木器|玩具|灯饰|板材|铝材|管件|锻造|冷冻|冷轧|轧钢|钣金|仪表|汽配|"
    r"发动机|变压器|发电|肥料|化肥|农药|日化|冶炼|化纤|橡胶|鞋业|服饰|材料|"
    r"造纸|造船|金属|铜材|零件|轴承|涂料|面粉|饲料|总|工|电|水|钢|酒|砖|纸|鞋)厂$"
)
_FACTORY_PROSE = re.compile(
    r"如果|假如|可以|应该|已经|需要|必须|所有|任何|还有|我们|这里|生产的|排行榜|排名|"
    r"十大|十佳|百强|最好的|哪个|哪些|怎么|如何|所谓|各类|各大|各个|一家|一家家|"
    r"我国|国内一线|全国各地|招聘进|负责|选择|了解|推荐|优质|知名|优选"
)
_FACTORY_ACTION = re.compile(r"(?:出厂|返厂|回厂|进厂|建厂|原厂|离厂|开厂|大厂|小厂|关厂|停厂|到厂|来厂|入厂|全厂|本厂|我厂|他厂|在厂)$")
_MOBILE_RE = re.compile(r"(?<!\d)(?:\+?86[ -]*)?(1[3-9]\d(?:[ -]?\d){8})(?!\d)")
_LANDLINE_RE = re.compile(
    r"(?<!\d)(?:\+?86[ -]*)?(?:\(0\d{2,3}\)[ -]?\d{7,8}|0\d{2,3}[ -]\d{7,8}|0\d{9,11})(?!\d)"
)
_HOTLINE_RE = re.compile(r"(?<!\d)((?:400|800)(?:[ -]?\d){7})(?!\d)")
_EXT_RE = re.compile(r"^\s*(?:转|分机|ext\.?|x)\s*[:：-]?\s*(\d{1,6})(?!\d)", re.I)
_ALLEGATIONS = re.compile(
    r"失信被执行人|拖欠工资|拖欠货款|质量不合格|虚假宣传|质量问题|行政处罚|"
    r"被处罚|被起诉|被执行|合同纠纷|质量纠纷|不发货|未发货|售后差|"
    r"诈骗|骗子|欺诈|违法|不合格|失信|黑名单|投诉|涉诉|诉讼|售假|假冒|"
    r"罚款|跑路|欠薪|欠款|维权"
)
_NEUTRAL_AFTER = re.compile(r"^(?:举报)?(?:电话|热线|渠道|入口|流程|方式|中心|平台|须知|指南|制度|处理机制|处理制度|处理流程|邮箱|管理制度|记录查询|信息查询|查询|检索)")
_NEGATING_BEFORE = re.compile(
    r"(?:没有发生|没有|暂无|未发现|未查到|未收到|未接到|未出现|未列入|未被|并未|尚未|不存在|从未|并非|并无|不是|不涉及|不涉嫌|未涉及|未发生|未见|不构成|不能认定|否认|非|无|零)"
    r"(?:任何|相关|此类|不良|该公司|公司|的|被|过|有|存在|记录|发现|信息|一宗|一起|一例|属于|涉嫌|受到|遭到|投诉|处罚|\s){0,9}$"
)
_NEGATING_AFTER = re.compile(
    r"^(?:记录|次数|案件|情况|信息|数量|量|指控|指称|传闻)?"
    r"(?:为零|为0|是零|是0|不属实|不成立|未获证实|已澄清|系谣言|系不实|不存在|不实|为谣言)"
)
_STATUS_RANK = {"未检查": 0, "本轮未发现": 1, "检查受限": 2, "发现待核实线索": 3}


def _text(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").replace("\u200b", "").replace("\ufeff", "")


def normalized_name(value: str) -> str:
    """Only width, case and whitespace normalization; no fuzzy abbreviations."""
    return re.sub(r"\s+", "", _text(value)).casefold()


def normalized_url(value: str) -> str:
    """Normalize exact URLs while preserving identity-bearing query and fragments."""
    try:
        parts = urlsplit((value or "").strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return ""
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        if port and not ((parts.scheme.lower() == "http" and port == 80) or (parts.scheme.lower() == "https" and port == 443)):
            host += f":{port}"
        # Analytics parameters are not company identifiers. All other parameters stay.
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(sorted(query)), parts.fragment))
    except (ValueError, UnicodeError):
        return ""


def _clean_name(value: str) -> str:
    value = value.strip()
    # Search titles and contact pages often concatenate a label and the name.
    changed = True
    while changed:
        changed = False
        for prefix in sorted(_PREFIXES, key=len, reverse=True):
            if value.startswith(prefix):
                value = value[len(prefix):]
                changed = True
                break
    return value.strip()


def _names(text: str, allow_generic_factory: bool = False) -> list[str]:
    names = []
    for match in _NAME_RE.finditer(_text(text)[:300_000]):
        name = _clean_name(match.group())
        if name in _GENERIC_NAMES or len(name) < 4 or len(re.findall(r"[\u3400-\u9fff]", name)) < 3:
            continue
        if name.endswith("厂"):
            if len(name) < 5 or _FACTORY_PROSE.search(name) or _FACTORY_ACTION.search(name):
                continue
            if not allow_generic_factory and not _FACTORY_INDUSTRY.search(name):
                continue
        # Do not present a prose fragment as a full company identity.
        if re.search(r"(?:的|是一家|这家|那家|多家|各家|所有|及其|或者|以及|不是|没有)", name[:-2]):
            continue
        if name not in names:
            names.append(name)
    return names


def _factory_identity_context(text: str, name: str) -> bool:
    """A body mention of XX厂 needs more than a prose fragment ending in 厂."""
    text = _text(text)
    for occurrence in re.finditer(re.escape(name), text):
        before = text[max(0, occurrence.start() - 24):occurrence.start()]
        after = text[occurrence.end():occurrence.end() + 100]
        if re.search(r"(?:企业名称|公司名称|工厂名称|厂家名称|厂名|生产厂家|版权所有|©)\s*[:：]?\s*$", before):
            return True
        if re.search(r"(?:联系电话|联系手机|电话|手机|热线|地址|版权所有|官网|官方网站|营业执照)", after):
            return True
    return False


def extract_phones(text: str) -> list[str]:
    """Extract Chinese public business contact formats without matching long IDs."""
    return [phone for phone, _, _ in _phone_matches(text)]


def _phone_matches(text: str) -> list[tuple[str, int, int]]:
    text = _text(text)
    found: list[tuple[str, int, int]] = []
    for pattern, kind in ((_MOBILE_RE, "mobile"), (_LANDLINE_RE, "landline"), (_HOTLINE_RE, "hotline")):
        for match in pattern.finditer(text):
            digits = re.sub(r"\D", "", match.group())
            if digits.startswith("86") and kind != "hotline":
                digits = digits[2:]
            if kind == "mobile":
                phone = digits
            elif kind == "hotline":
                phone = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
            else:
                # Some business pages prepend a domestic trunk 0 to a mobile
                # and even display it as 0130-02028857. 0130 is not a landline
                # area code; preserve the matched raw span but normalize once.
                if re.fullmatch(r"01[3-9]\d{9}", digits):
                    phone = digits[1:]
                    if not any(existing[0] == phone for existing in found):
                        found.append((phone, match.start(), match.end()))
                    continue
                area_length = 3 if digits[:3] in {"010", "020", "021", "022", "023", "024", "025", "027", "028", "029"} else 4
                if len(digits) - area_length not in {7, 8}:
                    continue
                if digits.startswith("000") or set(digits[area_length:]) == {"0"}:
                    continue
                phone = f"{digits[:area_length]}-{digits[area_length:]}"
                extension = _EXT_RE.match(text[match.end():match.end() + 20])
                if extension:
                    phone += f" 转{extension.group(1)}"
            if not any(existing[0] == phone for existing in found):
                found.append((phone, match.start(), match.end()))
    return sorted(found, key=lambda item: item[1])


def _append_evidence(company: Company, evidence: Evidence) -> None:
    key = (evidence.kind, evidence.url, evidence.excerpt, evidence.engine, evidence.query, evidence.page)
    if not any((v.kind, v.url, v.excerpt, v.engine, v.query, v.page) == key for v in company.evidence):
        company.evidence.append(evidence)


def _excerpt(text: str, needle: str, width: int = 260) -> str:
    text = re.sub(r"\s+", " ", _text(text)).strip()
    position = text.find(_text(needle))
    if position < 0:
        return text[:width]
    start = max(0, position - width // 3)
    return text[start:start + width]


def _unassigned_phone(company: Company, phone: str, excerpt: str, url: str, **provenance) -> None:
    _append_evidence(company, Evidence(
        kind="页面电话（未归属）", url=url, excerpt=excerpt,
        status=f"电话候选 {phone}；页面未能确认其所属厂家", **provenance,
    ))


def company_from_hit(hit: SearchHit) -> Company:
    title_names = _names(hit.title, allow_generic_factory=True)
    snippet_names = [name for name in _names(hit.snippet, allow_generic_factory=True)
                     if not name.endswith("厂") or name in title_names or _factory_identity_context(hit.snippet, name)]
    candidates = title_names if len(title_names) == 1 else (snippet_names if not title_names and len(snippet_names) == 1 else [])
    name = candidates[0] if candidates else (re.sub(r"\s+", " ", hit.title).strip() or "未提取到厂家名称")
    company = Company(name=name, name_confirmed=bool(candidates), urls=[hit.url] if hit.url else [], hits=[deepcopy(hit)])
    common = dict(url=hit.url, engine=hit.engine, query=hit.query, page=hit.page, fetched_at=hit.fetched_at)
    _append_evidence(company, Evidence(kind="收录", excerpt=_excerpt(f"{hit.title}\n{hit.snippet}", name), status="搜索结果", **common))
    all_names = set(title_names + snippet_names)
    if company.name_confirmed:
        _append_evidence(company, Evidence(kind="名称", excerpt=_excerpt(f"{hit.title}\n{hit.snippet}", name), status="文本提取，未验证工商主体", **common))
        # A result mentioning several suppliers cannot safely assign every phone.
        if len(all_names) == 1:
            for phone in extract_phones(hit.snippet):
                company.phones.append(phone)
                _append_evidence(company, Evidence(kind="电话", excerpt=_excerpt(hit.snippet, phone), status="搜索摘要提取，待核实归属", **common))
    for phone in extract_phones(hit.snippet):
        if phone in company.phones:
            continue
        _unassigned_phone(company, phone, _excerpt(hit.snippet, phone), **common)
        if not company.name_confirmed and not all_names and re.search(r"(?:联系电话|联系手机|电话|手机|热线|tel)\s*[:：]", _text(hit.snippet), re.I):
            company.phones.append(phone)
            company.notes = "页面公开电话候选，尚未确认厂家全称及电话归属。"
    return company


def enrich_company(company: Company, html: str, url: str) -> None:
    """Add attributable names and phones from a fetched page, with source excerpts."""
    soup = BeautifulSoup(html[:2_000_000], "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    heading_text = "\n".join(tag.get_text(" ", strip=True) for tag in soup.select("title, h1"))
    for tag in soup.select('meta[property="og:site_name"], meta[property="og:title"]'):
        heading_text += "\n" + tag.get("content", "")
    body_text = _text(soup.get_text("\n", strip=True))
    heading_names = _names(heading_text, allow_generic_factory=True)
    if not company.name_confirmed:
        candidates = heading_names
        if not candidates:
            candidates = [name for name in _names(body_text, allow_generic_factory=True)
                          if not name.endswith("厂") or _factory_identity_context(body_text, name)]
        if len(candidates) == 1:
            company.name = candidates[0]
            company.name_confirmed = True
    identity = normalized_name(company.name)
    if url and url not in company.urls:
        company.urls.append(url)
    if not company.name_confirmed:
        for phone, start, end in _phone_matches(body_text):
            context = body_text[max(0, start - 120):min(len(body_text), end + 100)]
            _unassigned_phone(company, phone, context, url)
            if not _names(body_text) and re.search(r"(?:联系电话|联系手机|电话|手机|热线|tel)\s*[:：]", context, re.I) and phone not in company.phones:
                company.phones.append(phone)
                company.notes = "\n".join(filter(None, [company.notes, "页面公开电话候选，尚未确认厂家全称及电话归属。"]))
        company.updated_at = now()
        return
    # An unrelated page must not supply this company's phone numbers.
    if not _has_exact_identity(body_text + "\n" + heading_text, company.name):
        for phone, start, end in _phone_matches(body_text):
            _unassigned_phone(company, phone, body_text[max(0, start - 120):min(len(body_text), end + 100)], url)
        company.updated_at = now()
        return
    _append_evidence(company, Evidence(kind="名称", url=url, excerpt=_excerpt(body_text, company.name), status="页面文本提取，未验证工商主体"))
    heading_identity = any(normalized_name(name) == identity for name in heading_names)
    for phone, start, end in _phone_matches(body_text):
        context = body_text[max(0, start - 160): min(len(body_text), end + 100)]
        context_names = {normalized_name(name) for name in _names(context)}
        contains_identity = _has_exact_identity(context, company.name)
        if context_names - {identity}:
            _unassigned_phone(company, phone, re.sub(r"\s+", " ", context).strip(), url)
            continue
        if not contains_identity and not heading_identity:
            _unassigned_phone(company, phone, re.sub(r"\s+", " ", context).strip(), url)
            continue
        if phone not in company.phones:
            company.phones.append(phone)
        _append_evidence(company, Evidence(kind="电话", url=url, excerpt=re.sub(r"\s+", " ", context).strip(), status="页面公开联系方式，待核实归属"))
    company.updated_at = now()


def _has_exact_identity(text: str, company_name: str) -> bool:
    identity = normalized_name(company_name)
    return any(normalized_name(candidate) == identity for candidate in _names(text, allow_generic_factory=True))


def negative_evidence(company: Company, hit: SearchHit) -> Evidence | None:
    """Return an allegation lead only; never a finding that the allegation is true."""
    if not company.name_confirmed:
        return None
    text = _text(f"{hit.title}\n{hit.snippet}")
    # Identity and allegation must occur in one bounded sentence; the query is not evidence.
    for sentence in re.split(r"[。！？!?；;\n]+", text):
        sentence = sentence.strip()
        if not sentence or not _has_exact_identity(sentence, company.name):
            continue
        identity = normalized_name(company.name)
        other_names = {normalized_name(name) for name in _names(sentence, allow_generic_factory=True)} - {identity}
        if other_names:
            # Do not assign one supplier's allegations to another in a comparison/list.
            continue
        comparable = re.sub(r"\s+", "", sentence)
        for match in _ALLEGATIONS.finditer(comparable):
            before = comparable[max(0, match.start() - 45):match.start()]
            before = before.replace(_text(company.name).replace(" ", ""), "该公司")
            after = comparable[match.end():match.end() + 20]
            if _NEGATING_BEFORE.search(before) or _NEGATING_AFTER.match(after) or _NEUTRAL_AFTER.match(after):
                continue
            if re.search(r"(?:是否|有没有|有无|如何查询|怎么查询)(?:该公司|公司|的|有|存在|涉嫌|涉及|被|过|相关|\s){0,8}$", before) or re.match(r"^(?:吗|么|与否)", after):
                continue
            if match.group() == "诈骗" and before.endswith(("反", "防", "预防", "打击", "防范", "谨防")):
                continue
            if match.group() == "投诉" and before.endswith(("受理", "处理", "欢迎", "接受", "咨询", "如何", "可以", "支持", "提供")):
                continue
            if abs(comparable.find(_text(company.name).replace(" ", "")) - match.start()) > 180:
                continue
            return Evidence(
                kind="负面线索", url=hit.url, excerpt=_excerpt(sentence, company.name, 600), engine=hit.engine,
                query=hit.query, page=hit.page, status="待核实：搜索文本含指称，不代表事实结论", fetched_at=hit.fetched_at,
            )
    return None


def _merge_into(target: Company, source: Company) -> None:
    if source.name_confirmed and not target.name_confirmed:
        target.name = source.name
        target.name_confirmed = True
    for attribute in ("phones", "urls"):
        existing = getattr(target, attribute)
        for value in getattr(source, attribute):
            if value not in existing:
                existing.append(value)
    hit_keys = {(h.engine, h.query, h.page, h.url, h.title, h.snippet) for h in target.hits}
    for hit in source.hits:
        key = (hit.engine, hit.query, hit.page, hit.url, hit.title, hit.snippet)
        if key not in hit_keys:
            target.hits.append(deepcopy(hit))
            hit_keys.add(key)
    for evidence in source.evidence:
        _append_evidence(target, deepcopy(evidence))
    # Admission is a property of an actually read manufacturer page. Keep its
    # proof and structured details when the same factory appears on many engines.
    if source.page_verified:
        target.page_verified = True
        if not target.verification_url:
            target.verification_url = source.verification_url
    for attribute in ("products", "admission_reasons"):
        values = getattr(target, attribute)
        for value in getattr(source, attribute):
            if value and value not in values:
                values.append(value)
    if source.business_scope and source.business_scope not in target.business_scope:
        target.business_scope = "；".join(filter(None, [target.business_scope, source.business_scope]))
    for attribute, label in (("region", "地区"), ("address", "地址")):
        value = getattr(source, attribute)
        if value and not getattr(target, attribute):
            setattr(target, attribute, value)
        elif value and value != getattr(target, attribute):
            note = f"另页公开{label}：{value}（来源：{source.verification_url or '见证据'}）"
            if note not in target.notes:
                target.notes = "\n".join(filter(None, [target.notes, note]))
    if _STATUS_RANK.get(source.negative_status, 0) > _STATUS_RANK.get(target.negative_status, 0):
        target.negative_status = source.negative_status
    if source.notes and source.notes not in target.notes:
        target.notes = "\n".join(value for value in (target.notes, source.notes) if value)
    target.updated_at = max(target.updated_at, source.updated_at)


def merge_companies(companies: list[Company]) -> list[Company]:
    """Merge exact full identities or exact source URLs, preserving conflicting firms."""
    merged: list[Company] = []
    for original in companies:
        candidate = deepcopy(original)
        match = None
        for target in merged:
            if _same_company(candidate, target):
                match = target
                break
        if match is None:
            merged.append(candidate)
        else:
            _merge_into(match, candidate)
            # A new exact identity can also connect previously unconfirmed URLs.
            for other in merged[:]:
                if other is not match and _same_company(match, other):
                    _merge_into(match, other)
                    merged.remove(other)
    return merged


def _same_company(left: Company, right: Company) -> bool:
    left_name = normalized_name(left.name) if left.name_confirmed else ""
    right_name = normalized_name(right.name) if right.name_confirmed else ""
    if left_name and left_name == right_name:
        return True
    if left_name and right_name and left_name != right_name:
        return False
    return bool(({normalized_url(url) for url in left.urls} - {""}) & ({normalized_url(url) for url in right.urls} - {""}))
