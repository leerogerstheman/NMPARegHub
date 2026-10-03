"""SQLite schema and full-text index for the drug-regulation knowledge base.

Design notes
------------
* Documents live in ``documents``; their searchable body text lives in the FTS5
  virtual table ``documents_fts``.  The two are kept in sync by triggers so the
  index can never drift from the data.
* Chinese text is indexed with the ``trigram`` tokenizer.  The default
  ``unicode61`` tokenizer treats a whole CJK sentence as one token, which makes
  searching useless; trigram gives substring matching that behaves the way a
  Chinese reader expects, and it is built into SQLite so we keep zero
  third-party dependencies.
* Tags (ICH / CTD / 法规类别) are stored in a join table rather than a comma
  string so that facet counts are exact.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Stable natural key: where the document came from plus its own id.
    source        TEXT NOT NULL,             -- gov.cn | chp | manual
    source_id     TEXT NOT NULL,
    title         TEXT NOT NULL,
    doc_number    TEXT,                      -- 发文字号, e.g. 国办发〔2026〕9号
    agency        TEXT,                      -- 发文机关
    pub_date      TEXT,                      -- ISO-8601 date, sortable
    category      TEXT,                      -- 卫生、体育\医药管理
    url           TEXT,
    summary       TEXT,
    body          TEXT,                      -- full text
    -- Effective/abolished tracking.  Regulations are frequently superseded and
    -- a registration specialist needs to know which text is currently in force.
    effective_date TEXT,
    abolished_date TEXT,
    status        TEXT NOT NULL DEFAULT 'unknown',  -- in_force | abolished | unknown
    fetched_at    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_documents_pub_date ON documents (pub_date DESC);
CREATE INDEX IF NOT EXISTS idx_documents_agency   ON documents (agency);
CREATE INDEX IF NOT EXISTS idx_documents_status   ON documents (status);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'topic'       -- topic | ich | ctd | category
);

CREATE TABLE IF NOT EXISTS document_tags (
    document_id INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags (id)      ON DELETE CASCADE,
    PRIMARY KEY (document_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_document_tags_tag ON document_tags (tag_id);

-- Version history: every time a document's body changes we keep the old text so
-- that a diff can be produced.  Silently overwriting a regulation on re-crawl
-- would destroy exactly the information a compliance user cares about.
CREATE TABLE IF NOT EXISTS document_revisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    revision_no  INTEGER NOT NULL,
    body         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    UNIQUE (document_id, revision_no)
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5 (
    title,
    body,
    doc_number,
    agency,
    content = 'documents',
    content_rowid = 'id',
    tokenize = 'trigram'
);

-- Keep the FTS index in lockstep with the base table.
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts (rowid, title, body, doc_number, agency)
    VALUES (new.id, new.title, new.body, new.doc_number, new.agency);
END;

CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts (documents_fts, rowid, title, body, doc_number, agency)
    VALUES ('delete', old.id, old.title, old.body, old.doc_number, old.agency);
END;

CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts (documents_fts, rowid, title, body, doc_number, agency)
    VALUES ('delete', old.id, old.title, old.body, old.doc_number, old.agency);
    INSERT INTO documents_fts (rowid, title, body, doc_number, agency)
    VALUES (new.id, new.title, new.body, new.doc_number, new.agency);
END;
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the knowledge base and apply the schema."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def close(conn: sqlite3.Connection) -> None:
    """Close a connection, releasing the file lock.

    Windows will not let a directory containing an open SQLite database be
    removed, so callers that use temporary directories must close explicitly.
    """
    try:
        conn.commit()
    except sqlite3.Error:
        pass
    conn.close()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default
