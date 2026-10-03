"""Command-line interface for the drug-regulation knowledge base."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow ``python regkb.py`` from the project root as well as ``python -m src.cli``.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src import db, fetchers, offline_import, search as search_mod, store
else:
    from . import db, fetchers, offline_import, search as search_mod, store

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "regkb.sqlite"

# Prefer UTF-8 on stdout so Chinese text and check marks render correctly even
# on a GBK Windows console.  ``reconfigure`` exists on Python 3.7+.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

# The keyword set used for the default crawl.  Chosen to cover the topics a
# registration / market-access specialist actually works with.
DEFAULT_KEYWORDS = [
    "药品", "药品注册", "药品管理法", "药品生产", "药品经营",
    "药物警戒", "药品标准", "药物临床试验", "疫苗", "中药",
]


def _p(msg: str) -> None:
    """Print, tolerating consoles that cannot encode every character.

    Windows consoles frequently run a GBK code page, which cannot represent
    characters such as ✓ or the box-drawing glyphs used below.  Rather than
    crash the whole crawl at the reporting step, fall back to a transliteration
    and then to a lossy write.
    """
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        safe = (msg.replace("✓", "[OK]").replace("→", "->")
                   .replace("·", "-").replace("─", "-").replace("…", "..."))
        try:
            print(safe, flush=True)
        except UnicodeEncodeError:
            sys.stdout.buffer.write(
                (safe + "\n").encode(enc, errors="replace")
            )
            sys.stdout.flush()


def cmd_crawl(args) -> int:
    conn = db.connect(args.db)
    keywords = args.keyword or DEFAULT_KEYWORDS
    _p(f"→ crawling gov.cn policy library ({len(keywords)} keywords, "
       f"{args.pages} page(s) each)…")
    docs = fetchers.crawl_gov_cn(
        keywords,
        pages=args.pages,
        page_size=args.page_size,
        fetch_bodies=not args.no_body,
        delay=args.delay,
        on_progress=_p,
    )
    _p(f"→ fetched {len(docs)} documents; storing…")
    counts = store.store_many(conn, docs, on_progress=_p)
    _p(f"✓ {counts}")
    _print_stats(conn)
    return 0


def cmd_crawl_chp(args) -> int:
    conn = db.connect(args.db)
    _p("→ crawling 国家药典委员会…")
    docs = fetchers.crawl_chp(delay=args.delay, on_progress=_p)
    if not docs:
        _p("! no documents found (site layout may have changed)")
        return 1
    counts = store.store_many(conn, docs, on_progress=_p)
    _p(f"✓ {counts}")
    _print_stats(conn)
    return 0


def cmd_import(args) -> int:
    conn = db.connect(args.db)
    target = Path(args.path)
    _p(f"→ importing from {target}…")
    if target.is_dir():
        docs, errors = offline_import.load_directory(target, on_progress=_p)
    else:
        docs, errors = [offline_import.load_file(target)], []
    if errors:
        _p(f"! {len(errors)} file(s) skipped:")
        for e in errors[:10]:
            _p(f"    {e}")
    if not docs:
        _p("! nothing imported")
        return 1
    counts = store.store_many(conn, docs, on_progress=_p)
    _p(f"✓ {counts}")
    _print_stats(conn)
    return 0


def cmd_search(args) -> int:
    conn = db.connect(args.db)
    hits, total = search_mod.search(
        conn, args.query, limit=args.limit, tag=args.tag,
        agency=args.agency, status=args.status,
        year_from=args.year_from, year_to=args.year_to,
    )
    if args.json:
        _p(json.dumps(
            [{"id": h.id, "title": h.title, "doc_number": h.doc_number,
              "agency": h.agency, "pub_date": h.pub_date, "status": h.status,
              "url": h.url, "tags": h.tags, "snippet": h.snippet} for h in hits],
            ensure_ascii=False, indent=2))
        return 0

    _p(f"找到 {total} 条结果" + (f"（显示前 {len(hits)} 条）" if total > len(hits) else ""))
    _p("")
    for i, h in enumerate(hits, 1):
        _p(f"[{i}] {h.title}")
        meta = " · ".join(x for x in [
            h.doc_number, h.agency, h.pub_date, h.status
        ] if x)
        if meta:
            _p(f"    {meta}")
        if h.tags:
            _p(f"    标签: {', '.join(h.tags)}")
        if h.snippet:
            _p(f"    {h.snippet}")
        if h.url:
            _p(f"    {h.url}")
        _p("")
    return 0


def cmd_facets(args) -> int:
    conn = db.connect(args.db)
    data = search_mod.facets(conn)
    labels = {
        "topic": "主题", "ich": "ICH", "ctd": "CTD",
        "agency": "机构标签", "agency_facet": "发文机关",
        "year": "年份", "status": "效力状态",
    }
    for kind, items in data.items():
        _p(f"── {labels.get(kind, kind)} ──")
        for name, count in items[: args.top]:
            _p(f"   {count:>5}  {name}")
        _p("")
    return 0


def cmd_diff(args) -> int:
    conn = db.connect(args.db)
    try:
        text = store.unified_diff(conn, args.document_id,
                                  from_rev=args.from_rev, to_rev=args.to_rev)
    except KeyError as exc:
        _p(f"! {exc}")
        return 1
    if not text.strip():
        _p("（两个版本内容相同，或无历史版本）")
        return 0
    _p(text)
    return 0


def cmd_show(args) -> int:
    conn = db.connect(args.db)
    row = conn.execute(
        "SELECT * FROM documents WHERE id = ?", (args.document_id,)
    ).fetchone()
    if row is None:
        _p(f"! 未找到 id={args.document_id}")
        return 1
    _p(f"标题: {row['title']}")
    for label, key in [("发文字号", "doc_number"), ("发文机关", "agency"),
                       ("发布日期", "pub_date"), ("效力状态", "status"),
                       ("分类", "category"), ("来源", "source"), ("网址", "url")]:
        if row[key]:
            _p(f"{label}: {row[key]}")
    revs = conn.execute(
        "SELECT COUNT(*) AS n FROM document_revisions WHERE document_id = ?",
        (args.document_id,),
    ).fetchone()["n"]
    _p(f"历史版本: {revs}")
    _p("")
    body = row["body"] or ""
    _p(body[: args.chars] + ("…" if len(body) > args.chars else ""))
    return 0


def cmd_reindex(args) -> int:
    conn = db.connect(args.db)
    _p("→ re-running classification over stored documents…")
    counts = store.reindex(conn, on_progress=_p)
    _p(f"✓ {counts}")
    _print_stats(conn)
    return 0


def _print_stats(conn) -> None:
    n = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    tag_n = conn.execute("SELECT COUNT(*) AS n FROM tags").fetchone()["n"]
    _p(f"\n库中共 {n} 份文件，{tag_n} 个标签。")
    top = conn.execute(
        "SELECT t.name, COUNT(*) AS n FROM tags t JOIN document_tags dt ON dt.tag_id=t.id "
        "GROUP BY t.id ORDER BY n DESC LIMIT 8"
    ).fetchall()
    if top:
        _p("主要主题: " + ", ".join(f"{r['name']}({r['n']})" for r in top))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="regkb",
        description="药品法规知识库 — 中国药品法规全文检索与 ICH/CTD 归类",
    )
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("crawl", help="抓取 gov.cn 政策文件库")
    c.add_argument("--keyword", action="append", help="关键词（可重复）")
    c.add_argument("--pages", type=int, default=1, help="每个关键词抓取页数")
    c.add_argument("--page-size", type=int, default=20)
    c.add_argument("--no-body", action="store_true", help="只抓列表，不抓全文")
    c.add_argument("--delay", type=float, default=fetchers.DEFAULT_DELAY)
    c.set_defaults(func=cmd_crawl)

    ch = sub.add_parser("crawl-chp", help="抓取国家药典委员会公告")
    ch.add_argument("--delay", type=float, default=fetchers.DEFAULT_DELAY)
    ch.set_defaults(func=cmd_crawl_chp)

    im = sub.add_parser("import", help="导入本地文件/目录（离线法规）")
    im.add_argument("path", help="文件或目录路径")
    im.set_defaults(func=cmd_import)

    s = sub.add_parser("search", help="全文检索")
    s.add_argument("query", help="查询词；支持 -排除 和 \"精确短语\"")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--tag", help="按标签筛选")
    s.add_argument("--agency", help="按发文机关筛选")
    s.add_argument("--status", choices=["in_force", "abolished", "unknown"])
    s.add_argument("--year-from", type=int)
    s.add_argument("--year-to", type=int)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    f = sub.add_parser("facets", help="显示标签/机关/年份统计")
    f.add_argument("--top", type=int, default=25)
    f.set_defaults(func=cmd_facets)

    sh = sub.add_parser("show", help="查看一份文件的全文")
    sh.add_argument("document_id", type=int)
    sh.add_argument("--chars", type=int, default=3000)
    sh.set_defaults(func=cmd_show)

    ri = sub.add_parser("reindex", help="按最新规则重新分类已入库文件")
    ri.set_defaults(func=cmd_reindex)

    d = sub.add_parser("diff", help="比较同一文件的历史版本")
    d.add_argument("document_id", type=int)
    d.add_argument("--from-rev", type=int, dest="from_rev")
    d.add_argument("--to-rev", type=int, dest="to_rev")
    d.set_defaults(func=cmd_diff)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
