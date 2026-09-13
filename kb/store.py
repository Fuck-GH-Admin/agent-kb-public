"""存储层：内容寻址不可变 blob 库 + SQLite 目录（WAL、FTS5 外部内容表、审计日志）。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time

from .policy import sql_scope
from .util import append_jsonl, blob_rel, now_iso


def shutil_copyfileobj(fin, fout, chunk: int = 1 << 20):
    for block in iter(lambda: fin.read(chunk), b""):
        fout.write(block)

SCHEMA_VERSION = 6

_ENTRY_COLS = (
    "id", "kind", "title", "summary", "keywords", "source_path", "blob", "mime",
    "ext", "size", "meta_json", "preview", "content_hash", "in_place", "version",
    "collection", "status", "origin", "visibility", "mtime", "parent_id", "chunk_no",
    "source_type", "source_principal", "created_by", "derived_from", "authority",
    "confidence", "epistemic_status", "retention_class", "valid_from", "valid_until",
    "created_at", "updated_at", "title_fts", "summary_fts", "keywords_fts", "preview_fts",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS entries(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  keywords TEXT NOT NULL DEFAULT '',
  source_path TEXT,
  blob TEXT NOT NULL DEFAULT '',
  mime TEXT NOT NULL DEFAULT '',
  ext TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  meta_json TEXT NOT NULL DEFAULT '{}',
  preview TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  in_place INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  collection TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL DEFAULT 'active',
  origin TEXT NOT NULL DEFAULT 'human',
  visibility TEXT NOT NULL DEFAULT 'internal',
  mtime REAL NOT NULL DEFAULT 0,
  parent_id TEXT,
  chunk_no INTEGER NOT NULL DEFAULT 0,
  source_type TEXT NOT NULL DEFAULT 'file',
  source_principal TEXT NOT NULL DEFAULT 'local',
  created_by TEXT NOT NULL DEFAULT 'local',
  derived_from TEXT,
  authority TEXT NOT NULL DEFAULT 'raw',
  confidence REAL NOT NULL DEFAULT 0,
  epistemic_status TEXT NOT NULL DEFAULT 'unknown',
  retention_class TEXT NOT NULL DEFAULT 'NORMAL',
  valid_from TEXT,
  valid_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  title_fts TEXT NOT NULL DEFAULT '',
  summary_fts TEXT NOT NULL DEFAULT '',
  keywords_fts TEXT NOT NULL DEFAULT '',
  preview_fts TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_entries_kind ON entries(kind);
CREATE INDEX IF NOT EXISTS idx_entries_hash ON entries(content_hash);
CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source_path);
CREATE TABLE IF NOT EXISTS tags(
  entry_id TEXT NOT NULL, tag TEXT NOT NULL,
  PRIMARY KEY(entry_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
CREATE TABLE IF NOT EXISTS semantic_cache(
  qkey TEXT PRIMARY KEY,      -- sha256(normalized query + scope + 参数指纹)
  qvec_blob BLOB,             -- 查询向量（L2 近义匹配用）
  result_json TEXT NOT NULL,
  data_version INTEGER NOT NULL,
  scope TEXT,                 -- 检索参数指纹
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS knowledge_claims(
  claim_id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL,
  claim TEXT NOT NULL,
  authority TEXT NOT NULL DEFAULT 'derived',
  confidence REAL NOT NULL DEFAULT 0,
  epistemic_status TEXT NOT NULL DEFAULT 'asserted',
  created_by TEXT NOT NULL DEFAULT 'local',
  derived_from TEXT,
  valid_from TEXT,
  valid_until TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_entry ON knowledge_claims(entry_id);
CREATE INDEX IF NOT EXISTS idx_claims_status ON knowledge_claims(epistemic_status);
CREATE TABLE IF NOT EXISTS provenance_edges(
  edge_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL,
  parent_id TEXT NOT NULL,
  relation TEXT NOT NULL DEFAULT 'derived_from',
  created_by TEXT NOT NULL DEFAULT 'local',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prov_child ON provenance_edges(child_id);
CREATE TABLE IF NOT EXISTS vec_rows(
  entry_id TEXT NOT NULL, shard INTEGER NOT NULL, row INTEGER NOT NULL,
  PRIMARY KEY(entry_id, shard, row)
);
CREATE INDEX IF NOT EXISTS idx_vec_entry ON vec_rows(entry_id);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
  title_fts, summary_fts, keywords_fts, preview_fts,
  content='entries', content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
  INSERT INTO entries_fts(rowid, title_fts, summary_fts, keywords_fts, preview_fts)
  VALUES (new.rowid, new.title_fts, new.summary_fts, new.keywords_fts, new.preview_fts);
END;
CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, title_fts, summary_fts, keywords_fts, preview_fts)
  VALUES ('delete', old.rowid, old.title_fts, old.summary_fts, old.keywords_fts, old.preview_fts);
END;
CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE OF
  title_fts, summary_fts, keywords_fts, preview_fts ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, title_fts, summary_fts, keywords_fts, preview_fts)
  VALUES ('delete', old.rowid, old.title_fts, old.summary_fts, old.keywords_fts, old.preview_fts);
  INSERT INTO entries_fts(rowid, title_fts, summary_fts, keywords_fts, preview_fts)
  VALUES (new.rowid, new.title_fts, new.summary_fts, new.keywords_fts, new.preview_fts);
END;
"""


class BlobStore:
    """内容寻址（SHA-256）不可变文件库；写入先落临时文件、校验哈希后原子改名。"""

    def __init__(self, root: str, trash_root: str):
        self.root = root
        self.trash_root = trash_root
        os.makedirs(root, exist_ok=True)
        os.makedirs(trash_root, exist_ok=True)

    def path(self, digest: str) -> str:
        return os.path.join(self.root, blob_rel(digest))

    def exists(self, digest: str) -> bool:
        return os.path.exists(self.path(digest))

    def put_file(self, src: str, move: bool = False, link: bool = False) -> tuple[str, int]:
        """copy / move / link 三种落库模式。
        copy/move 单遍完成哈希+写入；link 先哈希再硬链接（同文件系统 O(1)），
        跨设备或链接失败自动回退 copy。link 之后源文件原地修改会连带改动 blob，
        属于已知代价（--force-hash 可检测，verify 会报告）。"""
        digest = hashlib.sha256()
        size = 0
        os.makedirs(os.path.join(self.root, "tmp"), exist_ok=True)
        tmp = os.path.join(self.root, "tmp", os.urandom(8).hex())
        try:
            with open(src, "rb") as fin, open(tmp, "wb") as fout:
                for block in iter(lambda: fin.read(1 << 20), b""):
                    digest.update(block)
                    size += len(block)
                    if not link:
                        fout.write(block)
                if not link:
                    fout.flush()
                    os.fsync(fout.fileno())
            actual = digest.hexdigest()
            final = self.path(actual)
            if os.path.exists(final):
                return actual, size  # 内容寻址天然去重
            os.makedirs(os.path.dirname(final), exist_ok=True)
            if link:
                try:
                    os.link(src, final)
                except OSError:  # 跨设备等场景回退 copy
                    with open(src, "rb") as fin, open(tmp, "wb") as fout:
                        shutil_copyfileobj(fin, fout)
                    os.replace(tmp, final)
            else:
                os.replace(tmp, final)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        if move:
            os.unlink(src)
        return actual, size

    def put_bytes(self, data: bytes) -> tuple[str, int]:
        digest = hashlib.sha256(data).hexdigest()
        final = self.path(digest)
        if not os.path.exists(final):
            os.makedirs(os.path.dirname(final), exist_ok=True)
            tmp = final + ".tmp-" + os.urandom(6).hex()
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, final)
        return digest, len(data)

    def trash(self, digest: str) -> str | None:
        src = self.path(digest)
        if not os.path.exists(src):
            return None
        name = f"{time.strftime('%Y%m%d-%H%M%S')}_{digest[:16]}"
        os.replace(src, os.path.join(self.trash_root, name))
        return name

    def verify(self, digest: str) -> str:
        """返回 ok | missing | corrupt"""
        p = self.path(digest)
        if not os.path.exists(p):
            return "missing"
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return "ok" if h.hexdigest() == digest else "corrupt"

    def iter_blobs(self):
        for a in sorted(os.listdir(self.root)):
            if len(a) != 2 or any(c not in "0123456789abcdef" for c in a):
                continue
            for b in sorted(os.listdir(os.path.join(self.root, a))):
                for name in sorted(os.listdir(os.path.join(self.root, a, b))):
                    if len(name) == 64 and all(c in "0123456789abcdef" for c in name):
                        yield name


class Catalog:
    def __init__(self, db_path: str, audit_path: str, principal: str = "local"):
        self.db_path = db_path
        self.audit_path = audit_path
        self.principal = principal
        self.conn = sqlite3.connect(db_path, isolation_level=None, timeout=30,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA cache_size=-16000")
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self):
        self._ensure_columns()
        v = self.get_meta("schema_version")
        if v is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.set_meta("created_at", now_iso())
        elif int(v) != SCHEMA_VERSION:
            # 迁移前给旧库文件留一份快照，升级失败可回退
            try:
                pre = self.db_path + f".pre-v{SCHEMA_VERSION}.bak"
                if not os.path.exists(pre):
                    self.backup_to(pre)
                    self.audit("pre_migration_backup", path=pre)
            except sqlite3.Error:
                pass
            self.audit("migrate", from_version=int(v), to_version=SCHEMA_VERSION)
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    def _ensure_columns(self):
        """旧库打开自动补列（幂等）：v2 增 collection/status/origin，
        v3 增 visibility/mtime。避免索引建在缺失列上：索引在此统一创建。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(entries)")}
        adds = {
            "collection": "TEXT NOT NULL DEFAULT 'default'",
            "status": "TEXT NOT NULL DEFAULT 'active'",
            "origin": "TEXT NOT NULL DEFAULT 'human'",
            "visibility": "TEXT NOT NULL DEFAULT 'internal'",
            "mtime": "REAL NOT NULL DEFAULT 0",
            "parent_id": "TEXT",
            "chunk_no": "INTEGER NOT NULL DEFAULT 0",
            "source_type": "TEXT NOT NULL DEFAULT 'file'",
            "source_principal": "TEXT NOT NULL DEFAULT 'local'",
            "created_by": "TEXT NOT NULL DEFAULT 'local'",
            "derived_from": "TEXT",
            "authority": "TEXT NOT NULL DEFAULT 'raw'",
            "confidence": "REAL NOT NULL DEFAULT 0",
            "epistemic_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "retention_class": "TEXT NOT NULL DEFAULT 'NORMAL'",
            "valid_from": "TEXT",
            "valid_until": "TEXT",
        }
        for col, decl in adds.items():
            if col not in cols:
                self.conn.execute(f"ALTER TABLE entries ADD COLUMN {col} {decl}")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_collection ON entries(collection)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_visibility ON entries(visibility)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_epistemic ON entries(epistemic_status)")
        # v6：claims / provenance edges（CREATE IF NOT EXISTS，幂等）
        self.conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_claims(
            claim_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL, claim TEXT NOT NULL,
            authority TEXT NOT NULL DEFAULT 'derived', confidence REAL NOT NULL DEFAULT 0,
            epistemic_status TEXT NOT NULL DEFAULT 'asserted',
            created_by TEXT NOT NULL DEFAULT 'local', derived_from TEXT,
            valid_from TEXT, valid_until TEXT, created_at TEXT NOT NULL)""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_entry ON knowledge_claims(entry_id)")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS provenance_edges(
            edge_id TEXT PRIMARY KEY, child_id TEXT NOT NULL, parent_id TEXT NOT NULL,
            relation TEXT NOT NULL DEFAULT 'derived_from',
            created_by TEXT NOT NULL DEFAULT 'local', created_at TEXT NOT NULL)""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_prov_child ON provenance_edges(child_id)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_retention ON entries(retention_class)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_parent ON entries(parent_id)")

    # ---- meta ----
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str):
        """写 meta。data_version 之外的所有 meta 都不影响检索结果集；
        涉及检索行为的 meta（embedder/vec_*）由调用方显式 bump_data_version。"""
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def data_version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='data_version'").fetchone()
        return int(row[0]) if row else 0

    def bump_data_version(self):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES('data_version','1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)")

    def cache_get(self, qkey: str, version: int) -> str | None:
        row = self.conn.execute(
            "SELECT result_json, data_version FROM semantic_cache WHERE qkey=?",
            (qkey,)).fetchone()
        if row and row["data_version"] == version:
            return row["result_json"]
        if row:
            self.conn.execute("DELETE FROM semantic_cache WHERE qkey=?", (qkey,))
        return None

    def cache_put(self, qkey: str, result_json: str, version: int):
        """只更新结果列；qvec_blob 由 cache_store_similar 单独写入（同键两次写
        会互相覆盖，真实踩过：put 的全列 REPLACE 把向量冲成 NULL）。"""
        self.conn.execute(
            "INSERT INTO semantic_cache(qkey,result_json,data_version,created_at) "
            "VALUES(?,?,?,?) ON CONFLICT(qkey) DO UPDATE SET "
            "result_json=excluded.result_json, data_version=excluded.data_version",
            (qkey, result_json, version, now_iso()))

    def cache_clear(self) -> int:
        n = self.conn.execute("SELECT COUNT(*) FROM semantic_cache").fetchone()[0]
        self.conn.execute("DELETE FROM semantic_cache")
        self.audit("cache_clear", entries=n)
        return n

    def bump_version_for_meta(self, key: str):
        """检索行为相关的 meta 变更后调用（embedder/vec_dim/vec_shards/vec_next）。"""
        if key.startswith(("embedder", "vec_")):
            self.bump_data_version()

    # ---- entries ----
    def insert_entry(self, d: dict) -> str:
        cols = [c for c in _ENTRY_COLS if c in d]
        sql = (f"INSERT INTO entries({','.join(cols)}) "
               f"VALUES({','.join('?' * len(cols))})")
        with self.tx():
            self.conn.execute(sql, [d[c] for c in cols])
        self.bump_data_version()
        self.audit("insert", id=d["id"], kind=d.get("kind"), path=d.get("source_path"),
                   hash=d.get("content_hash"))
        return d["id"]

    def update_entry(self, eid: str, d: dict):
        cols = [c for c in _ENTRY_COLS if c in d]
        if not cols:
            return
        sql = (f"UPDATE entries SET {','.join(c + '=?' for c in cols)} WHERE id=?")
        with self.tx():
            self.conn.execute(sql, [d[c] for c in cols] + [eid])
        self.bump_data_version()
        self.audit("update", id=eid, **{k: d.get(k) for k in ("version", "content_hash")})

    def delete_entry(self, eid: str):
        with self.tx():
            self.conn.execute("DELETE FROM tags WHERE entry_id=?", (eid,))
            self.conn.execute("DELETE FROM vec_rows WHERE entry_id=?", (eid,))
            self.conn.execute("DELETE FROM entries WHERE id=?", (eid,))
        self.bump_data_version()
        self.audit("delete", id=eid)

    def get(self, eid: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
        return dict(row) if row else None

    def by_prefix(self, pfx: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM entries WHERE id LIKE ? ORDER BY id", (pfx + "%",)).fetchall()
        return [dict(r) for r in rows]

    def find_by_hash(self, h: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM entries WHERE content_hash=?", (h,)).fetchall()
        return [dict(r) for r in rows]

    def find_by_source(self, path: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM entries WHERE source_path=? ORDER BY id", (path,)).fetchall()
        return [dict(r) for r in rows]

    def find_parent_by_source(self, path: str) -> dict | None:
        """父条目查询（排除块子条目——块与父条目共享 source_path）。"""
        row = self.conn.execute(
            "SELECT * FROM entries WHERE source_path=? AND parent_id IS NULL",
            (path,)).fetchone()
        return dict(row) if row else None

    def chunks_of(self, parent_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM entries WHERE parent_id=? ORDER BY chunk_no",
            (parent_id,)).fetchall()
        return [dict(r) for r in rows]

    def delete_chunks(self, parent_id: str) -> int:
        chunks = self.chunks_of(parent_id)
        with self.tx():
            for c in chunks:
                self.conn.execute("DELETE FROM vec_rows WHERE entry_id=?", (c["id"],))
                self.conn.execute("DELETE FROM tags WHERE entry_id=?", (c["id"],))
                self.conn.execute("DELETE FROM entries WHERE id=?", (c["id"],))
        self.audit("chunks_delete", id=parent_id, count=len(chunks))
        return len(chunks)

    def count_chunks(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM entries WHERE parent_id IS NOT NULL").fetchone()[0]

    def count_by_kind_parents(self) -> dict:
        """只统计父条目（块不是独立资料，计入总数会误导用户，DESIGN-V2 C4）。"""
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) c, SUM(size) s FROM entries "
            "WHERE parent_id IS NULL GROUP BY kind").fetchall()
        return {r["kind"]: {"count": r["c"], "size": r["s"] or 0} for r in rows}

    def search_fts(self, match: str, extra_sql: str, params: list, limit: int,
                   weights=(6.0, 3.0, 2.0, 1.0)) -> list[dict]:
        """BM25 列加权（title_fts, summary_fts, keywords_fts, preview_fts），
        并用 snippet() 给出命中片段，帮助 agent 直接定位原文段落。"""
        w = ", ".join(repr(float(x)) for x in weights)
        sql = ("SELECT e.*, snippet(entries_fts, 3, '[', ']', '…', 24) AS snippet "
               "FROM entries_fts f JOIN entries e ON e.rowid = f.rowid "
               "WHERE entries_fts MATCH ? " + extra_sql +
               f" ORDER BY bm25(entries_fts, {w}) LIMIT ?")
        rows = self.conn.execute(sql, [match] + params + [limit]).fetchall()
        return [dict(r) for r in rows]

    def search_like(self, like_terms: list[str], extra_sql: str, params: list,
                    limit: int) -> list[dict]:
        """第三级降级：FTS 索引损坏/不可用时对 title/summary/preview 做 LIKE 扫描。
        慢但可用，保证检索功能不因索引损坏而完全失效。

        无有效词项时（如查询 "*"、纯标点）退化为按更新时间列出，
        不能拼出 "WHERE ()" 那样的空条件——那会直接抛 SQL 语法错误。
        """
        conds, like_params = [], []
        for t in like_terms:
            conds.append("(e.title LIKE ? OR e.summary LIKE ? OR e.preview LIKE ?)")
            like_params.extend([f"%{t}%"] * 3)
        term_sql = ("(" + " OR ".join(conds) + ")") if conds else "1=1"
        sql = (f"SELECT e.*, NULL AS snippet FROM entries e WHERE {term_sql} "
               + extra_sql + " ORDER BY e.updated_at DESC LIMIT ?")
        rows = self.conn.execute(sql, like_params + params + [limit]).fetchall()
        return [dict(r) for r in rows]

    def entries_page(self, kind: str | None, tag: str | None, limit: int, offset: int,
                     collection: str | None = None, include_pending: bool = False,
                     origin: str | None = None, status: str | None = None,
                     visible: tuple | None = None,
                     include_chunks: bool = False) -> list[dict]:
        # 状态与可见性统一由 policy 生成；status 显式指定时覆盖默认 active 过滤
        where, params = sql_scope(visible, include_pending=include_pending or bool(status),
                                  include_chunks=include_chunks)
        if status:
            where.append("e.status=?")
            params.append(status)
        if kind:
            where.append("e.kind=?")
            params.append(kind)
        if tag:
            where.append("EXISTS(SELECT 1 FROM tags t WHERE t.entry_id=e.id AND t.tag=?)")
            params.append(tag)
        if collection:
            where.append("e.collection=?")
            params.append(collection)
        if origin:
            where.append("e.origin=?")
            params.append(origin)
        extra = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self.conn.execute(
            f"SELECT e.* FROM entries e {extra} ORDER BY e.updated_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
        return [dict(r) for r in rows]

    def all_collections(self) -> dict:
        rows = self.conn.execute(
            "SELECT collection, COUNT(*) c FROM entries GROUP BY collection").fetchall()
        return {r["collection"]: r["c"] for r in rows}

    def set_children_visibility(self, parent_id: str, visibility: str) -> int:
        """块继承父条目可见性（块含父文档正文，不级联即越权泄漏）。"""
        with self.tx():
            cur = self.conn.execute(
                "UPDATE entries SET visibility=? WHERE parent_id=?",
                (visibility, parent_id))
            return cur.rowcount or 0

    def set_status(self, eid: str, status: str):
        with self.tx():
            self.conn.execute("UPDATE entries SET status=? WHERE id=?", (status, eid))
        self.bump_data_version()
        self.audit("status", id=eid, status=status)

    def count_status(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) c FROM entries GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    def count_by_kind(self) -> dict:
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) c, SUM(size) s FROM entries GROUP BY kind").fetchall()
        return {r["kind"]: {"count": r["c"], "size": r["s"] or 0} for r in rows}

    def all_tags_usage(self) -> dict:
        rows = self.conn.execute(
            "SELECT tag, COUNT(*) c FROM tags GROUP BY tag ORDER BY c DESC").fetchall()
        return {r["tag"]: r["c"] for r in rows}

    # ---- tags ----
    def set_tags(self, eid: str, tags: list[str]):
        with self.tx():
            for t in tags:
                self.conn.execute(
                    "INSERT OR IGNORE INTO tags(entry_id,tag) VALUES(?,?)", (eid, t))
        self.audit("tag_add", id=eid, tags=tags)

    def remove_tag(self, eid: str, tag: str):
        with self.tx():
            self.conn.execute("DELETE FROM tags WHERE entry_id=? AND tag=?", (eid, tag))
        self.audit("tag_rm", id=eid, tag=tag)

    def tags_of(self, eid: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT tag FROM tags WHERE entry_id=? ORDER BY tag", (eid,))]

    def entry_ids_with_tag(self, tag: str) -> set[str]:
        return {r[0] for r in self.conn.execute(
            "SELECT entry_id FROM tags WHERE tag=?", (tag,))}

    # ---- vectors ----
    def add_vec(self, eid: str, shard: int, row: int):
        with self.tx():
            self.conn.execute(
                "INSERT OR REPLACE INTO vec_rows(entry_id,shard,row) VALUES(?,?,?)",
                (eid, shard, row))

    def remove_vecs(self, eid: str):
        with self.tx():
            self.conn.execute("DELETE FROM vec_rows WHERE entry_id=?", (eid,))

    def all_vec_rows(self) -> list[tuple[str, int, int]]:
        return [(r[0], r[1], r[2]) for r in self.conn.execute(
            "SELECT entry_id, shard, row FROM vec_rows ORDER BY shard, row")]

    def scoped_vec_rows(self, where: list[str], params: list,
                        limit: int | None = None) -> list[tuple[str, int, int]]:
        """向量检索的候选行集：过滤下推到相似度计算之前（DESIGN-V2 C2）。

        where 为针对 entries 别名 e 的条件列表（由 policy.sql_scope 生成）。
        返回 [(entry_id, shard, row)]；空 where 等价 all_vec_rows。
        """
        sql = ("SELECT v.entry_id, v.shard, v.row FROM vec_rows v "
               "JOIN entries e ON e.id = v.entry_id")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY v.shard, v.row"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [(r[0], r[1], r[2]) for r in self.conn.execute(sql, params)]

    def count_vec(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM vec_rows").fetchone()[0]

    # ---- misc ----
    def iter_all(self, cols: str = "*"):
        for r in self.conn.execute(f"SELECT {cols} FROM entries ORDER BY created_at"):
            yield dict(r) if cols == "*" else r

    def quick_check(self) -> str:
        return self.conn.execute("PRAGMA quick_check").fetchone()[0]

    def fts_integrity(self) -> str:
        try:
            self.conn.execute("INSERT INTO entries_fts(entries_fts) VALUES('integrity-check')")
            return "ok"
        except sqlite3.DatabaseError as e:
            return f"fts损坏: {e}"

    def backup_to(self, dest: str):
        dst = sqlite3.connect(dest)
        with dst:
            self.conn.backup(dst)
        dst.close()

    class _Tx:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            self.conn.execute("BEGIN IMMEDIATE")

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
            return False

    def tx(self):
        return self._Tx(self.conn)

    def audit(self, op: str, **kw):
        """审计留痕。principal 记录"谁"做的（多管理员场景必需，DESIGN-V2 D2）。"""
        rec = {"ts": now_iso(), "op": op, "principal": self.principal}
        rec.update({k: v for k, v in kw.items() if v is not None})
        append_jsonl(self.audit_path, rec)

    def close(self):
        try:  # 收拢 WAL，减少冷备份时丢最近事务的窗口
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.conn.close()
