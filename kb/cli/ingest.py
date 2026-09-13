"""摄取与内容治理类子命令实现。"""
from __future__ import annotations

import json
import sys

from ..core import AmbiguousId, KnowledgeBase, NotFound

def cmd_add(kb: KnowledgeBase, args) -> int:
    tags = [t for t in (args.tags or "").split(",") if t.strip()]
    total = {"added": 0, "updated": 0, "skipped": 0, "failed": 0,
             "bytes_stored": 0, "chunks": 0, "errors": []}
    for p in args.paths:
        r = kb.add(p, tags=tags, in_place=args.in_place, move=args.move,
                   collection=args.collection, review=args.review,
                   visibility=args.visibility, force_hash=args.force_hash,
                   link=args.link, chunk=args.chunk if args.chunk else None)
        for k in ("added", "updated", "skipped", "failed", "bytes_stored", "chunks"):
            total[k] += r[k]
        total["errors"].extend(r["errors"])
    if args.json:
        print(json.dumps(total, ensure_ascii=False, indent=2))
    else:
        extra = (f"（分区 {args.collection or '保留原分区'}"
                 f"{'，待审核' if args.review else ''}）")
        print(f"新增 {total['added']}，更新 {total['updated']}，"
              f"跳过 {total['skipped']}，失败 {total['failed']}，"
              f"块 {total['chunks']} {extra}")
        for e in total["errors"][:10]:
            print(f"  ! {e}")
    warn_mb = int(kb.cfg.get("ingest.warn_copy_mb") or 0)
    if total["bytes_stored"] > warn_mb << 20 and not (args.in_place or args.link):
        print(f"提示: 本次实际复制入库 {total['bytes_stored']/1048576:.0f}MB。"
              f"大库建议 --in-place（零拷贝）或 --link（硬链接）", file=sys.stderr)
    return 0 if total["failed"] == 0 else 1


def cmd_note(kb: KnowledgeBase, args) -> int:
    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            text = f.read()
    elif args.text:
        text = args.text
    else:
        text = sys.stdin.read()
    if not text.strip():
        print("错误：note 内容为空", file=sys.stderr)
        return 2
    tags = [t for t in (args.tags or "").split(",") if t.strip()]
    eid = kb.note(args.title, text, tags=tags, collection=args.collection,
                  review=args.review, visibility=args.visibility)
    st = "（待审核）" if args.review else ""
    print(f"已写入笔记 {eid}{st}")
    return 0


def cmd_watch(kb: KnowledgeBase, args) -> int:
    """轮询自动摄取：mtime 快跳路径保证未变更文件零 IO。"""
    import time as _t
    paths = args.paths
    print(f"watch: {paths} 每 {args.interval}s 增量摄取（Ctrl+C 停止）")
    first = True
    try:
        while True:
            total = {"added": 0, "updated": 0, "skipped": 0, "failed": 0,
                     "bytes_stored": 0, "chunks": 0, "errors": []}
            for p in paths:
                r = kb.add(p, tags=[t for t in (args.tags or "").split(",") if t],
                           collection=args.collection, in_place=args.in_place,
                           link=args.link, chunk=args.chunk if args.chunk else None)
                for k in ("added", "updated", "skipped", "failed",
                          "bytes_stored", "chunks"):
                    total[k] += r[k]
                total["errors"].extend(r["errors"])
            if args.once:
                print(f"once: 新增 {total['added']}，更新 {total['updated']}，"
                      f"跳过 {total['skipped']}，块 {total['chunks']}")
                return 0 if total["failed"] == 0 else 1
            if total["added"] or total["updated"] or total["failed"] or first:
                print(f"[{_t.strftime('%H:%M:%S')}] 新增 {total['added']} "
                      f"更新 {total['updated']} 失败 {total['failed']}")
                for e in total["errors"][:5]:
                    print(f"  ! {e}")
                first = False
            _t.sleep(args.interval)
    except KeyboardInterrupt:
        print("watch 已停止")
        return 0


def cmd_tag(kb: KnowledgeBase, args) -> int:
    try:
        if args.action == "add":
            eid = kb.add_tag(args.id, args.tags)
        else:
            eid = kb.remove_tag(args.id, args.tags)
    except (NotFound, AmbiguousId) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    print(f"{eid}: 现在的标签: {', '.join(kb.catalog.tags_of(eid))}")
    return 0


def cmd_visibility(kb: KnowledgeBase, args) -> int:
    eid = kb.set_visibility(args.id, args.level)
    print(f"{eid} 可见性 -> {args.level}")
    return 0


def cmd_approve(kb: KnowledgeBase, args) -> int:
    try:
        eid = kb.approve(args.id)
    except (NotFound, AmbiguousId) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    print(f"{eid} 已审核通过，可被检索")
    return 0


def cmd_reject(kb: KnowledgeBase, args) -> int:
    if not args.yes:
        print("拒绝将删除该条目（blob 进回收站）。加 --yes 确认", file=sys.stderr)
        return 2
    try:
        eid = kb.reject(args.id)
    except (NotFound, AmbiguousId) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    print(f"{eid} 已拒绝并删除")
    return 0


def cmd_rm(kb: KnowledgeBase, args) -> int:
    if not args.yes:
        print("危险操作，加 --yes 确认（内容不会物理删除，仅进回收站/解除索引）",
              file=sys.stderr)
        return 2
    try:
        eid = kb.remove(args.id, purge=args.purge)
    except (NotFound, AmbiguousId) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    print(f"已删除条目 {eid}（可从审计日志/备份恢复）")
    return 0


def cmd_rechunk(kb: KnowledgeBase, args) -> int:
    rep = kb.rechunk(collection=args.collection, chunk=not args.disable)
    print(f"已处理 {rep['parents']} 个文本条目，块总数 {rep['chunks']}")
    return 0
