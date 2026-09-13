"""数据整理（curate）：离线档保证"不难看"，AI 档保证"好看"。

离线档（无需任何模型）：空摘要/关键词补齐、标签规范化（去空白/去重/小写）、
重复内容检测。AI 档（需配置 summarize.provider=api）：对未整理过的条目用
LLM 重写标题/摘要/关键词，并打上 ai_curated 标记，支持定期全量跑。
"""
from __future__ import annotations

import json
import os

from .summarize import build_summary
from .util import entry_text as _entry_text
from .util import fts_index_text, now_iso


def curate(kb, ai: bool = False, apply: bool = False, limit: int | None = None,
           collection: str | None = None, force: bool = False) -> dict:
    """AI 档默认跳过已编目条目；抽取源是**全文**而非 preview——4KB 预览看不到
    的功能（如 core.py 里的 drill）编目会漏掉，这是真实踩过的坑。force=True
    连已编目条目一起重做（代码/文档更新后使用）。"""
    cat = kb.catalog
    report = {"scanned": 0, "summary_fixed": 0, "tags_fixed": 0, "ai_curated": 0,
              "duplicates": [], "changes": []}
    hashes: dict[str, list] = {}
    pending_ai = []

    for row in cat.iter_all():
        if collection and row["collection"] != collection:
            continue
        report["scanned"] += 1
        hashes.setdefault(row["content_hash"], []).append(row)

        # 1) 标签规范化（离线）
        tags = cat.tags_of(row["id"])
        norm = sorted({t.strip().lower() for t in tags if t.strip()})
        if norm != tags:
            msg = f"{row['id'][:12]} 标签规范化: {tags} -> {norm}"
            report["changes"].append(msg)
            report["tags_fixed"] += 1
            if apply:
                for t in tags:
                    cat.remove_tag(row["id"], t)
                cat.set_tags(row["id"], norm)

        # 2) 空摘要/关键词补齐（离线）
        if not row["summary"].strip() or not row["keywords"].strip():
            meta = {}
            try:
                meta = json.loads(row["meta_json"] or "{}")
            except json.JSONDecodeError:
                pass
            text = _entry_text(kb, row)
            title, summary, kw = build_summary(
                row["kind"], row["title"] or "（无标题）", text, meta,
                row["size"], kb.cfg, kb.llm())
            msg = f"{row['id'][:12]} 补齐摘要: {summary[:60]}…"
            report["changes"].append(msg)
            report["summary_fixed"] += 1
            if apply:
                cat.update_entry(row["id"], {
                    "summary": summary, "keywords": kw,
                    "title": title or row["title"],
                    "summary_fts": fts_index_text(summary),
                    "keywords_fts": fts_index_text(kw.replace(",", " ")),
                })
                _revec(kb, row["id"])

        # 3) AI 档候选：未做过 AI 编目的文本类条目
        try:
            meta = json.loads(row["meta_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        if ai and (force or not meta.get("ai_curated")) \
                and row["kind"] in ("text", "code", "pdf", "note"):
            pending_ai.append(row)

    # 4) 重复内容检测（同 hash 多条目；同 collection 内重复才标问题）
    by_hash = {h: rows for h, rows in hashes.items() if h and len(rows) > 1}
    for h, rows in by_hash.items():
        collections = {r["collection"] for r in rows}
        dup = {"hash": h[:16], "entries": [r["id"][:12] for r in rows],
               "cross_collection": len(collections) > 1}
        report["duplicates"].append(dup)

    # 5) AI 重编目（bounded）
    if ai and apply:
        todo = pending_ai[:limit] if limit else pending_ai
        llm = kb.llm()
        for row in todo:
            if llm is None:
                report["changes"].append("AI 档需要配置 summarize.provider=api，已跳过")
                break
            text = _entry_text(kb, row)
            if not text:
                continue
            out = llm.chat_json(
                "你是知识库编目员。只输出 JSON："
                '{"title":"...","summary":"不超过200字摘要","keywords":["k1","k2"]}',
                f"文件名: {row['title']}\n\n{text}", 400)
            if not out or not out.get("summary"):
                continue
            meta = {}
            try:
                meta = json.loads(row["meta_json"] or "{}")
            except json.JSONDecodeError:
                pass
            meta["ai_curated"] = now_iso()
            new_kw = (out.get("keywords") if isinstance(out.get("keywords"), str)
                      else ",".join(map(str, out.get("keywords") or [])))
            cat.update_entry(row["id"], {
                "title": str(out.get("title") or row["title"])[:100],
                "summary": str(out.get("summary") or ""),
                "keywords": new_kw,
                "meta_json": json.dumps(meta, ensure_ascii=False),
                "summary_fts": fts_index_text(str(out.get("summary") or "")),
                "keywords_fts": fts_index_text((new_kw or "").replace(",", " ")),
            })
            _revec(kb, row["id"])
            report["ai_curated"] += 1

    cat.audit("curate", ai=ai, apply=apply, scanned=report["scanned"],
              summary_fixed=report["summary_fixed"], tags_fixed=report["tags_fixed"],
              ai_curated=report["ai_curated"])
    return report


def _revec(kb, eid: str):
    """整理改写摘要后同步向量。"""
    row = kb.catalog.get(eid)
    if row is None:
        return
    try:
        text = f"{row['title']}\n{row['summary']}\n{row['keywords'].replace(',', ' ')}"
        vec = kb.embedder().embed([text])[0]
        kb.catalog.remove_vecs(eid)
        shard, r = kb.vectors.add(vec)
        kb.catalog.add_vec(eid, shard, r)
    except Exception:
        kb.catalog.audit("vec_failed", id=eid, phase="curate")
