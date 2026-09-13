"""维护操作：备份（保留策略/备份后校验）、恢复、导出/导入（跨机与跨 embedder 迁移）。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time

from .store import _ENTRY_COLS
from .util import now_iso

_SKIP_COLS = {"title_fts", "summary_fts", "keywords_fts", "preview_fts", "rowid"}


def backup(kb, dest: str | None = None, with_blobs: bool = False,
           retention: int | None = None, verify: bool = False) -> dict:
    """快照备份。retention=N 只保留最近 N 份（超出自动清理）；verify 做备份后校验。"""
    base = os.path.join(kb.dirs["backups"], time.strftime("%Y%m%d-%H%M%S"))
    dest, i = base, 1
    while os.path.exists(dest):  # 同秒多次备份不覆盖
        dest, i = f"{base}-{i}", i + 1
    os.makedirs(dest, exist_ok=True)
    kb.catalog.backup_to(os.path.join(dest, "kb.db"))
    if os.path.exists(kb.dirs["vectors"]):
        shutil.copytree(kb.dirs["vectors"], os.path.join(dest, "vectors"),
                        dirs_exist_ok=True)
    audit = os.path.join(kb.dirs["logs"], "audit.jsonl")
    if os.path.exists(audit):
        shutil.copy2(audit, os.path.join(dest, "audit.jsonl"))
    cfg_path = os.path.join(kb.dirs["home"], "config.json")
    if os.path.exists(cfg_path):
        shutil.copy2(cfg_path, os.path.join(dest, "config.json"))
    if with_blobs:
        blob_dest = os.path.join(dest, "blobs")
        for d in kb.blobs.iter_blobs():
            rel = os.path.join(d[:2], d[2:4])
            os.makedirs(os.path.join(blob_dest, rel), exist_ok=True)
            dst = os.path.join(blob_dest, rel, d)
            if not os.path.exists(dst):
                try:
                    os.link(kb.blobs.path(d), dst)
                except OSError:
                    shutil.copy2(kb.blobs.path(d), dst)
    manifest = {"created_at": now_iso(), "home": kb.dirs["home"],
                "schema_version": kb.catalog.get_meta("schema_version"),
                "embedder": kb.catalog.get_meta("embedder"),
                "with_blobs": with_blobs}
    with open(os.path.join(dest, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    report = {"dest": dest, "verified": None, "pruned": 0}
    if verify:
        conn = sqlite3.connect(os.path.join(dest, "kb.db"))
        ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        n = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        conn.close()
        report["verified"] = {"quick_check": ok, "entries": n}
        if not ok:
            raise RuntimeError("备份校验失败，请检查磁盘与权限后重试")
    if retention:
        siblings = sorted(
            (d for d in os.listdir(kb.dirs["backups"])
             if os.path.isdir(os.path.join(kb.dirs["backups"], d))),
            reverse=True)
        for old in siblings[retention:]:
            shutil.rmtree(os.path.join(kb.dirs["backups"], old), ignore_errors=True)
            report["pruned"] += 1
    kb.catalog.audit("backup", dest=dest, with_blobs=with_blobs,
                     verified=bool(verify))
    return report


def restore(kb, src_dir: str, force: bool = False) -> dict:
    """从备份目录恢复数据（不覆盖 config.json，保留 token/API key）。
    当前库有数据时必须 --force（会先把当前库另存为 .pre-restore.bak）。"""
    src_dir = os.path.abspath(src_dir)
    db = os.path.join(src_dir, "kb.db")
    if not os.path.exists(db):
        raise FileNotFoundError(f"备份目录缺少 kb.db: {src_dir}")
    cur = os.path.join(kb.dirs["home"], "kb.db")
    if os.path.exists(cur) and not force:
        n = next(kb.catalog.iter_all("COUNT(*) c"))["c"]
        if n > 0:
            raise RuntimeError(
                f"当前库已有 {n} 条数据。确认覆盖请加 --force（当前库会另存为 .pre-restore.bak）")
    if os.path.exists(cur):
        # 关键：先冲刷当前连接的 WAL 并清空，否则进程退出时的 checkpoint
        # 会把连接里的旧状态写回恢复后的文件，恢复被静默撤销。
        try:
            kb.catalog.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        shutil.copy2(cur, cur + ".pre-restore.bak")
        for suf in ("-wal", "-shm"):
            if os.path.exists(cur + suf):
                os.unlink(cur + suf)
    shutil.copy2(db, cur)
    src_vec = os.path.join(src_dir, "vectors")
    if os.path.exists(src_vec):
        shutil.rmtree(kb.dirs["vectors"], ignore_errors=True)
        shutil.copytree(src_vec, kb.dirs["vectors"])
    src_audit = os.path.join(src_dir, "audit.jsonl")
    if os.path.exists(src_audit):
        cur_audit = os.path.join(kb.dirs["logs"], "audit.jsonl")
        if os.path.exists(cur_audit):
            os.replace(cur_audit, cur_audit + "." + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(src_audit, cur_audit)
    kb.catalog.audit("restore", src=src_dir)
    return {"restored_from": src_dir, "need": "重启进程后生效；建议立即运行 kb verify"}


def export_entries(kb, out_path: str, collection: str | None = None) -> dict:
    """导出编目元数据为 JSONL（不含 FTS 内部列）。配合 rsync blobs/ 即可整机迁移。"""
    out_path = os.path.abspath(out_path)
    count = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for row in kb.catalog.iter_all():
            if collection and row["collection"] != collection:
                continue
            rec = {k: v for k, v in row.items() if k not in _SKIP_COLS}
            rec["tags"] = kb.catalog.tags_of(row["id"])
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count += 1
    kb.catalog.audit("export", out=out_path, count=count)
    return {"out": out_path, "count": count}


def import_entries(kb, in_path: str, reembed: bool = True) -> dict:
    """按 id（优先）或 content_hash/source_path 匹配合并；缺 blob 的条目跳过并提示。
    换 embedder 迁移后用 kb reembed 统一重建向量。"""
    from .util import now_iso
    report = {"imported": 0, "updated": 0, "skipped_missing_blob": 0, "failed": 0,
              "errors": []}
    with open(in_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                eid = rec.get("id")
                blob = rec.get("blob") or ""
                if blob and not rec.get("in_place") and not kb.blobs.exists(blob):
                    report["skipped_missing_blob"] += 1
                    continue
                old = kb.catalog.get(eid) if eid else None
                if old is None and rec.get("content_hash"):
                    same = [r for r in kb.catalog.find_by_hash(rec["content_hash"])
                            if r["source_path"] == rec.get("source_path")]
                    old = same[0] if same else None
                fields = {k: rec.get(k) for k in _ENTRY_COLS
                          if k not in ("id",) and k in rec}
                fields["updated_at"] = now_iso()
                tags = rec.get("tags") or []
                if old is not None:
                    fields["created_at"] = old["created_at"]
                    kb.catalog.update_entry(old["id"], fields)
                    kb.catalog.remove_vecs(old["id"])
                    _revec(kb, old["id"], fields) if reembed else None
                    kb.catalog.set_tags(old["id"], tags)
                    report["updated"] += 1
                else:
                    fields["id"] = eid
                    kb.catalog.insert_entry(fields)
                    kb.catalog.set_tags(eid, tags)
                    if reembed:
                        _revec(kb, eid, fields)
                    report["imported"] += 1
            except Exception as e:
                report["failed"] += 1
                report["errors"].append(str(e))
    kb.catalog.audit("import", **{k: v for k, v in report.items() if k != "errors"})
    return report


def _revec(kb, eid: str, fields: dict):
    try:
        text = f"{fields.get('title','')}\n{fields.get('summary','')}\n" \
               f"{fields.get('keywords','').replace(',', ' ')}"
        vec = kb.embedder().embed([text])[0]
        shard, row = kb.vectors.add(vec)
        kb.catalog.add_vec(eid, shard, row)
    except Exception:
        kb.catalog.audit("vec_failed", id=eid, phase="import")
