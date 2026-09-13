"""argparse 装配：把各子命令模块挂到 kb <command>。"""
from __future__ import annotations

import argparse
import sys

from .. import __version__
from ..core import AmbiguousId, KnowledgeBase, NotFound
from .ingest import (cmd_add, cmd_approve, cmd_note, cmd_rechunk, cmd_reject,
                     cmd_rm, cmd_tag, cmd_visibility, cmd_watch)
from .maint import (cmd_backfill, cmd_backup, cmd_claim, cmd_compact, cmd_config,
                    cmd_curate, cmd_doctor, cmd_drill, cmd_eval, cmd_export,
                    cmd_gc, cmd_import, cmd_notify_test, cmd_reembed,
                    cmd_reindex, cmd_restore, cmd_serve_mcp, cmd_serve_web,
                    cmd_token, cmd_verify)
from .query import cmd_gaps, cmd_init, cmd_list, cmd_search, cmd_show, cmd_stats


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kb", description="agent 本地知识库（SQLite + 内容寻址 blob + 混合检索）")
    p.add_argument("--version", action="version", version=f"kb {__version__}")
    p.add_argument("--home", help="覆盖 KB_HOME 知识库目录")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化知识库").set_defaults(fn=cmd_init)

    sp = sub.add_parser("add", help="添加文件或目录")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--tags", default="", help="逗号分隔标签")
    sp.add_argument("--collection", default=None,
                    help="主题分区（新增缺省 default；更新时不指定则保留原分区）")
    sp.add_argument("--review", action="store_true", help="入库后置为待审核，需 approve 后可检索")
    sp.add_argument("--visibility", default="internal",
                    choices=["public", "internal", "private"], help="可见级别")
    sp.add_argument("--force-hash", action="store_true",
                    help="跳过 size+mtime 快路径，强制重算哈希")
    sp.add_argument("--link", action="store_true",
                    help="硬链接落库（同文件系统 O(1)；源文件此后不可原地修改）")
    sp.add_argument("--chunk", action="store_true",
                    help="对文本类条目建立块索引（长文档段落级检索）")
    sp.add_argument("--in-place", action="store_true",
                    help="不复制文件，仅索引原始位置（TB 级数据推荐）")
    sp.add_argument("--move", action="store_true", help="摄取后删除源文件")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_add)

    sp = sub.add_parser("note", help="直接写入一条文本笔记")
    sp.add_argument("--title", required=True)
    sp.add_argument("--text", help="笔记内容")
    sp.add_argument("--text-file", help="从文件读内容；缺省读 stdin")
    sp.add_argument("--tags", default="")
    sp.add_argument("--collection", default="default")
    sp.add_argument("--visibility", default="internal",
                    choices=["public", "internal", "private"])
    sp.add_argument("--review", action="store_true")
    sp.set_defaults(fn=cmd_note)

    sp = sub.add_parser("search", help="混合检索（关键词+语义）")
    sp.add_argument("query")
    sp.add_argument("--kind", help="text/code/image/audio/video/pdf/note/binary")
    sp.add_argument("--tag")
    sp.add_argument("--collection", help="限定主题分区")
    sp.add_argument("--pending", action="store_true", help="包含待审核条目")
    sp.add_argument("--path", help="源路径 GLOB，如 */papers/*")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("show", help="查看条目详情")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("list", help="浏览条目")
    sp.add_argument("--kind")
    sp.add_argument("--tag")
    sp.add_argument("--collection")
    sp.add_argument("--pending", action="store_true")
    sp.add_argument("--origin", choices=["human", "agent"], help="按写入来源过滤")
    sp.add_argument("--chunks", action="store_true", help="包含块子条目")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--offset", type=int, default=0)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("approve", help="审核通过待审条目")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_approve)

    sp = sub.add_parser("reject", help="拒绝并删除待审条目")
    sp.add_argument("id")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(fn=cmd_reject)

    sp = sub.add_parser("tag", help="管理标签")
    sp.add_argument("action", choices=["add", "rm"])
    sp.add_argument("id")
    sp.add_argument("tags", nargs="+")
    sp.set_defaults(fn=cmd_tag)

    sp = sub.add_parser("rm", help="删除条目")
    sp.add_argument("id")
    sp.add_argument("--purge", action="store_true", help="同时把无引用 blob 移入回收站")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(fn=cmd_rm)

    sp = sub.add_parser("gc", help="回收未引用 blob")
    sp.add_argument("--commit", action="store_true", help="把孤儿 blob 移入回收站")
    sp.add_argument("--empty-trash", action="store_true")
    sp.add_argument("--older-than", type=int, default=None, metavar="DAYS",
                    help="配合 --empty-trash：只清理 N 天前的回收站文件（老化）")
    sp.set_defaults(fn=cmd_gc)

    sp = sub.add_parser("verify", help="全量校验 blob 哈希")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_verify)

    sp = sub.add_parser("doctor", help="健康体检")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("stats", help="统计信息")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_stats)

    sp = sub.add_parser("backup", help="备份目录库/向量/审计/配置")
    sp.add_argument("dest", nargs="?", default=None)
    sp.add_argument("--blobs", action="store_true", help="同时备份 blob（硬链接优先）")
    sp.add_argument("--retention", type=int, help="只保留最近 N 份备份")
    sp.add_argument("--verify", action="store_true", help="备份后做完整性校验")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_backup)

    sp = sub.add_parser("restore", help="从备份恢复（覆盖当前库，config 不动）")
    sp.add_argument("src")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(fn=cmd_restore)

    sp = sub.add_parser("export", help="导出编目数据 JSONL（迁移用）")
    sp.add_argument("out")
    sp.add_argument("--collection")
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("import", help="导入编目数据 JSONL（按 hash 匹配 blobs）")
    sp.add_argument("path")
    sp.set_defaults(fn=cmd_import)

    sp = sub.add_parser("curate", help="数据整理（离线补齐/标签规范化；--ai 用 LLM 重编目）")
    sp.add_argument("--ai", action="store_true", help="启用 LLM 重编目（需配置 summarize api）")
    sp.add_argument("--apply", action="store_true", help="应用变更（缺省仅预览）")
    sp.add_argument("--limit", type=int, help="AI 编目条数上限")
    sp.add_argument("--force", action="store_true",
                    help="AI 档连已编目条目一起重做（内容更新后用）")
    sp.add_argument("--collection")
    sp.set_defaults(fn=cmd_curate)

    sp = sub.add_parser("eval", help="金标集评估检索质量（recall@k / MRR）")
    sp.add_argument("queries", nargs="?", default=None, help="JSONL: {query, relevant}")
    sp.add_argument("--from-log", metavar="OUT.jsonl",
                    help="先从查询/取用日志自动沉淀金标到该文件再评估")
    sp.add_argument("--k", type=int, default=5)
    sp.set_defaults(fn=cmd_eval)

    sp = sub.add_parser("gaps", help="缺口报告：knowledge/memory/awareness 三类")
    sp.add_argument("--days", type=int, default=30)
    sp.add_argument("--gap-type", default="knowledge",
                    choices=["knowledge", "memory", "awareness"])
    sp.add_argument("--min-score", type=float, default=0.012,
                    help="须低于 RRF 单路上限 1/61≈0.0164，避免误报单路命中")
    sp.set_defaults(fn=cmd_gaps)

    sp = sub.add_parser("rechunk", help="对既有文本条目（重）建块索引")
    sp.add_argument("--collection")
    sp.add_argument("--disable", action="store_true", help="移除全部块索引")
    sp.set_defaults(fn=cmd_rechunk)

    sp = sub.add_parser("watch", help="轮询目录自动增量摄取（配合 cron/--once 可守护）")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--interval", type=int, default=60)
    sp.add_argument("--once", action="store_true", help="跑一遍即退出（cron 友好）")
    sp.add_argument("--tags", default="")
    sp.add_argument("--collection", default="default")
    sp.add_argument("--in-place", action="store_true")
    sp.add_argument("--link", action="store_true")
    sp.add_argument("--chunk", action="store_true")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("token", help="面板 token 管理（config 只存哈希）")
    sp.add_argument("action", choices=["add", "list", "revoke", "rotate",
                                       "verify", "migrate"])
    sp.add_argument("name", nargs="?", default=None,
                    help="用户名（add/revoke/rotate/verify 用）或"
                         "待验证 token（verify 用）；list/migrate 可省")
    sp.add_argument("level", nargs="?", default=None,
                    choices=["admin", "operator", "viewer"],
                    help="add 的级别（rotate 可省略以沿用）")
    sp.set_defaults(fn=cmd_token)

    sp = sub.add_parser("drill", help="灾备演练：临时库跑完整备份→破坏→恢复→校验链路")
    sp.set_defaults(fn=cmd_drill)

    sp = sub.add_parser("notify-test", help="告警通道自测（POST alerts.webhook）")
    sp.add_argument("--message", default=None)
    sp.set_defaults(fn=cmd_notify_test)

    sp = sub.add_parser("serve-web", help="启动人类操作面板（token 鉴权）")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=7800)
    sp.set_defaults(fn=cmd_serve_web)

    sp = sub.add_parser("visibility", help="调整条目可见级别")
    sp.add_argument("id")
    sp.add_argument("level", choices=["public", "internal", "private"])
    sp.set_defaults(fn=cmd_visibility)

    sub.add_parser("reindex", help="重建 FTS 索引").set_defaults(fn=cmd_reindex)
    sub.add_parser("compact", help="压实向量分片").set_defaults(fn=cmd_compact)
    sub.add_parser("reembed", help="按当前配置重新生成全部向量").set_defaults(fn=cmd_reembed)

    sp = sub.add_parser("claim", help="Knowledge Claim：声明条目的认识论状态")
    sp.add_argument("id")
    sp.add_argument("--status", default="asserted",
                    choices=["asserted", "corroborated", "disputed", "obsolete",
                             "unknown"])
    sp.add_argument("--authority", default="derived",
                    choices=["raw", "derived", "verified", "external"])
    sp.add_argument("--confidence", type=float, default=0.0)
    sp.set_defaults(fn=cmd_claim)

    sp = sub.add_parser("backfill", help="增量补齐缺失向量（不动已有向量）")
    sp.add_argument("--batch", type=int, default=32)
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(fn=cmd_backfill)

    sp = sub.add_parser("config", help="查看/修改配置")
    sp.add_argument("action", choices=["get", "set"])
    sp.add_argument("key", nargs="?", default=None)
    sp.add_argument("value", nargs="?", default=None)
    sp.set_defaults(fn=cmd_config)

    sub.add_parser("serve-mcp", help="以 MCP stdio 服务启动（供 agent 客户端拉起）"
                   ).set_defaults(fn=cmd_serve_mcp)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        kb = KnowledgeBase(args.home)
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    try:
        return args.fn(kb, args)
    except (NotFound, AmbiguousId) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:   # 写锁冲突等：给出可操作提示而非堆栈
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    finally:
        kb.close()


if __name__ == "__main__":
    sys.exit(main())
