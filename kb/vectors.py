"""向量索引：numpy float32 分片 memmap，追加式写入，查询按块流式计算，内存占用恒定。"""
from __future__ import annotations

import os

import numpy as np

SHARD_ROWS = 65536
CHUNK = 16384


class VectorIndex:
    def __init__(self, dirpath: str, catalog):
        self.dir = dirpath
        self.catalog = catalog
        self._mm: dict[int, np.ndarray] = {}
        os.makedirs(dirpath, exist_ok=True)

    # ---- 状态 ----
    @property
    def dim(self) -> int | None:
        v = self.catalog.get_meta("vec_dim")
        return int(v) if v else None

    def _shards(self) -> int:
        return int(self.catalog.get_meta("vec_shards") or 0)

    def _next(self) -> int:
        return int(self.catalog.get_meta("vec_next") or 0)

    def _set_state(self, dim: int, shards: int, nxt: int, bump: bool = True):
        self.catalog.set_meta("vec_dim", str(dim))
        self.catalog.set_meta("vec_shards", str(shards))
        self.catalog.set_meta("vec_next", str(nxt))
        if bump:
            # 向量行指针变化 → 语义缓存失效（检索结果集可能变化）。
            # 注意：backfill 的批量路径用 add_many 在事务外统一 bump 一次，
            # 否则 3 次 meta 写/条会把 25 万条补齐拖成一天（真实踩过：3 条/s）。
            self.catalog.bump_data_version()

    def add_many(self, vecs) -> list[tuple[int, int]]:
        """批量追加：单事务 meta、单次 bump。返回 [(shard, row), ...]。"""
        out = []
        first = True
        for vec in vecs:
            vec = np.ascontiguousarray(vec, dtype=np.float32)
            self.ensure(vec.shape[0])
            shards, nxt = self._shards(), self._next()
            if shards == 0 or nxt >= SHARD_ROWS:
                shards += 1
                nxt = 0
                self._open(shards - 1, create=True)
                self._set_state(self.dim, shards, nxt, bump=first)
                first = False
            mm = self._open(shards - 1)
            mm[nxt] = vec
            out.append((shards - 1, nxt))
            nxt += 1
        self._set_state(self.dim, shards, nxt, bump=first)
        return out

    def ensure(self, dim: int):
        cur = self.dim
        if cur is None:
            self._set_state(dim, self._shards() or 0, self._next())
        elif cur != dim:
            raise RuntimeError(
                f"向量维度不一致：库中 {cur}，当前 embedder {dim}。请运行 kb reembed")

    def _shard_path(self, i: int) -> str:
        return os.path.join(self.dir, f"shard_{i:04d}.npy")

    def _open(self, i: int, create: bool = False) -> np.ndarray | None:
        if i in self._mm:
            return self._mm[i]
        p = self._shard_path(i)
        if not os.path.exists(p):
            if not create:
                return None
            np.lib.format.open_memmap(
                p, mode="w+", dtype=np.float32, shape=(SHARD_ROWS, self.dim))
        mm = np.lib.format.open_memmap(p, mode="r+")
        self._mm[i] = mm
        return mm

    # ---- 写 ----
    def add(self, vec: np.ndarray) -> tuple[int, int]:
        vec = np.ascontiguousarray(vec, dtype=np.float32)
        self.ensure(vec.shape[0])
        shards, nxt = self._shards(), self._next()
        if shards == 0 or nxt >= SHARD_ROWS:
            shards += 1
            nxt = 0
            self._open(shards - 1, create=True)
            self._set_state(self.dim, shards, nxt)
        mm = self._open(shards - 1)
        row = nxt
        mm[row] = vec
        self._set_state(self.dim, shards, nxt + 1)
        return shards - 1, row

    def reset(self, dim: int):
        self._mm.clear()
        for i in range(self._shards()):
            try:
                os.unlink(self._shard_path(i))
            except FileNotFoundError:
                pass
        self._set_state(dim, 0, 0)

    # ---- 读 ----
    def search(self, q: np.ndarray, k: int,
               pairs: list[tuple[str, int, int]] | None = None) -> list[tuple[str, float]]:
        """返回 [(entry_id, cosine)] 按相似度降序。分块流式，内存 O(CHUNK)。

        pairs 为候选行集（由 policy 过滤下推产生）；None 时退化为全库扫描。
        过滤下推避免"小分区条目挤不进全局 top-N"的召回黑洞（DESIGN-V2 C2）。
        """
        if pairs is None:
            pairs = self.catalog.all_vec_rows()
        if not pairs or self.dim is None:
            return []
        q = np.ascontiguousarray(q, dtype=np.float32)
        qn = float(np.linalg.norm(q)) or 1.0
        ids = [p[0] for p in pairs]
        sims = np.empty(len(pairs), dtype=np.float32)
        pos = 0
        while pos < len(pairs):
            chunk = pairs[pos:pos + CHUNK]
            by_shard: dict[int, list[int]] = {}
            for j, (_, sh, row) in enumerate(chunk):
                by_shard.setdefault(sh, []).append(j)
            for sh, js in by_shard.items():
                mm = self._open(sh)
                if mm is None:
                    continue
                rows = np.array([chunk[j][2] for j in js], dtype=np.int64)
                X = mm[rows]
                Xn = np.linalg.norm(X, axis=1)
                Xn[Xn == 0] = 1.0
                s = (X @ q) / (Xn * qn)
                for local, j in enumerate(js):
                    sims[pos + j] = s[local]
            pos += CHUNK
        if len(ids) > k:
            top = np.argpartition(-sims, k)[:k]
        else:
            top = np.argsort(-sims)
        return [(ids[i], float(sims[i])) for i in top]

    def compact(self) -> int:
        """把 vec_rows 指向的向量重写为连续分片，清除删除留下的空洞。返回保留条数。"""
        pairs = self.catalog.all_vec_rows()
        self._mm.clear()
        old_shards = self._shards()
        tmp_dir = self.dir + ".compact"
        os.makedirs(tmp_dir, exist_ok=True)
        dim = self.dim or 0
        out, row, shard_i, written = [], 0, 0, 0
        mm = None
        try:
            for eid, sh, r in pairs:
                if row == 0:
                    p = os.path.join(tmp_dir, f"shard_{shard_i:04d}.npy")
                    mm = np.lib.format.open_memmap(
                        p, mode="w+", dtype=np.float32, shape=(SHARD_ROWS, dim))
                src = np.lib.format.open_memmap(self._shard_path(sh), mode="r")
                mm[row] = src[r]
                del src
                out.append((eid, shard_i, row))
                row += 1
                written += 1
                if row >= SHARD_ROWS:
                    row = 0
                    shard_i += 1
        finally:
            del mm
        for i in range(old_shards):
            try:
                os.unlink(self._shard_path(i))
            except FileNotFoundError:
                pass
        for i in range(shard_i + 1):
            os.replace(os.path.join(tmp_dir, f"shard_{i:04d}.npy"), self._shard_path(i))
        os.rmdir(tmp_dir)
        self._set_state(dim, shard_i + (1 if written else 0), row)
        return written

    def stats(self) -> dict:
        return {"dim": self.dim, "shards": self._shards(), "rows": self.catalog.count_vec()}
