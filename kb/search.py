"""混合检索：FTS5 BM25 关键词路 + 向量语义路，RRF 融合。

过滤（kind/collection/tag/path/status/visibility）统一由 policy 层生成，
并**下推到向量候选集**，避免后过滤造成的召回黑洞。
"""
from __future__ import annotations

import json

from .policy import row_pass, sql_scope
from .util import fts_query, is_cjk, make_snippet


def to_hit(row: dict, catalog) -> dict:
    meta = {}
    try:
        meta = json.loads(row.get("meta_json") or "{}")
    except json.JSONDecodeError:
        pass
    blob = row.get("blob") or ""
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "summary": row["summary"],
        "keywords": row["keywords"],
        "source_path": row.get("source_path"),
        "blob": blob,
        "blob_path": row.get("source_path") if row.get("in_place") else None,
        "in_place": bool(row.get("in_place")),
        "collection": row.get("collection", "default"),
        "status": row.get("status", "active"),
        "origin": row.get("origin", "human"),
        "visibility": row.get("visibility", "internal"),
        "size": row["size"],
        "ext": row["ext"],
        "meta": meta,
        "tags": catalog.tags_of(row["id"]),
        "version": row["version"],
        "parent_id": row.get("parent_id"),
        "chunk_no": row.get("chunk_no") or 0,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "preview": row.get("preview", ""),
        "snippet": row.get("snippet"),
        "score": None,
        "fts_rank": None,
        "vec_rank": None,
        "vec_sim": None,
    }


def _filters(kind, tag, path_glob, collection, include_pending, visible):
    """组装 where 条件：policy 负责状态/可见性，此处只加检索维度过滤。"""
    where, params = sql_scope(visible, include_pending=include_pending,
                              include_chunks=True)
    if kind:
        where.append("e.kind=?")
        params.append(kind)
    if tag:
        where.append("EXISTS(SELECT 1 FROM tags t WHERE t.entry_id=e.id AND t.tag=?)")
        params.append(tag)
    if path_glob:
        where.append("e.source_path GLOB ?")
        params.append(path_glob)
    if collection:
        where.append("e.collection=?")
        params.append(collection)
    return where, params


def _like_terms(query: str) -> list[str]:
    """第三级降级用：从查询抽出粗粒度词项（拉丁词 + CJK 连串）。"""
    terms, run, word = [], [], []
    for ch in query.lower():
        if is_cjk(ch):
            run.append(ch)
            if word:
                terms.append("".join(word))
                word = []
        elif ch.isalnum():
            word.append(ch)
            if run:
                terms.append("".join(run))
                run = []
        else:
            if word:
                terms.append("".join(word))
                word = []
            if run:
                terms.append("".join(run))
                run = []
    terms.extend(x for x in ("".join(word), "".join(run)) if x)
    return [t for t in terms if len(t) >= 2][:8]


def search(kb, query: str, kind: str | None = None, tag: str | None = None,
           path_glob: str | None = None, limit: int = 10,
           collection: str | None = None, include_pending: bool = False,
           visible: tuple | None = None, use_cache: bool | None = None) -> dict:
    cfg = kb.cfg
    fts_cand = int(cfg.get("search.fts_candidates") or 200)
    vec_cand = int(cfg.get("search.vec_candidates") or 200)
    rrf_k = int(cfg.get("search.rrf_k") or 60)

    # ---- 语义缓存（L1 精确 + L2 近义）----
    # 键 = 归一化查询 + 检索参数指纹；命中条件 = 数据版本未变。
    # L2 用查询向量 cosine ≥ cache.sim_threshold（默认0.90，只捕获措辞微调，
    # 不做激进近义——实测同义变体 sim 0.75-0.85，激进阈值会跨意图误命中）。
    import hashlib
    cache_on = cfg.get("search.cache.enabled")
    cache_on = bool(cache_on) if cache_on is not None else True
    if use_cache is not None:
        cache_on = use_cache
    scope = repr((kind, tag, path_glob, collection, include_pending, visible, limit))
    # 归一化键：去空白+去标点 → "红烧肉！"与"红烧肉"同键（短查询加标点在
    # bge-m3 下 sim 会跌到 0.71，靠嵌入阈值兜不住，必须在文本层先归一）
    norm = "".join(ch for ch in query.strip().lower() if ch.isalnum() or _is_cjk(ch))
    qkey_exact = "e:" + hashlib.sha256((norm + "|" + scope).encode()).hexdigest()[:32]
    version = kb.catalog.data_version()
    if cache_on:
        cached = kb.catalog.cache_get(qkey_exact, version)
        if cached is None and query.strip():
            try:
                qvec = kb.embedder().embed([query])[0]
                sim_hit = kb.cache_lookup_similar(qvec, version, scope)
                if sim_hit is not None:
                    cached = sim_hit
            except RuntimeError:
                pass  # 嵌入不可用 → 只走精确键
        if cached is not None:
            res = json.loads(cached)
            res["cache"] = "hit"
            return res

    warning = None
    degraded = False
    fts_rows, vec_hits = [], []
    qvec = None  # 惰性：仅当 L2 需要或向量路需要时才嵌入

    if not (query and query.strip()):
        rows = kb.catalog.entries_page(kind, tag, limit, 0, collection=collection,
                                       include_pending=include_pending,
                                       visible=visible)
        return {"query": query, "warning": None, "degraded": False,
                "results": [to_hit(r, kb.catalog) for r in rows]}

    where, params = _filters(kind, tag, path_glob, collection,
                             include_pending, visible)
    extra = "".join(" AND " + w for w in where)

    # 关键词路：FTS5 → 失败降级 LIKE 扫描
    match = fts_query(query)
    if match:
        try:
            fts_rows = kb.catalog.search_fts(match, extra, params, fts_cand)
        except Exception as e:
            warning = f"FTS 不可用（{e}），已降级为 LIKE 扫描"
            degraded = True
            fts_rows = kb.catalog.search_like(_like_terms(query), extra, params,
                                              fts_cand)
    else:
        fts_rows = kb.catalog.search_like(_like_terms(query), extra, params, fts_cand)

    # 语义路：过滤下推到候选行集，再算相似度（qvec 已在缓存探测时嵌入则复用）
    try:
        if qvec is None:
            qvec = kb.embedder().embed([query])[0]
        cand_rows = kb.catalog.scoped_vec_rows(where, params)
        vec_hits = kb.vectors.search(qvec, vec_cand, pairs=cand_rows)
    except RuntimeError as e:
        warning = (warning + "；" if warning else "") + str(e) + "（本次仅关键词检索）"

    scores: dict[str, dict] = {}
    entries: dict[str, dict] = {}
    for rank, row in enumerate(fts_rows, start=1):
        scores[row["id"]] = {"s": 1.0 / (rrf_k + rank), "fts_rank": rank}
        entries[row["id"]] = row
    for rank, (eid, sim) in enumerate(vec_hits, start=1):
        if eid in scores:
            scores[eid]["s"] += 1.0 / (rrf_k + rank)
            scores[eid]["vec_rank"] = rank
            scores[eid]["vec_sim"] = sim
            continue
        row = kb.catalog.get(eid)
        # 候选集已下推过滤，此处仅作 fail-safe 复检
        if row is None or not row_pass(visible, row, include_pending):
            continue
        scores[eid] = {"s": 1.0 / (rrf_k + rank), "fts_rank": None,
                       "vec_rank": rank, "vec_sim": sim}
        entries[eid] = row

    ranked = sorted(scores.items(), key=lambda kv: -kv[1]["s"])[:limit]
    # 块命中时压制其父条目（父信息已在块的 parent_id 里，避免同文档占两席）
    chunk_parents = {entries[eid].get("parent_id")
                     for eid, _ in ranked if entries[eid].get("parent_id")}
    results = []
    rerank_used = False
    # rerank 精排（三路之外的可选第四路）：池按 RRF 顺序给 top_n 候选打分
    rerank_n = int(cfg.get("rerank.top_n") or 50)
    rr = kb.reranker() if (kb.cfg.get("rerank.provider") == "api") else None
    pool = [eid for eid, _ in ranked if eid not in chunk_parents][:rerank_n]
    if rr is not None and pool:
        try:
            reranked = rr.rerank_hits(query, [
                {**to_hit(entries[eid], kb.catalog), "score": None}
                for eid in pool])
            rerank_used = True
        except Exception as e:
            warning = (warning + "；" if warning else "") + \
                f"rerank 失败已降级 RRF 排序: {e}"
            reranked = None
    else:
        reranked = None

    if reranked is not None:
        for pos, hit in enumerate(reranked, start=1):
            sc = scores.get(hit["id"], {})
            hit["score"] = round(sc.get("s", 0.0), 6)
            hit["fts_rank"] = sc.get("fts_rank")
            hit["vec_rank"] = sc.get("vec_rank")
            hit["vec_sim"] = round(sc["vec_sim"], 4) \
                if sc.get("vec_sim") is not None else None
            hit["rerank_rank"] = pos
            results.append(hit)
        results = results[:limit]
    else:
        for eid, sc in ranked:
            if eid in chunk_parents:
                continue
            row = entries[eid]
            hit = to_hit(row, kb.catalog)
            # snippet 取自原文而非 FTS 索引列，避免 CJK 逐字切分的空格观感；
            # 命中片段按查询词项逐个尝试，兼顾"查询词未原样出现"的意译场景
            snip = None
            for probe in (query, *_snip_terms(query)):
                snip = make_snippet(row.get("preview") or "", probe) \
                    or make_snippet(row.get("summary") or "", probe)
                if snip:
                    break
            hit["snippet"] = snip
            hit["score"] = round(sc["s"], 6)
            hit["fts_rank"] = sc["fts_rank"]
            hit["vec_rank"] = sc.get("vec_rank")
            hit["vec_sim"] = round(sc["vec_sim"], 4) \
                if sc.get("vec_sim") is not None else None
            results.append(hit)
    out = {"query": query, "warning": warning, "degraded": degraded,
           "rerank": rerank_used, "results": results}
    if cache_on and not warning and results:
        # 写缓存：L1 精确键 + L2 向量（供变体查询近似匹配）
        try:
            if qvec is None:
                qvec = kb.embedder().embed([query])[0]
            kb.cache_store_similar(qvec, qkey_exact, out, version, scope)
        except RuntimeError:
            pass
        kb.catalog.cache_put(qkey_exact, json.dumps(out, ensure_ascii=False),
                             version)
    return out


def _is_cjk(ch: str) -> bool:
    from .util import is_cjk
    return is_cjk(ch)


def _snip_terms(query: str) -> list[str]:
    """snippet 定位的回退词项：查询词本身没出现时，用其 3 字滑窗子串。"""
    terms, run = [], []
    for ch in query:
        if is_cjk(ch):
            run.append(ch)
        else:
            if len(run) >= 3:
                s = "".join(run)
                terms.extend(s[i:i + 3] for i in range(len(s) - 2))
            run = []
    if len(run) >= 3:
        s = "".join(run)
        terms.extend(s[i:i + 3] for i in range(len(s) - 2))
    return terms[:8]