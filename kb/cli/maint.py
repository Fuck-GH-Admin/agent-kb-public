"""维护、备份迁移与服务启动类子命令实现。"""
from __future__ import annotations

import json
import sys

from ..core import AmbiguousId, KnowledgeBase, NotFound

def cmd_gc(kb: KnowledgeBase, args) -> int:
    rep = kb.gc(commit=args.commit, empty_trash=args.empty_trash,
                older_than=args.older_than)
    print(f"未引用 blob: {len(rep['orphan_blobs'])} 个")
    if rep["orphan_blobs"] and not args.commit:
        print("（预览模式，未移动。加 --commit 移入回收站；--empty-trash 彻底清空回收站）")
    elif args.commit:
        print(f"已移入回收站 {rep['trashed']} 个")
    if args.empty_trash:
        note = f"（仅 {args.older_than} 天前的）" if args.older_than else ""
        print(f"回收站已清理 {rep['trash_files_removed']} 个文件 {note}")
    return 0


def cmd_verify(kb: KnowledgeBase, args) -> int:
    rep = kb.verify(limit=args.limit)
    ok = not rep["corrupt"] and not rep["missing"]
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        status = "全部完好" if ok else \
            f"损坏 {len(rep['corrupt'])} 个, 丢失 {len(rep['missing'])} 个"
        print(f"已校验 {rep['checked']} 个 blob: {status}")
    return 0 if ok else 1


def cmd_doctor(kb: KnowledgeBase, args) -> int:
    rep = kb.doctor()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    for k, v in rep.items():
        if isinstance(v, list) and v:
            print(f"{k}: {len(v)} 项, 例: {v[:3]}")
        else:
            print(f"{k}: {v}")
    return 0


def cmd_backup(kb: KnowledgeBase, args) -> int:
    rep = kb.backup(args.dest, with_blobs=args.blobs,
                    retention=args.retention, verify=args.verify)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    v = rep.get("verified")
    print(f"备份完成: {rep['dest']}"
          + (f"（校验通过，{v['entries']} 条）" if v else "（未校验，可加 --verify）")
          + (f"，已清理旧备份 {rep['pruned']} 份" if rep.get("pruned") else ""))
    print("提示: blob 库本身是内容寻址的，可用 rsync/hardlink 增量同步 blobs/ 目录")
    return 0


def cmd_restore(kb: KnowledgeBase, args) -> int:
    if not args.yes:
        print("恢复会覆盖当前目录库/向量（config 不动，当前库自动另存 .bak）。"
              "确认请加 --yes", file=sys.stderr)
        return 2
    rep = kb.restore(args.src, force=True)
    print(json.dumps(rep, ensure_ascii=False))
    return 0


def cmd_export(kb: KnowledgeBase, args) -> int:
    rep = kb.export_entries(args.out, collection=args.collection)
    print(f"已导出 {rep['count']} 条编目数据 -> {rep['out']}")
    print("迁移步骤: ① rsync blobs/ 到新机 ② 新机 kb init ③ kb import 本文件 ④ kb reembed")
    return 0


def cmd_import(kb: KnowledgeBase, args) -> int:
    rep = kb.import_entries(args.path)
    print(f"导入完成: 新增 {rep['imported']}，更新 {rep['updated']}，"
          f"缺 blob 跳过 {rep['skipped_missing_blob']}，失败 {rep['failed']}")
    for e in rep["errors"][:5]:
        print(f"  ! {e}")
    if rep["skipped_missing_blob"]:
        print("提示: 先 rsync 同步 blobs/ 再重新 import")
    return 0


def cmd_curate(kb: KnowledgeBase, args) -> int:
    rep = kb.curate(ai=args.ai, apply=args.apply, limit=args.limit,
                    collection=args.collection, force=args.force)
    print(f"扫描 {rep['scanned']} 条：摘要补齐 {rep['summary_fixed']}，"
          f"标签规范化 {rep['tags_fixed']}，AI 编目 {rep['ai_curated']}，"
          f"重复组 {len(rep['duplicates'])}"
          f"（{'已应用' if args.apply else '预览模式，加 --apply 生效'}）")
    for d in rep["duplicates"][:10]:
        print(f"  重复组 {d['hash']}…: {', '.join(d['entries'])}"
              + ("（跨分区，可能是有意冗余）" if d["cross_collection"] else ""))
    for c in rep["changes"][:15]:
        print(f"  · {c}")
    return 0


def cmd_eval(kb: KnowledgeBase, args) -> int:
    if args.from_log:
        g = kb.goldens_from_log(args.from_log)
        print(f"已从查询日志沉淀金标 {g['goldens']} 条 -> {g['out']}")
        if not g["goldens"]:
            return 0
        args.queries = g["out"]
    rep = kb.evaluate(args.queries, k=args.k)
    print(f"金标 {rep['queries']} 条，k={rep['k']}: "
          f"recall@k 平均 {rep['recall@k_avg']}，MRR 平均 {rep['mrr_avg']}")
    for d in rep["detail"]:
        mark = "✓" if d["recall@k"] else "✗"
        print(f"  {mark} {d['query']}: recall={d['recall@k']} mrr={d['mrr']}")
    return 0


def cmd_reindex(kb: KnowledgeBase, args) -> int:
    rep = kb.reindex_fts()
    print(f"FTS 索引已重建: {rep['rebuilt']} 条")
    return 0


def cmd_compact(kb: KnowledgeBase, args) -> int:
    n = kb.compact_vectors()
    print(f"向量分片已压实: 保留 {n} 行")
    return 0


def cmd_claim(kb: KnowledgeBase, args) -> int:
    """Knowledge Claim（架构文档 §22.2）：记录认识论状态，不等于"真"。"""
    from ..policy import CAP_APPROVE
    if not kb.principal.can(CAP_APPROVE):
        print("错误: 当前主体缺少 substrate.approve 能力", file=sys.stderr)
        return 3
    eid = kb.resolve_id(args.id)
    kb.catalog.update_entry(eid, {
        "authority": args.authority, "confidence": args.confidence,
        "epistemic_status": args.status, "created_by": kb.principal.name})
    kb.catalog.audit("knowledge.assert", id=eid, authority=args.authority,
                     confidence=args.confidence, epistemic_status=args.status,
                     principal=kb.principal.name)
    print(f"{eid}: authority={args.authority} confidence={args.confidence} "
          f"epistemic_status={args.status}")
    return 0


def cmd_reembed(kb: KnowledgeBase, args) -> int:
    n = kb.reembed()
    print(f"已重新生成 {n} 条向量")
    return 0


def cmd_backfill(kb: KnowledgeBase, args) -> int:
    rep = kb.backfill_vecs(batch_size=args.batch, limit=args.limit)
    print(f"补齐 {rep['done']}/{rep['total']}，失败 {rep['failed']}"
          f"（失败条目已记审计，可重跑本命令续补）")
    return 0 if rep["failed"] == 0 else 1


def cmd_config(kb: KnowledgeBase, args) -> int:
    if args.action == "get":
        if args.key:
            print(json.dumps(kb.cfg.get(args.key), ensure_ascii=False))
        else:
            print(json.dumps(kb.cfg.data, ensure_ascii=False, indent=2))
        return 0
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError:
        value = args.value
    kb.cfg.set(args.key, value)
    print(f"已设置 {args.key} = {value!r}")
    print("注意: 更换 embed.provider 后需要运行 kb reembed")
    return 0


def cmd_token(kb: KnowledgeBase, args) -> int:
    """token 管理（C12）：config 只存哈希，明文仅创建/轮换时展示一次。"""
    from ..tokens import (LEVELS, add_token, lookup_token, migrate_tokens,
                          needs_migration, revoke_token, rotate_token)
    if args.action == "list" or args.action == "migrate":
        pass  # name 可省
    elif not args.name:
        print(f"{args.action} 需要 name 参数", file=sys.stderr)
        return 2
    if args.action == "add":
        if not args.level:
            print("add 需要 level 参数（admin/operator/viewer）", file=sys.stderr)
            return 2
        tok = add_token(kb.cfg, args.name, args.level)
        print(f"已创建 {args.level} 级 token（name={args.name}），明文仅此一次：\n"
              f"  {tok}\n"
              f"使用: ?token=… 或请求头 X-KB-Token；config 中只存哈希")
        return 0
    if args.action == "list":
        rows = kb.cfg.get("access.tokens") or []
        if not rows:
            print("（无 token）")
            return 0
        for e in rows:
            kind = "哈希" if e.get("hash") else "明文(旧格式!)"
            print(f"{e.get('name'):16} {e.get('level'):9} {kind}"
                  f"  建于 {e.get('created_at', '?')[:10]}")
        if needs_migration(kb.cfg):
            print("存在明文条目，建议 kb token migrate（旧明文 token 将失效）")
        return 0
    if args.action == "revoke":
        ok = revoke_token(kb.cfg, args.name)
        print(f"已撤销 {args.name}" if ok else f"找不到 {args.name}")
        return 0 if ok else 1
    if args.action == "rotate":
        tok = rotate_token(kb.cfg, args.name, args.level)
        print(f"已轮换 {args.name}，新明文仅此一次：\n  {tok}")
        return 0
    if args.action == "verify":   # 自测用：确认某 token 有效
        hit = lookup_token(kb.cfg, args.name)
        print(f"有效: {hit[0]}/{hit[1]}" if hit else "无效")
        return 0 if hit else 1
    if args.action == "migrate":
        dropped = migrate_tokens(kb.cfg)
        print(f"迁移完成：移除明文条目 {dropped} 个（等效 revoke），"
              f"哈希条目保留。请为受影响的用户重新 kb token add")
        return 0
    print(f"未知操作: {args.action}", file=sys.stderr)
    return 2


def cmd_serve_mcp(kb: KnowledgeBase, args) -> int:
    from ..mcp_server import serve
    serve(kb)
    return 0


def cmd_serve_web(kb: KnowledgeBase, args) -> int:
    kb.close()
    from ..web import serve_web
    serve_web(kb.dirs["home"], host=args.host, port=args.port)
    return 0


def cmd_drill(kb: KnowledgeBase, args) -> int:
    rep = kb.drill()
    for s in rep["steps"]:
        mark = "✓" if s["ok"] else "✗"
        extra = "".join(f"  {k}={v}" for k, v in s.items()
                        if k not in ("step", "ok"))
        print(f"  {mark} {s['step']}{extra}")
    print("灾备演练通过：备份→破坏→恢复→校验链路有效" if rep["ok"]
          else "灾备演练失败，请检查上面标 ✗ 的步骤")
    return 0 if rep["ok"] else 1


def cmd_notify_test(kb: KnowledgeBase, args) -> int:
    if not (kb.cfg.get("alerts.webhook") or ""):
        print("未配置 alerts.webhook（kb config set alerts.webhook https://...）",
              file=sys.stderr)
        return 2
    ok = kb.notify("test", {"message": args.message or "agent-kb 告警通道自测"})
    print("告警已投递" if ok else "告警投递失败（检查 webhook 地址与网络）")
    return 0 if ok else 1
