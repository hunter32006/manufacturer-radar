import pytest
from manufacturer_finder.extraction import _names, extract_phones
from manufacturer_finder.models import SearchHit
from manufacturer_finder.qualification import qualify_page


def hit(query="水处理 厂家"):
    return SearchHit("sogou", query, 1, "搜索摘要不作为准入依据", "https://factory.example/", search_url="https://search.example/")


@pytest.mark.parametrize("description", [
    "山东清源贸易有限公司专业销售、经销水处理设备。主营产品：水处理设备。经营模式：贸易型。",
    "山东清源贸易有限公司主营水处理设备维修、保养及售后服务。主营产品：水处理设备维修服务。",
    "山东清源贸易有限公司经营范围：机械设备销售、灯具批发及零售、进出口。主营产品：水处理设备。",
])
def test_dealers_and_service_companies_are_not_manufacturers(description):
    html = "<title>山东清源贸易有限公司</title><h1>山东清源贸易有限公司</h1><p>" + description + "</p>"
    assert qualify_page(hit(), html, "https://factory.example/").company is None


def test_recommended_product_heading_does_not_prove_dealer_production():
    html = """<title>山东清源贸易有限公司_水处理设备</title><h1>山东清源贸易有限公司</h1>
    <p>山东清源贸易有限公司经营范围：机械设备销售、灯具批发及零售。主营产品：水处理设备。</p>
    <div class="product-cards"><h3>专业生产水处理设备</h3></div>"""
    assert qualify_page(hit(), html, "https://market.example/offer/1").company is None


@pytest.mark.parametrize("url", ["https://www.1688.com/zhuti/-C8ABB6B9B8AFBBFA.html", "https://cn.trustexporter.com/baojiaaretrwc-pn10.htm", "http://china.makepolo.com/list/spc5241/", "https://www.b2b168.com/k-shuichuli/"])
def test_platform_topic_and_quote_lists_are_not_single_manufacturers(url):
    html = """<title>水处理设备</title><h1>山东清源环保有限公司</h1>
    <p>山东清源环保有限公司专业生产水处理设备，主营产品：水处理设备。</p>"""
    assert qualify_page(hit(), html, url).company is None


def test_full_factory_company_suffix_products_location_and_dummy_phone():
    name = "济南市水处理设备厂有限公司"
    assert _names(name, allow_generic_factory=True) == [name]
    assert extract_phones("联系电话：0000-00000000，销售：0531-85808866") == ["0531-85808866"]
    html = f"""<title>{name}</title><h1>{name}</h1><p>{name}，始建于1984年，是专业生产制造水处理设备、压力容器、环保设备的厂家。</p>
    <p>主营产品：水处理设备、压力容器、环保设备</p><p>{name} 所在区域：山东省济南市 企业类型：生产加工</p>
    <p>公司地址：山东省济南市天桥区演示工业园 联系电话：0000-00000000，销售电话：0531-85808866</p>"""
    result = qualify_page(hit(), html, "https://factory.example/")
    assert result.company, result.reason
    c = result.company
    assert c.name == name
    assert set(c.products[:3]) == {"水处理设备", "压力容器", "环保设备"}
    assert not any(word in "、".join(c.products) for word in ["始建", "设计", "1984", "注册资金"])
    assert c.region == "山东省济南市"
    assert "0000-00000000" not in c.phones


def test_location_navigation_and_delivery_prose_are_not_regions():
    name = "山东清源环保有限公司"
    html = f"""<title>{name}</title><h1>{name}</h1><p>{name}专业生产水处理设备，主营产品：水处理设备。</p>
    <div>地区：和交货时间向商家确认。页面同时整理相关资讯</div>
    <div>所在地：潍坊潍城区 潍坊寒亭区 潍坊坊子区 潍坊奎文区 潍坊临朐县 潍坊昌乐县</div>
    <p>{name} 所在区域：山东潍坊青州市 经营性质：私营企业</p>"""
    c = qualify_page(hit(), html, "https://factory.example/").company
    assert c and c.region == "山东潍坊青州市"
