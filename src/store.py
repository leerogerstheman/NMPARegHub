"""Persist fetched documents, maintain revision history, and compute diffs."""

from __future__ import annotations

import difflib
import hashlib
import sqlite3
from datetime import datetime, timezone

from .classify import (
    agency_from_doc_number,
    canonical_agency,
    classify,
    document_number_from_text,
    infer_status,
    normalise_doc_number,
)
from .fetchers import RawDocument


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _ensure_tag(conn: sqlite3.Connection, name: str, kind: str) -> int:
    conn.execute(
        "INSERT INTO tags (name, kind) VALUES (?, ?) ON CONFLICT (name) DO NOTHING",
        (name, kind),
    )
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    return int(row["id"])


def upsert_document(conn: sqlite3.Connection, doc: RawDocument) -> tuple[int, str]:
    """Insert or update a document.

    Returns ``(document_id, action)`` where action is ``inserted``, ``updated``
    or ``unchanged``.  When the body changes, the *previous* body is archived
    into ``document_revisions`` so a diff remains possible.
    """
    body = doc.body or ""
    content_hash = _hash(body)
    # Search the whole body, not a prefix: the issuing authority is often named
    # well past the first few hundred characters, and a truncated window made
    # the bare "2024年第38号" form unrecognisable.
    doc_number = normalise_doc_number(
        doc.doc_number or document_number_from_text(doc.title + "\n" + body)
    )
    agency = canonical_agency(doc.agency) or agency_from_doc_number(doc_number)
    status = infer_status(body, doc.pub_date)

    existing = conn.execute(
        "SELECT id, content_hash FROM documents WHERE source = ? AND source_id = ?",
        (doc.source, doc.source_id),
    ).fetchone()

    if existing:
        doc_id = int(existing["id"])
        if existing["content_hash"] == content_hash and body:
            return doc_id, "unchanged"

        prev = conn.execute(
            "SELECT body, content_hash FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if prev and prev["body"] and prev["content_hash"] != content_hash:
            next_no = conn.execute(
                "SELECT COALESCE(MAX(revision_no), 0) + 1 AS n "
                "FROM document_revisions WHERE document_id = ?",
                (doc_id,),
            ).fetchone()["n"]
            conn.execute(
                "INSERT INTO document_revisions "
                "(document_id, revision_no, body, content_hash, captured_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (doc_id, next_no, prev["body"], prev["content_hash"], _now()),
            )

        conn.execute(
            """UPDATE documents SET title=?, doc_number=?, agency=?, pub_date=?,
                   category=?, url=?, summary=?, body=?, status=?, fetched_at=?,
                   content_hash=?
               WHERE id=?""",
            (doc.title, doc_number, agency, doc.pub_date, doc.category, doc.url,
             doc.summary, body, status, _now(), content_hash, doc_id),
        )
        action = "updated"
    else:
        cur = conn.execute(
            """INSERT INTO documents
               (source, source_id, title, doc_number, agency, pub_date, category,
                url, summary, body, status, fetched_at, content_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc.source, doc.source_id, doc.title, doc_number, agency, doc.pub_date,
             doc.category, doc.url, doc.summary, body, status, _now(), content_hash),
        )
        doc_id = int(cur.lastrowid)
        action = "inserted"

    # Refresh tags for this document (classification rules may have changed).
    conn.execute("DELETE FROM document_tags WHERE document_id = ?", (doc_id,))
    for name, kind in classify(doc.title, body, doc_number):
        tag_id = _ensure_tag(conn, name, kind)
        conn.execute(
            "INSERT OR IGNORE INTO document_tags (document_id, tag_id) VALUES (?, ?)",
            (doc_id, tag_id),
        )

    return doc_id, action


def store_many(conn: sqlite3.Connection, docs: list[RawDocument], on_progress=None) -> dict:
    """Store a batch inside a single transaction and return action counts."""
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for i, doc in enumerate(docs, 1):
        _, action = upsert_document(conn, doc)
        counts[action] += 1
        if on_progress and i % 25 == 0:
            on_progress(f"  · stored {i}/{len(docs)}")
    conn.commit()
    return counts


def reindex(conn: sqlite3.Connection, on_progress=None) -> dict:
    """Re-run classification and doc-number normalisation over stored rows.

    Classification rules evolve; re-crawling to apply them would be wasteful and
    would hammer the source.  This recomputes derived fields in place from the
    already-stored text.
    """
    rows = conn.execute(
        "SELECT id, title, body, doc_number, agency FROM documents"
    ).fetchall()

    retagged = 0
    renumbered = 0
    for row in rows:
        doc_id = int(row["id"])
        title = row["title"] or ""
        body = row["body"] or ""

        new_number = normalise_doc_number(
            document_number_from_text(title + "\n" + body) or row["doc_number"]
        )
        new_agency = canonical_agency(row["agency"]) or agency_from_doc_number(new_number)
        if new_number != row["doc_number"] or new_agency != row["agency"]:
            conn.execute(
                "UPDATE documents SET doc_number = ?, agency = ? WHERE id = ?",
                (new_number, new_agency, doc_id),
            )
            renumbered += 1

        conn.execute("DELETE FROM document_tags WHERE document_id = ?", (doc_id,))
        for name, kind in classify(title, body, new_number):
            tag_id = _ensure_tag(conn, name, kind)
            conn.execute(
                "INSERT OR IGNORE INTO document_tags (document_id, tag_id) VALUES (?, ?)",
                (doc_id, tag_id),
            )
        retagged += 1
        if on_progress and retagged % 50 == 0:
            on_progress(f"  · reindexed {retagged}/{len(rows)}")

    # Drop tags that no longer apply to anything, so facet counts stay honest.
    conn.execute(
        "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM document_tags)"
    )
    conn.commit()
    return {"retagged": retagged, "renumbered": renumbered}


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------

def unified_diff(conn: sqlite3.Connection, document_id: int,
                 from_rev: int | None = None, to_rev: int | None = None) -> str:
    """Produce a unified diff between two revisions of a document.

    ``from_rev``/``to_rev`` are revision numbers; ``None`` for ``to_rev`` means
    the current text, and ``None`` for ``from_rev`` means the revision
    immediately before ``to_rev``.
    """
    rows = conn.execute(
        "SELECT revision_no, body FROM document_revisions "
        "WHERE document_id = ? ORDER BY revision_no",
        (document_id,),
    ).fetchall()
    revisions = {int(r["revision_no"]): r["body"] for r in rows}

    current = conn.execute(
        "SELECT title, body FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if current is None:
        raise KeyError(f"document {document_id} not found")

    if to_rev is None:
        to_body = current["body"] or ""
        to_label = "current"
        to_rev = (max(revisions) + 1) if revisions else 1
    else:
        to_body = revisions.get(to_rev, "")
        to_label = f"r{to_rev}"

    if from_rev is None:
        earlier = [n for n in revisions if n < to_rev]
        from_rev = max(earlier) if earlier else to_rev
    from_body = revisions.get(from_rev, "")

    diff = difflib.unified_diff(
        (from_body or "").splitlines(),
        (to_body or "").splitlines(),
        fromfile=f"r{from_rev}",
        tofile=to_label,
        lineterm="",
        n=2,
    )
    return "\n".join(diff)
