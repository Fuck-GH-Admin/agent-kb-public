"""检索质量评估：金标集跑 recall@k / MRR，量化"检索准确性"而不是凭感觉。

金标文件为 JSONL，每行：
  {"query": "注意力机制", "relevant": ["ml_notes"]}
relevant 为原文路径或标题应包含的子串（一条或多条）。
"""
from __future__ import annotations

import json


def evaluate(kb, queries_path: str, k: int = 5,
             visible: tuple | None = None) -> dict:
    queries = []
    with open(queries_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                queries.append(json.loads(line))
    results, recalls, mrrs = [], [], []
    for q in queries:
        res = kb.search(q["query"], limit=k, visible=visible)
        matched, first_rank = set(), None
        for rank, hit in enumerate(res["results"], start=1):
            blob = " ".join(filter(None, [
                hit.get("source_path") or "", hit.get("title") or "", hit["id"]]))
            for rel in q["relevant"]:
                if rel in blob and rel not in matched:
                    matched.add(rel)
                    if first_rank is None:
                        first_rank = rank
        relevant = len(q["relevant"])
        recall = len(matched) / relevant if relevant else 0.0
        mrr = 1.0 / first_rank if first_rank else 0.0
        recalls.append(recall)
        mrrs.append(mrr)
        results.append({"query": q["query"], "recall@k": round(recall, 3),
                        "mrr": round(mrr, 3), "k": k})
    n = len(recalls) or 1
    return {"queries": len(queries), "k": k,
            "recall@k_avg": round(sum(recalls) / n, 3),
            "mrr_avg": round(sum(mrrs) / n, 3),
            "detail": results}


def goldens_from_log(kb, out_path: str, max_pairs: int = 500) -> dict:
    """从查询/取用日志自动沉淀金标：检索后 5 分钟内 kb_get 过的条目视为相关。
    这是真实流量生成的金标，比手写金标更贴近实际查询分布。"""
    import os
    import time as _t

    def load(kind):
        path = os.path.join(kb.dirs["logs"], f"{kind}s.jsonl")
        if not os.path.exists(path):
            return []
        out = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def stamp(ts):
        try:
            return _t.mktime(_t.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, TypeError):
            return 0

    queries = load("query")
    gets = load("get")
    by_id = {g["id"]: g for g in gets if g.get("id")}
    pairs, seen = [], set()
    for q in reversed(queries):  # 新日志优先
        if not q.get("query") or not q.get("top_id"):
            continue
        g = by_id.get(q["top_id"])
        if not g or not stamp(g.get("ts", "")) or not stamp(q.get("ts", "")):
            continue
        if not (0 <= stamp(g["ts"]) - stamp(q["ts"]) <= 300):
            continue  # get 必须发生在 search 后 5 分钟内
        target = g.get("source_path") or g.get("title") or q["top_id"]
        key = (q["query"], target)
        if key in seen:
            continue
        seen.add(key)
        pairs.append({"query": q["query"], "relevant": [target]})
        if len(pairs) >= max_pairs:
            break
    with open(out_path, "w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    return {"out": out_path, "goldens": len(pairs)}
