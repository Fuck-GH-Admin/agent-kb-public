"""Rerank 后端（OpenAI 兼容 /rerank，如硅基流动 bge-reranker-v2-m3）。

三路检索的精排层：BM25 + 向量 RRF 融合的 top-N 候选，用 cross-encoder 逐对
打分重排。失败自动降级回 RRF 排序（degraded=True），绝不阻断检索。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class Reranker:
    RETRY_STATUS = (429, 500, 502, 503, 504)

    def __init__(self, api_base: str, api_key: str, model: str,
                 top_n: int = 50, timeout: int = 30):
        if not api_base or not model:
            raise RuntimeError("rerank 配置需要 rerank.api_base 与 rerank.model")
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.top_n = top_n
        self.timeout = timeout

    def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        """candidates: 至少含 text（给模型的文本），保留原字段。返回按相关性重排的前缀。

        文本截断 4000 字符（reranker 上限内），score 以 relevance_score 归一化。
        """
        if not candidates:
            return []
        docs = [(c.get("text") or "")[:4000] for c in candidates[: self.top_n]]
        body = {"model": self.model, "query": query[:2000], "documents": docs,
                "top_n": len(docs)}
        req = urllib.request.Request(
            self.api_base + "/rerank",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode())
                break
            except urllib.error.HTTPError as e:
                if e.code in self.RETRY_STATUS:
                    last = e
                else:
                    raise RuntimeError(f"rerank API {e.code}: {e.reason}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
        else:
            raise RuntimeError(f"rerank API 重试耗尽: {last}")

        results = data.get("results") or []
        out = []
        for r in results:
            idx = r.get("index")
            if idx is None or idx >= len(candidates):
                continue
            hit = dict(candidates[idx])
            hit["rerank_score"] = float(r.get("relevance_score") or 0.0)
            out.append(hit)
        return out

    def rerank_hits(self, query: str, hits: list[dict]) -> list[dict]:
        """把 search 结果转为 rerank 输入（title+summary+snippet 组合文本）。
        失败抛异常由调用方降级。"""
        cand = []
        for h in hits:
            text = " | ".join(filter(None, (
                h.get("title"), h.get("summary"),
                (h.get("snippet") or "")[:600], (h.get("preview") or "")[:1500])))
            cand.append({**h, "text": text})
        scored = self.rerank(query, cand)
        # 候选超出 top_n 的尾部落回原顺序（保持可预期）
        reranked_ids = {h["id"] for h in scored}
        tail = [h for h in hits if h["id"] not in reranked_ids]
        return scored + tail
