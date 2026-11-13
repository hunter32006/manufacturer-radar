"""Run the production pipeline and report real coverage without asserting availability."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manufacturer_finder.models import SearchOptions
from manufacturer_finder.pipeline import SearchRunner

parser = argparse.ArgumentParser()
parser.add_argument("--keyword", default="不锈钢水箱厂家")
parser.add_argument("--budget", type=int, default=60)
parser.add_argument("--output", default="live_smoke_report.json")
args = parser.parse_args()
events = []
start = time.monotonic()
result = SearchRunner(SearchOptions(query=args.keyword, budget_seconds=args.budget), callback=events.append).run()
payload = {
    "started_at": result.started_at,
    "finished_at": result.finished_at,
    "elapsed_seconds": round(time.monotonic() - start, 2),
    "query": result.query,
    "engine_status": result.engine_status,
    "companies": len(result.companies),
    "candidates": len(result.candidates),
    "qualification_stats": result.qualification_stats,
    "collection_log": result.collection_log,
    "named": sum(c.name_confirmed for c in result.companies),
    "with_phone": sum(bool(c.phones) for c in result.companies),
    "negative_pending": sum(c.negative_status == "发现待核实线索" for c in result.companies),
    "negative_checked": sum(c.negative_status in {"发现待核实线索", "本轮未发现"} for c in result.companies),
    "cancelled": result.cancelled,
    "timed_out": result.timed_out,
    "all_hits_with_urls": all(bool(h.url and h.search_url and 1 <= h.page <= 3) for c in result.companies for h in c.hits),
    "sample": [{"name": c.name, "url": c.verification_url, "products": c.products, "business_scope": c.business_scope, "region": c.region, "phones": c.phones, "admission_reasons": c.admission_reasons, "score": c.score, "page_verified": c.page_verified} for c in result.companies],
    "log": [event.get("message", "") for event in events if event.get("type") in {"log", "phase"}],
}
Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({key: value for key, value in payload.items() if key not in {"sample", "log", "collection_log"}}, ensure_ascii=False, indent=2))
