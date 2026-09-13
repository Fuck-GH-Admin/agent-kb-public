"""KnowledgeBase 门面：统一配置、目录、blob、向量、检索与全部维护命令。"""
from __future__ import annotations

import json
import os

from .config import Config, ensure_dirs
from .embed import make_embedder
from .ingest import Ingester
from .search import search as _search, to_hit
from .store import BlobStore, Catalog
from .policy import LEVEL_VISIBILITY, Principal
from .summarize import LLM
from .util import WriteLock, append_jsonl, human_size, now_iso, post_webhook
from .vectors import VectorIndex


def _log_path(kb, kind: str) -> str:
    return os.path.join(kb.dirs["logs"], f"{kind}s.jsonl")


def _log(kb, kind: str, rec: dict):
    """查询/取用日志（工作留痕：缺口报告与 eval 金标的原料）。带轮转。"""
    append_jsonl(_log_path(kb, kind), {"ts": now_iso(), **rec})


class NotFound(Exception):
    pass


class AmbiguousId(Exception):
    pass


class KnowledgeBase:
    def __init__(self, home: str | None = None, principal: Principal | None = None):
        self.cfg = Config(home)
        self.dirs = ensure_dirs(self.cfg.home)
        self.principal = principal or Principal()
        self.catalog = Catalog(os.path.join(self.dirs["home"], "kb.db"),
                               os.path.join(self.dirs["logs"], "audit.jsonl"),
                               principal=self.principal.name)
        self.blobs = BlobStore(self.dirs["blobs"], self.dirs["trash"])
        self.vectors = VectorIndex(self.dirs["vectors"], self.catalog)
        self.ingest = Ingester(self)
        self._embedder = None
        self._llm = None
        self._reranker = None
        self._wlock = WriteLock(self.dirs["home"])

    # ---- 写者互斥 ----
    def write_lock(self) -> WriteLock:
        """写路径互斥（DESIGN-V2 C3）：并发写会造成向量元数据竞态。

        返回本实例共享的可重入锁，嵌套写路径（reject→remove）不会自锁。
        """
        return self._wlock

    def notify(self, event: str, detail: dict) -> bool:
        """告警钩子（DESIGN-V2 D1）：失败事件 POST 到 alerts.webhook。"""
        url = self.cfg.get("alerts.webhook") or ""
        payload = {"event": event, "home": self.dirs["home"],
                   "ts": now_iso(), **detail}
        ok = post_webhook(url, payload) if url else False
        self.catalog.audit("alert", event=event, delivered=ok,
                           configured=bool(url))
        return ok

    # ---- 后端 ----
    def embedder(self):
        if self._embedder is None:
            self._embedder = make_embedder(self.cfg, self.catalog.get_meta("embedder"))
            self.vectors.ensure(self._embedder.dim)
            if self.catalog.get_meta("embedder") is None:
                self.catalog.set_meta("embedder", self._embedder.name)
        return self._embedder

    def llm(self) -> LLM | None:
        """返回摘要 LLM；配置不完整时降级为 None（抽取式摘要），不阻断摄取。"""
        if self._llm is None and self.cfg.get("summarize.provider") == "api":
            try:
                self._llm = LLM(self.cfg.get("summarize.api_base") or "",
                                self.cfg.get("summarize.api_key") or "",
                                self.cfg.get("summarize.model") or "")
            except RuntimeError as e:
                self.catalog.audit("llm_misconfigured", error=str(e))
                self._llm = False
        return self._llm or None

    def reranker(self):
        """rerank 精排器（rerank.provider=api 时启用）；配置缺失返回 None。"""
        if self._reranker is None and self.cfg.get("rerank.provider") == "api":
            try:
                from .rerank import Reranker
                self._reranker = Reranker(
                    self.cfg.get("rerank.api_base") or "",
                    self.cfg.get("rerank.api_key") or "",
                    self.cfg.get("rerank.model") or "",
                    top_n=int(self.cfg.get("rerank.top_n") or 50))
            except RuntimeError as e:
                self.catalog.audit("rerank_misconfigured", error=str(e))
                self._reranker = False
        return self._reranker or None

    # ---- 增查 ----
    def add(self, path: str, tags=(), in_place: bool = False, move: bool = False,
            collection: str | None = None, review: bool = False,
            origin: str = "human", visibility: str = "internal",
            force_hash: bool = False, link: bool = False,
            chunk: bool | None = None) -> dict:
        with self.write_lock():
            return self.ingest.add(path, tags=tags, in_place=in_place, move=move,
                                   collection=collection, review=review,
                                   origin=origin, visibility=visibility,
                                   force_hash=force_hash, link=link, chunk=chunk)

    def note(self, title: str, text: str, tags=(), collection: str = "default",
             review: bool = False, origin: str = "human",
             visibility: str = "internal") -> str:
        with self.write_lock():
            return self.ingest.note(title, text, tags=tags, collection=collection,
                                    review=review, origin=origin,
                                    visibility=visibility)

    # ---- 语义缓存（L1 精确 + L2 近义，见 search.py 头注）----
    CACHE_SIM_THRESHOLD = 0.90
    CACHE_MAX_ENTRIES = 5000

    def cache_lookup_similar(self, qvec, version: int, scope: str) -> str | None:
        """L2 近义命中：遍历已缓存查询向量，cosine ≥ 阈值即返回其结果 JSON。"""
        import numpy as np
        rows = self.catalog.conn.execute(
            "SELECT qkey, qvec_blob, result_json FROM semantic_cache "
            "WHERE data_version=? AND scope=?", (version, scope)).fetchall()
        if not rows:
            return None
        q = np.asarray(qvec, dtype=np.float32)
        for r in rows:
            c = np.frombuffer(r["qvec_blob"], dtype=np.float32)
            if c.shape != q.shape:
                continue  # embedder 换过但版本戳没跟上时的防御
            sim = float(c @ q)
            if sim >= self.CACHE_SIM_THRESHOLD:
                return r["result_json"]
        return None

    def cache_store_similar(self, qvec, qkey_exact: str, result: dict,
                            version: int, scope: str):
        """存 L2 条目（向量 blob + scope + 版本）。超量时按最旧清理 10%。"""
        import numpy as np
        blob = np.asarray(qvec, dtype=np.float32).tobytes()
        self.catalog.conn.execute(
            "INSERT OR REPLACE INTO semantic_cache(qkey, qvec_blob, result_json,"
            " data_version, scope, created_at) VALUES(?,?,?,?,?,datetime('now'))",
            (qkey_exact, blob, json.dumps(result, ensure_ascii=False),
             version, scope))
        n = self.catalog.conn.execute(
            "SELECT COUNT(*) FROM semantic_cache").fetchone()[0]
        if n > self.CACHE_MAX_ENTRIES:
            self.catalog.conn.execute(
                "DELETE FROM semantic_cache WHERE qkey IN (SELECT qkey FROM "
                "semantic_cache ORDER BY created_at LIMIT ?)", (n // 10,))
        self.catalog.audit("cache_store", qkey=qkey_exact[:16], total=n)

    def cache_clear(self) -> int:
        return self.catalog.cache_clear()

    def visible_for(self, level: str | None) -> tuple | None:
        """访问级别 → 可见 visibility 集合（策略裁决统一在 policy.py）。"""
        return Principal(level=level or "viewer").visible

    def approve(self, id_or_prefix: str) -> str:
        """审核通过：pending 条目（及其块子条目）转为 active，可被检索。"""
        with self.write_lock():
            eid = self.resolve_id(id_or_prefix)
            self.catalog.set_status(eid, "active")
            with self.catalog.tx():
                self.catalog.conn.execute(
                    "UPDATE entries SET status='active' WHERE parent_id=?", (eid,))
            return eid

    def reject(self, id_or_prefix: str) -> str:
        """审核拒绝：删除条目及其块子条目（blob 进回收站）。"""
        with self.write_lock():
            eid = self.resolve_id(id_or_prefix)
            self.catalog.delete_chunks(eid)
            return self.remove(eid, purge=True)

    def resolve_id(self, pfx: str) -> str:
        row = self.catalog.get(pfx)
        if row:
            return pfx
        rows = self.catalog.by_prefix(pfx)
        if not rows:
            raise NotFound(f"找不到条目: {pfx}")
        if len(rows) > 1:
            raise AmbiguousId(f"前缀 {pfx} 命中 {len(rows)} 个条目，请用更长前缀: "
                              + ", ".join(r["id"][:12] for r in rows[:5]))
        return rows[0]["id"]

    def search(self, query: str, kind=None, tag=None, path_glob=None, limit=10,
               collection=None, include_pending=False, visible=None,
               use_cache=None) -> dict:
        res = _search(self, query, kind=kind, tag=tag, path_glob=path_glob,
                      limit=limit, collection=collection,
                      include_pending=include_pending, visible=visible,
                      use_cache=use_cache)
        try:  # 查询日志：真实流量 → 缺口报告与 eval 金标的原料
            top = res["results"][0] if res["results"] else None
            _log(self, "query", {
                "query": query, "n": len(res["results"]),
                "top_score": top["score"] if top else 0,
                "top_id": top["id"] if top else None,
                "degraded": bool(res.get("degraded")),
                "warning": res.get("warning")})
        except Exception:
            pass
        return res

    def get(self, id_or_prefix: str) -> dict | None:
        eid = self.resolve_id(id_or_prefix)
        row = self.catalog.get(eid)
        if row is None:
            return None
        hit = to_hit(row, self.catalog)
        try:
            _log(self, "get", {"id": eid, "title": hit["title"],
                               "source_path": hit.get("source_path"),
                               "is_chunk": bool(hit.get("parent_id"))})
        except Exception:
            pass
        return hit

    def record_gap(self, gap_type: str, query_or_event: str, reason: str,
                   expected_id: str | None = None) -> None:
        """上层管线记录 memory/awareness 缺口（§9.2/§9.3）。"""
        if gap_type not in ("memory", "awareness"):
            raise ValueError("gap_type 必须是 memory/awareness")
        _log(self, gap_type, {"event": query_or_event, "gap": True,
                              "reason": reason, "expected_id": expected_id})

    def gaps(self, days: int = 30, min_score: float = 0.012,
             gap_type: str = "knowledge") -> dict:
        """缺口报告（架构文档 §9 三类）：knowledge（找不到可靠资料）/
        memory（本应召回却没召回）/ awareness（重要事件晚知道）。

        min_score 必须低于 RRF 单路第一名得分上限 1/(rrf_k+1)≈0.0164。
        memory/awareness 缺口由上层管线写入 logs/{type}s.jsonl
        （格式：{query/event, reason, expected_id}），本方法只做汇总。
        """
        import time as _t
        path = _log_path(self, "query" if gap_type == "knowledge" else gap_type)
        if not os.path.exists(path):
            return {"gap_type": gap_type, "queries": 0, "gaps": []}
        cutoff = _t.time() - days * 86400
        agg: dict[str, int] = {}
        total = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts", "")
                try:
                    stamp = _t.mktime(_t.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) \
                        if ts else 0
                except ValueError:
                    stamp = 0
                if stamp and stamp < cutoff:
                    continue
                total += 1
                if gap_type == "knowledge":
                    hit = (not rec.get("n")
                           or (rec.get("top_score") or 0) < min_score)
                else:
                    hit = rec.get("gap") is True  # memory/awareness 日志显式标记
                if hit:
                    q = (rec.get("query") or rec.get("event") or "").strip()
                    if q:
                        agg[q] = agg.get(q, 0) + 1
        gaps = sorted(agg.items(), key=lambda kv: -kv[1])
        return {"gap_type": gap_type, "queries": total,
                "gaps": [{"query": q, "count": c} for q, c in gaps[:50]]}

    def rechunk(self, collection: str | None = None, chunk: bool = True) -> dict:
        """对既有文本类条目（重）建块。"""
        with self.write_lock():
            done = total_chunks = 0
            for row in self.catalog.iter_all():
                if row.get("parent_id"):
                    continue
                if collection and row["collection"] != collection:
                    continue
                if row["kind"] not in ("text", "code", "pdf", "note"):
                    continue
                n = self.ingest.sync_chunks_for(row, chunk_flag=chunk)
                done += 1
                total_chunks += max(n, 0)
                self.catalog.audit("rechunk", id=row["id"], chunks=n)
            return {"parents": done, "chunks": total_chunks}

    def entries(self, kind=None, tag=None, limit=50, offset=0,
                collection=None, include_pending=False, origin=None,
                status=None, visible=None, include_chunks=False) -> list[dict]:
        return [to_hit(r, self.catalog)
                for r in self.catalog.entries_page(
                    kind, tag, limit, offset, collection=collection,
                    include_pending=include_pending, origin=origin, status=status,
                    visible=visible, include_chunks=include_chunks)]

    def set_visibility(self, id_or_prefix: str, visibility: str) -> str:
        """调整可见级别，并级联到块子条目。

        块携带父文档正文，若不级联，把父设为 private 后仍可经块读到内容（越权泄漏）。
        """
        eid = self.resolve_id(id_or_prefix)
        self.catalog.update_entry(eid, {"visibility": visibility})
        n = self.catalog.set_children_visibility(eid, visibility)
        self.catalog.audit("visibility", id=eid, visibility=visibility, chunks=n)
        return eid

    def add_tag(self, id_or_prefix: str, tags: list[str]) -> str:
        eid = self.resolve_id(id_or_prefix)
        self.catalog.set_tags(eid, tags)
        return eid

    def remove_tag(self, id_or_prefix: str, tags: list[str]) -> str:
        eid = self.resolve_id(id_or_prefix)
        for t in tags:
            self.catalog.remove_tag(eid, t)
        return eid

    def remove(self, id_or_prefix: str, purge: bool = False) -> str:
        with self.write_lock():
            eid = self.resolve_id(id_or_prefix)
            row = self.catalog.get(eid)
            if row and not row.get("parent_id"):
                self.catalog.delete_chunks(eid)  # 父条目删除时级联清块
            self.catalog.delete_entry(eid)
            if purge and row and row["blob"]:
                others = [r for r in self.catalog.find_by_hash(row["content_hash"])
                          if r["blob"] == row["blob"]]
                if not others:
                    self.blobs.trash(row["blob"])
                    self.catalog.audit("blob_trash", hash=row["blob"])
            return eid

    # ---- 维护 ----
    def stats(self) -> dict:
        kinds = self.catalog.count_by_kind_parents()  # 块不计入资料条目数
        blob_count = blob_size = 0
        for digest in self.blobs.iter_blobs():
            blob_count += 1
            try:
                blob_size += os.path.getsize(self.blobs.path(digest))
            except OSError:
                pass
        db_size = os.path.getsize(self.catalog.db_path) if os.path.exists(
            self.catalog.db_path) else 0
        vec_file_size = 0
        for root, _, files in os.walk(self.dirs["vectors"]):
            for f in files:
                vec_file_size += os.stat(os.path.join(root, f)).st_blocks * 512
        return {
            "home": self.dirs["home"],
            "schema_version": self.catalog.get_meta("schema_version"),
            "embedder": self.catalog.get_meta("embedder"),
            "entries_total": sum(v["count"] for v in kinds.values()),
            "chunks": self.catalog.count_chunks(),
            "rows_total": sum(v["count"] for v in
                              self.catalog.count_by_kind().values()),
            "pending": self.catalog.count_status().get("pending", 0),
            "collections": self.catalog.all_collections(),
            "by_kind": kinds,
            "blobs": {"count": blob_count, "size": human_size(blob_size)},
            "db_size": human_size(db_size),
            "vectors": {**self.vectors.stats(), "size": human_size(vec_file_size)},
            "tags": self.catalog.all_tags_usage(),
        }

    def verify(self, limit: int | None = None) -> dict:
        digests = list(self.blobs.iter_blobs())
        if limit:
            digests = digests[:limit]
        bad, missing = [], []
        for i, d in enumerate(digests):
            st = self.blobs.verify(d)
            if st == "corrupt":
                bad.append(d)
            elif st == "missing":
                missing.append(d)
        return {"checked": len(digests), "corrupt": bad, "missing": missing}

    def doctor(self) -> dict:
        cat = self.catalog
        rep: dict = {}
        rep["quick_check"] = cat.quick_check()
        rep["fts_integrity"] = cat.fts_integrity()
        n_entries = next(cat.iter_all("COUNT(*) c"))["c"]
        rep["entries"] = n_entries
        rep["vec_rows"] = cat.count_vec()
        missing_blob = [r["id"] for r in cat.iter_all()
                        if r["blob"] and not self.blobs.exists(r["blob"])]
        rep["entries_missing_blob"] = missing_blob[:20]
        rep["entries_missing_blob_count"] = len(missing_blob)
        miss_src = [r["id"] for r in cat.iter_all()
                    if r["in_place"] and r["source_path"] and not os.path.exists(r["source_path"])]
        rep["in_place_missing_source"] = miss_src[:20]
        rep["in_place_missing_source_count"] = len(miss_src)
        emb_name = cat.get_meta("embedder")
        try:
            emb = make_embedder(self.cfg, emb_name)
            rep["embedder"] = emb.name
            rep["vec_dim_ok"] = (cat.get_meta("vec_dim") in (None, str(emb.dim)))
        except RuntimeError as e:
            rep["embedder"] = f"配置不一致: {e}"
            rep["vec_dim_ok"] = False
        if self.cfg.get("summarize.provider") == "api" and self.llm() is None:
            rep["summarize"] = "api 配置不完整，已降级为抽取式摘要"
        else:
            rep["summarize"] = self.cfg.get("summarize.provider")
        rep["pending"] = self.catalog.count_status().get("pending", 0)
        # §22.6 Secret 不进普通信息层：config 中残留明文凭据即告警
        plaintext = []
        for section, key in (("summarize", "api_key"), ("embed", "api_key"),
                             ("rerank", "api_key")):
            v = self.cfg.data.get(section, {}).get(key, "")
            if v and not (isinstance(v, str) and v.startswith("env:")):
                plaintext.append(f"{section}.{key}")
        rep["plaintext_secrets"] = plaintext
        trash_n = sum(len(files) for _, _, files in os.walk(self.dirs["trash"]))
        rep["trash_files"] = trash_n
        return rep

    def gc(self, commit: bool = False, empty_trash: bool = False,
           older_than: int | None = None) -> dict:
        """回收未引用 blob；empty_trash 清回收站，older_than=N 天只清更早的（老化）。"""
        referenced = {r[0] for r in self.catalog.conn.execute(
            "SELECT DISTINCT blob FROM entries WHERE blob != ''")}
        orphans = [d for d in self.blobs.iter_blobs() if d not in referenced]
        trashed = []
        if commit:
            for d in orphans:
                name = self.blobs.trash(d)
                if name:
                    trashed.append(name)
            self.catalog.audit("gc", trashed=len(trashed))
        removed_trash = 0
        if empty_trash:
            import time as _t
            cutoff = _t.time() - older_than * 86400 if older_than else None
            for root, _, files in os.walk(self.dirs["trash"]):
                for f in files:
                    fp = os.path.join(root, f)
                    if cutoff is not None and os.path.getmtime(fp) > cutoff:
                        continue  # 太新，留给下次老化
                    os.unlink(fp)
                    removed_trash += 1
        return {"orphan_blobs": orphans, "trashed": len(trashed),
                "trash_files_removed": removed_trash}

    def reindex_fts(self) -> dict:
        self.catalog.conn.execute("INSERT INTO entries_fts(entries_fts) VALUES('rebuild')")
        n = next(self.catalog.iter_all("COUNT(*) c"))["c"]
        self.catalog.audit("reindex_fts")
        return {"rebuilt": n}

    def compact_vectors(self) -> int:
        with self.write_lock():
            n = self.vectors.compact()
            self.catalog.audit("compact_vectors", rows=n)
            return n

    def reembed(self) -> int:
        with self.write_lock():
            emb = make_embedder(self.cfg)  # 不校验旧 meta，允许换后端
            self.vectors.reset(emb.dim)
            self.catalog.set_meta("embedder", emb.name)
            rows = list(self.catalog.iter_all())
            done = 0
            for r in rows:
                text = f"{r['title']}\n{r['summary']}\n{r['keywords'].replace(',', ' ')}"
                try:
                    vec = emb.embed([text])[0]
                    shard, row = self.vectors.add(vec)
                    self.catalog.remove_vecs(r["id"])
                    self.catalog.add_vec(r["id"], shard, row)
                except Exception:
                    self.catalog.audit("vec_failed", id=r["id"])
                done += 1
                if done % 200 == 0:
                    self.catalog.audit("reembed_progress", done=done, total=len(rows))
            self.catalog.audit("reembed", total=done)
            return done

    def backfill_vecs(self, batch_size: int = 32, limit: int | None = None) -> dict:
        """增量补齐缺失向量（不动已有向量，区别于 reembed 的全量重建）。

        场景：大规模摄取期间 API 抖动/超时导致部分条目没有向量
        （真实发生过：38.6 万条目欠 26.9 万行），只补欠账。
        """
        from .embed import make_embedder
        with self.write_lock():
            emb = make_embedder(self.cfg, self.catalog.get_meta("embedder"))
            rows = self.catalog.conn.execute("""
                SELECT e.id, e.title, e.summary, e.keywords FROM entries e
                LEFT JOIN vec_rows v ON v.entry_id = e.id
                WHERE v.entry_id IS NULL ORDER BY e.created_at
                """ + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
            total = len(rows)
            done = failed = 0
            for i in range(0, total, batch_size):
                batch = rows[i:i + batch_size]
                texts = [f"{r['title']}\n{r['summary']}\n"
                         f"{r['keywords'].replace(',', ' ')}" for r in batch]
                try:
                    vecs = emb.embed(texts)
                    slots = self.vectors.add_many(vecs)
                    for r, (shard, row) in zip(batch, slots):
                        self.catalog.add_vec(r["id"], shard, row)  # add_vec 自带 tx
                    self.catalog.bump_data_version()
                    done += len(batch)
                except Exception as batch_err:
                    self.catalog.audit("vec_batch_fallback",
                                       reason=str(batch_err)[:200],
                                       count=len(batch))
                    for r, text in zip(batch, texts):  # 批失败逐条兜底
                        try:
                            vec = emb.embed([text])[0]
                            shard, row = self.vectors.add(vec)
                            self.catalog.add_vec(r["id"], shard, row)
                            done += 1
                        except Exception as e:
                            failed += 1
                            self.catalog.audit("vec_failed", id=r["id"], error=str(e))
                if (i // batch_size) % 10 == 0:
                    self.catalog.audit("backfill_progress", done=done,
                                       failed=failed, total=total)
            self.catalog.audit("backfill_vecs", done=done, failed=failed, total=total)
            return {"done": done, "failed": failed, "total": total}

    def backup(self, dest: str | None = None, with_blobs: bool = False,
               retention: int | None = None, verify: bool = False) -> dict:
        from .admin import backup as _backup
        return _backup(self, dest=dest, with_blobs=with_blobs,
                       retention=retention, verify=verify)

    def restore(self, src_dir: str, force: bool = False) -> dict:
        from .admin import restore as _restore
        with self.write_lock():
            return _restore(self, src_dir, force=force)

    def export_entries(self, out_path: str, collection: str | None = None) -> dict:
        from .admin import export_entries
        return export_entries(self, out_path, collection=collection)

    def import_entries(self, in_path: str, reembed: bool = True) -> dict:
        from .admin import import_entries
        with self.write_lock():
            return import_entries(self, in_path, reembed=reembed)

    def curate(self, ai: bool = False, apply: bool = False,
               limit: int | None = None, collection: str | None = None,
               force: bool = False) -> dict:
        from .curate import curate as _curate
        with self.write_lock():
            return _curate(self, ai=ai, apply=apply, limit=limit,
                           collection=collection, force=force)

    def evaluate(self, queries_path: str, k: int = 5) -> dict:
        from .eval import evaluate
        return evaluate(self, queries_path, k=k)

    def goldens_from_log(self, out_path: str) -> dict:
        from .eval import goldens_from_log
        return goldens_from_log(self, out_path)

    def drill(self) -> dict:
        """灾备演练（DESIGN-V2 D3）：在临时库上跑完整"备份→破坏→恢复→校验"链路。

        不触碰生产库；失败时触发告警。可挂 timer 定期自证备份链路有效。
        """
        import shutil
        import tempfile
        from .admin import backup as _backup, restore as _restore

        tmp = tempfile.mkdtemp(prefix="kb_drill_")
        steps, ok = [], True
        try:
            drill_kb = KnowledgeBase(tmp)
            note_id = drill_kb.note("演练样本", "灾备演练：备份恢复链路自检。")
            steps.append({"step": "seed", "ok": bool(drill_kb.get(note_id))})
            bk = _backup(drill_kb, verify=True)
            steps.append({"step": "backup", "ok": bool(bk.get("verified", {}).get(
                "quick_check")), "dest": bk["dest"]})
            drill_kb.remove(note_id, purge=True)
            gone = True
            try:
                gone = drill_kb.get(note_id) is None
            except NotFound:
                gone = True
            steps.append({"step": "destroy", "ok": gone})
            _restore(drill_kb, bk["dest"], force=True)
            drill_kb.close()
            drill_kb = KnowledgeBase(tmp)
            restored = drill_kb.catalog.get(note_id) is not None
            steps.append({"step": "restore", "ok": restored})
            ver = drill_kb.verify()
            steps.append({"step": "verify", "ok": not ver["corrupt"] and
                          not ver["missing"], "checked": ver["checked"]})
            drill_kb.close()
            ok = all(s["ok"] for s in steps)
        except Exception as e:
            steps.append({"step": "exception", "ok": False, "error": str(e)})
            ok = False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.catalog.audit("drill", ok=ok, steps=len(steps))
        if not ok:
            self.notify("drill_failed", {"steps": steps})
        return {"ok": ok, "steps": steps}

    def close(self):
        self.catalog.close()
