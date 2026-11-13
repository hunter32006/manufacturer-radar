"""Search, visit, qualify, then admit manufacturers; raw hits remain audit data.

Only the coordinator mutates the run or emits callbacks. Every executor is joined
before returning, so cancelling keeps admitted factories without late mutations.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import asdict
from queue import Empty, Queue
import re
import threading
import time
from urllib.parse import urljoin, urlsplit

from . import extraction, network, qualification, scoring
from .models import Company, Evidence, RunResult, SearchHit, SearchOptions, now
from .providers import ENGINES, FetchError, SearchBlocked, SearchProvider
from .source_types import DiscoveryBatch, LeadSeed, SOURCE_LABELS


class _Stopped(Exception):
    pass


class _Deadline(Exception):
    pass


def _hit_key(hit):
    return (hit.engine, hit.query, hit.page, hit.url, hit.title, hit.snippet,
            hit.source_url, hit.company_hint, hit.discovery_source, hit.lookup_query)


def manufacturer_search_query(query: str) -> str:
    """Add manufacturer intent to a plain product, preserving explicit intent."""
    query = query.strip()
    if not query or re.search(r"厂商|厂家|制造商|供应商|生产商|供货商|经销商|工厂|公司|厂\s*$", query):
        return query
    return query + " 厂家"


class SearchRunner:
    """One bounded run. ``seed_hits`` bypasses search, never page qualification.

    ``enrich=False`` disables optional contact/about followups only. Actual target
    page visits and manufacturer/relevance qualification are always mandatory.
    Stats count unique candidate URLs; accepted URLs may merge into fewer firms.
    """

    ENGINE_DELAY = 0.6
    MAX_WORKERS = 6
    MAX_CANDIDATE_URLS = 120
    MAX_CONTACT_PAGES = 2

    def __init__(self, options: SearchOptions, callback=None, provider=None, seed_hits=None,
                 seed_leads=None, captured_pages=None, discovery_provider=None, map_provider=None,
                 source_status=None):
        self.options = deepcopy(options)
        self.search_query = manufacturer_search_query(self.options.query)
        self.callback = callback
        self.provider = provider if provider is not None else SearchProvider()
        self.seed_hits = None if seed_hits is None else deepcopy(list(seed_hits))
        self.seed_leads = deepcopy(list(seed_leads or []))[:100]
        self.captured_pages = {url: html for url, html in (captured_pages or {}).items()
            if network.safe_url(url) and isinstance(html, str) and len(html.encode('utf-8')) <= 2_000_000}
        self.discovery_provider = discovery_provider
        self.map_provider = map_provider
        self._leads = []
        self._source_status = deepcopy(source_status or {})
        self._cancel = threading.Event()
        self._engine_locks = {engine: threading.Lock() for engine in ENGINES}
        self._engine_last_start = {}
        self._blocked_engines = {}
        self._records = {}
        self._result = None

    def cancel(self):
        self._cancel.set()

    def snapshot(self) -> RunResult:
        """Return an interrupted partial run after ``run`` raised and joined.

        This does not mutate the runner or emit events. Callers use it from the
        coordinator's exception handler, after executor context managers exited.
        A page whose qualification did not finish is never promoted to a firm.
        """
        partial = deepcopy(self._result) if self._result is not None else RunResult(query=self.options.query)
        groups = {}
        for hit in partial.candidates:
            groups.setdefault(extraction.normalized_url(hit.url) or hit.url, []).append(hit)
        records = deepcopy(self._records)
        partial.collection_log = []
        for key, hits in groups.items():
            row = records.get(key, {"url": hits[0].url, "title": hits[0].title,
                "status": "unvisited", "reason": "运行中断，尚未完成实际页面识别，未入库", "contact_pages": 0})
            if row.get("status") == "visiting":
                row.update(status="failed", reason="运行中断，页面访问/识别未完成，未作为厂家入库")
            row["source_count"] = len(hits)
            partial.collection_log.append(row)
        statuses = [row["status"] for row in partial.collection_log]
        partial.qualification_stats = {
            "candidates": len(groups), "visited": sum(status != "unvisited" for status in statuses),
            "accepted": statuses.count("accepted"), "rejected": statuses.count("rejected"),
            "unvisited": statuses.count("unvisited"), "failed": statuses.count("failed"),
        }
        partial.companies = [company for company in partial.companies if self._admissible(company)]
        for state in partial.engine_status.values():
            if state.get("status") in {"pending", "running"}:
                state.update(status="cancelled", message="运行中断，保留已读取的搜索页范围")
        partial.finished_at, partial.cancelled = now(), True
        return partial

    def _emit(self, event_type, **data):
        if self.callback is not None:
            try:
                self.callback(deepcopy({"type": event_type, **data}))
            except Exception:
                pass

    def _check(self, deadline):
        if self._cancel.is_set():
            raise _Stopped()
        if time.monotonic() >= deadline:
            raise _Deadline()

    def _fetch(self, engine, query, page, deadline):
        gate = self._engine_locks[engine]
        while True:
            self._check(deadline)
            if gate.acquire(timeout=min(0.1, max(0.001, deadline - time.monotonic()))):
                break
        try:
            if engine in self._blocked_engines:
                raise SearchBlocked(self._blocked_engines[engine])
            while True:
                self._check(deadline)
                delay = self.ENGINE_DELAY - (time.monotonic() - self._engine_last_start.get(engine, float("-inf")))
                if delay <= 0:
                    break
                self._cancel.wait(min(delay, max(0.001, deadline - time.monotonic())))
            self._check(deadline)
            self._engine_last_start[engine] = time.monotonic()
            try:
                return self.provider.fetch(engine, query, page, self._cancel, deadline,
                    timeout=min(float(self.options.request_timeout), max(0.1, deadline - time.monotonic())))
            except SearchBlocked as exc:
                self._blocked_engines[engine] = str(exc)
                raise
        finally:
            gate.release()

    def _engine_worker(self, engine, deadline, queue):
        queue.put(("engine", engine, "running", "正在发现候选网页"))
        pages = 0
        try:
            for page in range(1, min(3, self.options.pages) + 1):
                self._check(deadline)
                hits = self._fetch(engine, self.search_query, page, deadline)
                pages += 1
                queue.put(("page", engine, page, hits))
            queue.put(("engine", engine, "complete", "已搜索前 %d 页；候选网页需实际访问后识别" % pages))
        except SearchBlocked as exc:
            queue.put(("engine", engine, "blocked", "访问或解析受限：%s" % exc))
        except _Stopped:
            queue.put(("engine", engine, "cancelled", "已停止，保留候选网页审计记录"))
        except _Deadline:
            queue.put(("engine", engine, "partial" if pages else "timed_out", "搜索阶段时间到，优先保留实际网页识别时间"))
        except (FetchError, TimeoutError) as exc:
            if self._cancel.is_set():
                status, message = "cancelled", "已停止，保留候选网页"
            elif time.monotonic() >= deadline:
                status, message = ("partial" if pages else "timed_out"), "搜索阶段时间到，保留已获取候选网页"
            else:
                status, message = ("partial" if pages else "error"), "请求未完成：%s" % exc
            queue.put(("engine", engine, status, message))
        except Exception as exc:
            queue.put(("engine", engine, "partial" if pages else "error", "解析或请求失败：%s" % exc))
        finally:
            queue.put(("finished", engine))

    def _groups(self):
        groups = {}
        for hit in self._result.candidates:
            key = extraction.normalized_url(hit.url) or hit.url
            groups.setdefault(key, []).append(hit)
        return groups

    def _audit(self):
        groups = self._groups()
        for key, hits in groups.items():
            if key not in self._records:
                self._records[key] = {"url": hits[0].url, "title": hits[0].title,
                    "status": "unvisited", "reason": "尚未实际访问，不能作为厂家入库", "contact_pages": 0}
            self._records[key]["source_count"] = len(hits)
        self._result.collection_log = [deepcopy(self._records[key]) for key in groups]
        statuses = [self._records[key]["status"] for key in groups]
        self._result.qualification_stats = {
            "candidates": len(groups), "visited": sum(status != "unvisited" for status in statuses),
            "accepted": statuses.count("accepted"), "rejected": statuses.count("rejected"),
            "failed": statuses.count("failed"), "unvisited": statuses.count("unvisited"),
        }
        self._emit("stats", stats=self._result.qualification_stats)

    def _add_candidates(self, hits):
        keys = {_hit_key(hit) for hit in self._result.candidates}
        for hit in hits:
            if not isinstance(hit, SearchHit) or hit.query not in {self.options.query, self.search_query} or not 1 <= hit.page <= 3:
                raise ValueError("候选网页与当前关键词或前 3 页范围不一致")
            if _hit_key(hit) not in keys:
                self._result.candidates.append(deepcopy(hit))
                keys.add(_hit_key(hit))
        self._emit("candidates", candidates=self._result.candidates, count=len(self._result.candidates))
        self._audit()

    def _score(self):
        for company in self._result.companies:
            # A rejected article can be a same-name allegation source without
            # ever being admitted as a manufacturer.
            for hit in self._result.candidates:
                evidence = extraction.negative_evidence(company, hit)
                if evidence is not None and not any(e.kind == evidence.kind and e.url == evidence.url and e.excerpt == evidence.excerpt for e in company.evidence):
                    company.evidence.append(evidence)
            scoring.score_company(company)

    def _publish(self):
        self._score()
        self._emit("results", companies=self._result.companies)

    def _collect_search(self, engines, deadline):
        queue, finished = Queue(), 0
        channels = set(self.options.discovery_channels)
        jobs = []
        if channels.intersection({'b2b', 'catalog'}):
            if self.discovery_provider is not None:
                jobs.append(self.discovery_provider)
            else:
                from .discovery import discover
                jobs.append(discover)
        if 'maps' in channels:
            if self.map_provider is not None:
                jobs.append(self.map_provider)
            else:
                from .map_sources import discover_maps
                jobs.append(discover_maps)
        if not engines and not jobs:
            return
        def channel_emit(*args, **kwargs):
            payload = dict(args[0]) if args and isinstance(args[0], dict) else dict(kwargs)
            if args and isinstance(args[0], str):
                payload['type'] = args[0]
            queue.put(('source_event', '', payload))
        def source_worker(discoverer):
            try:
                batch = discoverer(self.options, self._cancel, deadline,
                    emit=channel_emit)
                queue.put(('batch', '', batch))
            except Exception as exc:
                queue.put(('source_event', '', {'source': 'discovery', 'status': 'error',
                    'message': '渠道采集未完成：' + str(type(exc).__name__)}))
            finally:
                queue.put(('finished', ''))
        with ThreadPoolExecutor(max_workers=min(8, len(engines) + len(jobs)), thread_name_prefix="search") as pool:
            futures = [pool.submit(self._engine_worker, engine, deadline, queue) for engine in engines]
            futures += [pool.submit(source_worker, job) for job in jobs]
            while finished < len(futures) or not queue.empty():
                try:
                    event = queue.get(timeout=0.1)
                except Empty:
                    continue
                kind, engine = event[:2]
                if kind == 'finished':
                    finished += 1
                    continue
                if kind == 'source_event':
                    payload = event[2]
                    source = payload.get('source', 'discovery')
                    state = {k: v for k,v in payload.items() if k not in {'type', 'source'}}
                    self._result.source_status.setdefault(source, {}).update(state)
                    self._emit('source', source=source, **self._result.source_status[source])
                    continue
                if kind == 'batch':
                    self._add_batch(event[2])
                    continue
                state = self._result.engine_status[engine]
                if kind == "page":
                    _, _, page, hits = event
                    state["pages"] += 1
                    state["page_numbers"].append(page)
                    state["hits"] += len(hits)
                    state["message"] = "第 %d 页：发现 %d 条候选；尚未认定为厂家" % (page, len(hits))
                    self._add_candidates(hits)
                    self._publish()
                elif kind == "engine":
                    state["status"], state["message"] = event[2:]
                elif kind == "finished":
                    finished += 1
                    continue
                self._emit("engine", engine=engine, **state)

    def _add_batch(self, batch):
        if not isinstance(batch, DiscoveryBatch):
            return
        if batch.hits:
            self._add_candidates(batch.hits)
        for source, state in batch.statuses.items():
            self._result.source_status[source] = deepcopy(state)
            self._emit('source', source=source, **state)
        known = {(lead.name, lead.url, lead.source, lead.source_url) for lead in self._leads}
        for lead in batch.seeds:
            key = lead.name, lead.url, lead.source, lead.source_url
            if key not in known and len(self._leads) < 200:
                self._leads.append(deepcopy(lead))
                known.add(key)
        self._result.discovery_seeds = [asdict(lead) for lead in self._leads]
        for note in batch.notes:
            self._emit('log', message=note)

    def _resolve_leads(self, engines, deadline):
        """Imported/directory/POI identities are leads, never qualified firms."""
        direct, names = [], []
        for lead in self._leads:
            if network.safe_url(lead.url):
                direct.append(SearchHit(engine=lead.source, query=self.options.query, page=1,
                    title=lead.name or lead.url, url=lead.url, snippet=lead.excerpt,
                    source_url=lead.source_url, company_hint=lead.name,
                    source_excerpt=lead.excerpt, discovery_source=lead.source))
            elif lead.name:
                names.append(lead)
        if direct:
            self._add_candidates(direct[:100])
        working = [engine for engine in engines if engine not in self._blocked_engines]
        if not names or not working:
            if names:
                self._emit('log', message='有 %d 条仅名称线索；未选择可用搜索引擎，保留在线索记录中，不作为厂家入库。' % len(names))
            return
        self._emit('phase', message='按名录中的完整公司名称寻找官网，每条仅搜索第 1 页；命中后仍需页面核实')
        def resolve(item):
            index, lead = item
            engine = working[index % len(working)]
            query = '"%s" 官网' % lead.name
            try:
                hits = self._fetch(engine, query, 1, deadline)
                selected = []
                for hit in hits[:5]:
                    hit.lookup_query, hit.query = query, self.options.query
                    hit.source_url, hit.company_hint = lead.source_url, lead.name
                    hit.source_excerpt, hit.discovery_source = lead.excerpt, lead.source
                    selected.append(hit)
                return selected
            except Exception:
                return []
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='lead-lookup') as pool:
            for hits in pool.map(resolve, enumerate(names[:20])):
                if hits:
                    self._add_candidates(hits)
        if len(names) > 20:
            self._emit('log', message='名称线索本轮最多补查 20 家，其余保留在采集记录中。')

    @staticmethod
    def _candidate_priority(hits):
        titles = " ".join(hit.title for hit in hits)
        maker = bool(re.search(r"工厂|厂家|制造|公司|生产|供应|批发|官网", titles))
        article = bool(re.search(r"百科|知乎|知识|问答|怎么|是什么|新闻|资讯", titles))
        full_name = any(extraction.company_from_hit(hit).name_confirmed for hit in hits)
        return not maker, not full_name, article, -len({hit.engine for hit in hits})

    def _page_fetch(self, url, deadline):
        self._check(deadline)
        if url in self.captured_pages:
            return self.captured_pages[url], url
        return network.fetch_text(url, self._cancel, deadline,
            timeout=min(float(self.options.request_timeout), max(0.1, deadline - time.monotonic())), max_bytes=2_000_000)

    @staticmethod
    def _admissible(company):
        return bool(company is not None and scoring.is_page_qualified(company))

    @staticmethod
    def _provenance(company, hits, final_url):
        company = deepcopy(company)
        hits = [hit for hit in hits if not hit.company_hint or
            extraction.normalized_name(hit.company_hint) == extraction.normalized_name(company.name)]
        company.verification_url = company.verification_url or final_url
        for url in [*(hit.url for hit in hits), final_url]:
            if url and url not in company.urls:
                company.urls.append(url)
        keys = {_hit_key(hit) for hit in company.hits}
        for hit in hits:
            if _hit_key(hit) not in keys:
                company.hits.append(deepcopy(hit))
                keys.add(_hit_key(hit))
            if hit.source_url:
                evidence = Evidence(kind='发现来源', url=hit.source_url,
                    excerpt=(hit.company_hint + ' ' + hit.source_excerpt).strip()[:1200],
                    engine=hit.discovery_source or hit.engine, query=hit.lookup_query or hit.query,
                    page=hit.page, status='线索来源；厂家身份以已读取的详情页为准')
                if not any(e.kind == evidence.kind and e.url == evidence.url and e.excerpt == evidence.excerpt for e in company.evidence):
                    company.evidence.append(evidence)
        return company

    def _matches_hint(self, company, hits):
        expected = {extraction.normalized_name(hit.company_hint) for hit in hits if hit.company_hint}
        return not expected or extraction.normalized_name(company.name) in expected

    @staticmethod
    def _social_identity(company, html, url):
        host = (urlsplit(url).hostname or '').casefold()
        if any(host == domain or host.endswith('.' + domain) for domain in ('douyin.com', 'kuaishou.com')):
            from .browser_assist import social_identity_matches
            return social_identity_matches(html, company.name)
        return True

    def _qualify_worker(self, key, hits, deadline, queue):
        attempted, company, final_url, contact_count = False, None, "", 0
        reason = "未访问"
        try:
            self._check(deadline)
            attempted = True
            queue.put(("visiting", key))
            html, final_url = self._page_fetch(hits[0].url, deadline)
            result = qualification.qualify_page(hits[0], html, final_url)
            reason = result.reason
            if (self._admissible(result.company) and self._matches_hint(result.company, hits)
                    and self._social_identity(result.company, html, final_url)):
                company = self._provenance(result.company, hits, final_url)
                queue.put(("admission", key, deepcopy(company), reason))
            elif result.company is not None:
                if not self._matches_hint(result.company, hits):
                    reason = '页面主体与名单指定厂家不一致，未归入该厂家'
                elif not self._social_identity(result.company, html, final_url):
                    reason = '未读取到明确企业主体标签，社交昵称和视频文案不能作为厂家身份依据'
                else:
                    reason = "未获得页面核实的完整厂家主体，排除搜索摘要或标题推测"
            contacts = []
            if self.options.enrich and (company is None or not company.phones):
                for link in result.contact_urls:
                    url = urljoin(final_url, link)
                    if (urlsplit(url).hostname or "").casefold() != (urlsplit(final_url).hostname or "").casefold():
                        continue
                    if url not in contacts and url not in {final_url, hits[0].url} and network.safe_url(url):
                        contacts.append(url)
            # Selected same-host contact/about pages only, never recursive.
            for url in contacts[:self.MAX_CONTACT_PAGES]:
                try:
                    self._check(deadline)
                    contact_html, contact_final = self._page_fetch(url, deadline)
                    if (urlsplit(contact_final).hostname or "").casefold() != (urlsplit(final_url).hostname or "").casefold():
                        continue
                    contact_count += 1
                    contact_result = qualification.qualify_page(hits[0], contact_html, contact_final)
                    if (self._admissible(contact_result.company) and self._matches_hint(contact_result.company, hits)
                            and self._social_identity(contact_result.company, contact_html, contact_final)):
                        candidate = self._provenance(contact_result.company, hits, contact_final)
                        if company is None:
                            company, reason = candidate, contact_result.reason
                        elif extraction.normalized_name(company.name) == extraction.normalized_name(candidate.name):
                            company = extraction.merge_companies([company, candidate])[0]
                    if company is not None:
                        extraction.enrich_company(company, contact_html, contact_final)
                        if any(e.kind == "名称" and extraction.normalized_url(e.url) == extraction.normalized_url(contact_final) for e in company.evidence):
                            company.evidence.append(Evidence(kind="联系页核验", url=contact_final,
                                excerpt="同域联系页出现已识别厂家的完整名称：" + company.name,
                                status="页面文本提取，联系方式归属待核实"))
                        queue.put(("admission", key, deepcopy(company), reason))
                        if company.phones:
                            break
                except (_Stopped, _Deadline):
                    break
                except Exception:
                    continue
            status = "accepted" if company is not None else "rejected"
        except (_Stopped, _Deadline):
            status = "failed" if attempted else "unvisited"
            reason = "实际访问已取消或达到识别阶段时限；未完成页面不入库"
        except Exception as exc:
            status = "accepted" if company is not None else "failed"
            reason = reason if company is not None else "实际页面访问或识别失败：%s" % exc
        queue.put(("outcome", key, status, reason, company, final_url, contact_count))

    def _admit(self, key, company):
        if not self._admissible(company):
            return
        identity = extraction.normalized_name(company.name)
        for lead in self._leads:
            if not lead.name or extraction.normalized_name(lead.name) != identity:
                continue
            for phone in lead.phones:
                for number in extraction.extract_phones(phone):
                    if number not in company.phones:
                        company.phones.append(number)
                        company.evidence.append(Evidence(kind='外部公开电话', url=lead.source_url or lead.url,
                            excerpt=company.name + '：' + number, engine=lead.source,
                            status='名录/地图/导入来源，归属待人工核实；不计详情页电话加分'))
        self._admitted_by_url[key] = deepcopy(company)
        self._result.companies = extraction.merge_companies(list(self._admitted_by_url.values()))
        self._publish()

    def _qualify_candidates(self, deadline):
        self._emit("phase", message="实际访问候选网页，识别相关生产厂家后入库；资讯、目录和未识别主体页面排除")
        groups = self._groups()
        prioritized = sorted(groups.items(), key=lambda item: self._candidate_priority(item[1]))
        selected = prioritized[:self.MAX_CANDIDATE_URLS]
        for key, _ in prioritized[self.MAX_CANDIDATE_URLS:]:
            self._records[key]["reason"] = "超过本轮最多 %d 个候选网页访问上限，未识别、不入库" % self.MAX_CANDIDATE_URLS
        self._admitted_by_url = {}
        queue, finished = Queue(), 0
        if not selected:
            self._audit()
            return
        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS, thread_name_prefix="qualify") as pool:
            futures = [pool.submit(self._qualify_worker, key, hits, deadline, queue) for key, hits in selected]
            while finished < len(futures) or not queue.empty():
                try:
                    event = queue.get(timeout=0.1)
                except Empty:
                    continue
                kind, key = event[:2]
                record = self._records[key]
                if kind == "visiting":
                    record.update(status="visiting", reason="正在实际访问目标页面，尚未完成厂家识别")
                elif kind == "admission":
                    record.update(status="accepted", reason=event[3])
                    self._admit(key, event[2])
                elif kind == "outcome":
                    _, _, status, reason, company, final_url, contact_count = event
                    record.update(status=status, reason=reason, final_url=final_url, contact_pages=contact_count)
                    if company is not None:
                        self._admit(key, company)
                    self._emit("log", message="%s：%s（%s）" % ({"accepted": "厂家入库", "rejected": "排除", "failed": "访问失败", "unvisited": "未访问"}.get(status, status), record["title"], reason))
                    finished += 1
                self._audit()
        self._publish()

    @staticmethod
    def _note(company, message):
        if message not in company.notes:
            company.notes = (company.notes + "；" + message).strip("；")

    @staticmethod
    def _negative_priority(company):
        name = re.sub(r"\s+", "", company.name)
        engines = {hit.engine for hit in company.hits if hit.engine}
        title_mentions = sum(name in re.sub(r"\s+", "", hit.title) for hit in company.hits)
        return len(name) < 8, -len(engines), -title_mentions, not bool(company.phones), -len(name)

    def _negative(self, engines, deadline):
        self._emit("phase", message="仅按已实际识别厂家的完整名称补查负面线索，每家仅第 1 页")
        eligible = []
        for company in sorted(self._result.companies, key=self._negative_priority):
            if not self._admissible(company):
                continue
            if len(eligible) >= self.options.negative_limit:
                self._note(company, "负面未检查：达到本轮 %d 家补充查询上限" % self.options.negative_limit)
            else:
                eligible.append(company)
        working = [engine for engine in engines if self._result.engine_status.get(engine, {}).get("pages", 0) > 0
                   and self._result.engine_status[engine]["status"] not in ("blocked", "cancelled")]
        if not working:
            for company in eligible:
                if company.negative_status != "发现待核实线索":
                    company.negative_status = "检查受限"
                self._note(company, "负面检查受限：没有可用搜索引擎")
            self._publish()
            return
        if self._cancel.is_set() or time.monotonic() >= deadline:
            for company in eligible:
                self._note(company, "负面未检查：已取消或达到总时间上限")
            return
        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS, thread_name_prefix="negative") as pool:
            futures = {}
            for index, company in enumerate(eligible):
                engine = working[index % len(working)]
                query = '"%s" 投诉 OR 曝光 OR 处罚 OR 失信' % company.name
                futures[pool.submit(self._fetch, engine, query, 1, deadline)] = (company, engine)
            for future in as_completed(futures):
                company, engine = futures[future]
                try:
                    for hit in future.result():
                        evidence = extraction.negative_evidence(company, hit)
                        if evidence is not None and not any(e.kind == evidence.kind and e.url == evidence.url and e.excerpt == evidence.excerpt for e in company.evidence):
                            company.evidence.append(evidence)
                    company.negative_status = "发现待核实线索" if any(e.kind == "负面线索" for e in company.evidence) else "本轮未发现"
                    self._note(company, "负面补查：%s 第 1 页；未发现不代表不存在" % ENGINES[engine])
                except (_Stopped, _Deadline):
                    self._note(company, "负面未检查：已取消或达到总时间上限")
                except Exception as exc:
                    if self._cancel.is_set() or time.monotonic() >= deadline:
                        self._note(company, "负面未检查：已取消或达到总时间上限")
                    elif company.negative_status != "发现待核实线索":
                        company.negative_status = "检查受限"
                        self._note(company, "负面检查受限：%s" % exc)
                self._publish()

    def run(self) -> RunResult:
        if self._result is not None:
            raise RuntimeError("每个 SearchRunner 只能运行一次")
        start = time.monotonic()
        deadline = start + self.options.budget_seconds
        engines = list(dict.fromkeys(engine for engine in self.options.engines if engine in ENGINES))
        self._result = RunResult(query=self.options.query)
        self._result.source_status = deepcopy(self._source_status)
        for engine in engines:
            self._result.engine_status[engine] = {"status": "pending", "pages": 0, "page_numbers": [], "hits": 0,
                "message": "等待搜索", "requested_pages": min(3, self.options.pages), "query": self.search_query}
        self._publish()
        if self.seed_leads:
            self._add_batch(DiscoveryBatch(seeds=self.seed_leads,
                statuses={'import': {'status': 'imported', 'pages': 0, 'hits': 0,
                    'seeds': len(self.seed_leads), 'message': '已读取名单，需访问网页识别后入库'}}))
        if self.seed_hits is not None:
            self._emit("phase", message="读取已保存候选网页；跳过搜索，开始实际页面识别")
            self._add_candidates(self.seed_hits)
            for engine in engines:
                hits = [hit for hit in self.seed_hits if hit.engine == engine]
                pages = sorted({hit.page for hit in hits})
                self._result.engine_status[engine].update(status="imported", pages=len(pages), page_numbers=pages,
                    hits=len(hits), message="已保存候选，必须实际访问后识别")
        elif engines or self.options.discovery_channels:
            self._emit("phase", message="按“%s”并行搜索所选引擎前 3 页，搜索阶段最多使用 30%% 总时间" % self.search_query)
            self._collect_search(engines, start + self.options.budget_seconds * 0.30)
        else:
            self._emit("log", message="没有选择有效的搜索引擎")
        if self._leads:
            self._resolve_leads(engines, min(deadline, start + self.options.budget_seconds * 0.45))
        remaining = max(0, deadline - time.monotonic())
        negative_reserve = min(self.options.budget_seconds * 0.15, remaining * 0.20) if self.options.check_negative else 0
        self._qualify_candidates(deadline - negative_reserve)
        if self.options.check_negative:
            self._negative(engines, deadline)
        else:
            for company in self._result.companies:
                self._note(company, "本轮未启用补充负面查询（已采集文本仍可提示待核实线索）")
        self._result.cancelled = self._cancel.is_set()
        self._result.timed_out = time.monotonic() >= deadline
        self._result.finished_at = now()
        self._audit()
        self._publish()
        self._emit("done", result=self._result)
        return deepcopy(self._result)
