from dataclasses import asdict
import json
from threading import Event
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from manufacturer_finder import map_sources
from manufacturer_finder.models import SearchOptions
from manufacturer_finder.network import FetchError


SECRET = "private-unit-test-key-9283"


def options(**changes):
    params = dict(query="水处理", map_key=SECRET, map_city="郑州", map_authorized=True)
    params.update(changes)
    return SearchOptions(**params)


@pytest.mark.parametrize("changes", [{"map_key": ""}, {"map_city": ""}, {"map_authorized": False}])
def test_configuration_gate_never_makes_request(monkeypatch, changes):
    def unexpected(*args, **kwargs):
        raise AssertionError("must not fetch")
    monkeypatch.setattr(map_sources, "fetch_text", unexpected)
    batch = map_sources.discover_maps(options(**changes), Event(), time.monotonic() + 10)
    assert batch.statuses["amap"]["state"] == "configuration_required" and batch.seeds == []


@pytest.mark.parametrize("provider,page_field,start", [("amap", "page", 1), ("baidu_map", "page_num", 0)])
def test_three_page_cap_phones_and_public_proof_without_key(monkeypatch, provider, page_field, start):
    calls = []
    def fetch(url, **kwargs):
        params = parse_qs(urlsplit(url).query)
        calls.append(params)
        page = int(params[page_field][0])
        if provider == "amap":
            payload = {"status": "1", "pois": [{"id": f"B{page:02d}{index:03d}", "name": f"河南第{page}{index}水处理设备有限公司",
                        "website": "https://example.com/", "tel": "13800138000", "address": "工业园区", "cityname": "郑州"} for index in range(20)]}
        else:
            payload = {"status": 0, "results": [{"uid": f"b{page:02d}{index:03d}", "name": f"河南第{page}{index}水处理设备有限公司",
                        "telephone": "0371-12345678", "address": "工业园区"} for index in range(20)]}
        assert kwargs["max_bytes"] <= 1_000_000
        return json.dumps(payload), url
    monkeypatch.setattr(map_sources, "fetch_text", fetch)
    opts = options(map_provider=provider)
    opts.pages = 20
    batch = map_sources.discover_maps(opts, Event(), time.monotonic() + 10)
    assert [int(call[page_field][0]) for call in calls] == [start, start + 1, start + 2]
    assert len(batch.seeds) == 60 and batch.statuses[provider]["pages"] == 3
    assert batch.seeds[0].phones
    assert SECRET not in json.dumps(asdict(batch))
    assert all(seed.source_url.startswith("https://") and not hasattr(seed, "score") for seed in batch.seeds)


def test_map_source_is_evidence_not_immediate_factory(monkeypatch):
    payload = {"status": "1", "pois": [{"id": "B01234", "name": "水处理产业园", "website": "http://127.0.0.1/", "tel": "13800138000"}]}
    monkeypatch.setattr(map_sources, "fetch_text", lambda *a, **kw: (json.dumps(payload), ""))
    batch = map_sources.discover_maps(options(), Event(), time.monotonic() + 10)
    assert batch.hits == [] and len(batch.seeds) == 1 and batch.seeds[0].url == ""


@pytest.mark.parametrize("provider,payload,state", [
    ("amap", {"status": "0", "infocode": "10003", "info": SECRET}, "quota_limited"),
    ("amap", {"status": "0", "infocode": "10001", "info": SECRET}, "auth_required"),
    ("baidu_map", {"status": 302, "message": SECRET}, "quota_limited"),
    ("baidu_map", {"status": 210, "message": SECRET}, "auth_required"),
])
def test_api_errors_sanitized(monkeypatch, provider, payload, state):
    monkeypatch.setattr(map_sources, "fetch_text", lambda *a, **kw: (json.dumps(payload), ""))
    batch = map_sources.discover_maps(options(map_provider=provider), Event(), time.monotonic() + 10)
    assert batch.statuses[provider]["state"] == state
    assert SECRET not in json.dumps(asdict(batch))


def test_network_error_does_not_leak_request_url(monkeypatch):
    def fail(*args, **kwargs):
        raise FetchError("could not connect " + args[0])
    monkeypatch.setattr(map_sources, "fetch_text", fail)
    batch = map_sources.discover_maps(options(), Event(), time.monotonic() + 10)
    assert batch.statuses["amap"]["state"] == "error"
    assert SECRET not in json.dumps(asdict(batch))


def test_cancellation_and_deadline_stop_before_request(monkeypatch):
    monkeypatch.setattr(map_sources, "fetch_text", lambda *a, **kw: pytest.fail("must not fetch"))
    cancel = Event()
    cancel.set()
    assert map_sources.discover_maps(options(), cancel, time.monotonic() + 10).statuses["amap"]["state"] == "cancelled"
    assert map_sources.discover_maps(options(), Event(), time.monotonic() - 1).statuses["amap"]["state"] == "timeout"


def test_no_response_echo_can_put_secret_in_evidence(monkeypatch):
    payload = {"status": "1", "pois": [{"id": "B123", "name": SECRET + "有限公司", "website": "https://example.com?key=" + SECRET,
               "tel": SECRET, "address": SECRET}]}
    monkeypatch.setattr(map_sources, "fetch_text", lambda *a, **kw: (json.dumps(payload), ""))
    batch = map_sources.discover_maps(options(), Event(), time.monotonic() + 10)
    assert SECRET not in json.dumps(asdict(batch))
