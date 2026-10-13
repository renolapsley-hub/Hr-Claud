"""All outgoing HTTP. Polite by design: timeouts, retries, per-site delay, robots.txt, no evasion."""
import ipaddress
import socket
import threading
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config


def _session():
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=1.0, status_forcelist=(502, 503, 504),
                  allowed_methods=("GET",), respect_retry_after_header=True)
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": config.USER_AGENT, "Accept-Language": "en-GB,en;q=0.8"})
    return s


_api = _session()


class ApiHttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def api_get(url, params=None, auth=None, headers=None):
    """GET a JSON API. Raises ApiHttpError with a human-readable message."""
    try:
        r = _api.get(url, params=params, auth=auth, headers=headers, timeout=config.REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise ApiHttpError(0, f"network problem ({e.__class__.__name__})")
    if r.status_code in (401, 403):
        raise ApiHttpError(r.status_code, "credentials rejected - check the API key/ID in your settings")
    if r.status_code == 429:
        raise ApiHttpError(429, "rate limit or daily quota reached - try again later")
    if r.status_code >= 400:
        raise ApiHttpError(r.status_code, f"provider returned HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise ApiHttpError(r.status_code, "provider did not return JSON")


@dataclass
class Page:
    url: str
    final_url: str = ""
    status: int = 0
    html: str = ""
    error: str = ""
    robots_blocked: bool = False

    @property
    def ok(self):
        return self.status == 200 and bool(self.html)


def _public_host(host):
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except (socket.gaierror, ValueError):
        return False


class Fetcher:
    """Fetches public web pages one at a time per host, honouring robots.txt."""

    def __init__(self):
        self.s = _session()
        self._last = {}
        self._robots = {}
        self._lock = threading.Lock()

    def _wait(self, host):
        with self._lock:
            ready = max(self._last.get(host, 0) + config.HOST_DELAY, time.time())
            self._last[host] = ready
        delay = ready - time.time()
        if delay > 0:
            time.sleep(delay)

    def _robots_ok(self, url):
        p = urllib.parse.urlsplit(url)
        base = f"{p.scheme}://{p.netloc}"
        rp = self._robots.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            self._wait(p.netloc)
            try:
                r = self.s.get(base + "/robots.txt", timeout=config.REQUEST_TIMEOUT)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                elif r.status_code >= 500:
                    rp.disallow_all = True      # RFC 9309: server error -> assume disallowed
                else:
                    rp.allow_all = True         # no robots.txt -> allowed
            except requests.RequestException:
                rp.disallow_all = True
            self._robots[base] = rp
        return rp.can_fetch(config.USER_AGENT.split("/")[0], url)

    def get(self, url):
        page = Page(url=url)
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            page.error = "not a web address"
            return page
        if not _public_host(p.hostname):
            page.error = "address could not be resolved"
            return page
        if not self._robots_ok(url):
            page.robots_blocked, page.error = True, "robots.txt does not allow automated access"
            return page
        self._wait(p.netloc)
        try:
            r = self.s.get(url, timeout=config.REQUEST_TIMEOUT, stream=True, allow_redirects=True)
        except requests.RequestException as e:
            page.error = f"could not connect ({e.__class__.__name__})"
            return page
        page.status, page.final_url = r.status_code, r.url
        if r.status_code in (401, 403, 429, 503):
            page.error = f"site declined automated access (HTTP {r.status_code})"   # we do not try to bypass
            r.close()
            return page
        if r.status_code != 200:
            page.error = f"HTTP {r.status_code}"
            r.close()
            return page
        if "html" not in r.headers.get("Content-Type", "").lower():
            page.error, page.status = "not an HTML page", 0
            r.close()
            return page
        chunks, size = [], 0
        for chunk in r.iter_content(65536):
            chunks.append(chunk)
            size += len(chunk)
            if size > config.MAX_BYTES:
                break
        r.close()
        page.html = b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
        return page
