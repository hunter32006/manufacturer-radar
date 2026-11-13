import json

import pytest

from manufacturer_finder.extraction import merge_companies
from manufacturer_finder.models import Company, SearchHit
from manufacturer_finder.qualification import qualify_page


def hit(query="水处理设备厂家", **kwargs):
    values = dict(engine="baidu", query=query, page=1, title="水处理设备厂家", url="https://result.example/link", snippet="电话13911112222，很多厂家")
    values.update(kwargs)
    return SearchHit(**values)


WATER = """<html><head><title>山东清源环保设备有限公司 - 水处理设备</title></head>
<body><h1>山东清源环保设备有限公司</h1>
<p>山东清源环保设备有限公司是一家从事水处理设备研发、生产、销售的企业。</p>
<p>主营产品：水处理设备、反渗透设备、纯净水设备</p>
<div><p>联系电话：13800138000</p><p>工厂地址：山东省青岛市城阳区工业园创新路18号</p></div>
<a href="/contact.html">联系我们</a><a href="/about.html">公司简介</a>
<footer>技术支持：青岛互联网络科技有限公司 电话400-100-2000</footer></body></html>"""


def test_actual_water_manufacturer_is_qualified_with_provenance_and_fields():
    source = hit()
    result = qualify_page(source, WATER, "https://water.example/")
    assert result.company is not None, result.reason
    company = result.company
    assert company.name == "山东清源环保设备有限公司"
    assert company.page_verified and company.name_confirmed
    assert company.verification_url == "https://water.example/"
    assert company.urls == [source.url, "https://water.example/"]
    assert company.hits == [source]
    assert "水处理设备" in company.products
    assert "反渗透设备" in company.business_scope
    assert company.region == "山东省青岛市"
    assert "工业园" in company.address
    assert company.phones == ["13800138000"]
    assert "13911112222" not in company.phones
    assert all(e.url == "https://water.example/" for e in company.evidence if e.kind != "收录")
    assert {"详情页核验", "名称", "主营产品", "经营范围", "地区地址", "电话"} <= {e.kind for e in company.evidence}
    assert len(company.admission_reasons) >= 3


def test_tofu_factory_is_qualified_but_missing_phone_stays_missing():
    html = """<title>曲阜益农食品机械厂 - 豆腐机</title><h1>曲阜益农食品机械厂</h1>
    <p>曲阜益农食品机械厂专业生产豆腐机、豆浆设备、豆腐生产线。</p><p>主营产品：全自动豆腐机、豆制品设备</p>
    <a href="/contact">联系方式</a><a href="/about">关于我们</a><a href="https://third.example/contact">联系我们</a>"""
    result = qualify_page(hit("豆腐机厂家"), html, "https://tofu.example/")
    assert result.company is not None, result.reason
    assert result.company.name == "曲阜益农食品机械厂"
    assert not result.company.phones
    assert result.contact_urls == ["https://tofu.example/contact", "https://tofu.example/about"]


def test_single_b2b_supplier_preserves_own_phone_and_ignores_platform_operator():
    html = """<title>水处理设备报价 - 采购平台</title><h1>反渗透水处理设备</h1>
    <main><div class="supplier"><p>供应商：山东清源环保设备有限公司</p><p>山东清源环保设备有限公司 企业类型：生产加工</p><p>主营产品：水处理设备、纯净水设备</p>
    <div class="contact">山东清源环保设备有限公司 联系人：王经理 手机：13800138000</div></div></main>
    <footer><p>平台运营主体：北京平台信息技术有限公司</p><p>平台客服电话：400-100-2000</p></footer>"""
    result = qualify_page(hit(), html, "https://b2b.baidu.com/offer/123.html")
    assert result.company is not None, result.reason
    assert result.company.name == "山东清源环保设备有限公司"
    assert result.company.phones == ["13800138000"]


def test_name_and_phone_only_in_search_snippet_are_never_admitted():
    result = qualify_page(hit(title="山东清源环保设备有限公司", snippet="主营水处理设备 电话13800138000"), "<title>供应商查询</title><p>请登录查看企业详情</p>", "https://directory.example/123")
    assert result.company is None
    assert qualify_page(hit(), "", "https://factory.example").company is None
    assert qualify_page(hit(), WATER, "javascript:alert(1)").company is None


@pytest.mark.parametrize("url,title", [
    ("https://baike.baidu.com/item/水处理设备", "水处理设备_百度百科"),
    ("https://zhidao.baidu.com/question/123", "哪里找水处理设备厂家_百度知道"),
    ("https://www.zhihu.com/question/123", "水处理设备厂家有哪些"),
    ("https://www.sohu.com/a/123", "山东清源环保设备有限公司发布水处理设备"),
    ("https://www.baidu.com/s?wd=水处理设备", "水处理设备_百度搜索"),
    ("https://cn.bing.com/search?q=水处理设备", "水处理设备 - 搜索"),
    ("https://m.sm.cn/s?q=水处理设备", "水处理设备"),
])
def test_wrappers_knowledge_news_and_qna_are_rejected_even_when_text_names_factory(url, title):
    html = WATER.replace("山东清源环保设备有限公司 - 水处理设备", title)
    assert qualify_page(hit(), html, url).company is None


def test_unknown_news_site_article_is_not_qualified_from_company_mentions():
    html = WATER.replace("<head>", '<head><meta property="og:type" content="article">')
    result = qualify_page(hit(), html, "https://industry-news.example/article/123")
    assert result.company is None
    assert "文章" in result.reason


def test_old_official_site_links_full_copyright_identity_to_explicit_title_alias():
    html = """<title>山东泰和科技股份有限公司(泰和科技) - 水处理药剂专业生产商</title>
    <div><h2>关于泰和</h2><p>泰和科技，是水处理药剂专业生产商，生产水处理剂、阻垢剂。</p></div>
    <div>销售热线：0632-5113088 地址：山东省枣庄市市中区十里泉东路1号 版权所有 山东泰和科技股份有限公司</div>"""
    result = qualify_page(hit("水处理厂家"), html, "http://old-factory.example/")
    assert result.company is not None, result.reason
    assert result.company.name == "山东泰和科技股份有限公司"
    assert result.company.phones == ["0632-5113088"]
    assert "水处理" in result.company.business_scope


def test_water_material_factory_client_sector_terms_do_not_count_as_extra_companies():
    html = """<title>巩义市鹏泽环保科技有限公司</title>
    <p>巩义市鹏泽环保科技有限公司是生产净水材料的综合性企业。本厂产品适用于水处理设备，是自来水公司、污水处理厂的理想产品。</p>
    <div class="product"><a>水处理药剂</a><a>水处理滤料</a><a>活性炭</a></div>"""
    result = qualify_page(hit("水处理厂家"), html, "https://water-material.example/")
    assert result.company is not None, result.reason
    assert "水处理药剂" in result.company.products
    # A chemical/filter-material maker does not become an equipment maker merely
    # because its materials are used in water-treatment equipment.
    assert qualify_page(hit("水处理设备厂家"), html, "https://water-material.example/").company is None


def test_company_cms_product_article_can_pass_with_specific_ownership_and_product_proof():
    html = """<title>工业水处理设备_山东清源环保设备有限公司</title><meta property="og:type" content="article">
    <h1>工业水处理设备</h1><p>山东清源环保设备有限公司专业生产水处理设备，主营产品：水处理设备、净水设备。</p>
    <div>销售电话：13800138000 地址：山东省青岛市城阳区工业园 版权所有 山东清源环保设备有限公司</div>"""
    result = qualify_page(hit(), html, "https://factory.example/product/1")
    assert result.company is not None, result.reason


def test_plain_article_with_time_and_hidden_owner_are_rejected():
    html = """<title>环保行业报道</title><article><time>2026-11-11</time><h1>工业水处理设备</h1>
    <p>山东清源环保设备有限公司是一家生产水处理设备的企业，主营产品：水处理设备。</p></article>"""
    assert qualify_page(hit(), html, "https://news.example/article/1").company is None
    html = '<title>水处理设备采购资料</title><div style="display:none"><h1>山东清源环保设备有限公司</h1><p>主营产品：水处理设备</p></div><p>登录查看详情</p>'
    assert qualify_page(hit(), html, "https://market.example/info").company is None
    html = '<title>水处理设备</title><div style="display:none"><p style="display:none">山东清源环保设备有限公司 主营水处理设备</p></div><p>请登录</p>'
    assert qualify_page(hit(), html, "https://market.example/info").company is None


def test_public_fax_is_not_mislabeled_as_phone():
    html = WATER.replace("联系电话：13800138000", "联系电话：13800138000 传真：0531-88889999")
    result = qualify_page(hit(), html, "https://water.example/")
    assert result.company is not None
    assert result.company.phones == ["13800138000"]


def test_generic_company_list_does_not_turn_platform_operator_into_factory():
    html = """<title>水处理设备厂家大全</title><h1>全国水处理设备厂家</h1>
    <div>供应商：山东清源环保设备有限公司 主营产品：水处理设备 电话13800138000</div>
    <div>供应商：江苏蓝海环保有限公司 主营产品：水处理设备 电话13900139000</div>
    <footer>版权所有：北京黄页信息技术有限公司 客服4001234567</footer>"""
    assert qualify_page(hit(), html, "https://market.example/list").company is None
    html = html.replace("水处理设备厂家大全", "水处理设备现货")
    result = qualify_page(hit(), html, "https://market.example/list")
    assert result.company is None
    assert "多个供应商" in result.reason


def test_wrong_business_even_if_keyword_in_footer_or_customer_news_is_rejected():
    html = """<title>青岛互联网络科技有限公司</title><h1>青岛互联网络科技有限公司</h1>
    <p>青岛互联网络科技有限公司专业从事网站建设、互联网广告推广。</p><p>主营业务：网站建设、软件开发</p>
    <aside>客户案例：水处理设备厂家</aside><footer>友情链接：水处理设备</footer>"""
    result = qualify_page(hit(), html, "https://web-agency.example/")
    assert result.company is None
    assert "不匹配" in result.reason
    # Product words in SEO titles or a client description cannot override the
    # actual agency's own business scope.
    html = html.replace("青岛互联网络科技有限公司</title>", "水处理设备厂家网站建设_青岛互联网络科技有限公司</title>")
    html = html.replace("主营业务：网站建设、软件开发", "主营业务：为水处理设备厂家提供网站建设、软件开发")
    assert qualify_page(hit(), html, "https://web-agency.example/").company is None


def test_competitor_recommendation_and_unlabeled_platform_footer_phones_are_not_attached():
    html = """<title>水处理设备</title><h1>水处理设备</h1><main>
    <p>供应商：山东清源环保设备有限公司</p><p>山东清源环保设备有限公司 企业类型：生产加工</p><p>主营产品：水处理设备</p>
    <div>联系人：王经理 手机：13800138000</div>
    <div class="related"><div>其他品牌推荐 联系电话：13900139000</div></div>
    </main><footer>联系我们 电话：400-100-2000</footer>"""
    result = qualify_page(hit(), html, "https://b2b.baidu.com/offer/123")
    assert result.company is not None, result.reason
    assert result.company.phones == ["13800138000"]


def test_multiple_supplier_labels_in_one_flat_block_are_not_admitted():
    html = """<title>水处理设备现货供应</title><h1>水处理设备现货供应</h1><p>
    供应商：山东清源环保设备有限公司；供应商：江苏蓝海环保有限公司；主营产品：水处理设备</p>"""
    assert qualify_page(hit(), html, "https://market.example/list").company is None


def test_encyclopedia_like_word_mentions_without_owner_business_are_rejected():
    html = """<title>水处理设备原理介绍</title><h1>什么是水处理设备</h1><p>水处理设备是通过过滤改变水质的设备。</p>
    <p>例如山东清源环保设备有限公司。</p><footer>北京知识网络有限公司 电话4001234567</footer>"""
    assert qualify_page(hit(), html, "https://knowledge.example/water").company is None


def test_structured_product_manufacturer_identity_address_and_phone_are_qualified():
    data = {"@context": "https://schema.org", "@type": "Product", "name": "反渗透水处理设备",
            "manufacturer": {"@type": "Organization", "name": "山东清源环保设备有限公司", "telephone": "+86 13800138000",
                             "address": {"addressRegion": "山东省", "addressLocality": "青岛市", "streetAddress": "城阳区创新路18号"}}}
    html = f'<title>工业水处理设备</title><script type="application/ld+json">{json.dumps(data, ensure_ascii=False)}</script><h1>反渗透水处理设备</h1>'
    result = qualify_page(hit(), html, "https://factory.example/product/1")
    assert result.company is not None, result.reason
    assert result.company.name == "山东清源环保设备有限公司"
    assert result.company.phones == ["13800138000"]
    assert result.company.region == "山东省青岛市"
    assert "反渗透水处理设备" in result.company.products


def test_structured_publisher_is_not_a_supplier():
    data = {"@type": "WebSite", "name": "水处理设备采购平台", "publisher": {"@type": "Organization", "name": "北京平台信息技术有限公司", "description": "水处理设备采购网站", "telephone": "4001002000"}}
    html = f'<title>水处理设备采购信息</title><script type="application/ld+json">{json.dumps(data, ensure_ascii=False)}</script><p>找水处理设备厂家，请先搜索供应商</p>'
    assert qualify_page(hit(), html, "https://market.example/search").company is None


def test_industry_keyword_is_insufficient_without_manufacturing_or_supply_relation():
    html = """<title>山东清源环保设备有限公司_水处理设备</title><h1>山东清源环保设备有限公司</h1>
    <p>水处理设备知识介绍、政策资讯、行业会议通知。</p>"""
    assert qualify_page(hit(), html, "https://factory.example/").company is None


def test_factory_prose_does_not_create_company_and_region_constraint_is_enforced():
    html = "<title>水处理设备</title><p>如果出厂前没有检查水处理设备，需要返厂维修。</p>"
    assert qualify_page(hit(), html, "https://industry.example/").company is None
    assert qualify_page(hit("山东水处理设备厂家"), WATER, "https://water.example/").company is not None
    assert qualify_page(hit("江苏水处理设备厂家"), WATER, "https://water.example/").company is None


def test_merging_preserves_admission_proof_and_structured_details():
    verified = qualify_page(hit(), WATER, "https://water.example/").company
    old = Company(name=verified.name, name_confirmed=True, urls=["https://other.example/"])
    merged = merge_companies([old, verified])
    assert len(merged) == 1
    company = merged[0]
    assert company.page_verified
    assert company.verification_url == verified.verification_url
    assert company.products == verified.products
    assert company.business_scope == verified.business_scope
    assert company.region == verified.region
    assert company.address == verified.address
    assert company.admission_reasons == verified.admission_reasons
    assert any(e.kind == "详情页核验" and e.url == company.verification_url for e in company.evidence)
    assert not old.page_verified  # merge leaves input snapshots untouched
