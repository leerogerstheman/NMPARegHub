"""Full-text search and faceting over the knowledge base.

The FTS index uses the ``trigram`` tokenizer, which needs queries of at least
three characters.  Shorter queries (very common in Chinese: 药品, 注册, 医保)
are handled by a LIKE-based fallback so the tool never returns "no results" for
a perfectly reasonable two-character search.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass


@dataclass
class SearchHit:
    id: int
    title: str
    doc_number: str | None
    agency: str | None
    pub_date: str | None
    status: str
    url: str | None
    source: str
    snippet: str
    score: float
    tags: list[str]


def _fts_quote(term: str) -> str:
    """Quote a term for FTS5. Doubling embedded quotes is the documented escape."""
    return '"' + term.replace('"', '""') + '"'


def parse_query(query: str) -> tuple[list[str], list[str]]:
    """Split raw user input into ``(positive_terms, excluded_terms)``.

    Quoted phrases are kept together.  A leading ``-`` marks an exclusion and any
    leading ``+`` is stripped (it is the FTS5 explicit-AND operator and users
    paste it from search engines).
    """
    positive: list[str] = []
    negative: list[str] = []
    for token in re.findall(r'"[^"]+"|\S+', query.strip()):
        if token.startswith("-") and len(token) > 1:
            body = token[1:].strip('"')
            if body:
                negative.append(body)
            continue
        body = token.lstrip("+").strip('"')
        if body:
            positive.append(body)
    return positive, negative


def build_match_query(positive: list[str], negative: list[str]) -> str:
    """Build an FTS5 MATCH expression from pre-split terms.

    Only terms of three or more characters are eligible: the trigram tokenizer
    used by the index cannot match shorter strings, so those are left to the LIKE
    path.  Exclusions are *not* expressed here — they are applied as SQL
    predicates by :func:`search`, because an FTS ``NOT`` still requires a
    positive term to be present and would silently return nothing otherwise.
    """
    return " ".join(_fts_quote(t) for t in positive if len(t) >= 3)


def _snippet(body: str, terms: list[str], width: int = 160) -> str:
    """Return a context window around the first matching term."""
    text = (body or "").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    for term in terms:
        idx = text.find(term)
        if idx >= 0:
            start = max(0, idx - width // 3)
            end = min(len(text), start + width)
            frag = text[start:end]
            if start > 0:
                frag = "…" + frag
            if end < len(text):
                frag = frag + "…"
            return frag
    return text[:width] + ("…" if len(text) > width else "")


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    offset: int = 0,
    tag: str | None = None,
    agency: str | None = None,
    status: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    source: str | None = None,
) -> tuple[list[SearchHit], int]:
    """Search documents. Returns ``(hits, total_count)``."""
    where: list[str] = []
    params: list = []

    if tag:
        where.append(
            "d.id IN (SELECT dt.document_id FROM document_tags dt "
            "JOIN tags t ON t.id = dt.tag_id WHERE t.name = ?)"
        )
        params.append(tag)
    if agency:
        where.append("d.agency LIKE ?")
        params.append(f"%{agency}%")
    if status:
        where.append("d.status = ?")
        params.append(status)
    if source:
        where.append("d.source = ?")
        params.append(source)
    if year_from is not None:
        where.append("d.pub_date >= ?")
        params.append(f"{year_from}-01-01")
    if year_to is not None:
        where.append("d.pub_date <= ?")
        params.append(f"{year_to}-12-31")

    positive, negative = parse_query(query)
    # Trigram needs >=3 chars; shorter terms must go through LIKE.
    fts_terms = [t for t in positive if len(t) >= 3]
    like_terms = [t for t in positive if len(t) < 3]
    match_expr = build_match_query(positive, negative)

    if match_expr:
        base_from = "documents_fts f JOIN documents d ON d.id = f.rowid"
        where.insert(0, "documents_fts MATCH ?")
        params.insert(0, match_expr)
        select_score = "bm25(documents_fts) AS score"
        order = "score ASC, d.pub_date DESC"
    else:
        base_from = "documents d"
        select_score = "0.0 AS score"
        order = "d.pub_date DESC"

    # Short positive terms are ANDed as LIKE predicates (works in both paths).
    for t in like_terms:
        where.append("(d.title LIKE ? OR d.body LIKE ? OR d.doc_number LIKE ?)")
        params.extend([f"%{t}%", f"%{t}%", f"%{t}%"])

    # Exclusions are always SQL predicates.  Expressing them as FTS "NOT" makes
    # an exclusion-only query unmatchable, and silently zeroes results.
    #
    # COALESCE is required: `doc_number NOT LIKE ?` is NULL when doc_number is
    # NULL, and `NULL AND x` is never true, so a plain NOT LIKE would silently
    # drop every document that has no 发文字号.
    for t in negative:
        where.append(
            "(COALESCE(d.title,'') NOT LIKE ? "
            "AND COALESCE(d.body,'') NOT LIKE ? "
            "AND COALESCE(d.doc_number,'') NOT LIKE ?)"
        )
        params.extend([f"%{t}%", f"%{t}%", f"%{t}%"])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    try:
        total = int(conn.execute(
            f"SELECT COUNT(*) AS n FROM {base_from}{where_sql}", params
        ).fetchone()["n"])
        rows = conn.execute(
            f"SELECT d.id, d.title, d.doc_number, d.agency, d.pub_date, d.status, "
            f"d.url, d.source, d.body, {select_score} "
            f"FROM {base_from}{where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    except sqlite3.OperationalError:
        # A malformed FTS expression must not take down the caller. Re-run as a
        # plain LIKE search over the same filters.
        return _like_fallback(
            conn, positive, negative,
            where=where[1:] if match_expr else where,
            params=params[1:] if match_expr else params,
            limit=limit, offset=offset,
        )

    return _rows_to_hits(conn, rows, positive), total


def _like_fallback(
    conn: sqlite3.Connection,
    positive: list[str],
    negative: list[str],
    *,
    where: list[str],
    params: list,
    limit: int,
    offset: int,
) -> tuple[list["SearchHit"], int]:
    """LIKE-based search used when an FTS expression cannot be parsed."""
    local = list(where)
    local_params = list(params)
    for t in positive:
        local.append("(d.title LIKE ? OR d.body LIKE ? OR d.doc_number LIKE ?)")
        local_params.extend([f"%{t}%"] * 3)
    for t in negative:
        local.append(
            "(COALESCE(d.title,'') NOT LIKE ? "
            "AND COALESCE(d.body,'') NOT LIKE ? "
            "AND COALESCE(d.doc_number,'') NOT LIKE ?)"
        )
        local_params.extend([f"%{t}%"] * 3)

    where_sql = (" WHERE " + " AND ".join(local)) if local else ""
    total = int(conn.execute(
        f"SELECT COUNT(*) AS n FROM documents d{where_sql}", local_params
    ).fetchone()["n"])
    rows = conn.execute(
        f"SELECT d.id, d.title, d.doc_number, d.agency, d.pub_date, d.status, "
        f"d.url, d.source, d.body, 0.0 AS score FROM documents d{where_sql} "
        f"ORDER BY d.pub_date DESC LIMIT ? OFFSET ?",
        local_params + [limit, offset],
    ).fetchall()
    return _rows_to_hits(conn, rows, positive), total


def _rows_to_hits(conn: sqlite3.Connection, rows, terms: list[str]) -> list["SearchHit"]:
    hits: list[SearchHit] = []
    for row in rows:
        tag_rows = conn.execute(
            "SELECT t.name FROM document_tags dt JOIN tags t ON t.id = dt.tag_id "
            "WHERE dt.document_id = ? ORDER BY t.kind, t.name",
            (row["id"],),
        ).fetchall()
        hits.append(SearchHit(
            id=int(row["id"]),
            title=row["title"],
            doc_number=row["doc_number"],
            agency=row["agency"],
            pub_date=row["pub_date"],
            status=row["status"],
            url=row["url"],
            source=row["source"],
            snippet=_snippet(row["body"] or "", terms),
            score=float(row["score"] or 0.0),
            tags=[r["name"] for r in tag_rows],
        ))
    return hits


def facets(conn: sqlite3.Connection) -> dict[str, list[tuple[str, int]]]:
    """Facet counts for tags, agencies, years and status."""
    tags = conn.execute(
        "SELECT t.name, t.kind, COUNT(*) AS n FROM tags t "
        "JOIN document_tags dt ON dt.tag_id = t.id "
        "GROUP BY t.id ORDER BY n DESC, t.name"
    ).fetchall()
    agencies = conn.execute(
        "SELECT COALESCE(agency,'(未知)') AS a, COUNT(*) AS n FROM documents "
        "GROUP BY a ORDER BY n DESC LIMIT 30"
    ).fetchall()
    years = conn.execute(
        "SELECT substr(pub_date,1,4) AS y, COUNT(*) AS n FROM documents "
        "WHERE pub_date IS NOT NULL AND pub_date != '' GROUP BY y ORDER BY y DESC"
    ).fetchall()
    statuses = conn.execute(
        "SELECT status, COUNT(*) AS n FROM documents GROUP BY status ORDER BY n DESC"
    ).fetchall()

    by_kind: dict[str, list[tuple[str, int]]] = {}
    for r in tags:
        by_kind.setdefault(r["kind"], []).append((r["name"], int(r["n"])))
    by_kind["agency_facet"] = [(r["a"], int(r["n"])) for r in agencies]
    by_kind["year"] = [(r["y"], int(r["n"])) for r in years if r["y"]]
    by_kind["status"] = [(r["status"], int(r["n"])) for r in statuses]
    return by_kind
