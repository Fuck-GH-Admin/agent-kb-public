"""摄取管线：目录遍历、哈希去重、版本更新、分块索引、向量索引。每文件一个事务。"""
from __future__ import annotations

import hashlib
import json
import os
import uuid

from .extract import inspect_file, make_title
from .store import BlobStore
from .summarize import build_summary
from .util import chunk_text, entry_text as util_entry_text, human_size
from .util import fts_index_text, keywords, now_iso, sha256_file


class Ingester:
    def __init__(self, kb):
        self.kb = kb
        self._pending_vecs: dict[str, str] = {}  # eid -> 嵌入文本（攒批）

    def add(self, path: str, tags=(), in_place: bool = False, move: bool = False,
            collection: str | None = None, review: bool = False,
            origin: str = "human:local", visibility: str = "internal",
            force_hash: bool = False, link: bool = False,
            chunk: bool | None = None) -> dict:
        p = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(p):
            raise FileNotFoundError(p)
        stats = {"added": 0, "updated": 0, "skipped": 0, "failed": 0,
                 "bytes_stored": 0, "chunks": 0, "errors": []}
        files = [p] if os.path.isfile(p) else self._walk(p)
        for f in files:
            try:
                status, stored, nchunks = self._ingest_file(
                    f, tags, in_place, move, collection, review, origin,
                    visibility, force_hash, link, chunk)
                stats[status] += 1
                stats["bytes_stored"] += stored
                stats["chunks"] += nchunks
            except Exception as e:  # 单文件失败不拖垮整批
                stats["failed"] += 1
                stats["errors"].append(f"{f}: {e}")
        # 批量补嵌入（API 后端 14x 加速，真实教训：逐条嵌入 40 万文件需 333 小时）
        self._flush_pending_vecs()
        return stats

    def _flush_pending_vecs(self):
        """把本批攒下的待嵌入条目按批送 API（失败逐条回退，再失败留待 reembed）。"""
        if not self._pending_vecs:
            return
        kb = self.kb
        try:
            emb = kb.embedder()
        except RuntimeError as e:
            kb.catalog.audit("vec_batch_failed", error=str(e),
                             count=len(self._pending_vecs))
            self._pending_vecs.clear()
            return
        items = list(self._pending_vecs.items())
        self._pending_vecs.clear()
        B = 32
        for i in range(0, len(items), B):
            batch = items[i:i + B]
            try:
                vecs = emb.embed([t for _, t in batch])
                slots = kb.vectors.add_many(vecs)
                for (eid, _), (shard, row) in zip(batch, slots):
                    kb.catalog.add_vec(eid, shard, row)  # add_vec 自带 tx
                kb.catalog.bump_data_version()
            except Exception:
                for eid, t in batch:  # 整批失败 → 逐条兜底
                    try:
                        vec = emb.embed([t])[0]
                        shard, row = kb.vectors.add(vec)
                        kb.catalog.add_vec(eid, shard, row)
                    except Exception as e:
                        kb.catalog.audit("vec_failed", id=eid, error=str(e))

    def _walk(self, root: str) -> list[str]:
        cfg = self.kb.cfg
        excludes = set(cfg.get("ingest.exclude_names") or [])
        out = []
        for dirpath, dirnames, filenames in os.walk(
                root, followlinks=bool(cfg.get("ingest.follow_symlinks"))):
            dirnames[:] = [d for d in dirnames if d not in excludes and
                           not (d.startswith(".") and d != root)]
            for name in sorted(filenames):
                if name in excludes or name.startswith("."):
                    continue
                out.append(os.path.join(dirpath, name))
        return out

    def _ingest_file(self, path: str, tags, in_place: bool, move: bool,
                     collection: str, review: bool, origin: str,
                     visibility: str, force_hash: bool, link: bool,
                     chunk: bool | None) -> tuple[str, int, int]:
        kb = self.kb
        cfg = kb.cfg
        size = os.path.getsize(path)
        max_bytes = int(cfg.get("ingest.max_file_mb") or 512) << 20
        # 上限只约束"会把字节复制进 blob 库"的模式；in-place 只索引原位置、
        # link 零复制，拦它们没有意义（真实教训：600MB 模型文件无法 --in-place 入库）。
        if size > max_bytes and not (in_place or link):
            raise ValueError(
                f"文件 {human_size(size)} 超过 ingest.max_file_mb 复制上限；"
                f"大文件请用 --in-place（零拷贝索引）或 --link（硬链接），"
                f"或调大该配置")
        src = os.path.abspath(path)
        mtime = os.stat(src).st_mtime_ns / 1e9
        cat = kb.catalog

        parent = cat.find_parent_by_source(src)
        row = parent
        # collection=None 表示"未显式指定"：更新时保留原分区，新增时落 default。
        # 否则重摄取一个 papers 分区的文件会被静默改回 default（真实踩过）。
        eff_collection = collection if collection is not None else \
            (row["collection"] if row else "default")
        # 快跳路径：size+mtime 未变则不重算哈希。TB 级库反复增量摄取时避免海量读 IO。
        # 注：精度是"mtime 粒度"，改了内容但刻意还原 mtime 的场景请用 --force-hash。
        if row and not force_hash and row["size"] == size \
                and abs((row["mtime"] or 0) - mtime) < 1e-6 \
                and row["in_place"] == (1 if in_place else 0):
            if tags:
                cat.set_tags(row["id"], list(tags))
            if row["collection"] != eff_collection:
                with cat.tx():
                    cat.conn.execute("UPDATE entries SET collection=? WHERE id=?",
                                     (eff_collection, row["id"]))
            return "skipped", 0, 0

        digest = sha256_file(src)

        fields = self._build_fields(src, digest, size)
        fields["collection"] = eff_collection
        fields["status"] = "pending" if review else "active"
        fields["origin"] = origin
        # Producer 语义（架构文档 §5.4）：origin 为类型前缀，source_principal 为具体身份
        fields["source_type"] = origin.split(":")[0] if ":" in origin else "external:file"
        fields["source_principal"] = origin
        fields["visibility"] = visibility
        fields["mtime"] = mtime
        stored_bytes = 0
        if in_place:
            fields["blob"], fields["in_place"] = "", 1
        else:
            if kb.blobs.exists(digest):
                fields["blob"], fields["in_place"] = digest, 0
            else:
                h, _ = kb.blobs.put_file(src, move=False, link=link)
                fields["blob"], fields["in_place"] = h, 0
                # link 是硬链接（共享 inode，零字节复制），不计入复制量；
                # 跨设备回退 copy 的罕见场景接受记账误差
                stored_bytes = 0 if link else size
        n_chunks = 0
        if row:  # 更新：内容已变化
            eid = row["id"]
            fields.pop("id", None)
            fields["version"] = row["version"] + 1
            fields["created_at"] = row["created_at"]
            cat.update_entry(eid, fields)
            if tags:
                cat.set_tags(eid, list(tags))
            n_chunks = self._sync_chunks(eid, fields, chunk)
            kb.catalog.remove_vecs(eid)
            self._embed_entry(eid, fields["title"], fields["summary"], fields["keywords"])
            kb.catalog.audit("update", id=eid, path=src, hash=digest)
            return "updated", stored_bytes, n_chunks

        eid = uuid.uuid4().hex[:16]
        fields["id"] = eid
        fields["version"] = 1
        ts = now_iso()
        fields["created_at"] = fields["updated_at"] = ts
        cat.insert_entry(fields)
        if tags:
            cat.set_tags(eid, list(tags))
        n_chunks = self._sync_chunks(eid, fields, chunk)
        self._embed_entry(eid, fields["title"], fields["summary"], fields["keywords"])
        if move and not in_place:
            if os.path.exists(src):
                os.unlink(src)
            kb.catalog.audit("move", id=eid, path=src, hash=digest)
        return "added", stored_bytes, n_chunks

    def _build_fields(self, src: str, digest: str, size: int) -> dict:
        kb = self.kb
        ins = inspect_file(src, kb.cfg)
        fallback_title = make_title(src, ins["kind"], ins["text"])
        title, summary, kw = build_summary(
            ins["kind"], fallback_title, ins["text"], ins["meta"], size, kb.cfg, kb.llm())
        preview = (ins["text"] or "")[: int(kb.cfg.get("ingest.preview_chars") or 4096)]
        d = {
            "kind": ins["kind"],
            "title": title,
            "summary": summary,
            "keywords": kw,
            "source_path": src,
            "mime": ins["mime"],
            "ext": ins["ext"],
            "size": size,
            "meta_json": _dumps(ins["meta"]),
            "preview": preview,
            "content_hash": digest,
            "title_fts": fts_index_text(title),
            "summary_fts": fts_index_text(summary),
            "keywords_fts": fts_index_text(kw.replace(",", " ")),
            "preview_fts": fts_index_text(preview),
        }
        d["_text"] = ins["text"]  # 仅供分块用，不入库（_ENTRY_COLS 过滤）
        return d

    def _sync_chunks(self, parent_id: str, fields: dict,
                     chunk_flag: bool | None) -> int:
        """按配置为父条目同步块子条目：参数或内容变化时重建，一致时保留。"""
        kb = self.kb
        cfg = kb.cfg
        enabled = bool(chunk_flag) if chunk_flag is not None \
            else bool(cfg.get("ingest.chunk.enabled"))
        size = int(cfg.get("ingest.chunk.size") or 1200)
        overlap = int(cfg.get("ingest.chunk.overlap") or 200)
        text = fields.get("_text") or util_entry_text(kb, fields)
        existing = kb.catalog.chunks_of(parent_id)

        if not enabled or not text:
            return -kb.catalog.delete_chunks(parent_id) if existing else 0

        want = chunk_text(text, size=size, overlap=overlap)
        # text_len 捕捉"文件没变但抽取逻辑变了"的情形（如编码修复后重摄取），
        # 否则指纹只看 content_hash 会保留旧的坏块。
        fp = {"size": size, "overlap": overlap, "hash": fields["content_hash"],
              "text_len": len(text)}
        if existing:
            try:
                old_fp = json.loads(existing[0]["meta_json"]).get("chunk", {})
            except json.JSONDecodeError:
                old_fp = {}
            if len(existing) == len(want) and {k: old_fp.get(k) for k in fp} == fp:
                return 0  # 参数与内容均未变，块仍有效
        kb.catalog.delete_chunks(parent_id)

        n = 0
        ts = now_iso()
        for i, ctext in enumerate(want):
            cid = uuid.uuid4().hex[:16]
            meta = {"chunk": {**fp, "no": i, "parent": parent_id}}
            row = {
                "id": cid, "kind": fields["kind"], "title": fields["title"],
                "summary": ctext[:300], "keywords": keywords(ctext),
                "source_path": fields["source_path"], "blob": "",
                "mime": fields["mime"], "ext": fields["ext"],
                "size": len(ctext.encode("utf-8")),
                "meta_json": json.dumps(meta, ensure_ascii=False),
                "preview": ctext,
                "content_hash": hashlib.sha256(
                    ctext.encode("utf-8")).hexdigest(),
                "in_place": 0, "version": 1,
                "collection": fields["collection"], "status": fields["status"],
                "origin": fields["origin"], "visibility": fields["visibility"],
                "mtime": fields.get("mtime", 0),
                "parent_id": parent_id, "chunk_no": i,
                "created_at": ts, "updated_at": ts,
                "title_fts": fts_index_text(fields["title"]),
                "summary_fts": fts_index_text(ctext),
                "keywords_fts": fts_index_text(keywords(ctext).replace(",", " ")),
                "preview_fts": fts_index_text(ctext),
            }
            kb.catalog.insert_entry(row)
            self._embed_entry(cid, fields["title"], ctext[:300], row["keywords"])
            n += 1
        return n

    def sync_chunks_for(self, parent_row: dict, chunk_flag: bool = True) -> int:
        """kb rechunk 用：对已有条目（重）建块。"""
        row = dict(parent_row)
        row["_text"] = util_entry_text(self.kb, row)
        return self._sync_chunks(row["id"], row, chunk_flag)

    def _embed_entry(self, eid: str, title: str, summary: str, kw: str):
        """嵌入攒批：add() 结束时 _flush_pending_vecs 批量送 API（14x 提速）。
        hash 后端无网络开销，攒批同样正确。"""
        self._pending_vecs[eid] = f"{title}\n{summary}\n{kw.replace(',', ' ')}"

    def note(self, title: str, text: str, tags=(), collection: str = "default",
             review: bool = False, origin: str = "human:local",
             visibility: str = "internal") -> str:
        kb = self.kb
        digest, _ = kb.blobs.put_bytes(text.encode("utf-8"))
        preview = text[: int(kb.cfg.get("ingest.preview_chars") or 4096)]
        eid = uuid.uuid4().hex[:16]
        ts = now_iso()
        fields = {
            "id": eid, "kind": "note", "title": title, "summary": text[:400],
            "keywords": _kw(text), "source_path": None, "blob": digest, "mime": "text/plain",
            "ext": ".txt", "size": len(text.encode("utf-8")), "meta_json": "{}",
            "preview": preview, "content_hash": digest, "in_place": 0, "version": 1,
            "collection": collection, "status": "pending" if review else "active",
            "origin": origin, "visibility": visibility, "mtime": 0,
            "created_at": ts, "updated_at": ts,
            "title_fts": fts_index_text(title), "summary_fts": fts_index_text(text[:4000]),
            "keywords_fts": "", "preview_fts": fts_index_text(preview),
        }
        kb.catalog.insert_entry(fields)
        if tags:
            kb.catalog.set_tags(eid, list(tags))
        self._embed_entry(eid, title, fields["summary"], fields["keywords"])
        return eid


def _kw(text: str) -> str:
    from .util import keywords
    return keywords(text)


def _dumps(meta: dict) -> str:
    import json
    return json.dumps(meta, ensure_ascii=False)
