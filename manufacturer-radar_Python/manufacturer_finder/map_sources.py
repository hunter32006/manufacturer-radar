"""Opt-in official map APIs. Returned POIs are unverified discovery leads.

Docs checked 2026-11-11:
https://lbs.amap.com/api/webservice/guide/api/search/
https://lbsyun.baidu.com/docs/webapi?title=placev3%2Fguide%2Fwebservice-placeapiV3%2FinterfaceDocumentV2
The operator's own account must authorize the intended retrieval, storage and
export. Having an API key alone is not such authorization; quotas/fees vary.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from .extraction import extract_phones
from .network import DeadlineExceeded, FetchCancelled, HTTPFetchError, check_control, fetch_text, safe_url
from .source_types import DiscoveryBatch, LeadSeed, SOURCE_LABELS


def _clean(value, key: str, limit: int = 500) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    text = str(value)
    if key:
        text = text.replace(key, "[密钥已隐藏]").replace(quote(key, safe=""), "[密钥已隐藏]")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text).strip()[:limit]


def _public_url(value, key: str, allowed_hosts=()) -> str:
    if not isinstance(value, str) or not value or (key and (key in value or quote(key, safe="") in value)):
        return ""
    if not value.startswith(("https://", "http://")) and re.match(r"(?:www\.)?[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/|$)", value, re.I):
        value = "https://" + value
    if not safe_url(value):
        return ""
    parts = urlsplit(value)
    if allowed_hosts and (parts.hostname or "").lower() not in allowed_hosts:
        return ""
    if any(name.casefold() in {"key", "ak", "apikey", "api_key", "token", "access_token", "sn", "sig"}
           for name, _value in parse_qsl(parts.query)):
        return ""
    return value


def _seed(record: dict, provider: str, key: str) -> LeadSeed | None:
    name = _clean(record.get("name"), key, 120)
    if not name:
        return None
    if provider == "amap":
        identifier = _clean(record.get("id"), key, 80)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
            return None
        source_url = "https://www.amap.com/detail/" + quote(identifier, safe="")
        website = _public_url(record.get("website"), key)
        phone = _clean(record.get("tel"), key)
        detail = record
        address = " ".join(dict.fromkeys(_clean(record.get(field), key, 200) for field in
                                         ("pname", "cityname", "adname", "address") if _clean(record.get(field), key, 200)))
    else:
        identifier = _clean(record.get("uid"), key, 80)
        detail = record.get("detail_info") if isinstance(record.get("detail_info"), dict) else {}
        source_url = _public_url(detail.get("detail_url"), key, ("map.baidu.com", "www.map.baidu.com"))
        if not source_url:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
                return None
            source_url = "https://map.baidu.com/poi/" + quote(identifier, safe="")
        website = _public_url(record.get("website") or detail.get("website"), key)
        phone = _clean(record.get("telephone"), key)
        address = " ".join(dict.fromkeys(_clean(record.get(field), key, 200) for field in
                                         ("province", "city", "area", "address") if _clean(record.get(field), key, 200)))
    category = _clean(detail.get("type") or detail.get("tag") or record.get("type"), key, 200)
    proof = f"地图公开商户线索：{name}；地址：{address}；公开电话：{phone}；分类：{category}。需访问企业页面核实生产身份。"
    return LeadSeed(name=name, url=website, phones=extract_phones(phone), address=address[:300],
                    source=provider, source_url=source_url, excerpt=proof[:800])


def _error_state(provider: str, payload: dict) -> tuple[str, str]:
    # Never display an API's free-form error message; it may echo credentials.
    code = str(payload.get("infocode" if provider == "amap" else "status", ""))
    if provider == "amap":
        if code in {"10003", "10004", "10010", "10019", "10020", "10021", "10044", "10045"}:
            return "quota_limited", "地图接口配额或频率受限，请在自己的平台账户查看额度"
        if code in {"10001", "10002", "10005", "10006", "10007", "10008", "10009", "10011", "10012", "10013", "10015", "10016"}:
            return "auth_required", "地图接口密钥、权限或白名单校验未通过，请在平台账户核对"
    else:
        if code in {"4", "301", "302", "401", "402"}:
            return "quota_limited", "地图接口配额或频率受限，请在自己的平台账户查看额度"
        if code in {"3", "5", "101", "102", "200", "201", "202", "203", "210", "211", "220", "240", "250", "251", "252", "261", "502"}:
            return "auth_required", "地图接口密钥、授权或白名单校验未通过，请在平台账户核对"
    return "error", "地图接口未成功返回，请核对参数和平台服务状态"


def discover_maps(options, cancel, deadline, emit=None) -> DiscoveryBatch:
    batch = DiscoveryBatch()
    provider = getattr(options, "map_provider", "amap")
    if provider not in {"amap", "baidu_map"}:
        provider = "amap"
    label = SOURCE_LABELS[provider]
    state = {"status": "pending", "state": "pending", "label": "待开始", "pages": 0, "hits": 0, "seeds": 0, "message": ""}
    batch.statuses[provider] = state

    def update(code, message):
        state["hits"] = state["seeds"] = len(batch.seeds)
        state.update(state=code, status=code, label={"ok": "完成", "empty": "无结果", "configuration_required": "待配置",
                     "auth_required": "授权受限", "quota_limited": "额度受限", "error": "失败",
                     "cancelled": "已取消", "timeout": "超时", "running": "采集中"}.get(code, code), message=message)
        if emit:
            emit("source", source=provider, **state)

    key = str(getattr(options, "map_key", "") or "").strip()
    city = str(getattr(options, "map_city", "") or "").strip()
    if not key or not city or not getattr(options, "map_authorized", False):
        update("configuration_required", "需填写自己的 Web 服务密钥与城市，并确认已获得本次采集、存储和导出用途的授权")
        return batch
    if len(key) > 200 or len(city) > 80 or any(ord(char) < 32 for char in key):
        update("configuration_required", "地图参数格式不正确，请重新填写密钥和城市")
        return batch
    pages = min(3, max(1, int(getattr(options, "pages", 3))))
    seen = set()
    try:
        check_control(cancel, deadline)
        update("running", f"正在通过{label}查找公开企业线索，每页最多 20 条、最多 3 页")
        for page in range(pages):
            check_control(cancel, deadline)
            if provider == "amap":
                endpoint = "https://restapi.amap.com/v3/place/text"
                params = {"key": key, "keywords": options.query, "city": city, "citylimit": "true",
                          "offset": 20, "page": page + 1, "extensions": "all", "output": "JSON"}
            else:
                endpoint = "https://api.map.baidu.com/place/v2/search"
                params = {"ak": key, "query": options.query, "region": city, "city_limit": "true",
                          "page_size": 20, "page_num": page, "scope": 2, "output": "json"}
            # fetch_text applies public-IP validation, bounded size and control.
            # API request URLs never enter returned evidence, logs or histories.
            text, _ = fetch_text(endpoint + "?" + urlencode(params), cancel=cancel, deadline=deadline,
                                 timeout=getattr(options, "request_timeout", 8), max_bytes=1_000_000)
            check_control(cancel, deadline)
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("invalid map response")
            success = str(payload.get("status")) == ("1" if provider == "amap" else "0")
            if not success:
                code, message = _error_state(provider, payload)
                update(code, message)
                return batch
            state["pages"] += 1
            records = payload.get("pois" if provider == "amap" else "results", [])
            if not isinstance(records, list):
                raise ValueError("invalid map result list")
            for record in records[:20]:
                check_control(cancel, deadline)
                if not isinstance(record, dict):
                    continue
                seed = _seed(record, provider, key)
                if seed and seed.source_url not in seen:
                    seen.add(seed.source_url)
                    batch.seeds.append(seed)
            state["hits"] = len(batch.seeds)
            state["seeds"] = len(batch.seeds)
            if len(records) < 20:
                break
        update("ok" if batch.seeds else "empty", f"获得 {len(batch.seeds)} 条地图线索；需进入企业页面识别厂家，地图评分不作为厂家评分")
        batch.notes.append(f"{label}仅使用用户自己已授权的 API；配额、费用及允许的数据用途以账户授权为准。")
    except FetchCancelled:
        update("cancelled", "地图线索采集已取消")
    except DeadlineExceeded:
        update("timeout", "地图线索采集达到本轮时间上限")
    except HTTPFetchError as exc:
        if exc.status_code == 429:
            update("quota_limited", "地图接口频率或配额受限，请稍后重试或在平台账户查看额度")
        elif exc.status_code in {401, 403}:
            update("auth_required", "地图接口访问授权受限，请在平台账户核对密钥权限")
        else:
            update("error", "地图服务暂时未正常响应，请稍后重试")
    except Exception:
        # Network exceptions may contain the original URL including key/ak.
        update("error", "地图接口访问或解析失败；请检查网络及平台服务状态")
    return batch
