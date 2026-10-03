"""Offline import for regulation documents the user has lawfully saved.

This exists because ``nmpa.gov.cn``, ``cde.org.cn`` and ``nhc.gov.cn`` block
ordinary HTTP clients with a JavaScript WAF.  Rather than defeat that control,
the intended workflow is: open the page in a browser, save it (HTML or PDF),
and import the file here.

Supported inputs:
  * ``.html`` / ``.htm`` — a page or a "Webpage, complete" save
  * ``.txt``            — plain text
  * ``.md``             — Markdown
  * ``.pdf``            — extracted with a pure-stdlib fallback, or pypdf if present

The importer is deliberately tolerant: it tries hard to recover a title, a
发文字号 and a publication date from whatever it is handed, and it records the
origin file so the provenance of every row is auditable.
"""

from __future__ import annotations

import re
from pathlib import Path

from .classify import document_number_from_text
from .fetchers import RawDocument, strip_html

_META_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_META_DATE_RE = re.compile(
    r'<meta[^>]+(?:name|property)="[^"]*(?:date|time|pubdate)[^"]*"[^>]+'
    r'content="([^"]+)"',
    re.I,
)
_CHINESE_DATE_RE = re.compile(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})")


def _norm_date(value: str | None) -> str | None:
    if not value:
        return None
    m = _CHINESE_DATE_RE.search(value)
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _publication_date(text: str) -> str | None:
    """Find a document's *publication* date.

    A regulation body mentions several dates — effective dates, expiry dates,
    comment deadlines.  The publication date is conventionally the last date in
    the document, printed under the issuing authority's name, so we scan from
    the end rather than taking the first match.
    """
    matches = list(_CHINESE_DATE_RE.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _clean(text: str) -> str:
    """Strip a UTF-8 BOM and stray leading whitespace.

    Files saved by Windows editors routinely carry a BOM; left in place it
    becomes part of the title and breaks equality checks and sorting.
    """
    return (text or "").lstrip("\ufeff\u200b \t\r\n")


def _title_from_text(text: str, fallback: str) -> str:
    """Pick the most plausible title: first substantial non-empty line."""
    for line in _clean(text).splitlines()[:40]:
        line = line.strip()
        if 8 <= len(line) <= 120 and not line.startswith(("http", "第", "—", "-")):
            # Skip obvious navigation/menu fragments.
            if line.count(" ") > 12:
                continue
            return line
    return fallback


def _extract_pdf_text(path: Path) -> str:
    """Extract text from a PDF.

    Prefers ``pypdf`` when installed, otherwise falls back to a minimal
    stdlib-only sweep of the content streams.  The fallback handles the common
    case of uncompressed text operators and is intentionally best-effort.
    """
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        pass
    else:
        try:
            reader = PdfReader(str(path))
            parts = [(page.extract_text() or "") for page in reader.pages]
            text = "\n".join(parts).strip()
            if text:
                return text
        except Exception:
            pass

    # --- stdlib fallback -------------------------------------------------
    import zlib

    raw = path.read_bytes()
    chunks: list[str] = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        data = m.group(1)
        try:
            data = zlib.decompress(data)
        except zlib.error:
            continue
        for tm in re.finditer(rb"\((?:\\.|[^\\()])*\)\s*Tj", data):
            lit = tm.group(0)
            lit = lit[lit.find(b"(") + 1 : lit.rfind(b")")]
            lit = lit.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
            try:
                chunks.append(lit.decode("utf-8", errors="replace"))
            except Exception:
                continue
        for tj in re.finditer(rb"\[(.*?)\]\s*TJ", data, re.S):
            for lit in re.finditer(rb"\((?:\\.|[^\\()])*\)", tj.group(1)):
                s = lit.group(0)[1:-1]
                chunks.append(s.decode("utf-8", errors="replace"))
    return "\n".join(chunks).strip()


def load_file(path: str | Path, *, source: str = "manual") -> RawDocument:
    """Load one local file into a :class:`RawDocument`."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)

    suffix = p.suffix.lower()
    if suffix in (".html", ".htm"):
        raw = p.read_text(encoding="utf-8", errors="replace")
        title = ""
        m = _H1_RE.search(raw) or _META_TITLE_RE.search(raw)
        if m:
            title = strip_html(m.group(1))
        body = _clean(strip_html(raw))
        date = None
        dm = _META_DATE_RE.search(raw)
        if dm:
            date = _norm_date(dm.group(1))
        if not date:
            date = _publication_date(body)
    elif suffix == ".pdf":
        body = _clean(_extract_pdf_text(p))
        title = _title_from_text(body, p.stem)
        date = _publication_date(body)
    elif suffix in (".txt", ".md"):
        body = _clean(p.read_text(encoding="utf-8", errors="replace"))
        title = _title_from_text(body, p.stem)
        date = _publication_date(body)
    else:
        raise ValueError(f"unsupported file type: {suffix}")

    title = _clean(title)
    if not title:
        title = p.stem

    # Search the whole body: the 发文字号 is often printed after the preamble.
    doc_number = document_number_from_text(title) or document_number_from_text(body)
    agency = None
    am = re.search(r"(国家药品监督管理局|国家药监局|国家卫生健康委员会|"
                   r"国家医疗保障局|国家药典委员会|国务院办公厅|国务院)", body[:2000])
    if am:
        agency = am.group(1)

    return RawDocument(
        source=source,
        source_id=str(p.resolve()),
        title=title,
        doc_number=doc_number,
        agency=agency,
        pub_date=date,
        category=None,
        url=None,
        summary=body[:300],
        body=body,
        extra={"imported_from": str(p.resolve())},
    )


def load_directory(directory: str | Path, *, source: str = "manual",
                   on_progress=None) -> tuple[list[RawDocument], list[str]]:
    """Load every supported file in a directory tree.

    Returns ``(documents, errors)``.
    """
    root = Path(directory)
    docs: list[RawDocument] = []
    errors: list[str] = []
    patterns = ("*.html", "*.htm", "*.pdf", "*.txt", "*.md")
    files: list[Path] = []
    for pattern in patterns:
        files.extend(sorted(root.rglob(pattern)))

    for f in files:
        try:
            docs.append(load_file(f, source=source))
        except Exception as exc:  # noqa: BLE001 - report and continue
            errors.append(f"{f}: {exc}")
        if on_progress and len(docs) % 20 == 0 and docs:
            on_progress(f"  · imported {len(docs)} files")

    return docs, errors
