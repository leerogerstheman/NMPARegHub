"""Fetchers for Chinese drug-regulation sources.

Compliance posture
------------------
This module deliberately talks only to endpoints that serve public documents to
ordinary HTTP clients:

* ``gov.cn`` 政策文件库 — the State Council's own policy library, which exposes a
  documented JSON search endpoint and server-rendered article pages.
* ``chp.org.cn`` 国家药典委员会 — server-rendered pages.

``nmpa.gov.cn``, ``cde.org.cn`` and ``nhc.gov.cn`` are **not** fetched.  They sit
behind a JavaScript proof-of-work WAF that returns HTTP 412/202 with an
obfuscated challenge script.  Passing that challenge means defeating a security
control the site owner deliberately deployed, so this project does not do it.
Documents from those bodies are brought in through :mod:`offline_import` instead,
which is the intended workflow for material a user has lawfully saved.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_DELAY = 1.2  # seconds between requests; be a good citizen
REQUEST_TIMEOUT = 30


class FetchError(RuntimeError):
    pass


@dataclass
class RawDocument:
    source: str
    source_id: str
    title: str
    doc_number: str | None = None
    agency: str | None = None
    pub_date: str | None = None
    category: str | None = None
    url: str | None = None
    summary: str | None = None
    body: str = ""
    extra: dict = field(default_factory=dict)


def _request(url: str, *, delay: float = DEFAULT_DELAY) -> bytes:
    if delay:
        time.sleep(delay)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            enc = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"network error for {url}: {exc.reason}") from exc
    try:
        return raw.decode(enc, errors="replace").encode("utf-8")
    except LookupError:
        return raw


def _get_text(url: str, *, delay: float = DEFAULT_DELAY) -> str:
    return _request(url, delay=delay).decode("utf-8", errors="replace")


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)


def strip_html(fragment: str) -> str:
    """Convert an HTML fragment to readable plain text."""
    if not fragment:
        return ""
    text = _SCRIPT_RE.sub(" ", fragment)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html_mod.unescape(text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _norm_date(value: str | None) -> str | None:
    """Normalise the several date shapes the sources use to ISO-8601."""
    if not value:
        return None
    value = value.strip()
    m = re.match(r"(\d{4})[.\-/年](\d{1,2})[.\-/月](\d{1,2})", value)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


# --------------------------------------------------------------------------
# gov.cn 政策文件库
# --------------------------------------------------------------------------

GOV_SEARCH_ENDPOINT = "https://sousuo.www.gov.cn/search-gov/data"

# Document-type facets exposed by the policy library.
GOV_DOC_TYPES = {
    "zhengcelibrary_gw": "国务院文件",
    "zhengcelibrary_bm": "部门文件",
}


def search_gov_cn(
    keyword: str,
    *,
    page: int = 1,
    page_size: int = 20,
    doc_type: str = "zhengcelibrary_gw",
    search_field: str = "title",
    delay: float = DEFAULT_DELAY,
) -> tuple[list[RawDocument], int]:
    """Query the State Council policy library.

    Returns ``(documents, total_count)``.  ``search_field`` is ``title`` or
    ``content``; title search is the sensible default because a body search on a
    common word returns everything.
    """
    params = {
        "t": doc_type,
        "q": keyword,
        "timetype": "timeqb",
        "mintime": "",
        "maxtime": "",
        "sort": "pubtime",
        "sortType": "1",
        "searchfield": search_field,
        "pcodeJiguan": "",
        "childtype": "",
        "subchildtype": "",
        "tsbq": "",
        "pubtimeyear": "",
        "puborg": "",
        "pcodeYear": "",
        "pcodeNum": "",
        "filetype": "",
        "p": str(page),
        "n": str(page_size),
        "inpro": "",
        "bmfl": "",
        "dup": "",
        "orpro": "",
        "bmpubyear": "",
    }
    url = f"{GOV_SEARCH_ENDPOINT}?{urllib.parse.urlencode(params)}"
    text = _get_text(url, delay=delay)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchError(f"gov.cn returned non-JSON for {url}: {text[:200]}") from exc

    search_vo = payload.get("searchVO") or {}
    total = int(search_vo.get("totalCount") or 0)
    items = search_vo.get("listVO") or []

    docs: list[RawDocument] = []
    for item in items:
        title = strip_html(item.get("title") or "")
        # The API returns the document id; the article URL is in `url`.
        link = item.get("url") or ""
        source_id = str(item.get("id") or link or title)
        docs.append(
            RawDocument(
                source="gov.cn",
                source_id=source_id,
                title=title,
                doc_number=(item.get("pcode") or "").strip() or None,
                agency=(item.get("puborg") or "").strip() or None,
                pub_date=_norm_date(item.get("pubtimeStr")),
                category=(item.get("childtype") or "").strip() or None,
                url=link or None,
                summary=strip_html(item.get("summary") or ""),
                body="",
            )
        )
    return docs, total


_CONTENT_CONTAINERS = [
    re.compile(r'<div[^>]*id="UCAP-CONTENT"[^>]*>(.*?)</div>\s*</div>', re.S | re.I),
    re.compile(r'<div[^>]*class="[^"]*pages_content[^"]*"[^>]*>(.*?)</div>\s*</div>', re.S | re.I),
    re.compile(r'<div[^>]*id="UCAP-CONTENT"[^>]*>(.*)', re.S | re.I),
]


def fetch_gov_article(url: str, *, delay: float = DEFAULT_DELAY) -> str:
    """Fetch and extract the body text of a gov.cn article page."""
    page = _get_text(url, delay=delay)
    for pattern in _CONTENT_CONTAINERS:
        m = pattern.search(page)
        if m:
            body = strip_html(m.group(1))
            if len(body) > 100:
                return body
    # Fall back to the whole page minus navigation chrome.
    return strip_html(page)


def crawl_gov_cn(
    keywords: list[str],
    *,
    pages: int = 1,
    page_size: int = 20,
    doc_types: list[str] | None = None,
    fetch_bodies: bool = True,
    delay: float = DEFAULT_DELAY,
    on_progress=None,
) -> list[RawDocument]:
    """Search several keywords and optionally pull each article's full text."""
    doc_types = doc_types or ["zhengcelibrary_gw", "zhengcelibrary_bm"]
    collected: dict[tuple[str, str], RawDocument] = {}

    for doc_type in doc_types:
        for keyword in keywords:
            for page in range(1, pages + 1):
                try:
                    docs, total = search_gov_cn(
                        keyword, page=page, page_size=page_size,
                        doc_type=doc_type, delay=delay,
                    )
                except FetchError as exc:
                    if on_progress:
                        on_progress(f"  ! {keyword} [{doc_type}] p{page}: {exc}")
                    continue
                if on_progress:
                    on_progress(
                        f"  · {keyword} [{GOV_DOC_TYPES.get(doc_type, doc_type)}] "
                        f"p{page}: {len(docs)}/{total}"
                    )
                if not docs:
                    break
                for doc in docs:
                    collected.setdefault((doc.source, doc.source_id), doc)

    results = list(collected.values())

    if fetch_bodies:
        for i, doc in enumerate(results, 1):
            if not doc.url:
                continue
            try:
                doc.body = fetch_gov_article(doc.url, delay=delay)
            except FetchError as exc:
                if on_progress:
                    on_progress(f"  ! body {doc.title[:30]}: {exc}")
                doc.body = doc.summary or ""
            if on_progress and (i % 10 == 0 or i == len(results)):
                on_progress(f"  · fetched bodies {i}/{len(results)}")

    return results


# --------------------------------------------------------------------------
# 国家药典委员会 chp.org.cn
# --------------------------------------------------------------------------

CHP_BASE = "https://www.chp.org.cn"


def crawl_chp(*, delay: float = DEFAULT_DELAY, on_progress=None) -> list[RawDocument]:
    """Collect 药典委 news/announcement items.

    The site is a typical server-rendered CMS; we follow its listing pages and
    keep the entries that look like notices or standards announcements.
    """
    listing_paths = ["/", "/news", "/notice"]
    docs: dict[str, RawDocument] = {}

    for path in listing_paths:
        url = CHP_BASE + path
        try:
            page = _get_text(url, delay=delay)
        except FetchError as exc:
            if on_progress:
                on_progress(f"  ! chp {path}: {exc}")
            continue

        for m in re.finditer(
            r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S | re.I
        ):
            href, inner = m.group(1), strip_html(m.group(2))
            if not inner or len(inner) < 6:
                continue
            if not re.search(r"(公告|通知|标准|药典|公示|征求意见)", inner):
                continue
            if href.startswith("/"):
                href = CHP_BASE + href
            elif not href.startswith("http"):
                continue
            docs.setdefault(href, RawDocument(
                source="chp", source_id=href, title=inner, url=href,
                agency="国家药典委员会",
            ))
        if on_progress:
            on_progress(f"  · chp {path}: {len(docs)} items so far")

    for doc in docs.values():
        try:
            doc.body = strip_html(_get_text(doc.url, delay=delay))
        except FetchError:
            doc.body = doc.title
        m = re.search(r"(\d{4})[-./年](\d{1,2})[-./月](\d{1,2})", doc.body)
        if m:
            doc.pub_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    return list(docs.values())
