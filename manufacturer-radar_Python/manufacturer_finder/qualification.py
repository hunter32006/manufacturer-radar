"""Admission rules for actually fetched, product-relevant manufacturer pages.

Search snippets are discovery/audit material only. They never supply a company
identity, business scope or phone to an admitted record in this module. A passed
page is an identified public page, not a verified business/credit endorsement.
This module performs no network requests.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .extraction import _names as _raw_names, _text, extract_phones, normalized_name, normalized_url
from .models import Company, Evidence, SearchHit


@dataclass
class QualificationResult:
    company: Company | None
    reason: str
    contact_urls: list[str] = field(default_factory=list)


@dataclass
class _Owner:
    name: str
    score: int = 0
    signals: list[str] = field(default_factory=list)
    excerpts: list[str] = field(default_factory=list)
    data: list[dict] = field(default_factory=list)


_OWNER_LABEL = re.compile(
    r"(?:公司名称|企业名称|厂家名称|工厂名称|厂名|供应商|生产厂家|制造商|供货商|企业主体|商家名称|店铺主体|经营主体)\s*(?:[:：]|\s)\s*(.{4,100})"
)
_BUSINESS_LABEL = re.compile(
    r"(?:主营产品|主营业务|主营行业|经营范围|经营产品|主要产品|产品范围|生产产品|供应产品|产品名称)\s*[:：]?\s*(.{2,650})"
)
_MANUFACTURING = re.compile(r"主营|经营范围|生产|制造|研发|销售|供应|供货|批发|经销|直销|加工|产品中心|产品展示|产品名称")
_SELF_DESCRIPTION = re.compile(r"成立于|始建于|是一家|是专业|专业从事|主要从事|致力于|专注于|主营|专业生产|专业制造|研发|生产|制造|供应|销售")
_PLATFORM_CONTACT = re.compile(r"平台(?:客服|服务|热线|电话|咨询|运营)|网站(?:客服|服务|运营)|技术支持|建站服务|备案咨询|举报|投诉|网站运营|平台运营|运营主体")
_CLIENT_OR_PROMOTION = re.compile(r"网站建设|网站制作|网站设计|官网制作|建站|SEO|搜索引擎优化|广告推广|网络推广|客户案例|成功案例|合作客户|服务客户|客户行业", re.I)
_LIST_TITLE = re.compile(r"厂家排行榜|厂家排名|公司排名|企业排名|供应商列表|企业列表|公司列表|厂家列表|企业名录|公司名录|公司黄页|企业黄页|厂家大全|供应商大全|十大.{0,12}(?:厂家|公司|品牌)|品牌排行榜")
_REMOVE_MAIN = (
    "head,script,style,noscript,svg,iframe,nav,aside,footer,header,form,"
    "[hidden],[aria-hidden='true'],.footer,#footer,.copyright,.friendlink,.friendlinks,"
    ".friend-link,.links,.related,.recommend,.recommendations,.tuijian,.sidebar,"
    ".news-list,.news_list,.news-recommend,.site-info,.platform-footer"
)
_REGIONS = (
    "内蒙古自治区", "广西壮族自治区", "宁夏回族自治区", "新疆维吾尔自治区", "西藏自治区",
    "黑龙江", "内蒙古", "山东", "河北", "河南", "山西", "陕西", "江苏", "浙江", "安徽", "福建",
    "广东", "广西", "湖北", "湖南", "江西", "辽宁", "吉林", "四川", "重庆", "贵州", "云南",
    "甘肃", "青海", "海南", "宁夏", "新疆", "西藏", "北京", "上海", "天津", "台湾", "香港", "澳门",
    "石家庄", "青岛", "济南", "潍坊", "青州", "临沂", "烟台", "淄博", "济宁", "诸城", "泰安", "德州",
    "郑州", "新乡", "洛阳", "许昌", "漯河", "焦作", "南阳", "安阳", "邢台", "保定", "沧州", "廊坊", "衡水",
    "东莞", "广州", "深圳", "佛山", "中山", "惠州", "汕头", "江门", "珠海", "武汉", "长沙", "合肥",
    "苏州", "无锡", "常州", "南京", "南通", "徐州", "扬州", "盐城", "镇江", "泰州", "连云港",
    "杭州", "宁波", "温州", "台州", "绍兴", "金华", "义乌", "丽水", "湖州", "嘉兴", "永康",
    "成都", "绵阳", "西安", "宝鸡", "沈阳", "大连", "长春", "哈尔滨", "厦门", "福州", "泉州", "南昌",
)
_QUERY_INTENT = re.compile(r"生产厂家|生产厂商|生产商|制造厂家|制造商|供应厂家|供应商|厂家直销|厂家|厂商|工厂|批发商|经销商|联系电话|联系方式|电话|报价|价格|哪家好|哪里有|哪里|推荐|排行|排名|附近|官网|官方网站|采购|购买|寻找|搜索|我要找|我要|公司")
_GENERIC_SECTOR_NAMES = {"自来水公司", "污水处理厂", "水处理厂", "自来水厂", "客户公司", "环保公司", "生产工厂", "某某公司", "其他公司"}
_PRODUCT_NOUN = re.compile(r"设备|机械|生产线|容器|锅炉|辅机|膜组件|滤芯|滤料|填料|活性炭|药剂|处理剂|阻垢剂|缓蚀剂|絮凝剂|净水材料|配件|材料|过滤器|水泵|水箱|玻璃|钢材|铝材|涂料|塑料|制品|电缆|轴承|管道|管件|阀门|仪表|仪器|机床|螺栓|HEDP|ATMP|(?:机|泵|箱|纸|鞋|服装|食品)$", re.I)
_PRODUCT_JUNK = re.compile(r"始建|成立|注册|资金|资产|收入|销售额|平方米|占地|拥有|发展成为|研发投入|保证|降低|实现|致力于|赢得|保障|售前|售中|售后|服务|质量|客户|遍布|工商|认证|招聘|获奖|荣获|入围|新闻|资讯|案例|了解详情|更多|查看|获取|报价|在线|微信|经理|联系|原产地|产地|区域|经营性质|企业类型|供应信息|返回|举报|评价|推荐|收藏|分享|二维码|购物|询价|留言|咨询|下载|登录|首页|网站|关于|公司|[12]\d{3}[-年/]\d")
_PRODUCTION = re.compile(r"生产加工|专业生产|生产型|生产企业|生产商|制造(?!业|商列表|商名录)|生产(?!厂家|厂商|线|设备|工艺|流程|技术|日期|批号|原理|过程|管理)")
_EXHIBITION_SERVICE = re.compile(
    r"(?:展位|摊位)(?:申请|预订|预定|转让|租赁|预售)|"
    r"展会会刊|展会刊|参展商会刊|展商会刊|展商名录|厂家名录|企业名录|"
    r"(?:展会|展览|会展)(?:策划|招展|代理|咨询|服务)|参观证|门票办理"
)
_SPECIFIC_ALIASES = {
    "水处理设备": ("水处理设备", "水处理系统", "水处理装置", "反渗透设备", "纯净水设备", "净水设备", "污水处理设备", "软化水设备", "超纯水设备"),
    "水处理": ("水处理", "反渗透", "纯净水设备", "净水设备", "软化水设备", "超纯水设备"),
    "豆腐机": ("豆腐机", "豆腐生产设备", "豆腐加工设备", "豆腐设备", "豆腐生产线"),
    "豆腐设备": ("豆腐设备", "豆腐机", "豆腐生产设备", "豆腐加工设备", "豆腐生产线"),
}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", _text(value)).strip()


def _names(value: str, allow_generic_factory: bool = False) -> list[str]:
    # Sector/customer categories in an introduction are not additional legal owners.
    return [name for name in _raw_names(value, allow_generic_factory=allow_generic_factory)
            if name not in _GENERIC_SECTOR_NAMES and not (name.startswith("是") and name[1:] in _GENERIC_SECTOR_NAMES)]


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _page_type_rejection(url: str, title: str, soup: BeautifulSoup, text: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if ((_host_is(host, "1688.com") and re.search(r"/(?:zhuti|chanpin|selloffer|companylist)/", parsed.path, re.I))
            or (any(_host_is(host, domain) for domain in ("trustexporter.com", "trustexporter.cn")) and re.search(r"/(?:baojia|search|sell/list|category)", parsed.path, re.I))
            or (_host_is(host, "makepolo.com") and re.search(r"/(?:list|search|chanpin|company)/", parsed.path, re.I))
            or (_host_is(host, "b2b168.com") and re.search(r"^/(?:k-|company/|search/|list/)", parsed.path, re.I))):
        return "平台主题/报价聚合列表，需要进入具体单一厂家或产品详情"
    if any(_host_is(host, domain) for domain in ("baike.baidu.com", "baike.sogou.com", "baike.so.com", "baike.com", "wikipedia.org")):
        return "百科页面，只介绍知识或企业词条，不作为厂家详情入库"
    if any(_host_is(host, domain) for domain in ("zhidao.baidu.com", "wenwen.sogou.com", "iask.sina.com.cn", "zhihu.com")):
        return "问答/讨论页面，未取得厂家自述或供应商详情"
    if host in {"baidu.com", "www.baidu.com", "m.baidu.com", "sogou.com", "www.sogou.com", "m.sogou.com", "so.com", "www.so.com", "m.so.com", "bing.com", "www.bing.com", "cn.bing.com", "m.sm.cn", "sm.cn"}:
        return "仍在搜索引擎结果或跳转包装页，尚未进入厂家页面"
    if _host_is(host, "toutiao.com") or any(_host_is(host, domain) for domain in ("sohu.com", "thepaper.cn", "xinhuanet.com", "people.com.cn", "cctv.com", "mp.weixin.qq.com")):
        return "新闻/资讯发布页面，不将报道中的企业或平台当厂家入库"
    if (any(_host_is(host, domain) for domain in ("163.com", "sina.com.cn", "qq.com"))
            and ("/article/" in parsed.path or "/a/" in parsed.path or host.startswith(("news.", "finance.", "tech.", "dy.")))):
        return "新闻/资讯页面，不属于厂家或单一供应商详情"
    if _LIST_TITLE.search(title):
        return "厂家名录/排行/多供应商列表，需要进入具体厂家详情"
    if re.search(r"百度百科|搜狗百科|360百科|维基百科|百度知道|搜狗问问|知乎|问答平台", title):
        return "百科或问答页面，不属于厂家详情"
    if re.search(r"(?:搜索结果|相关搜索|search results)\s*[-_|—]", title, re.I) or soup.select_one('[itemtype*="SearchResultsPage"]'):
        return "搜索列表包装页，需要进入具体厂家详情"
    if (re.search(r"安全验证|人机验证|请输入验证码|请完成验证|访问过于频繁|访问被拒绝|access denied|just a moment", title + " " + text[:500], re.I)
            and len(text) < 3000):
        return "详情页受验证码或访问限制，未取得可识别厂家内容"
    return ""


def _small_blocks(soup: BeautifulSoup) -> list[str]:
    result = []
    seen = set()
    for tag in soup.find_all(["p", "tr", "dl", "li", "address", "div", "section", "h1", "h2", "h3"], limit=6000):
        value = _clean(tag.get_text(" ", strip=True))
        if 3 <= len(value) <= 1100 and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _structured(soup: BeautifulSoup) -> tuple[list[tuple[str, dict, str]], list[str], bool]:
    owners = []
    products = []
    article = False

    def type_names(node: dict) -> set[str]:
        types = node.get("@type", [])
        return {str(item).rsplit("/", 1)[-1] for item in (types if isinstance(types, list) else [types])}

    def organization(value, product: dict | None = None, linked: bool = False, role: str = ""):
        if isinstance(value, str):
            value = {"name": value}
        if not isinstance(value, dict):
            return
        names = _names(str(value.get("legalName") or value.get("name") or ""), allow_generic_factory=True)
        if len(names) == 1:
            data = dict(value)
            if product:
                data["_product"] = str(product.get("name") or "")
                data["_product_description"] = str(product.get("description") or "")
                data["_role"] = role
            owners.append((names[0], data, "结构化产品供应主体" if linked else "结构化企业主体"))

    def walk(value, depth=0, context=""):
        nonlocal article
        if depth > 8:
            return
        if isinstance(value, list):
            for child in value[:80]:
                walk(child, depth + 1, context)
            return
        if not isinstance(value, dict):
            return
        types = type_names(value)
        if types & {"NewsArticle", "BlogPosting", "SearchResultsPage", "QAPage", "FAQPage"}:
            article = True
        if "Product" in types:
            product_name = _clean(str(value.get("name") or ""))
            if product_name:
                products.append(product_name)
            for key in ("manufacturer", "seller", "provider"):
                if key in value:
                    organization(value[key], value, True, key)
            offers = value.get("offers", {})
            for offer in offers if isinstance(offers, list) else [offers]:
                if isinstance(offer, dict) and "seller" in offer:
                    organization(offer["seller"], value, True, "seller")
        if types & {"Organization", "Corporation", "LocalBusiness", "Store", "ManufacturingBusiness"} and context not in {"publisher", "author", "copyrightHolder", "brand"}:
            organization(value)
        for key, child in value.items():
            if key not in {"publisher", "author", "copyrightHolder", "brand", "manufacturer", "seller", "provider"}:
                walk(child, depth + 1, key)

    for tag in soup.select('script[type="application/ld+json"]')[:20]:
        try:
            walk(json.loads((tag.string or tag.get_text())[:500_000]))
        except (ValueError, TypeError, RecursionError):
            continue
    return owners, list(dict.fromkeys(products)), article


def _add_owner(owners: dict[str, _Owner], name: str, weight: int, signal: str, excerpt: str, data: dict | None = None) -> None:
    key = normalized_name(name)
    owner = owners.setdefault(key, _Owner(name))
    if signal not in owner.signals:
        owner.score += weight
        owner.signals.append(signal)
    if excerpt and excerpt not in owner.excerpts:
        owner.excerpts.append(excerpt[:600])
    if data and data not in owner.data:
        owner.data.append(data)


def _owner_candidates(soup: BeautifulSoup, main: BeautifulSoup, title: str, blocks: list[str], structured) -> dict[str, _Owner]:
    owners: dict[str, _Owner] = {}
    for name in _names(title, allow_generic_factory=True):
        _add_owner(owners, name, 4, "详情页标题全称", title)
    for tag in main.select("h1,h2,.company-name,.company_name,.companyName,.supplier-name,.shop-name,[itemprop='legalName']")[:100]:
        value = _clean(tag.get_text(" ", strip=True))
        if len(value) > 160:
            continue
        for name in _names(value, allow_generic_factory=True):
            _add_owner(owners, name, 6, "页面企业标题", value)
    for block in blocks:
        if _PLATFORM_CONTACT.search(block) and not re.search(r"(?:供应商|生产厂家|店铺主体)\s*[:：]", block):
            continue
        for match in _OWNER_LABEL.finditer(block):
            names = _names(match.group(1).split("联系电话")[0], allow_generic_factory=True)
            for name in names:
                _add_owner(owners, name, 12, "单一供应主体标签", block)
        for name in _names(block, allow_generic_factory=True):
            position = block.find(name)
            after = block[position + len(name):position + len(name) + 150]
            if _SELF_DESCRIPTION.search(after) and position >= 0:
                _add_owner(owners, name, 8, "企业自述经营介绍", block)
    for name, data, signal in structured:
        # A bare WebSite Organization often names the marketplace operator.
        if signal != "结构化产品供应主体" and not data.get("description"):
            continue
        _add_owner(owners, name, 12 if signal == "结构化产品供应主体" else 8, signal,
                   json.dumps(data, ensure_ascii=False)[:600], data)
    # Manufacturer contact blocks in a footer can establish identity; copyright alone cannot.
    for tag in soup.select("footer,address,.footer,#footer")[:30]:
        value = _clean(tag.get_text(" ", strip=True))
        if len(value) <= 800 and not _PLATFORM_CONTACT.search(value) and re.search(r"联系电话|销售电话|公司地址|工厂地址", value):
            names = _names(value, allow_generic_factory=True)
            if len(names) == 1:
                _add_owner(owners, names[0], 8, "厂家联系信息主体", value)
    # Older company sites use arbitrary bottom <div>s instead of <footer>.
    # Corroborate an existing title identity; never invent an owner from copyright.
    for block in _small_blocks(soup):
        if len(block) > 800 or _PLATFORM_CONTACT.search(block):
            continue
        if re.search(r"版权所有|copyright|©", block, re.I) and re.search(r"销售热线|销售电话|联系电话|地址", block):
            for owner in list(owners.values()):
                if owner.name in block and "详情页标题全称" in owner.signals:
                    _add_owner(owners, owner.name, 8, "标题主体与版权联系信息一致", block)
    return owners


def _query_plan(query: str) -> tuple[list[tuple[str, ...]], list[str], str]:
    value = _clean(query).lower().strip('"\'“”‘’')
    identity = _names(value, allow_generic_factory=True)
    if len(identity) == 1 and normalized_name(identity[0]) == normalized_name(value):
        return [], [], identity[0]
    value = _QUERY_INTENT.sub(" ", value)
    value = re.sub(r"^(?:全国|中国|国内)\s*", "", value)
    requested_regions = []
    # Location prefixes are constraints, not product keywords.
    value = value.strip()
    changed = True
    while changed:
        changed = False
        for region in sorted(_REGIONS, key=len, reverse=True):
            if value.startswith(region):
                requested_regions.append(region)
                value = value[len(region):]
                if value.startswith(("省", "市")):
                    value = value[1:]
                value = value.lstrip()
                changed = True
                break
    parts = [part for part in re.split(r"[\s,，、;；/|]+", value) if part and part not in {"的", "好", "有哪些", "有", "找", "全国", "中国"}]
    if len(parts) > 1:
        parts = [part for part in parts if part not in {"设备", "机械", "机器", "产品", "生产", "销售", "制造"}]
    groups = []
    for part in parts[:8]:
        part = re.sub(r"^(?:找|生产|销售|制造)", "", part)
        if not part:
            continue
        aliases = _SPECIFIC_ALIASES.get(part)
        if aliases:
            groups.append(tuple(aliases))
        else:
            alternatives = [part]
            if len(part) >= 5 and part.endswith(("设备", "机械", "机器", "装置", "系统")):
                alternatives.append(part[:-2])
            elif len(part) >= 5 and part.endswith("机"):
                alternatives.append(part[:-1] + "设备")
            groups.append(tuple(alternatives))
    return groups, requested_regions, ""


def _product_phrase(value: str, noun_required: bool = False) -> str:
    value = _clean(value).strip(" 。:：,，;；|-_+")
    value = re.sub(r"^(?:公司围绕|本公司围绕|专业型|专业|围绕)", "", value)
    value = re.sub(r"^(?:主营产品|主营业务|主要产品|经营范围|专业生产|主要生产|研发生产|生产销售|公司生产|本厂生产|生产|制造|销售|供应|包括|各种|各类)\s*[:：]?", "", value)
    value = re.split(r"的专业厂家|专业生产商|专业制造商|专业厂家|厂家直销|生产厂家|专业定做|等产品|等设备|系列产品|产品展示", value)[0].strip()
    if "的" in value and _PRODUCT_NOUN.search(value.rsplit("的", 1)[-1]):
        value = value.rsplit("的", 1)[-1]
    value = re.sub(r"(?:生产企业|制造企业|企业|厂家)$", "", value).strip()
    if not 2 <= len(value) <= 45 or _PRODUCT_JUNK.search(value) or _names(value, allow_generic_factory=True):
        return ""
    if re.search(r"[!！?？:]|都是|^制做|^制作|^技术|上一页|下一页", value):
        return ""
    if value in set(_REGIONS) | {"设计", "制造", "生产", "开发", "研发", "批发", "销售", "采购", "维修", "保养", "工厂", "公司", "厂家", "专业定做", "生产材料", "产品中心", "设备中心", "产品展示", "主营产品", "关于我们", "企业风采", "资质证书", "全国", "水处理", "生产型", "生产加工"}:
        return ""
    if re.search(r"中心|查看更多|^\d{4}[-/]\d|^第\d|^[a-z]+://", value, re.I):
        return ""
    if noun_required and not _PRODUCT_NOUN.search(value):
        return ""
    return value


def _scope_excerpt(value: str) -> str:
    return re.split(r"历经|企业注册资金|注册资本|占地面积|拥有大型|新闻动态|招聘英才|我司参加|更多详情|了解详情|公司资质|拥有先进|完善的售前|可靠的产品质量|产品销售至|用户遍布", value)[0].strip(" 。；;")[:700]


def _own_production(owner: _Owner, contexts: list[str], scope: str, blocks: list[str]) -> str:
    owner_key = normalized_name(owner.name)
    for block in blocks:
        if re.search(r"(?:经营模式|企业类型|经营类型)\s*[:：]?\s*(?:生产型|生产加工|生产企业)", block):
            names = {normalized_name(name) for name in _names(block, allow_generic_factory=True)}
            if owner_key in names and not names - {owner_key}:
                return block[:500]
    # Product headings and recommendation cards can say '专业生产' about
    # someone else. Only the identified owner's own business prose supplies
    # production evidence; a bare product title never turns a dealer into a factory.
    attributed = [scope] + [value for value in contexts if owner.name in value or re.search(r"本公司|本厂|我司|我们专业", value)]
    for value in attributed:
        value = value.replace(owner.name, " ")
        for match in _PRODUCTION.finditer(value):
            before = value[max(0, match.start() - 25):match.start()]
            if re.search(r"(?:不|无|未|没有|不从事|不负责|不进行|并非|非).{0,4}$", before):
                continue
            if re.search(r"维修|保养|检修|清洗|租赁", value[max(0, match.start() - 15):match.end() + 25]) and not re.search(r"专业生产|生产制造|制造销售", value):
                continue
            return value[:500]
    for data in owner.data:
        if data.get("_role") == "manufacturer" and not re.search(r"贸易型|专业经销|仅销售|批发及零售|不生产", scope):
            return f"结构化产品 manufacturer 明确指向 {owner.name}"
    return ""


def _service_business_rejection(owner: _Owner, title: str, blocks: list[str]) -> str:
    """Identify the seller's actual service, not industries in its ad copy.

    A supplier selling a water-industry exhibitor catalogue does not manufacture
    water equipment. A manufacturer describing its own products and attendance
    at an exhibition remains eligible; exhibition words alone are insufficient.
    """
    owner_key = normalized_name(owner.name)
    own_scopes = []
    commercial_service = False
    for block in blocks:
        names = {normalized_name(name) for name in _names(block, allow_generic_factory=True)}
        if names - {owner_key}:
            continue
        if re.search(r"(?:企业类型|经营模式|经营类型)\s*[:：]?\s*(?:商业服务|商务服务|服务型)", block):
            commercial_service = True
        for match in _BUSINESS_LABEL.finditer(block):
            value = re.split(r"公司介绍|企业介绍|供应信息|联系方式|企业已认证|个人实名|联系电话|联系人|公司地址|注册资金|经营性质|企业类型", match.group(1))[0]
            if value:
                own_scopes.append(value[:400])
    service_scopes = [value for value in own_scopes if _EXHIBITION_SERVICE.search(value)]
    # A physical product named in a separate own-business clause prevents a
    # blanket exclusion of genuine manufacturers with additional services.
    physical_scope = any(_PRODUCT_NOUN.search(clause) and not _EXHIBITION_SERVICE.search(clause)
                         for value in own_scopes for clause in re.split(r"[、,，;；|。]", value))
    if service_scopes and not physical_scope:
        return "主体主营为展会会刊、名录或展位等会展服务，行业宣传中的生产词不能证明其自身是相关设备厂家"
    if commercial_service and _EXHIBITION_SERVICE.search(title) and not physical_scope:
        return "页面销售会刊、名录或会展服务，且主体标为商业服务，不能作为相关生产厂家入库"
    return ""


def _business_material(owner: _Owner, title: str, main: BeautifulSoup, blocks: list[str]) -> tuple[list[str], str, list[str], str]:
    contexts = []
    scopes = []
    product_scopes = []
    products = []
    manufacturing = ""
    owner_key = normalized_name(owner.name)
    aliases = re.findall(re.escape(owner.name) + r"\s*\(([^()]{2,20})\)", title)
    for block in blocks:
        if len(block) < 180 and re.search(r"荣获|入围|获奖|喜讯|榜单|称号|开展调研", block) and not re.search(r"主营|专业生产|主要生产|生产商|制造商", block):
            continue
        other = {normalized_name(name) for name in _names(block, allow_generic_factory=True)} - {owner_key}
        if other:
            continue
        match = _BUSINESS_LABEL.search(block)
        if match:
            scope = re.split(r"联系电话|联系手机|联系人|公司地址|工厂地址|联系地址|客服电话|技术支持|公司名称|企业名称|供应商|注册资金|经营性质|企业类型|供应信息|企业已认证|公司介绍|个人实名", match.group(1))[0].strip(" :：，,。;；")
            if scope:
                scopes.append(scope[:650])
                product_scopes.append(scope[:650])
                contexts.append(block)
                manufacturing = manufacturing or block
        if (owner.name in block or any(alias in block for alias in aliases) or re.search(r"本公司|本厂|我司|我们专业", block)) and _SELF_DESCRIPTION.search(block) and not re.search(r"销售热线|销售电话|版权所有|copyright", block, re.I):
            contexts.append(block)
            scopes.append(_scope_excerpt(block))
            if _MANUFACTURING.search(block):
                manufacturing = manufacturing or block
    for data in owner.data:
        for key in ("description", "_product_description"):
            value = _clean(str(data.get(key) or ""))
            if value:
                contexts.append(value)
                scopes.append(value[:650])
                if _MANUFACTURING.search(value):
                    manufacturing = manufacturing or value
        product = _clean(str(data.get("_product") or ""))
        if product:
            products.append(product)
            contexts.append(product)
            manufacturing = manufacturing or "结构化 Product 指向此厂家/供应商"
    # Product areas are evidence; unrelated links/news/footer text are not.
    for tag in main.select(".product-name,.product_name,.pro-name,.product-title,.pro_text,[itemprop='name'],[class*='product'] h2,[class*='product'] h3,[class*='product'] a,[class*='index_pro'] a,[class*='pro-list'] a,[class*='prolist'] a")[:160]:
        value = _clean(tag.get_text(" ", strip=True))
        if 2 <= len(value) <= 100 and not _names(value, allow_generic_factory=True):
            products.append(value)
            contexts.append(value)
    for scope in product_scopes:
        for part in re.split(r"[、,，;；|/]+", scope):
            part = part.strip(" 。:：")
            part = re.sub(r"^(?:主营产品|主营业务|主要产品|经营范围|专业生产|主要生产|研发生产|生产销售|生产|销售|供应)\s*[:：]?", "", part)
            if 2 <= len(part) <= 100 and owner.name not in part and not re.search(r"成立于|位于|坐落|有限公司", part):
                products.append(part)
    primary_owner = any(signal in owner.signals for signal in ("详情页标题全称", "页面企业标题", "单一供应主体标签", "结构化产品供应主体"))
    if primary_owner:
        for tag in main.select("h1")[:10]:
            value = _clean(tag.get_text(" ", strip=True))
            if owner.name not in value and len(value) <= 140 and not _CLIENT_OR_PROMOTION.search(value):
                contexts.append(value)
                if any(signal in owner.signals for signal in ("单一供应主体标签", "结构化产品供应主体")):
                    products.append(value)
                    manufacturing = manufacturing or "产品详情中的单一供应商/制造商标签"
    clean_products = [_product_phrase(value) for value in products]
    # Extract noun phrases from business prose, never arbitrary comma clauses.
    for scope in scopes:
        for clause in re.split(r"[、,，;；。]|以及|及", scope):
            clause = re.sub(r"^.*?(?:主营|包括|(?:专业|主要)?生产|制造|研发|开发|设计)(?:各种|各类)?", "", clause)
            candidate = _product_phrase(clause, noun_required=True)
            if candidate:
                clean_products.append(candidate)
    # Short noun phrases occurring verbatim in the owner's business material
    # provide useful products even when the page only has a prose introduction.
    noun_phrase = re.compile(r"(?:反渗透|水处理|纯净水|净水|软化水|超纯水|污水|废水|超滤|豆腐|豆制品|豆皮|豆浆|豆干|腐竹)[\u3400-\u9fffA-Za-z0-9]{0,8}?(?:设备|药剂|系统|装置|过滤器|生产线|加工线|机械|机)")
    for context in [*scopes, *products]:
        clean_products.extend(_product_phrase(match.group()) for match in noun_phrase.finditer(context))
    clean_scopes = [value for value in dict.fromkeys(_scope_excerpt(value) for value in scopes) if value]
    minimal_scopes = [value for value in clean_scopes if not any(other != value and len(other) >= 30 and other in value for other in clean_scopes)]
    return list(dict.fromkeys(contexts)), "；".join(minimal_scopes)[:1800], list(dict.fromkeys(value for value in clean_products if value))[:18], manufacturing


def _business_match(groups: list[tuple[str, ...]], contexts: list[str]) -> tuple[bool, list[str], str]:
    matching = []
    excerpts = []
    for alternatives in groups:
        match = None
        for context in contexts:
            # An agency serving water-equipment customers is not itself a
            # water-equipment supplier. Keep such terms only for matching the
            # actual web/marketing-service query.
            if _CLIENT_OR_PROMOTION.search(context) and not any(_CLIENT_OR_PROMOTION.search(term) for term in alternatives):
                continue
            compact = normalized_name(context)
            for term in alternatives:
                if normalized_name(term) in compact:
                    position = context.find(term)
                    before = re.split(r"[。;；]", context[:max(0, position)])[-1]
                    if re.search(r"适用于|适用范围|客户行业|服务客户|应用领域", before):
                        continue
                    match = term
                    excerpts.append(context[:500])
                    break
            if match:
                break
        if not match:
            return False, [], ""
        matching.append(match)
    return bool(groups), matching, " | ".join(dict.fromkeys(excerpts))[:900]


def _address(owner: _Owner, blocks: list[str], full_soup: BeautifulSoup) -> tuple[str, str, str]:
    for data in owner.data:
        address = data.get("address")
        if isinstance(address, dict):
            region = _clean("".join(str(address.get(key) or "") for key in ("addressRegion", "addressLocality")))
            street = _clean(str(address.get("streetAddress") or ""))
            if (not region or _valid_region(region)) and (region or street) and len(region + street) <= 180:
                return region, region + street, json.dumps(address, ensure_ascii=False)
        elif isinstance(address, str) and _valid_address(address):
            return _region_from_address(address), _clean(address), address
    region = ""
    address = ""
    evidence = ""
    extra = []
    for tag in full_soup.select("footer,address,.footer,#footer")[:30]:
        value = _clean(tag.get_text(" ", strip=True))
        if len(value) <= 1000 and not _PLATFORM_CONTACT.search(value):
            extra.append(value)
    for block in sorted(blocks + extra, key=lambda value: (owner.name not in value, len(value))):
        if _PLATFORM_CONTACT.search(block):
            continue
        names = {normalized_name(name) for name in _names(block, allow_generic_factory=True)}
        if names - {normalized_name(owner.name)}:
            continue
        match = re.search(r"(?:公司地址|工厂地址|联系地址|生产地址|企业地址|经营地址|地址)\s*[:：]\s*(.{6,160})", block)
        if match and not address:
            value = re.split(r"联系电话|联系手机|电话|手机|联系人|邮编|邮箱|传真|网址|主营|版权所有|企业类型|经营性质|注册地|经营范围|查看地图", match.group(1))[0].strip(" :：，,。;；")
            if _valid_address(value):
                address = value
                evidence = block
        match = re.search(r"(?:公司所在地|所在地|所在地区|所在区域)\s*[:：]\s*(.{2,80})", block)
        if match and not region:
            value = re.split(r"地址|联系电话|电话|手机|主营|经营范围|联系人|经营性质|企业类型|注册地|注册资金|供应信息|企业已认证", match.group(1))[0].strip(" :：，,。;；")
            if _valid_region(value):
                region = value
                evidence = evidence or block
    return region or _region_from_address(address), address, evidence


def _region_from_address(address: str) -> str:
    match = re.match(r"((?:北京市|天津市|上海市|重庆市)|(?:[\u3400-\u9fff]{2,7}?(?:省|自治区))?[\u3400-\u9fff]{2,7}?市)", _clean(address).removeprefix("中国"))
    if match:
        return match.group(1)
    for region in sorted(_REGIONS, key=len, reverse=True):
        if _clean(address).startswith(region):
            return region
    return ""


def _valid_region(value: str) -> bool:
    value = _clean(value).removeprefix("中国").strip()
    return (2 <= len(value) <= 35 and len(value.split()) <= 4
            and not re.search(r"确认|商家|地区:|全国|不限|请选择|交货|价格|供应商|https?://|[。；;]", value)
            and any(value.startswith(region) for region in _REGIONS))


def _valid_address(value: str) -> bool:
    value = _clean(value).removeprefix("中国").strip()
    return (6 <= len(value) <= 160 and not re.search(r"确认|商家|交货|价格|https?://|请联系|本站|供应商", value)
            and (any(value.startswith(region) for region in _REGIONS) or bool(re.search(r"工业园|开发区|.{1,12}[路街镇乡].{0,12}\d+号", value))))


def _valid_contact_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value.split(" 转")[0])
    subscriber = digits[-8:] if value.startswith("0") else digits[-7:]
    return not (digits.startswith("000") or len(set(subscriber)) == 1 or subscriber in {"1234567", "12345678", "87654321"})


def _phone_evidence(owner: _Owner, soup: BeautifulSoup, main: BeautifulSoup, platform: bool) -> list[tuple[str, str]]:
    result = []
    owner_key = normalized_name(owner.name)
    for data in owner.data:
        for phone in extract_phones(str(data.get("telephone") or "")):
            result.append((phone, f"结构化供应主体 {owner.name}；telephone={data.get('telephone')}"))
    # Work from individual contact blocks, not all digit sequences on a marketplace.
    for tag in soup.find_all(["p", "tr", "li", "address", "div", "section", "footer", "a"], limit=7000):
        if any(ancestor.name in {"nav", "aside"} or re.search(r"related|recommend|tuijian|friend|sidebar", " ".join(ancestor.get("class", [])) + " " + str(ancestor.get("id", "")), re.I)
               for ancestor in [tag, *list(tag.parents)[:5]]):
            continue
        value = _clean(tag.get_text(" ", strip=True))
        for label in ("电话", "手机", "热线", "联系", "地址"):
            value = re.sub(r"\s+".join(label), label, value)
        if tag.name == "a" and str(tag.get("href", "")).lower().startswith("tel:"):
            value += " " + str(tag.get("href"))[4:]
        if len(value) > 650 or not extract_phones(value):
            continue
        if _PLATFORM_CONTACT.search(value):
            continue
        if platform and re.search(r"客服|网站热线|服务平台|客服电话", value):
            continue
        if not re.search(r"电话|手机|热线|tel|联系方式|联系手机", value, re.I) and tag.name != "a":
            continue
        # Context can establish whose contact block this is; ancestors also expose
        # operator/footer/competitor ownership hidden by a tiny '电话' child node.
        scope = value
        blocked = False
        for ancestor in list(tag.parents)[:3]:
            if ancestor.name in {"html", "body", "[document]"}:
                break
            ancestor_text = _clean(ancestor.get_text(" ", strip=True))
            if len(ancestor_text) > 1400:
                break
            names = {normalized_name(name) for name in _names(ancestor_text, allow_generic_factory=True)}
            if names - {owner_key}:
                blocked = True
                break
            if _PLATFORM_CONTACT.search(ancestor_text) and owner.name not in value:
                blocked = True
                break
            scope = ancestor_text
            if owner.name in scope:
                break
        if blocked:
            continue
        names = {normalized_name(name) for name in _names(scope, allow_generic_factory=True)}
        if names - {owner_key}:
            continue
        if platform and owner.name not in scope:
            # A separate contact card is usable only for one clearly identified supplier.
            if not any(signal == "单一供应主体标签" for signal in owner.signals) or tag.name in {"footer", "nav", "aside"} or tag.find_parent(["footer", "nav", "aside"]):
                continue
        for phone in extract_phones(value):
            digits = re.sub(r"\D", "", phone.split(" 转")[0])
            number = re.search(r"[\s-]*".join(digits), value)
            if number and re.search(r"(?:传真|fax)\s*[:：]?\s*$", value[max(0, number.start() - 24):number.start()], re.I):
                continue
            result.append((phone, scope[:650]))
    unique = []
    seen = set()
    for phone, context in result:
        if phone not in seen and _valid_contact_phone(phone):
            unique.append((phone, context))
            seen.add(phone)
    return unique


def _contact_urls(soup: BeautifulSoup, final_url: str, platform: bool) -> list[str]:
    candidates = []
    host = (urlsplit(final_url).hostname or "").lower()
    for tag in soup.select("a[href]")[:3000]:
        label = _clean(tag.get_text(" ", strip=True))
        if not re.search(r"联系我们|联系方式|联系信息|公司简介|关于我们|企业介绍|公司介绍|企业概况", label):
            continue
        if _PLATFORM_CONTACT.search(label):
            continue
        href = str(tag.get("href") or "").strip()
        if href.startswith(("#", "javascript:", "tel:", "mailto:")):
            continue
        url = urljoin(final_url, href)
        if not normalized_url(url) or (urlsplit(url).hostname or "").lower() != host or normalized_url(url) == normalized_url(final_url):
            continue
        if platform and re.fullmatch(r"/(?:contact|about|help)(?:\.html?)?/?", urlsplit(url).path, re.I):
            continue
        priority = 0 if re.search(r"联系", label) else 1
        candidates.append((priority, url))
    return list(dict.fromkeys(url for _, url in sorted(candidates, key=lambda value: value[0])))[:2]


def qualify_page(hit: SearchHit, html: str, final_url: str) -> QualificationResult:
    """Return a manufacturer only after page owner + relevant business evidence pass."""
    if not isinstance(html, str) or not html.strip() or not normalized_url(final_url):
        return QualificationResult(None, "未取得可读的 HTTP/HTTPS 厂家详情页，搜索摘要不能替代页面核验")
    soup = BeautifulSoup(html[:2_000_000], "html.parser")
    for tag in soup.find_all(style=True):
        if tag.attrs is None:
            continue
        if re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", str(tag.get("style")), re.I):
            tag.decompose()
    title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    structured, _, article = _structured(soup)
    for tag in soup.select("script,style,noscript,svg,iframe,[hidden],[aria-hidden='true']"):
        tag.decompose()
    body = soup.body or soup
    full_text = _clean(body.get_text(" ", strip=True))
    rejection = _page_type_rejection(final_url, title, soup, full_text)
    if rejection:
        return QualificationResult(None, rejection)
    if not full_text and not structured:
        return QualificationResult(None, "页面无可读厂家正文或供应主体结构化数据")
    main = deepcopy(soup)
    for tag in main.select(_REMOVE_MAIN):
        tag.decompose()
    blocks = _small_blocks(main)
    owners = _owner_candidates(soup, main, title, blocks, structured)
    if not owners:
        return QualificationResult(None, "实际页面未识别出完整、可归属的厂家/供应商名称")
    ordered = sorted(owners.values(), key=lambda value: value.score, reverse=True)
    explicit = [owner for owner in ordered if "单一供应主体标签" in owner.signals or "结构化产品供应主体" in owner.signals]
    if len(explicit) > 1:
        return QualificationResult(None, "同页存在多个供应商主体，属于列表或归属不明确，需要进入单一厂家详情")
    owner = explicit[0] if explicit else ordered[0]
    if not explicit and len(ordered) > 1 and ordered[1].score >= owner.score - 3:
        return QualificationResult(None, "页面出现多个相近权重的企业名称，未能确定厂家主体")
    host = (urlsplit(final_url).hostname or "").lower()
    platform = any(_host_is(host, domain) for domain in ("b2b.baidu.com", "b2b168.com", "makepolo.com", "trustexporter.com", "trustexporter.cn", "21food.cn", "1688.com", "alibaba.com", "hc360.com", "made-in-china.com", "china.cn", "qqma.com", "huangye88.com", "gys.cn", "58.com", "liebiao.com", "souqian.com", "sooland.com", "jdzj.com"))
    contacts = _contact_urls(soup, final_url, platform)
    if owner.score < 6 and not (owner.score >= 4 and any(_BUSINESS_LABEL.search(block) for block in blocks)):
        return QualificationResult(None, "企业全称仅在弱上下文出现，缺少厂家主体或经营信息支持", contacts)
    if platform and not explicit and "企业自述经营介绍" not in owner.signals:
        return QualificationResult(None, "平台页面缺少明确的单一供应商归属，未把平台运营主体当厂家", contacts)
    service_rejection = _service_business_rejection(owner, title, blocks)
    if service_rejection:
        return QualificationResult(None, service_rejection, contacts)
    contexts, scope, products, manufacturer = _business_material(owner, title, main, blocks)
    groups, requested_regions, identity_query = _query_plan(hit.query)
    if identity_query:
        relevant = normalized_name(identity_query) == normalized_name(owner.name)
        matched = []
        product_excerpt = scope or " | ".join(contexts)[:700]
    else:
        if not groups:
            return QualificationResult(None, "搜索关键词缺少具体产品、行业或完整企业名称，无法判定厂家相关性", contacts)
        relevant, matched, product_excerpt = _business_match(groups, contexts)
    if not relevant:
        return QualificationResult(None, "实际厂家的主营/产品信息与搜索关键词不匹配，未按标题或链接直接入库", contacts)
    if not manufacturer:
        return QualificationResult(None, "虽出现关键词，但缺少生产、制造、主营、销售或单一产品供应关系的页面证据", contacts)
    production = _own_production(owner, contexts, scope, blocks)
    if not production:
        return QualificationResult(None, "页面只证明供应、经销或维修服务，缺少此主体自身生产/制造依据，未作为厂家入库", contacts)
    manufacturer = production
    if not scope and not products:
        return QualificationResult(None, "未提取到厂家自身的主营产品或经营范围，关键词出现不足以入库", contacts)
    metadata = soup.select_one('meta[property="og:type"]')
    article_like = article or bool(metadata and metadata.get("content", "").lower() in {"article", "news", "newsarticle"})
    article_like = article_like or bool(soup.select_one("article time,article .byline,article .author") or (re.search(r"/(?:news|article|xinwen)/", urlsplit(final_url).path, re.I) and soup.select_one("time,.byline,.article-author,.news-source")))
    if article_like:
        product_heading = any(owner.name not in _clean(tag.get_text(" ", strip=True))
                              and not re.search(r"新闻|资讯|发布|报道|记者|采访|动态|举行|获奖|网站建设|案例", _clean(tag.get_text(" ", strip=True)))
                              and any(normalized_name(term) in normalized_name(tag.get_text(" ", strip=True)) for group in groups for term in group)
                              for tag in main.select("h1,.product-title,.product-name"))
        supplier_detail = bool(explicit) or ("详情页标题全称" in owner.signals and "标题主体与版权联系信息一致" in owner.signals and product_heading)
        if not supplier_detail:
            return QualificationResult(None, "文章/资讯页面缺少厂家自身单一产品详情的归属证据，不据报道提及入库")
    region, address, address_excerpt = _address(owner, blocks, soup)
    if requested_regions and not all(region_name in owner.name + region + address for region_name in requested_regions):
        return QualificationResult(None, "产品相关，但页面未提供与搜索地域一致的厂家身份/地址依据", contacts)
    # All accepted records have real full names and source-page proof, never title placeholders.
    company = Company(name=owner.name, name_confirmed=True, page_verified=True, verification_url=final_url,
                      urls=list(dict.fromkeys(url for url in (hit.url, final_url) if url)), hits=[deepcopy(hit)],
                      products=products or list(dict.fromkeys(value for term in matched if (value := _product_phrase(term)))), business_scope=scope,
                      region=region, address=address)
    company.admission_reasons = [
        f"已实际读取详情页并识别主体：{owner.name}（{'、'.join(owner.signals)}）",
        f"厂家自身产品/经营信息与“{hit.query}”匹配：{product_excerpt[:260]}",
        f"厂家生产/制造依据：{manufacturer[:260]}",
    ]
    company.notes = "页面主体与产品相关性已按公开内容识别；未进行工商资质、信用或电话归属的外部认证。"
    provenance = dict(engine=hit.engine, query=hit.query, page=hit.page)
    company.evidence.append(Evidence(kind="收录", url=hit.url, excerpt=_clean(f"{hit.title} {hit.snippet}")[:600], status="搜索发现来源，仅作追溯", fetched_at=hit.fetched_at, **provenance))
    company.evidence.append(Evidence(kind="详情页核验", url=final_url, excerpt=" | ".join(company.admission_reasons)[:1400], status="已读取并识别页面主体，未认证工商或信用", **provenance))
    company.evidence.append(Evidence(kind="名称", url=final_url, excerpt=" | ".join(owner.excerpts)[:1000], status="厂家/供应商页面主体文本提取", **provenance))
    company.evidence.append(Evidence(kind="主营产品", url=final_url, excerpt=product_excerpt or "、".join(company.products), status="厂家页面公开产品描述", **provenance))
    if scope:
        company.evidence.append(Evidence(kind="经营范围", url=final_url, excerpt=scope, status="公开业务描述，未核实工商登记范围", **provenance))
    if address_excerpt:
        company.evidence.append(Evidence(kind="地区地址", url=final_url, excerpt=address_excerpt[:900], status="页面公开地址，未实地核实", **provenance))
    for phone, excerpt in _phone_evidence(owner, soup, main, platform):
        company.phones.append(phone)
        company.evidence.append(Evidence(kind="电话", url=final_url, excerpt=excerpt, status="厂家公开联系方式，号码有效性待核实", **provenance))
    if company.phones and company.address and company.business_scope:
        contacts = []
    return QualificationResult(company, "已读取详情页，厂家主体、产品相关性及生产/供应关系均有页面依据", contacts)
