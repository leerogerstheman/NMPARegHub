"""Unit tests for the knowledge base.

Run standalone::

    python tests/test_regkb.py

or with pytest if installed.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db, store  # noqa: E402
from src.classify import (  # noqa: E402
    canonical_agency,
    classify,
    document_number_from_text,
    infer_status,
    normalise_doc_number,
)
from src.fetchers import RawDocument, strip_html  # noqa: E402
from src.search import parse_query, search  # noqa: E402


# ---------------------------------------------------------------- doc numbers

def test_doc_number_bracketed():
    got = document_number_from_text("国务院办公厅关于健全药品价格形成机制的若干意见 国办发〔2026〕9号")
    assert got == "国办发〔2026〕9号", got


def test_doc_number_decree():
    got = document_number_from_text("中华人民共和国药品管理法实施条例 国令第828号")
    assert got == "国令第828号", got


def test_doc_number_nmpa_long_form():
    text = "国家药监局关于发布《化妆品注册备案资料管理规定》的公告 药监局公告2021年第32号"
    got = document_number_from_text(text)
    assert got == "国药监〔2021〕32号", got


def test_doc_number_bare_form_needs_authority():
    """A bare '2024年第38号' is only a 发文字号 when a drug authority is named."""
    with_authority = "国家药监局关于某事项的通告 (2024年第38号)"
    assert document_number_from_text(with_authority) == "国药监〔2024〕38号"

    without = "某公司内部通知 2024年第38号"
    assert document_number_from_text(without) is None


def test_doc_number_ignores_prose():
    """A permissive prefix used to swallow ordinary prose. Guard against that."""
    prose = "关于暂行延长药品注册申请补充资料时限的公告"
    assert document_number_from_text(prose) is None


def test_normalise_doc_number():
    assert normalise_doc_number("药监局公告2021年第31号") == "国药监〔2021〕31号"
    assert normalise_doc_number("国办发〔2026〕9号") == "国办发〔2026〕9号"
    assert normalise_doc_number(None) is None


# --------------------------------------------------------------- agency names

def test_canonical_agency_collapses_aliases():
    for variant in ("药监局", "国家药监局", "国家药品监督管理局"):
        assert canonical_agency(variant) == "国家药品监督管理局"


# --------------------------------------------------------------- classify

def test_classify_cosmetics_not_misfiled_as_drug_registration():
    """'化妆品注册备案' contains 注册 but is not a drug registration document."""
    tags = classify(
        "国家药监局关于实施《化妆品注册备案资料管理规定》有关事项的公告",
        "为规范化妆品注册备案资料管理，现就有关事项公告如下。",
    )
    labels = [name for name, _ in tags]
    assert "化妆品" in labels, labels
    assert "药品注册" not in labels, labels


def test_classify_ich_detected():
    tags = classify(
        "国家药监局关于适用《E6（R3）：药物临床试验质量管理规范技术指导原则》的公告",
        "为持续推动药品注册技术标准与国际接轨，决定适用 E6（R3）指导原则。",
    )
    assert ("ICH 有效性/GCP", "ich") in tags, tags


def test_classify_pharmacovigilance():
    tags = classify(
        "国家药监局关于发布《药物警戒质量管理规范》的公告",
        "为规范和指导药品上市许可持有人的药物警戒活动，制定本规范。",
    )
    assert ("药物警戒", "topic") in tags, tags


def test_classify_returns_unique_pairs():
    tags = classify("药品注册管理办法", "药品注册 药品注册 药品注册")
    assert len(tags) == len(set(tags))


# --------------------------------------------------------------- status

def test_infer_status_in_force():
    assert infer_status("本规范自2021年12月1日起正式施行。", "2021-11-29") == "in_force"


def test_infer_status_abolished_only_from_tail():
    text = "依据《药品管理法》，本公告自发布之日起施行。" + ("正文。" * 200) + "本公告自2024年1月1日起废止。"
    assert infer_status(text, "2020-01-01") == "abolished"


def test_infer_status_unknown_when_silent():
    assert infer_status("本公告公布了有关事项。", "2020-01-01") == "unknown"


# --------------------------------------------------------------- html

def test_strip_html_collapses_markup():
    got = strip_html("<p>药品<b>注册</b></p><script>x=1</script><p>管理</p>")
    assert "药品注册" in got
    assert "管理" in got
    assert "x=1" not in got
    assert "<" not in got


# --------------------------------------------------------------- query parsing

def test_parse_query_splits_exclusions():
    pos, neg = parse_query('注册 -化妆品 "精确短语"')
    assert pos == ["注册", "精确短语"], pos
    assert neg == ["化妆品"], neg


def test_parse_query_strips_leading_plus():
    pos, neg = parse_query("+药品注册")
    assert pos == ["药品注册"], pos


# --------------------------------------------------------------- integration

def _fresh_conn(tmp: Path):
    return db.connect(tmp / "test.sqlite")


@contextlib.contextmanager
def _db():
    """Yield a connection to a throwaway database, then close it.

    Closing matters on Windows: an open SQLite handle keeps the temp directory
    locked and ``TemporaryDirectory`` cleanup then raises PermissionError.
    """
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_conn(Path(td))
        try:
            yield conn
        finally:
            db.close(conn)


def _doc(source_id: str, title: str, body: str, **kw) -> RawDocument:
    return RawDocument(source="test", source_id=source_id, title=title, body=body, **kw)


def test_roundtrip_and_search():
    with _db() as conn:
        docs = [
            _doc("1", "国家药监局关于发布《药物警戒质量管理规范》的公告",
                 "为规范药物警戒活动，制定本规范，自2021年12月1日起施行。"),
            _doc("2", "国家药监局关于发布《化妆品注册备案资料管理规定》的公告",
                 "为规范化妆品注册备案资料管理，现予公布。"),
            _doc("3", "国务院办公厅关于健全药品价格形成机制的若干意见",
                 "为健全药品价格形成机制，经国务院同意，现提出如下意见。"),
        ]
        counts = store.store_many(conn, docs)
        assert counts["inserted"] == 3, counts

        # Long (>=3 char) terms go through the FTS5 trigram index.
        hits, total = search(conn, "药物警戒")
        assert total == 1, (total, [h.title for h in hits])

        # Two-character terms must still work via the LIKE fallback.
        hits, total = search(conn, "药品")
        assert total == 1, (total, [h.title for h in hits])

        # Only the cosmetics document contains 注册, so excluding 化妆品 leaves
        # nothing.  This guards the exclusion predicate against the SQL
        # three-valued-logic trap where `doc_number NOT LIKE ?` is NULL.
        hits, total = search(conn, "注册 -化妆品")
        assert total == 0, (total, [h.title for h in hits])

        # An exclusion-only query must match everything else.  This returned 0
        # before COALESCE was added around the NOT LIKE predicate.
        hits, total = search(conn, "-化妆品")
        assert total == 2, (total, [h.title for h in hits])

        # Tag facet filtering.
        hits, total = search(conn, "公告", tag="药物警戒")
        assert total == 1, (total, [h.title for h in hits])


def test_unchanged_documents_are_not_duplicated():
    with _db() as conn:
        store.store_many(conn, [_doc("1", "药品管理法", "第一条 为加强药品管理。")])
        counts = store.store_many(conn, [_doc("1", "药品管理法", "第一条 为加强药品管理。")])
        assert counts["unchanged"] == 1, counts
        assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 1


def test_revision_history_and_diff():
    with _db() as conn:
        did, _ = store.upsert_document(
            conn, _doc("1", "某规范", "第一条 自2021年12月1日起施行。")
        )
        conn.commit()
        store.upsert_document(
            conn, _doc("1", "某规范", "第一条 自2026年1月1日起施行。")
        )
        conn.commit()

        revs = conn.execute(
            "SELECT COUNT(*) AS n FROM document_revisions WHERE document_id = ?", (did,)
        ).fetchone()["n"]
        assert revs == 1, revs

        diff = store.unified_diff(conn, did)
        assert "2021年12月1日" in diff, diff
        assert "2026年1月1日" in diff, diff


def test_reindex_applies_new_rules():
    with _db() as conn:
        # Insert with a deliberately wrong doc number, then let reindex fix it.
        store.store_many(conn, [_doc(
            "1", "国家药监局关于某事项的公告",
            "国家药监局关于某事项的公告 药监局公告2021年第32号",
            doc_number="垃圾数据",
        )])
        store.reindex(conn)
        row = conn.execute("SELECT doc_number FROM documents WHERE id = 1").fetchone()
        assert row["doc_number"] in (None, "国药监〔2021〕32号"), row["doc_number"]


def test_fts_index_stays_in_sync_on_delete():
    with _db() as conn:
        store.store_many(conn, [_doc("1", "药物警戒规范", "药物警戒内容。")])
        conn.execute("DELETE FROM documents WHERE source_id = '1'")
        conn.commit()
        hits, total = search(conn, "药物警戒")
        assert total == 0, (total, [h.title for h in hits])


def _run_all() -> int:
    tests = [
        (name, obj) for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
