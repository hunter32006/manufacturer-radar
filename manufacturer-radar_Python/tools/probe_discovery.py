"""Manual public-adapter probe; no login, browser challenge, or hidden API."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import sys
import time
import threading
from dataclasses import asdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup
from manufacturer_finder.network import fetch_text

URLS = {
    "madeinchina_home": "https://cn.made-in-china.com/",
    "b2b168_home": "https://www.b2b168.com/",
    "gys_home": "https://www.gys.cn/",
    "hc360_home": "https://www.hc360.com/",
    "1688_home": "https://www.1688.com/",
    "aicaigou_home": "https://b2b.baidu.com/",
}

def probe(name, url):
    try:
        html, final = fetch_text(url, deadline=time.monotonic() + 22, timeout=10)
        soup = BeautifulSoup(html, "html.parser")
        folder = Path(__file__).resolve().parents[1] / "tests" / "live_discovery"
        folder.mkdir(exist_ok=True)
        (folder / (name + ".html")).write_text(html, encoding="utf8")
        result = {"name": name, "url": url, "final_url": final, "length":len(html),
                  "title": soup.title.get_text(" ",strip=True) if soup.title else "",
                  "forms": [str(x)[:2200] for x in soup.select("form")[:5]],
                  "links": [(a.get_text(" ",strip=True)[:100],a.get("href")) for a in soup.select("a[href]") if any(x in str(a.get("href")) for x in ("search","?q=","supply","s.html","gongsi"))][:20]}
    except Exception as exc:
        result = {"name":name,"url":url,"error":f"{type(exc).__name__}: {exc}"}
    return result


def run_discovery(query="水处理"):
    """Reproducible live probe of real discovery + real detail qualification."""
    from manufacturer_finder.discovery import discover
    from manufacturer_finder.models import SearchOptions
    from manufacturer_finder.qualification import qualify_page
    started = time.monotonic()
    cancel = threading.Event()
    options = SearchOptions(query=query, engines=[], discovery_channels=["b2b", "catalog"], pages=3, request_timeout=6, check_negative=False)
    batch = discover(options, cancel, time.monotonic() + 45)
    hits = sorted(batch.hits, key=lambda h: (h.engine != "b2b168", not bool(h.company_hint), "设备" not in h.title, "水处理" not in h.title))[:16]
    def qualify(hit):
        try:
            html, final = fetch_text(hit.url, cancel, time.monotonic() + 10, timeout=6)
            result = qualify_page(hit, html, final)
            return {"source":hit.engine,"title":hit.title,"url":hit.url,"final_url":final,
                    "reason":result.reason,"company":asdict(result.company) if result.company else None}
        except Exception as exc:
            return {"source":hit.engine,"title":hit.title,"url":hit.url,"error":str(exc)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(qualify,hits))
    report = {"query":query,"elapsed_seconds":round(time.monotonic()-started,2),
              "statuses":batch.statuses,"hits":[asdict(h) for h in batch.hits],
              "seeds":[asdict(s) for s in batch.seeds],"qualification":results}
    path=Path(__file__).resolve().parents[1]/"tests"/"live_discovery_run.json"
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf8")
    print(json.dumps({"query":query,"elapsed_seconds":report["elapsed_seconds"],"hits":len(batch.hits),"seeds":len(batch.seeds),
                      "statuses":batch.statuses,"qualification":[{k:v for k,v in x.items() if k!='company'}|{"company":x["company"]["name"] if x.get("company") else None,"phones":x["company"]["phones"] if x.get("company") else []} for x in results]},ensure_ascii=False,indent=2))

if __name__ == "__main__":
    if len(sys.argv)>1 and sys.argv[1]=="--run":
        run_discovery(sys.argv[2] if len(sys.argv)>2 else "水处理")
        raise SystemExit(0)
    selected = dict(x.split("=",1) for x in sys.argv[1:]) if len(sys.argv)>1 else URLS
    results=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in as_completed(pool.submit(probe,k,v) for k,v in selected.items()):
            value=result.result()
            results.append(value)
            print(json.dumps(value,ensure_ascii=False),flush=True)
    (Path(__file__).resolve().parents[1] / "tests" / "live_discovery_report.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf8")
