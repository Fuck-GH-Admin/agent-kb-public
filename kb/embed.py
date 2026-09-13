"""可插拔嵌入后端。默认 hash（离线、零依赖、确定性）；可切换 OpenAI 兼容 API 或本地模型。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request

import numpy as np


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


class HashEmbedder:
    """字符 3-gram 特征哈希（sign hashing）。离线可用、跨进程确定性；
    捕捉字面相似度（含 CJK 子串），语义泛化弱于真实 embedding，与 BM25 互补。"""

    name = "hash"
    _CAP = 8192

    def __init__(self, dim: int = 512):
        self.dim = dim

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            t = _normalize(t)[: self._CAP]
            v = out[i]
            for j in range(max(0, len(t) - 2)):
                g = t[j:j + 3].encode("utf-8")
                d = hashlib.blake2b(g, digest_size=8).digest()
                idx = int.from_bytes(d[:4], "little") % self.dim
                v[idx] += 1.0 if d[4] & 1 else -1.0
            n = float(np.linalg.norm(v))
            if n > 0:
                v /= n
        return out


class ApiEmbedder:
    """OpenAI 兼容 /embeddings 接口。

    - 输入截断 8000 字符（bge-m3 上限 8192 token，超限报错，宁短勿错）。
    - 批量 32/请求（多数供应商上限 32~64），429/5xx 指数退避重试 3 次。
    - 维度按首次返回自适应登记（bge-m3=1024），换模型时 ensure() 会拦截。
    """

    MAX_INPUT_CHARS = 8000
    RETRY_STATUS = (429, 500, 502, 503, 504)

    def __init__(self, api_base: str, api_key: str, model: str, batch: int = 32):
        if not api_base or not model:
            raise RuntimeError("embed.provider=api 需要 embed.api_base 与 embed.model")
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.batch = batch
        self._dim: int | None = None

    @property
    def name(self) -> str:
        return f"api:{self.model}"

    @property
    def dim(self) -> int:
        if self._dim is None:
            # 探测维度：单条空跑一次
            vec = self.embed(["维度探测"])[0]
            self._dim = len(vec)
        return self._dim

    @property
    def name(self) -> str:
        return f"api:{self.model}"

    def _post(self, texts: list[str]) -> list[list[float]]:
        req = urllib.request.Request(
            self.api_base + "/embeddings",
            data=json.dumps({"model": self.model,
                             "input": [t[: self.MAX_INPUT_CHARS] for t in texts]}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        items = sorted(data["data"], key=lambda x: x["index"])
        return [it["embedding"] for it in items]

    def _post_retry(self, texts: list[str]) -> list[list[float]]:
        """指数退避重试：429/5xx/网络错误各 3 次。HTTPError 带 code 需单判。"""
        import time as _t
        last = None
        for attempt in range(3):
            try:
                return self._post(texts)
            except urllib.error.HTTPError as e:
                if e.code in self.RETRY_STATUS:
                    last = e
                else:
                    raise RuntimeError(f"embedding API {e.code}: {e.reason}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
            _t.sleep(2 ** attempt)
        raise RuntimeError(f"embedding API 重试耗尽: {last}")

    def embed(self, texts: list[str]) -> np.ndarray:
        vecs: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            vecs.extend(self._post_retry(texts[i:i + self.batch]))
        arr = np.asarray(vecs, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        if self._dim is None and len(arr):
            self._dim = arr.shape[1]
        return arr / norms


class LocalEmbedder:
    """sentence-transformers 本地模型（需要已安装且模型已缓存）。"""

    _model = None

    def __init__(self, model_name: str):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "embed.provider=local 需要 pip install sentence-transformers") from e
        LocalEmbedder._model = SentenceTransformer(model_name, device="cpu")

    @property
    def name(self) -> str:
        return f"local:{self._model.name_or_path}"

    def embed(self, texts: list[str]) -> np.ndarray:
        arr = self._model.encode(texts, batch_size=8, show_progress_bar=False,
                                 normalize_embeddings=True,
                                 convert_to_numpy=True).astype(np.float32)
        return arr


def make_embedder(cfg, meta_name: str | None = None):
    """按配置构建 embedder；meta_name 用于校验与库内记录一致。"""
    provider = cfg.get("embed.provider") or "hash"
    if provider == "hash":
        emb = HashEmbedder(int(cfg.get("embed.dim") or 512))
    elif provider == "api":
        emb = ApiEmbedder(cfg.get("embed.api_base") or os.environ.get("KB_EMBED_API_BASE", ""),
                          cfg.get("embed.api_key") or os.environ.get("KB_EMBED_API_KEY", ""),
                          cfg.get("embed.model") or os.environ.get("KB_EMBED_MODEL", ""))
    elif provider == "local":
        emb = LocalEmbedder(cfg.get("embed.local_model") or "BAAI/bge-small-zh-v1.5")
    else:
        raise RuntimeError(f"未知 embed.provider: {provider}")
    if meta_name and meta_name != emb.name:
        raise RuntimeError(
            f"库内向量由 {meta_name} 生成，当前配置为 {emb.name}。"
            f"请运行 kb reembed 后再使用向量检索")
    return emb
