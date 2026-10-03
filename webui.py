#!/usr/bin/env python3
"""A zero-dependency web UI for the drug-regulation knowledge base.

Only the standard library is used (``http.server`` + ``sqlite3``), so the tool
runs anywhere Python does — no pip install, no Node, no build step.  The UI
targets the searches a registration specialist actually performs: full-text
search with exclusion, facet filtering, document reading, and version diffing.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import db, search as search_mod, store  # noqa: E402
from src.cli import DEFAULT_DB  # noqa: E402

STATUS_LABELS = {
    "in_force": "现行有效",
    "abolished": "已废止",
    "unknown": "未标注",
}
SOURCE_LABELS = {
    "gov.cn": "中国政府网",
    "chp": "国家药典委员会",
    "manual": "本地导入",
}

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>药品法规知识库</title>
<style>
  :root {{
    --bg:#f6f7f9; --card:#fff; --fg:#1a1c1e; --muted:#5f6368;
    --line:#e0e3e7; --accent:#1a5fb4; --accent-soft:#e8f0fe; --warn:#b3261e;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#121417; --card:#1c1f23; --fg:#e3e5e8; --muted:#9aa0a6;
             --line:#2c3035; --accent:#8ab4f8; --accent-soft:#1e2a3a; --warn:#f2b8b5; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
    font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; font-size:15px; }}
  header {{ background:var(--card); border-bottom:1px solid var(--line);
    padding:14px 20px; position:sticky; top:0; z-index:10; }}
  .brand {{ font-size:17px; font-weight:600; }}
  .brand span {{ color:var(--muted); font-weight:400; font-size:13px; margin-left:8px; }}
  form {{ display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; }}
  input[type=text] {{ flex:1; min-width:220px; padding:9px 12px; font-size:15px;
    border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--fg); }}
  button {{ padding:9px 18px; border:0; border-radius:8px; background:var(--accent);
    color:#fff; font-size:15px; cursor:pointer; }}
  button:hover {{ opacity:.9; }}
  .layout {{ display:grid; grid-template-columns:240px 1fr; gap:20px;
    padding:20px; align-items:start; max-width:1400px; margin:0 auto; }}
  @media (max-width:820px) {{ .layout {{ grid-template-columns:1fr; }} }}
  .panel {{ background:var(--card); border:1px solid var(--line);
    border-radius:10px; padding:14px; }}
  .panel h3 {{ margin:0 0 8px; font-size:13px; text-transform:uppercase;
    letter-spacing:.04em; color:var(--muted); font-weight:600; }}
  .panel + .panel {{ margin-top:14px; }}
  .facet {{ display:flex; justify-content:space-between; gap:8px; padding:3px 6px;
    border-radius:6px; text-decoration:none; color:var(--fg); font-size:14px; }}
  .facet:hover {{ background:var(--accent-soft); }}
  .facet.on {{ background:var(--accent-soft); font-weight:600; }}
  .facet .n {{ color:var(--muted); font-variant-numeric:tabular-nums; }}
  .meta {{ color:var(--muted); font-size:13px; margin:6px 0 14px; }}
  .hit {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:14px 16px; margin-bottom:12px; }}
  .hit h2 {{ margin:0 0 6px; font-size:16px; line-height:1.4; }}
  .hit h2 a {{ color:var(--fg); text-decoration:none; }}
  .hit h2 a:hover {{ color:var(--accent); }}
  .badges {{ display:flex; flex-wrap:wrap; gap:6px; margin:8px 0; }}
  .badge {{ font-size:12px; padding:2px 8px; border-radius:99px;
    background:var(--accent-soft); color:var(--accent); }}
  .badge.topic {{ background:#e6f4ea; color:#137333; }}
  .badge.ich {{ background:#fef7e0; color:#8a6d00; }}
  .badge.ctd {{ background:#f3e8fd; color:#6b21a8; }}
  .badge.status {{ background:#eceff1; color:#455a64; }}
  .snippet {{ color:var(--muted); font-size:14px; line-height:1.6; margin:6px 0; }}
  .empty {{ text-align:center; color:var(--muted); padding:50px 20px; }}
  pre {{ background:var(--bg); border:1px solid var(--line); border-radius:8px;
    padding:12px; overflow:auto; font-size:13px; line-height:1.5; }}
  .add {{ color:#137333; }} .del {{ color:var(--warn); }}
  .back {{ color:var(--accent); text-decoration:none; font-size:14px; }}
</style>
</head>
<body>
<header>
  <div class="brand">药品法规知识库 <span>Drug Regulation Knowledge Hub</span></div>
  <form method="get" action="/">
    <input type="text" name="q" value="{q}" placeholder="检索法规，例如：药物警戒、药品注册 -化妆品">
    <button type="submit">检索</button>
  </form>
</header>
<div class="layout">
  <aside>{facets}</aside>
  <main>{body}</main>
</div>
</body>
</html>
"""


def _esc(text: str | None) -> str:
    return html.escape(text or "")


def _badges(tags: list[str]) -> str:
    out = []
    for t in tags:
        cls = "badge"
        if t in ("ICH",) or t.startswith("ICH "):
            cls += " ich"
        elif t.startswith("CTD"):
            cls += " ctd"
        elif t.startswith("国家") or t in ("国务院", "国务院办公厅"):
            cls = "badge status"
        else:
            cls += " topic"
        out.append(f'<span class="{cls}">{_esc(t)}</span>')
    return f'<div class="badges">{"".join(out)}</div>' if out else ""


def _facets_html(conn, active: dict) -> str:
    data = search_mod.facets(conn)
    blocks = []

    def block(title: str, items, key: str):
        if not items:
            return
        rows = []
        for name, count in items[:18]:
            params = dict(active)
            params.pop("page", None)
            if params.get(key) == name:
                params.pop(key, None)
                cls, label = "facet on", name
            else:
                params[key] = name
                cls, label = "facet", name
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v})
            rows.append(
                f'<a class="{cls}" href="/?{qs}"><span>{_esc(label)}</span>'
                f'<span class="n">{count}</span></a>'
            )
        blocks.append(f'<div class="panel"><h3>{title}</h3>{"".join(rows)}</div>')

    block("主题", data.get("topic"), "tag")
    block("ICH 指导原则", data.get("ich"), "tag")
    block("CTD 模块", data.get("ctd"), "tag")
    block("发文机关", data.get("agency_facet"), "agency")
    block("年份", data.get("year"), "year")
    return "".join(blocks) or '<div class="panel">库为空，请先抓取数据。</div>'


def render_index(conn, params: dict) -> str:
    q = params.get("q", "")
    tag = params.get("tag") or None
    agency = params.get("agency") or None
    year = params.get("year") or None
    status = params.get("status") or None
    page = max(1, int(params.get("page") or 1))
    limit = 20

    year_from = year_to = None
    if year and year.isdigit():
        year_from = year_to = int(year)

    hits, total = search_mod.search(
        conn, q, limit=limit, offset=(page - 1) * limit,
        tag=tag, agency=agency, status=status,
        year_from=year_from, year_to=year_to,
    )

    if not hits:
        body = '<div class="empty">没有匹配的法规。<br>试试更短的关键词，或用 <code>-词</code> 排除。</div>'
    else:
        parts = [f'<div class="meta">找到 <b>{total}</b> 条结果'
                 f'{"（第 " + str(page) + " 页）" if total > limit else ""}</div>']
        for h in hits:
            link = f'<a href="/doc?id={h.id}">{_esc(h.title)}</a>'
            meta = " · ".join(x for x in [
                _esc(h.doc_number), _esc(h.agency), _esc(h.pub_date),
                STATUS_LABELS.get(h.status, h.status),
                SOURCE_LABELS.get(h.source, h.source),
            ] if x)
            parts.append(
                f'<div class="hit"><h2>{link}</h2>'
                f'<div class="meta">{meta}</div>'
                f'{_badges(h.tags)}'
                f'<div class="snippet">{_esc(h.snippet)}</div></div>'
            )
        if total > limit:
            pages = (total + limit - 1) // limit
            nav = []
            for p in range(1, min(pages, 12) + 1):
                pp = dict(params)
                pp["page"] = str(p)
                qs = urllib.parse.urlencode({k: v for k, v in pp.items() if v})
                style = "font-weight:700" if p == page else ""
                nav.append(f'<a class="back" style="{style}" href="/?{qs}">{p}</a>')
            parts.append('<div class="meta">' + " ".join(nav) + "</div>")
        body = "".join(parts)

    active = {"q": q, "tag": tag, "agency": agency, "year": year, "status": status}
    return PAGE.format(q=_esc(q), facets=_facets_html(conn, active), body=body)


def render_doc(conn, doc_id: int) -> str:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        return PAGE.format(q="", facets="", body='<div class="empty">未找到该文件。</div>')

    tags = [r["name"] for r in conn.execute(
        "SELECT t.name FROM document_tags dt JOIN tags t ON t.id = dt.tag_id "
        "WHERE dt.document_id = ? ORDER BY t.kind, t.name", (doc_id,))]

    revs = conn.execute(
        "SELECT revision_no FROM document_revisions WHERE document_id = ? "
        "ORDER BY revision_no", (doc_id,)).fetchall()

    meta = " · ".join(x for x in [
        _esc(row["doc_number"]), _esc(row["agency"]), _esc(row["pub_date"]),
        STATUS_LABELS.get(row["status"], row["status"]),
        SOURCE_LABELS.get(row["source"], row["source"]),
    ] if x)

    diff_html = ""
    if revs:
        try:
            d = store.unified_diff(conn, doc_id)
        except Exception:
            d = ""
        if d.strip():
            lines = []
            for line in d.splitlines():
                cls = ""
                if line.startswith("+") and not line.startswith("+++"):
                    cls = "add"
                elif line.startswith("-") and not line.startswith("---"):
                    cls = "del"
                lines.append(f'<span class="{cls}">{_esc(line)}</span>')
            diff_html = (
                f'<div class="panel"><h3>版本变更（共 {len(revs)} 个历史版本）</h3>'
                f'<pre>{"<br>".join(lines)}</pre></div>'
            )

    src_link = (f'<div class="meta">来源：<a class="back" href="{_esc(row["url"])}" '
                f'target="_blank" rel="noopener">{_esc(row["url"])}</a></div>'
                if row["url"] else "")

    body = (
        f'<a class="back" href="/">← 返回检索</a>'
        f'<div class="panel" style="margin-top:12px">'
        f'<h2 style="margin:0 0 6px">{_esc(row["title"])}</h2>'
        f'<div class="meta">{meta}</div>{_badges(tags)}{src_link}'
        f'<pre>{_esc(row["body"])}</pre></div>{diff_html}'
    )
    return PAGE.format(q="", facets=_facets_html(conn, {}), body=body)


class Handler(BaseHTTPRequestHandler):
    db_path = str(DEFAULT_DB)

    def _send(self, content: str, code: int = 200) -> None:
        payload = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - required name
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        conn = db.connect(self.db_path)
        try:
            if parsed.path == "/doc":
                doc_id = int(params.get("id") or 0)
                self._send(render_doc(conn, doc_id))
            else:
                self._send(render_index(conn, params))
        except Exception as exc:  # noqa: BLE001 - surface errors to the page
            self._send(PAGE.format(
                q="", facets="",
                body=f'<div class="empty">发生错误：{_esc(str(exc))}</div>'), 500)
        finally:
            db.close(conn)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("  %s\n" % (fmt % args))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="药品法规知识库 Web 界面")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args(argv)

    Handler.db_path = args.db
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"药品法规知识库 → http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
