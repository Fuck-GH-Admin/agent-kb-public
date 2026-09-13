"""MCP stdio 服务（stdlib 实现，无第三方依赖）。

工具面由级别决定（KB_ACCESS_LEVEL / access.mcp_level，默认 operator）：
读工具恒可见；operator/admin 另暴露 kb_add / kb_note / kb_approve / kb_reject，
写入默认进待审队列。KB_ALLOW_WRITE=1 仅为兼容逃生阀。级别-能力映射见
docs/ARCHITECTURE.md §22.1。
启动方式：kb serve-mcp（或 python3 -m kb.mcp_server）
"""
from __future__ import annotations

import json
import os
import sys

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "agent-kb", "version": "0.1.0"}


def _tool(name: str, desc: str, props: dict, required: list) -> dict:
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props,
                            "required": required}}


def read_tools() -> list[dict]:
    return [
        _tool("kb_search", "在本地知识库中检索：混合关键词(BM25)与语义(向量)匹配，"
              "长文档已按块索引，命中可能带 parent_id+chunk_no（块定位），"
              "需要全文时按 parent_id 取父条目或读 source_path。"
              "注意：语义路总返回最近邻，结果非空不代表命中；请依据 snippet 与"
              "vec_sim/score 判断相关性，必要时加 include_preview 复核。"
              "检索结果不含待审核条目", {
                  "query": {"type": "string", "description": "检索词，中英文均可"},
                  "kind": {"type": "string", "enum": ["text", "code", "image", "audio",
                                                      "video", "pdf", "note", "model",
                                                      "archive", "binary"]},
                  "tag": {"type": "string"},
                  "collection": {"type": "string",
                                 "description": "限定主题分区，缺省查全部"},
                  "include_preview": {"type": "boolean", "default": False,
                                      "description": "附带正文前400字，用于判断相关性"},
                  "limit": {"type": "integer", "default": 8}}, ["query"]),
        _tool("kb_get", "按 id 读取条目完整信息（摘要、元数据、原文文件路径）",
              {"id": {"type": "string", "description": "条目 id 或其前缀"}}, ["id"]),
        _tool("kb_list", "按类型、标签或主题分区浏览最近条目",
              {"kind": {"type": "string"}, "tag": {"type": "string"},
               "collection": {"type": "string"},
               "limit": {"type": "integer", "default": 20}}, []),
        _tool("kb_stats", "知识库统计（条目数、待审核数、容量、分片等）", {}, []),
    ]


def write_tools() -> list[dict]:
    return [
        _tool("kb_add", "把文件或目录加入知识库。默认进入待审核队列(pending)，"
              "经人确认 approve 后才可被检索，防止污染；review=false 可跳过审核。"
              "路径受白名单限制：默认只接受 $HOME 下的非隐藏目录", {
                  "path": {"type": "string"}, "tags": {"type": "string"},
                  "collection": {"type": "string", "default": "default"},
                  "review": {"type": "boolean", "default": True},
                  "in_place": {"type": "boolean", "default": False}}, ["path"]),
        _tool("kb_note", "向知识库写入一条文本笔记。默认进入待审核队列，"
              "请以客观事实口吻记录（不要夹带 agent 个性语气），注明信息依据",
              {"title": {"type": "string"}, "text": {"type": "string"},
               "tags": {"type": "string"},
               "collection": {"type": "string", "default": "default"},
               "review": {"type": "boolean", "default": True}},
              ["title", "text"]),
        _tool("kb_approve", "审核通过待审核条目（仅在允许写入时可用）",
              {"id": {"type": "string"}}, ["id"]),
        _tool("kb_reject", "拒绝并删除待审核条目（仅在允许写入时可用）",
              {"id": {"type": "string"}}, ["id"]),
    ]


def _fmt_hits(results: list[dict], include_preview: bool = False) -> str:
    keys = ["id", "kind", "title", "summary", "snippet", "tags", "score",
            "collection", "origin", "source_path"]
    out = []
    for r in results:
        item = {k: r[k] for k in keys if r.get(k) is not None}
        if include_preview and r.get("preview"):
            item["preview"] = r["preview"][:400]
        out.append(item)
    return json.dumps(out, ensure_ascii=False, indent=1)


def _call_tool(kb, name: str, args: dict, allow_write: bool,
               level: str = "admin") -> tuple[str, bool]:
    from .core import LEVEL_VISIBILITY
    vis = LEVEL_VISIBILITY.get(level, LEVEL_VISIBILITY["viewer"])
    if name == "kb_search":
        res = kb.search(str(args.get("query", "")), kind=args.get("kind"),
                        tag=args.get("tag"), collection=args.get("collection"),
                        limit=int(args.get("limit") or 8), visible=vis)
        out = _fmt_hits(res["results"],
                        include_preview=bool(args.get("include_preview")))
        if res.get("warning"):
            out += f"\n\nwarning: {res['warning']}"
        return out, False
    if name == "kb_get":
        hit = kb.get(str(args.get("id", "")))
        if hit is None:
            return "找不到条目", True
        if vis is not None and hit.get("visibility") not in vis:
            return "条目不存在（或当前级别无权查看）", True
        return json.dumps(hit, ensure_ascii=False, indent=1), False
    if name == "kb_list":
        rows = kb.entries(kind=args.get("kind"), tag=args.get("tag"),
                          collection=args.get("collection"), visible=vis,
                          limit=int(args.get("limit") or 20))
        return json.dumps([{k: r[k] for k in ("id", "kind", "title", "collection",
                                              "status", "source_path",
                                              "updated_at")} for r in rows],
                          ensure_ascii=False, indent=1), False
    if name == "kb_stats":
        return json.dumps(kb.stats(), ensure_ascii=False, indent=1), False
    if name in ("kb_add", "kb_note", "kb_approve", "kb_reject"):
        if not allow_write:
            return (f"知识库处于只读模式。如确需写入，请在启动配置中设置环境变量 "
                    f"KB_ALLOW_WRITE=1", True)
        if name == "kb_add":
            from .policy import WritePathDenied, resolve_write_path
            try:  # 远程写通道路径白名单（默认 $HOME 非隐藏目录）
                safe = resolve_write_path(str(args.get("path", "")),
                                          kb.cfg.get("access.write_roots"))
            except WritePathDenied as e:
                kb.catalog.audit("write_denied", path=str(args.get("path", "")),
                                 reason=str(e))
                return f"拒绝：{e}", True
            stats = kb.add(safe,
                           tags=[t for t in str(args.get("tags") or "").split(",") if t],
                           in_place=bool(args.get("in_place")),
                           collection=str(args.get("collection") or "default"),
                           review=bool(args.get("review", True)),
                           origin="agent")
            return json.dumps(stats, ensure_ascii=False), False
        if name == "kb_note":
            eid = kb.note(str(args.get("title", "")), str(args.get("text", "")),
                          tags=[t for t in str(args.get("tags") or "").split(",") if t],
                          collection=str(args.get("collection") or "default"),
                          review=bool(args.get("review", True)),
                          origin="agent")
            state = "待审核" if args.get("review", True) else "已激活"
            return f"已写入笔记 {eid}（{state}）", False
        if name == "kb_approve":
            return f"{kb.approve(str(args.get('id', '')))} 已审核通过", False
        if name == "kb_reject":
            return f"{kb.reject(str(args.get('id', '')))} 已拒绝删除", False
    return f"未知工具: {name}", True


def serve(kb) -> None:
    # Capability 模型（架构文档 §22.1）：写能力由级别映射，不再用
    # KB_ALLOW_WRITE 环境总开关做唯一裁决；后者保留为兼容逃生阀。
    _LEVEL_CAPS = {
        "admin": ("substrate.reflect", "substrate.write", "substrate.approve",
                  "substrate.admin"),
        "operator": ("substrate.reflect", "substrate.write"),
        "viewer": ("substrate.reflect",),
    }
    level = os.environ.get("KB_ACCESS_LEVEL") or \
        kb.cfg.get("access.mcp_level") or "operator"
    allow_write = (os.environ.get("KB_ALLOW_WRITE") == "1"
                   or "substrate.write" in _LEVEL_CAPS.get(level, ()))
    kb.catalog.principal = f"mcp:{level}"   # 审计留痕：区分通道与级别
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _send(out, {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "parse error"}})
            continue
        if "id" not in msg:  # 通知：无需应答
            continue
        mid, method = msg["id"], msg.get("method", "")
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                          "capabilities": {"tools": {}},
                          "serverInfo": SERVER_INFO}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": read_tools() + (write_tools() if allow_write else [])}
            elif method == "tools/call":
                name = str(params.get("name", ""))
                args = params.get("arguments") or {}
                text, is_err = _call_tool(kb, name, args, allow_write, level)
                result = {"content": [{"type": "text", "text": text}], "isError": is_err}
            else:
                _send(out, {"jsonrpc": "2.0", "id": mid,
                            "error": {"code": -32601, "message": f"未知方法: {method}"}})
                continue
            _send(out, {"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as e:  # 单请求异常不退出服务
            _send(out, {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32603, "message": str(e)}})


def _send(out, obj: dict) -> None:
    out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    out.flush()


if __name__ == "__main__":
    from .core import KnowledgeBase
    serve(KnowledgeBase())
