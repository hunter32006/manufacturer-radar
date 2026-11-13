"""Small, bounded HTTP client for publicly reachable HTML pages.

Redirects and DNS answers are checked before connection.  The connection uses
the checked DNS address itself, so a second DNS lookup cannot turn a public
address into a loopback/private address. No browser challenges are bypassed.
"""
from __future__ import annotations

import ipaddress
import queue
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import HTTPError as UrllibHTTPError


class FetchError(RuntimeError):
    """A page could not be retrieved as bounded public text."""


class FetchCancelled(FetchError):
    pass


class DeadlineExceeded(FetchError):
    pass


class UnsafeURL(FetchError):
    pass


class HTTPFetchError(FetchError):
    def __init__(self, message: str, status_code: int, url: str):
        super().__init__(message)
        self.status_code = status_code
        self.url = url


def check_control(cancel=None, deadline: float | None = None) -> None:
    """Deadlines use time.monotonic(), as does the pipeline's total budget."""
    if cancel is not None and cancel.is_set():
        raise FetchCancelled("搜索已取消")
    if deadline is not None and time.monotonic() >= deadline:
        raise DeadlineExceeded("已达到本次搜索时间上限")


def _public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


def safe_url(url: str) -> bool:
    """Validate an HTTP(S) URL without issuing a DNS query.

    Literal private/reserved IPs, local names, userinfo, unusual numeric host
    spellings and malformed URLs are rejected. DNS results receive the same
    public-IP check when fetch_text opens the socket.
    """
    if not isinstance(url, str) or not url or len(url) > 8192:
        return False
    if url != url.strip() or re.search(r"[\x00-\x20\x7f\\]", url):
        return False
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            return False
        if parts.username is not None or parts.password is not None:
            return False
        if parts.port is not None and not 1 <= parts.port <= 65535:
            return False
        host = (parts.hostname or "").rstrip(".").lower()
        if not host or "%" in host:
            return False
        try:
            return _public_ip(host)
        except ValueError:
            pass
        if "." not in host or host.split(".")[-1].isdigit():
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa", ".invalid", ".test")):
            return False
        ascii_host = host.encode("idna").decode("ascii")
        return len(ascii_host) <= 253 and all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in ascii_host.split(".")
        )
    except (ValueError, UnicodeError):
        return False


def _remaining_timeout(timeout: float, deadline: float | None) -> float:
    remaining = min(float(timeout), 30.0)
    if deadline is not None:
        remaining = min(remaining, deadline - time.monotonic())
    if remaining <= 0:
        raise DeadlineExceeded("已达到本次搜索时间上限")
    return max(0.05, remaining)


def _resolve_public(host: str, port: int, cancel, deadline, timeout: float):
    """Do not let an unavailable resolver consume the complete search budget."""
    check_control(cancel, deadline)
    result: queue.Queue = queue.Queue(maxsize=1)

    def lookup():
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception as exc:
            result.put(exc)

    threading.Thread(target=lookup, name="public-dns", daemon=True).start()
    lookup_end = time.monotonic() + _remaining_timeout(timeout, deadline)
    while True:
        check_control(cancel, deadline)
        remaining = lookup_end - time.monotonic()
        if remaining <= 0:
            raise FetchError("DNS 解析超时")
        try:
            answers = result.get(timeout=min(0.1, remaining))
            break
        except queue.Empty:
            continue
    if isinstance(answers, Exception):
        raise FetchError(f"DNS 解析失败：{answers}") from answers
    if not answers or any(not _public_ip(answer[4][0]) for answer in answers):
        raise UnsafeURL("目标域名指向本机、内网或保留地址，已跳过")
    return answers


def _public_adapter(cancel, deadline, request_timeout):
    # Keeping the original host on HTTPSConnection preserves TLS SNI and
    # certificate hostname verification while the socket uses a checked IP.
    class CheckedHTTPConnection(HTTPConnection):
        def _new_conn(self):
            check_control(cancel, deadline)
            answers = _resolve_public(self._dns_host, self.port, cancel, deadline, request_timeout)
            last_error = None
            connect_end = time.monotonic() + _remaining_timeout(request_timeout, deadline)
            for family, socktype, protocol, _canonical, sockaddr in answers:
                check_control(cancel, deadline)
                sock = socket.socket(family, socktype, protocol)
                try:
                    sock.settimeout(_remaining_timeout(request_timeout, min(connect_end, deadline) if deadline is not None else connect_end))
                    for option in self.socket_options or []:
                        sock.setsockopt(*option)
                    if self.source_address:
                        sock.bind(self.source_address)
                    sock.connect(sockaddr)
                    return sock
                except OSError as exc:
                    last_error = exc
                    sock.close()
                except BaseException:
                    sock.close()
                    raise
            raise last_error or OSError("无可连接的公网地址")

    class CheckedHTTPSConnection(HTTPSConnection):
        _new_conn = CheckedHTTPConnection._new_conn

    class CheckedHTTPPool(HTTPConnectionPool):
        ConnectionCls = CheckedHTTPConnection

    class CheckedHTTPSPool(HTTPSConnectionPool):
        ConnectionCls = CheckedHTTPSConnection

    class PublicAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
            self.poolmanager.pool_classes_by_scheme = {
                "http": CheckedHTTPPool, "https": CheckedHTTPSPool,
            }

    return PublicAdapter(max_retries=0, pool_connections=2, pool_maxsize=2)


def _decode_html(data: bytes, declared: str | None) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    candidates = []
    if declared and declared.lower() not in {"iso-8859-1", "latin-1", "ascii"}:
        candidates.append(declared)
    match = re.search(br"charset\s*=\s*[\"']?([a-zA-Z0-9_-]{1,40})", data[:12000], re.I)
    if match:
        candidates.append(match.group(1).decode("ascii"))
    candidates += ["utf-8-sig", "gb18030"]
    for encoding in dict.fromkeys(candidates):
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _transparent_html_redirect(html: str, base_url: str) -> str | None:
    """Recognize a tiny navigation wrapper, never execute JavaScript.

    Some search engines publish HTTP-200 redirectors with a literal location
    replacement and a noscript meta refresh. Only empty/status-only documents
    qualify; scripts on manufacturer pages are not followed.
    """
    if len(html) > 16_384:
        return None
    soup = BeautifulSoup(html, "html.parser")
    visible = soup.body.get_text(" ", strip=True) if soup.body else soup.get_text(" ", strip=True)
    if soup.body is None and soup.title:
        visible = visible.replace(soup.title.get_text(" ", strip=True), "", 1).strip()
    if len(visible) > 160:
        return None
    status_text = re.sub(
        r"正在(?:跳转|加载)|页面跳转|跳转中|即将跳转|请稍候|请稍等|点击(?:这里|此处)?(?:继续|跳转)|继续访问|redirecting|please\s+wait|loading|click\s+here(?:\s+to\s+continue)?",
        "", visible, flags=re.I,
    )
    if re.sub(r"[\s.,:;!?。…，：；！？|\-–—]", "", status_text):
        return None
    targets = set()
    for meta in soup.find_all("meta"):
        if str(meta.get("http-equiv", "")).lower() != "refresh":
            continue
        refresh = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*;\s*url\s*=\s*(.+?)\s*", str(meta.get("content", "")), re.I)
        if refresh and float(refresh.group(1)) <= 3:
            target = refresh.group(2).strip()
            if len(target) >= 2 and target[0] in "\"'" and target[-1] == target[0]:
                target = target[1:-1]
            if target:
                targets.add(urljoin(base_url, target))
    # fullmatch excludes conditional code, function bodies, concatenation,
    # escaped/obfuscated literals, and every form of JavaScript execution.
    assignment = re.compile(r"\s*(?:(?:window|document)\.)?location(?:\.href)?\s*=\s*([\"'])([^\"'\\\r\n]+)\1\s*;?\s*", re.I)
    replacement = re.compile(r"\s*(?:(?:window|document)\.)?location\.(?:replace|assign)\s*\(\s*([\"'])([^\"'\\\r\n]+)\1\s*\)\s*;?\s*", re.I)
    for script in soup.find_all("script"):
        if script.get("src"):
            continue
        source = script.string or script.get_text()
        match = assignment.fullmatch(source) or replacement.fullmatch(source)
        if match:
            targets.add(urljoin(base_url, match.group(2)))
    return next(iter(targets)) if len(targets) == 1 else None


def _known_search_wrapper_target(url: str) -> str | None:
    """Read a destination explicitly published by these exact engine wrappers.

    parse_qs performs one ordinary URL-query decode. Nested wrappers consume
    the same five-redirect budget as HTTP and tiny HTML redirects; arbitrary
    sites' url/target parameters are never interpreted.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path.rstrip("/")
    if host in {"sou.toutiao.com", "so.toutiao.com"} and path == "/search/jump":
        parameter = "url"
    elif host == "link.wtturl.cn" and path == "/middle-page":
        parameter = "target"
    else:
        return None
    values = parse_qs(parts.query, keep_blank_values=True).get(parameter, [])
    return values[0].strip() if len(values) == 1 and values[0].strip() else None


def fetch_text(url: str, cancel=None, deadline: float | None = None,
               timeout: float = 8, max_bytes: int = 2_000_000, *, _binary=False,
               method: str = 'GET', data: dict | None = None) -> tuple[str, str]:
    """Fetch bounded decompressed text with at most five navigation redirects.

    Each blocking socket operation is bounded by timeout and the remaining
    total budget. Cancellation is checked between socket operations and chunks.
    """
    check_control(cancel, deadline)
    if max_bytes <= 0 or timeout <= 0:
        raise ValueError("timeout 和 max_bytes 必须大于 0")
    method = method.upper()
    if method not in {'GET', 'POST'}:
        raise ValueError('仅支持公开搜索表单的 GET/POST')
    if data is not None and len(str(data).encode('utf-8')) > 64_000:
        raise ValueError('表单数据超过上限')
    if not safe_url(url):
        raise UnsafeURL("仅允许访问有效的公网 HTTP/HTTPS 链接")
    # Per-call sessions are safe across the pipeline's search workers. Disable
    # environment proxies: their private destinations cannot be validated here.
    with requests.Session() as session:
        session.trust_env = False
        adapter = _public_adapter(cancel, deadline, timeout)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.7,*/*;q=0.1",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
        })
        current = url
        for redirect in range(6):
            check_control(cancel, deadline)
            if not safe_url(current):
                raise UnsafeURL("重定向目标不是允许的公网链接")
            published_target = _known_search_wrapper_target(current)
            if published_target is not None:
                if not safe_url(published_target):
                    raise UnsafeURL("搜索跳转包装页提供的目标不是有效公网链接")
                if redirect == 5:
                    raise FetchError("页面重定向次数过多")
                current = published_target
                continue
            try:
                operation_timeout = _remaining_timeout(timeout, deadline)
                request = session.get if method == 'GET' else session.post
                kwargs = {'data': data} if method == 'POST' else {}
                with request(current, timeout=operation_timeout,
                             allow_redirects=False, stream=True, **kwargs) as response:
                    check_control(cancel, deadline)
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location", "")
                        if not location or redirect == 5:
                            raise FetchError("页面重定向次数过多或缺少目标")
                        current = urljoin(response.url, location)
                        if response.status_code in {301, 302, 303}:
                            method, data = 'GET', None
                        continue
                    if response.status_code >= 400:
                        raise HTTPFetchError(f"服务器返回 HTTP {response.status_code}", response.status_code, response.url)
                    media_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
                    if not _binary and media_type and not (media_type.startswith("text/") or media_type in {"application/xhtml+xml", "application/xml", "application/json"}):
                        raise FetchError("链接返回的不是可读取的文本网页")
                    length = response.headers.get("Content-Length", "")
                    if length.isdigit() and int(length) > max_bytes:
                        raise FetchError("网页超过单页读取大小上限")
                    chunks = []
                    size = 0
                    while True:
                        check_control(cancel, deadline)
                        # Recompute after DNS, headers and every chunk, so a
                        # late read cannot reuse the original remaining budget.
                        connection = getattr(response.raw, "_connection", None)
                        connected_socket = getattr(connection, "sock", None)
                        if connected_socket is not None:
                            connected_socket.settimeout(_remaining_timeout(timeout, deadline))
                        reader = getattr(response.raw, "read1", response.raw.read)
                        chunk = reader(16 * 1024, decode_content=True)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise FetchError("网页超过单页读取大小上限")
                        chunks.append(chunk)
                    check_control(cancel, deadline)
                    if _binary:
                        return b"".join(chunks), response.url
                    text = _decode_html(b"".join(chunks), response.encoding)
                    target = _transparent_html_redirect(text, response.url) if media_type in {"", "text/html", "application/xhtml+xml"} else None
                    if target is not None:
                        if not safe_url(target):
                            raise UnsafeURL("HTML 跳转目标不是允许的公网链接")
                        if redirect == 5:
                            raise FetchError("页面重定向次数过多")
                        current = target
                        continue
                    return text, response.url
            except FetchError:
                raise
            except (requests.RequestException, UrllibHTTPError, OSError) as exc:
                check_control(cancel, deadline)
                raise FetchError(f"网页访问失败：{exc}") from exc
        raise FetchError("页面重定向次数过多")


def fetch_bytes(url: str, cancel=None, deadline: float | None = None,
                timeout: float = 8, max_bytes: int = 20_000_000, *,
                method: str = 'GET', data: dict | None = None) -> tuple[bytes, str]:
    """Fetch bounded public documents with the same DNS and redirect checks."""
    return fetch_text(url, cancel, deadline, timeout, max_bytes, _binary=True, method=method, data=data)
