import io
import json

from openpyxl import Workbook, load_workbook
from pypdf import PdfWriter
import pytest

from manufacturer_finder import ingestion


NAME = "河南清泉水处理设备有限公司"


@pytest.mark.parametrize("encoding", ["utf-8-sig", "gb18030"])
def test_csv_preserves_explicit_record_contacts_and_source(tmp_path, encoding):
    path = tmp_path / "名单.csv"
    path.write_bytes(("厂家名称,厂家网址,电话,主营,地区\n" + NAME + ",https://example.com,13800138000,水处理设备,河南\n").encode(encoding))
    leads = ingestion.read_leads(path)
    assert len(leads) == 1
    assert leads[0].name == NAME and leads[0].phones == ["13800138000"]
    assert leads[0].products == ["水处理设备"] and leads[0].address == "河南"
    assert leads[0].source_url == str(path.resolve())
    assert not hasattr(leads[0], "page_verified")


def test_url_only_and_whole_company_name_but_no_phone_only():
    leads = ingestion.read_text_leads(NAME + "\nhttps://example.com/about\n13800138000\n水处理厂家哪家好\n立即联系\nhttp://127.0.0.1/secret\nhttp://localhost/test")
    assert [(lead.name, lead.url) for lead in leads] == [(NAME, ""), ("", "https://example.com/about")]


def test_structured_unsafe_url_is_dropped_but_name_resolvable(tmp_path):
    path = tmp_path / "leads.json"
    path.write_text(json.dumps([{"公司名称": NAME, "官网": "http://192.168.1.1/", "电话": "13800138000"},
                               {"官网": "file:///C:/secrets", "电话": "13900139000"}]), encoding="utf-8")
    leads = ingestion.read_leads(path)
    assert len(leads) == 1 and leads[0].url == "" and leads[0].name == NAME


def test_json_objects_list_wrapper_and_english_name(tmp_path):
    path = tmp_path / "leads.json"
    path.write_text(json.dumps({"companies": [{"name": "Acme Water Equipment Ltd.", "website": "www.example.com", "phones": ["13800138000"]}]}), encoding="utf-8")
    lead = ingestion.read_leads(path)[0]
    assert lead.name == "Acme Water Equipment Ltd." and lead.url == "https://www.example.com"


def test_xlsx_reads_values_does_not_run_formula(tmp_path):
    path = tmp_path / "leads.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["厂家名称", "厂家网址", "电话"])
    sheet.append([NAME, "https://example.com/", 13800138000])
    sheet.append(['=HYPERLINK("https://evil.com", "山东恶意机械有限公司")', "", ""])
    workbook.save(path)
    leads = ingestion.read_leads(path)
    assert len(leads) == 1 and leads[0].name == NAME and leads[0].phones == ["13800138000"]


def test_html_table_maps_columns_and_resolves_link():
    html = "<table><tr><th>企业名称</th><th>官网</th><th>电话</th></tr>" + f'<tr><td>{NAME}</td><td><a href="/detail/12">官网</a></td><td>13800138000</td></tr></table>'
    lead = ingestion.read_text_leads(html, source_url="https://example.com/list")[0]
    assert lead.name == NAME and lead.url == "https://example.com/detail/12" and lead.phones == ["13800138000"]


def test_html_footers_and_multi_company_containers_cannot_own_phone():
    html = f'''<html><body><div class="cards"><article><h3><a href="https://factory.com">{NAME}</a></h3><p>电话：13800138000</p></article>
    <article><h3>山东蓝海机械有限公司</h3></article></div><footer>网站客服：13900139000</footer></body></html>'''
    leads = ingestion.read_text_leads(html, source_url="https://directory.com/list")
    assert all("13900139000" not in lead.phones for lead in leads)
    assert next(lead for lead in leads if lead.name == NAME).phones == ["13800138000"]
    assert next(lead for lead in leads if "山东" in lead.name).phones == []


@pytest.mark.parametrize("footer", ['<div class="site-footer">电话：13900139000</div>',
                                     '<div>客服电话：13900139000</div>',
                                     '<div>电话：13900139000</div>'])
def test_single_company_parent_cannot_absorb_publisher_sibling_phone(footer):
    html = f'<div><div><a href="https://factory.com">{NAME}</a></div>{footer}</div>'
    leads = ingestion.read_text_leads(html, source_url="https://catalog.com/list")
    assert leads and all(not lead.phones for lead in leads)


def test_explicit_company_card_keeps_own_phone_and_source():
    html = f'<div><div class="company-card"><div>{NAME}</div><p>电话：13800138000</p></div><div>电话：13900139000</div></div>'
    leads = ingestion.read_text_leads(html, source_url="https://catalog.com/list")
    assert len(leads) == 1 and leads[0].phones == ["13800138000"]
    assert leads[0].source_url == "https://catalog.com/list"


def test_two_names_one_line_does_not_assign_one_phone_to_both():
    leads = ingestion.read_text_leads(NAME + " 山东蓝海机械有限公司 电话：13800138000")
    assert len(leads) == 2 and all(not seed.phones and not seed.url for seed in leads)


def test_labelled_multiline_records_preserve_ownership():
    text = f"公司名称：{NAME}\n官网：https://example.com/\n电话：13800138000\n主营：水处理设备\n\n公司名称：山东蓝海机械有限公司\n电话：13900139000\n"
    leads = ingestion.read_text_leads(text)
    assert len(leads) == 2
    assert leads[0].url == "https://example.com/" and leads[0].phones == ["13800138000"]
    assert leads[1].phones == ["13900139000"] and leads[1].url == ""


def test_limits_and_deduplication(tmp_path):
    names = [f"河南第{number}机械有限公司" for number in range(1100)]
    leads = ingestion.read_text_leads("\n".join(names))
    assert len(leads) == 100
    assert len(ingestion.read_text_leads(NAME + "\n" + NAME)) == 1
    too_large = tmp_path / "large.txt"
    with too_large.open("wb") as handle:
        handle.truncate(ingestion.MAX_FILE_BYTES + 1)
    with pytest.raises(ValueError, match="20 MB"):
        ingestion.read_leads(too_large)


def test_image_pdf_has_clear_ocr_message():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(ValueError, match="OCR"):
        ingestion.parse_pdf_bytes(buffer.getvalue())


def test_pdf_parser_extracts_no_embedded_links(monkeypatch):
    class Page:
        def extract_text(self):
            return NAME + " 电话：13800138000 https://example.com/"

    class Reader:
        is_encrypted = False
        pages = [Page()]

        def __init__(self, data):
            pass

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    leads = ingestion.parse_pdf_bytes(b"%PDF-1.7 mocked", source_url="https://directory.com/catalog.pdf")
    assert leads[0].name == NAME and leads[0].source_url.endswith("catalog.pdf")


def test_template_has_expected_headers(tmp_path):
    path = tmp_path / "template.xlsx"
    ingestion.write_import_template(path)
    workbook = load_workbook(path, read_only=True)
    assert list(next(workbook.active.values)) == ["厂家名称", "厂家网址", "电话", "主营", "地区", "来源链接"]
    workbook.close()


@pytest.mark.parametrize("value", ["not pdf", "", "<script>ignored()</script>"])
def test_invalid_pdf_rejected(value):
    with pytest.raises(ValueError, match="PDF"):
        ingestion.parse_pdf_bytes(value.encode())
