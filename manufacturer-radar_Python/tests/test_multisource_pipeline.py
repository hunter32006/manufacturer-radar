import threading
from unittest.mock import patch

from manufacturer_finder.models import SearchHit, SearchOptions, RunResult
from manufacturer_finder.pipeline import SearchRunner
from manufacturer_finder.source_types import DiscoveryBatch, LeadSeed
from manufacturer_finder.scoring import is_page_qualified, score_company

NAME = '山东清源环保设备有限公司'
URL = 'https://factory.example/'
HTML = f'<title>{NAME}</title><h1>{NAME}</h1><p>{NAME}专业生产水处理设备，拥有生产车间。主营产品：水处理设备。</p>'


def options(**kwargs):
    return SearchOptions(query='水处理', engines=[], enrich=False, check_negative=False, **kwargs)


def test_directory_seed_and_phone_require_actual_matching_factory_page():
    seed = LeadSeed(name=NAME, url=URL, phones=['0531-85808866'], source='amap',
        source_url='https://ditu.amap.com/place/example', excerpt=NAME + ' 水处理设备')
    with patch('manufacturer_finder.network.fetch_text', return_value=(HTML, URL)) as fetch:
        result = SearchRunner(options(), seed_hits=[], seed_leads=[seed]).run()
    assert fetch.call_count == 1
    assert len(result.companies) == 1
    company = result.companies[0]
    assert is_page_qualified(company)
    assert company.phones == ['0531-85808866']
    assert any(e.kind == '发现来源' and e.url == seed.source_url for e in company.evidence)
    assert any(e.kind == '外部公开电话' for e in company.evidence)
    assert any('未取得已核验页面' in line for line in company.score_reasons)
    assert result.discovery_seeds[0]['source'] == 'amap'
    assert RunResult.from_dict(result.to_dict()).discovery_seeds == result.discovery_seeds


def test_imported_list_is_never_admitted_by_itself_or_wrong_company():
    seed = LeadSeed(name='山东另一环保设备有限公司', url=URL, source='import')
    with patch('manufacturer_finder.network.fetch_text', return_value=(HTML, URL)):
        result = SearchRunner(options(), seed_hits=[], seed_leads=[seed]).run()
    assert not result.companies
    assert result.qualification_stats['rejected'] == 1


def test_read_browser_content_qualifies_without_reopening_http():
    hit = SearchHit('browser', '水处理', 1, NAME, URL)
    with patch('manufacturer_finder.network.fetch_text', side_effect=AssertionError('must use captured content')):
        result = SearchRunner(options(), seed_hits=[hit], captured_pages={URL:HTML}).run()
    assert len(result.companies) == 1


def test_additional_source_batch_is_coordinated_and_status_persisted():
    def discovery(opts, cancel, deadline, emit=None):
        emit({'source':'b2b168', 'status':'running', 'pages':0})
        return DiscoveryBatch(hits=[SearchHit('b2b168', '水处理', 1, NAME, URL)],
            statuses={'b2b168': {'status':'ok', 'pages':3, 'hits':1, 'message':'已读公开目录'}})
    with patch('manufacturer_finder.network.fetch_text', return_value=(HTML, URL)):
        result = SearchRunner(options(discovery_channels=['b2b']), discovery_provider=discovery).run()
    assert len(result.companies) == 1
    assert result.source_status['b2b168']['pages'] == 3
    assert result.engine_status == {}


def test_named_resolution_query_preserved_and_wrong_company_rejected():
    class Provider:
        def fetch(self, engine, query, page, cancel, deadline, timeout):
            assert query == f'"{NAME}" 官网' and page == 1
            return [SearchHit(engine,query,page,NAME,URL)]
    opts=options()
    opts.engines=['sogou']
    with patch('manufacturer_finder.network.fetch_text', return_value=(HTML,URL)):
        result=SearchRunner(opts,provider=Provider(),seed_hits=[],seed_leads=[LeadSeed(name=NAME,source='catalog',source_url='https://catalog.example/list')]).run()
    assert len(result.companies)==1
    hit=result.companies[0].hits[0]
    assert hit.lookup_query==f'"{NAME}" 官网' and hit.discovery_source=='catalog'
    assert hit.query=='水处理'


def test_non_search_channels_do_not_award_multiple_search_engine_points():
    hits = [SearchHit(source,'水处理',1,NAME,URL) for source in ('b2b168','gys')]
    result=SearchRunner(options(),seed_hits=hits,captured_pages={URL:HTML}).run()
    company=result.companies[0]
    assert not any(line.startswith('+3：') for line in company.score_reasons)


def test_wrong_name_discovery_provenance_is_not_attached_to_matching_url():
    hits=[SearchHit('gys','水处理',1,NAME,URL,company_hint=NAME,source_url='https://catalog.example/correct'),
        SearchHit('b2b168','水处理',1,'另一公司',URL,company_hint='山东另一设备有限公司',source_url='https://catalog.example/wrong')]
    result=SearchRunner(options(),seed_hits=hits,captured_pages={URL:HTML}).run()
    assert len(result.companies)==1
    company=result.companies[0]
    assert len(company.hits)==1 and company.hits[0].engine=='gys'
    assert not any(e.url=='https://catalog.example/wrong' for e in company.evidence)


def test_map_callback_style_works_in_combined_coordinator():
    def maps(opts,cancel,deadline,emit=None):
        emit('source',source='amap',status='configuration_required',pages=0)
        return DiscoveryBatch(statuses={'amap':{'status':'configuration_required','pages':0}})
    result=SearchRunner(options(discovery_channels=['maps']),map_provider=maps).run()
    assert result.source_status['amap']['status']=='configuration_required'
    assert not result.companies
