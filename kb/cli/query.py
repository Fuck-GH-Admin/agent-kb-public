"""检索与浏览类子命令实现。"""
from __future__ import annotations

import json
import sys

from ..core import AmbiguousId, KnowledgeBase, NotFound

def _print_hit(i: int, hit: dict) -> None:
    score = f"{hit['score']:.4f}" if hit.get("score") is not None else "-"
    title = hit["title"] or "(无标题)"
    print(f"#{i}  {score}  [{hit['kind']}] {title}")
    loc = hit.get("blob_path") or hit.get("source_path") or f"blob:{hit['blob'][:16]}"
    print(f"    id: {hit['id']}  位置: {loc}")
    if hit.get("tags"):
        print(f"    标签: {', '.join(hit['tags'])}")
    if hit.get("summary"):
        print(f"    摘要: {hit['summary'][:180]}")


def cmd_init(kb: KnowledgeBase, args) -> int:
    print(f"知识库已就绪: {kb.dirs['home']}")
    print(f"  目录库: {kb.catalog.db_path}")
    print(f"  blob库: {kb.dirs['blobs']}")
    return 0


def cmd_search(kb: KnowledgeBase, args) -> int:
    res = kb.search(args.query, kind=args.kind, tag=args.tag,
                    path_glob=args.path, limit=args.limit,
                    collection=args.collection, include_pending=args.pending)
    if res.get("warning"):
        print(f"提示: {res['warning']}", file=sys.stderr)
    if args.json:
        print(json.dumps(res["results"], ensure_ascii=False, indent=2))
        return 0
    if not res["results"]:
        print("无结果")
        return 0
    for i, hit in enumerate(res["results"], 1):
        _print_hit(i, hit)
        if hit.get("snippet"):
            print(f"    片段: {hit['snippet'][:180]}")
    return 0


def cmd_show(kb: KnowledgeBase, args) -> int:
    try:
        hit = kb.get(args.id)
    except (NotFound, AmbiguousId) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    if hit is None:
        print("错误: 找不到条目", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(hit, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(hit, ensure_ascii=False, indent=2))
    return 0


def cmd_list(kb: KnowledgeBase, args) -> int:
    rows = kb.entries(kind=args.kind, tag=args.tag, limit=args.limit,
                      offset=args.offset, collection=args.collection,
                      include_pending=args.pending, origin=args.origin,
                      status="pending" if args.pending else None,
                      include_chunks=args.chunks)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for hit in rows:
        flag = " [待审核]" if hit.get("status") == "pending" else ""
        if hit.get("parent_id"):
            flag += f" [块{hit.get('chunk_no')}]"
        loc = hit.get("source_path") or "-"
        print(f"{hit['id'][:12]}  [{hit['kind']:6}] {hit['title'][:48]:48}  {loc}{flag}")
    print(f"-- 共 {len(rows)} 条", file=sys.stderr)
    return 0


def cmd_stats(kb: KnowledgeBase, args) -> int:
    rep = kb.stats()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print(f"知识库: {rep['home']}")
    print(f"条目: {rep['entries_total']}  (embedder: {rep['embedder']}, schema v{rep['schema_version']})")
    for kind, v in sorted(rep["by_kind"].items()):
        print(f"  {kind:8} {v['count']:6} 条  {v['size']/1024/1024:10.1f} MB")
    print(f"blob: {rep['blobs']['count']} 个, {rep['blobs']['size']}")
    print(f"目录库: {rep['db_size']}   向量: {rep['vectors']['rows']} 行 / "
          f"{rep['vectors']['shards']} 分片 / {rep['vectors']['size']} (dim={rep['vectors']['dim']})")
    if rep["tags"]:
        top = list(rep["tags"].items())[:10]
        print("标签: " + ", ".join(f"{k}({v})" for k, v in top))
    return 0


def cmd_gaps(kb: KnowledgeBase, args) -> int:
    rep = kb.gaps(days=args.days, min_score=args.min_score,
                  gap_type=args.gap_type)
    label = {"knowledge": "该补的资料", "memory": "本应召回未召回",
             "awareness": "晚获知的重要事件"}[args.gap_type]
    print(f"近 {args.days} 天 {args.gap_type} 日志 {rep['queries']} 条，"
          f"缺口 {len(rep['gaps'])} 组（{label}）:")
    for g in rep["gaps"][:20]:
        print(f"  · {g['count']:3}x  {g['query']}")
    return 0
