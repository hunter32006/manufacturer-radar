"""Regressions from the live B2B discovery run, 2026-11-11."""
import pytest

from manufacturer_finder.extraction import _phone_matches, extract_phones
from manufacturer_finder.models import SearchHit
from manufacturer_finder.qualification import qualify_page


URL = "https://info.b2b168.com/s168-290744999.html"


def hit(query="水处理"):
    return SearchHit("b2b168", query, 1, "摘要不参与准入", URL)


def exhibition_page():
    # The real page's own profile and offer differ from the manufacturing
    # industry advert pasted below the offer. Retain those material assertions.
    return '''<title>广东国际水处理展会刊-亲戚物流发展博览会会刊 - 八方资源网</title>
    <h1>广东国际水处理展会刊-亲戚物流发展博览会会刊</h1>
    <div class="company-name">广州时光展览有限公司</div>
    <div>广州时光展览有限公司 会员12年 所在区域：广东广州海珠区
    经营性质：私有企业 企业类型：商业服务 注册地：广州
    主营产品：光亚展展位申请,光亚展摊位申请,光亚展展位预定 公司介绍 供应信息 联系方式</div>
    <article>产品详细信息 广州时光展览有限公司 广东水处理展会刊
    如需购买参展商会刊联系李雪经理，我司提供全国各大展会会刊。
    行业新经济新业态使纺机产业加速升级。行业将重点围绕智能化、绿色化发展，
    大力发展兼容性强、节能降耗、清洁生产的各类纺织设备产品，助力产业链竞争力。</article>
    <div>商家联系方式 广州时光展览有限公司 电话：013002028857 手机：13002028857
    地址：广东广州海珠区广州大道中900号金穗大厦5楼</div>'''


def test_exhibitor_catalogue_seller_is_not_water_manufacturer():
    result = qualify_page(hit(), exhibition_page(), URL)
    assert result.company is None
    assert "会展服务" in result.reason or "会刊" in result.reason


def test_catalogue_ad_industry_manufacturing_claim_is_not_own_production():
    page = exhibition_page().replace("清洁生产", "专业生产制造水处理设备")
    assert qualify_page(hit(), page, URL).company is None


def test_real_water_manufacturer_attending_exhibition_is_admitted():
    name = "河南清泉水处理设备有限公司"
    page = f'''<title>{name}参加国际水处理展览会</title><h1>{name}</h1>
    <p>公司名称：{name}</p><p>{name}专业生产制造水处理设备、反渗透净水设备。</p>
    <p>主营产品：水处理设备、反渗透净水设备</p>
    <p>{name}今年参加水处理展览会，参展商会刊介绍了公司的新产品。</p>
    <p>{name} 电话：0130-02028857 地址：河南省郑州市高新区工业园18号</p>'''
    result = qualify_page(hit(), page, "https://factory.example/about")
    assert result.company, result.reason
    assert result.company.phones == ["13002028857"]
    assert any("0130-02028857" in evidence.excerpt for evidence in result.company.evidence if evidence.kind == "电话")


@pytest.mark.parametrize("raw", ["0130-02028857", "013002028857", "+86 0130-02028857"])
def test_mobile_with_extra_leading_zero_is_not_landline(raw):
    assert extract_phones("电话：" + raw) == ["13002028857"]
    phone, start, end = _phone_matches("电话：" + raw)[0]
    assert phone == "13002028857"
    assert ("电话：" + raw)[start:end] == raw


def test_normal_landlines_unchanged_and_mobile_deduplicated():
    assert extract_phones("电话：0130-02028857 手机：13002028857 座机：020-34076446 0531-85808866") == ["13002028857", "020-34076446", "0531-85808866"]
