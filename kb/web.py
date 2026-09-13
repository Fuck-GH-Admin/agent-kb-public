"""零依赖人类操作面板（kb serve-web）。

- 纯标准库 http.server，无前端框架，无构建步骤。
- token 三级权限（config access.tokens）：admin > operator > viewer。
  可见范围与写权限统一由 policy.Principal 裁决（本文件不自行拼可见性 SQL）。
- 每请求独立打开 KnowledgeBase（SQLite 连接不跨线程共享），并带上 principal
  以便审计留痕（谁审批了什么）。
"""
from __future__ import annotations

import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Config
from .core import KnowledgeBase
from .policy import Principal

CAN_WRITE = {"admin", "operator"}
CAN_ADMIN = {"admin"}

_CSS = """body{font-family:system-ui,sans-serif;margin:24px;max-width:960px;
color:#222}h1{font-size:20px}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;font-size:14px}
form{margin:8px 0;display:inline-block}input[type=text]{width:60%;padding:6px}
button{padding:4px 10px}.tag{background:#eee;border-radius:4px;padding:1px 6px;
font-size:12px;margin-right:4px}.warn{color:#a00}.ok{color:#070}
pre{background:#f6f6f6;padding:10px;white-space:pre-wrap;font-size:13px}"""

_PAGE = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<title>agent-kb 面板</title><style>{css}</style></head><body>{body}</body></html>"""


def _tokens(home: str) -> dict:
    """token -> (level, name)。每次读取配置，改 token 无需重启。

    C12：条目可为哈希（{hash,...}）或历史明文（{token,...}，迁移期兼容）。
    """
    cfg = Config(home)
    out = {}
    for entry in (cfg.get("access.tokens") or []):
        tok_hash = entry.get("hash") or entry.get("token")
        if not tok_hash:
            continue
        out[tok_hash] = (entry.get("level", "viewer"), entry.get("name") or "anon")
    return out


def _match_token(token: str, home: str):
    """哈希比对查找：先算提交 token 的哈希，再与存储比对（兼容明文条目）。"""
    from .tokens import lookup_token
    return lookup_token(Config(home), token)


def _h(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _table(rows, cols):
    head = "".join(f"<th>{_h(c)}</th>" for c in cols)
    trs = []
    for r in rows:
        tds = "".join(f"<td>{r.get(c, '')}</td>" for c in cols)
        trs.append(f"<tr>{tds}</tr>")
    return f"<table><tr>{head}</tr>{''.join(trs)}</table>"


def make_handler(home: str):
    # C14：SQLite 连接不能跨线程共享，ThreadingHTTPServer 是每请求一线程，
    # thread-local 复用不会命中。改为**有界实例池**：固定数量实例循环出租，
    # 归还时清事务；写路径（备份/审批/拒绝）与租不到实例时回退为按需新建。
    import queue
    import threading

    POOL_SIZE = 8
    _pool: "queue.Queue[KnowledgeBase]" = queue.LifoQueue()
    _pool_lock = threading.Lock()
    _created = [0]
    _all: list[KnowledgeBase] = []

    class _Rented:
        """租约上下文：with 结束自动归还（并做事务清理）。"""

        def __init__(self, principal: Principal):
            self.principal = principal
            self.kb: KnowledgeBase | None = None
            self._fresh = False

        def __enter__(self) -> KnowledgeBase:
            try:
                self.kb = _pool.get_nowait()
            except queue.Empty:
                with _pool_lock:
                    if _created[0] < POOL_SIZE:
                        self.kb = KnowledgeBase(home, principal=self.principal)
                        _created[0] += 1
                        _all.append(self.kb)
                    else:  # 池满：短时新建（写锁保证写路径互斥）
                        self.kb = KnowledgeBase(home, principal=self.principal)
                        self._fresh = True
            self.kb.principal = self.principal
            self.kb.catalog.principal = self.principal.name
            return self.kb

        def __exit__(self, *exc):
            kb, self.kb = self.kb, None
            if kb is None:
                return False
            try:
                if kb.catalog.conn.in_transaction:
                    kb.catalog.conn.rollback()
            except Exception:
                pass
            if self._fresh:
                kb.close()      # 临时实例用完即关
            else:
                _pool.put(kb)
            return False

    def _rented(principal) -> _Rented:
        return _Rented(principal)

    import atexit

    def _close_pool():
        for kb in list(_all):
            try:
                kb.close()
            except Exception:
                pass
        _all.clear()

    atexit.register(_close_pool)

    class Handler(BaseHTTPRequestHandler):
        _principal: Principal | None = None
        _token: str = ""

        def log_message(self, *a):  # 安静
            pass

        def _auth(self, qs) -> Principal | None:
            token = (qs.get("token") or [self.headers.get("X-KB-Token", "")])[0]
            self._token = token
            hit = _match_token(token, home)
            if hit is None:
                return None
            level, name = hit
            self._principal = Principal(level=level, name=f"web:{name}")
            return self._principal

        def _page(self, body: str, code: int = 200):
            data = _PAGE.format(css=_CSS, body=body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            who = self._auth(qs)
            if who is None:
                self._page("<h1>agent-kb 面板</h1><p class=warn>未授权。"
                           "URL 后追加 ?token=xxx 或请求头 X-KB-Token。</p>", 401)
                return
            with _rented(who) as kb:
                if u.path == "/":
                    body = self._dashboard(kb, who)
                elif u.path == "/search":
                    body = self._search(kb, who, qs)
                elif u.path == "/entry":
                    body = self._entry(kb, who, qs)
                elif u.path == "/pending":
                    body = self._pending(kb, who)
                elif u.path == "/gaps":
                    body = self._gaps(kb, who)
                else:
                    body = "<h1>404</h1>"
                self._page(body)

        def do_POST(self):
            u = urlparse(self.path)
            form = parse_qs(self.rfile.read(
                int(self.headers.get("Content-Length") or 0)).decode())
            who = self._auth(form)
            if who is None:
                self._page("<h1 class=warn>未授权</h1>", 401)
                return
            with _rented(who) as kb:
              try:
                if u.path in ("/approve", "/reject") and who.level in CAN_WRITE:
                    eid = (form.get("id") or [""])[0]
                    try:
                        if u.path == "/approve":
                            kb.approve(eid)
                            msg = f"{eid} 已通过"
                        else:
                            kb.reject(eid)
                            msg = f"{eid} 已拒绝"
                    except Exception as e:
                        msg = f"失败: {e}"
                    self._page(f"<h1>{_h(msg)}</h1>"
                               f"<p><a href='/pending?token={_h(self._token)}'>"
                               f"返回待审核</a></p>")
                elif u.path == "/backup" and who.level in CAN_ADMIN:
                    rep = kb.backup(with_blobs=False, verify=True, retention=7)
                    self._page(f"<h1>备份完成</h1><pre>"
                               f"{_h(json.dumps(rep, ensure_ascii=False, indent=1))}</pre>"
                               f"<p><a href='/?token={_h(self._token)}'>返回</a></p>")
                else:
                    self._page("<h1 class=warn>无权限或未知操作</h1>", 403)
              except Exception as e:
                  self._page(f"<h1 class=warn>处理失败: {_h(e)}</h1>", 500)

        # ---- 页面 ----
        def _nav(self, who: Principal) -> str:
            t = _h(self._token)
            links = [f"<a href='/?token={t}'>仪表盘</a>"]
            if who.level in CAN_WRITE:
                links.append(f"<a href='/pending?token={t}'>待审核</a>")
                links.append(f"<a href='/gaps?token={t}'>知识缺口</a>")
            return "<p>" + " · ".join(links) + "</p>"

        def _dashboard(self, kb, who: Principal):
            st = kb.stats()
            rows = [{"项": k, "值": _h(v)} for k, v in {
                "资料条目": st["entries_total"], "块子条目": st["chunks"],
                "待审核": st["pending"],
                "blob 数": st["blobs"]["count"], "blob 容量": st["blobs"]["size"],
                "目录库": st["db_size"],
                "向量": f"{st['vectors']['rows']} 行 / {st['vectors']['size']}",
                "embedder": st["embedder"], "schema": f"v{st['schema_version']}",
            }.items()]
            colls = "".join(
                f'<span class=tag>{_h(k)}({v})</span>'
                for k, v in (st.get("collections") or {}).items())
            backup_form = ("""<form method=post action=/backup>
<input type=hidden name=token value=%s><button>立即备份（校验+保留7份）</button></form>"""
                           % _h(self._token)) if who.level in CAN_ADMIN else ""
            return (f"<h1>agent-kb 面板（{_h(who.level)} / {_h(who.name)}）</h1>"
                    + self._nav(who)
                    + f"""<form action=/search method=get>
<input type=text name=q placeholder="检索知识库…">
<input type=hidden name=token value={_h(self._token)}>
<button>搜索</button></form>{backup_form}"""
                    + f"<h2>统计</h2>{_table(rows, ['项', '值'])}"
                    + f"<h2>主题分区</h2><p>{colls or '（空）'}</p>")

        def _search(self, kb, who: Principal, qs):
            q = (qs.get("q") or [""])[0]
            res = kb.search(q, limit=15, visible=who.visible)
            rows = []
            for r in res["results"]:
                link = (f"<a href='/entry?id={r['id']}&token={_h(self._token)}'>"
                        f"{_h(r['title'])}</a>")
                mark = f"[块{r['chunk_no']}]" if r.get("parent_id") else ""
                rows.append({"标题": link + mark, "类型": r["kind"],
                             "分区": r["collection"], "来源": r["origin"],
                             "分数": r["score"],
                             "命中": _h((r.get("snippet") or r["summary"] or "")[:100])})
            warn = f"<p class=warn>{_h(res['warning'])}</p>" if res.get("warning") else ""
            return (f"<h1>检索：{_h(q)}</h1>" + self._nav(who) + warn
                    + (_table(rows, ["标题", "类型", "分区", "来源", "分数", "命中"])
                       if rows else "<p>无结果</p>"))

        def _entry(self, kb, who: Principal, qs):
            eid = (qs.get("id") or [""])[0]
            try:
                hit = kb.get(eid)
            except Exception:
                hit = None
            # 越权条目一律等同于不存在（不泄漏存在性）
            if hit is None or (who.visible is not None
                               and hit["visibility"] not in who.visible):
                return "<h1>条目不存在或无权限</h1>" + self._nav(who)
            meta = {k: _h(hit.get(k)) for k in (
                "id", "kind", "title", "collection", "status", "origin",
                "visibility", "source_path", "updated_at")}
            return (f"<h1>{meta['title']}</h1>" + self._nav(who)
                    + f"<pre>{_h(json.dumps(meta, ensure_ascii=False, indent=1))}</pre>"
                    f"<h2>摘要</h2><p>{_h(hit['summary'])}</p>"
                    f"<h2>预览</h2><pre>{_h(hit['preview'][:800])}</pre>")

        def _pending(self, kb, who: Principal):
            if who.level not in CAN_WRITE:
                return "<h1 class=warn>无权限</h1>"
            rows = kb.entries(status="pending", include_pending=True, limit=100,
                              visible=who.visible)
            items = []
            for r in rows:
                btns = "".join(
                    f"""<form method=post action=/{act}>
<input type=hidden name=id value={r['id']}>
<input type=hidden name=token value={_h(self._token)}>
<button>{label}</button></form>"""
                    for act, label in (("approve", "通过"), ("reject", "拒绝")))
                items.append(
                    f"<tr><td>{_h(r['title'])}</td><td>{r['kind']}</td>"
                    f"<td>{_h(r['summary'][:80])}</td><td>{r['origin']}</td>"
                    f"<td>{btns}</td></tr>")
            return (f"<h1>待审核（{len(rows)}）</h1>" + self._nav(who)
                    + (f"<table><tr><th>标题</th><th>类型</th><th>摘要</th>"
                       f"<th>来源</th><th>操作</th></tr>{''.join(items)}</table>"
                       if items else "<p>队列空。</p>"))

        def _gaps(self, kb, who: Principal):
            if who.level not in CAN_WRITE:
                return "<h1 class=warn>无权限</h1>"
            rep = kb.gaps(days=30)
            rows = [{"次数": g["count"], "查询": _h(g["query"])}
                    for g in rep["gaps"][:30]]
            return (f"<h1>知识缺口（近 30 天 {rep['queries']} 次查询）</h1>"
                    + self._nav(who)
                    + "<p>以下查询零结果或分数过低，提示该补充哪些资料：</p>"
                    + (_table(rows, ["次数", "查询"]) if rows else "<p>暂无缺口。</p>"))

    return Handler


def serve_web(home: str, host: str = "127.0.0.1", port: int = 7800):
    os.makedirs(home, exist_ok=True)
    cfg = Config(home)
    from .tokens import needs_migration
    if not (cfg.get("access.tokens") or []):
        print("警告：尚未配置任何访问 token（kb token add <name> <level>），"
              "面板将拒绝所有请求。")
    elif needs_migration(cfg):
        print("提示：access.tokens 中仍有明文 token（历史格式）。"
              "请执行 kb token migrate 转为哈希存储（旧 token 将失效）。")
    server = ThreadingHTTPServer((host, port), make_handler(home))
    print(f"agent-kb 面板: http://{host}:{port}/?token=YOUR_TOKEN")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass