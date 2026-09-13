"""Information Substrate Capability API（架构文档 §22）。

上层 BOT 组件（Knowledge/Memory/Subject Runtime/Task）不直接访问 SQLite，
经本门面的稳定 Capability 使用底座。权限由注入的 Principal 决定：
  - 本机 CLI/脚本：Substrate(principal=policy.local_principal())
  - Kernel 注入：Substrate(principal=Principal(level=..., capabilities=...))

Capability 清单：
  artifact.put_bytes / artifact.get / artifact.inspect
  knowledge.ingest / knowledge.search / knowledge.get
  knowledge.assert_claim / knowledge.claims_of / knowledge.retire
  memory.evidence_append / memory.search
  provenance.trace
  retention.classify / retention.retire
  doctor_run / backup_create / restore_verify（flat 运维方法）
"""
from __future__ import annotations

import json
import os

from .core import AmbiguousId, KnowledgeBase, NotFound
from .policy import CAP_ADMIN, CAP_APPROVE, CAP_REFLECT, CAP_WRITE, Principal


class CapabilityDenied(Exception):
    """主体缺少所需 Capability。"""


class Substrate:
    """Information Substrate 门面。所有操作走注入的 Principal 执行。"""

    def __init__(self, home: str | None = None, principal: Principal | None = None,
                 kb: KnowledgeBase | None = None):
        self._kb = kb
        self._home = home
        self._own_kb = kb is None
        self.principal = principal or Principal()  # 缺省=本机完整能力

    # ---- 生命周期 ----
    def _kb_inst(self) -> KnowledgeBase:
        if self._kb is None:
            self._kb = KnowledgeBase(self._home, principal=self.principal)
        return self._kb

    def close(self):
        if self._own_kb and self._kb is not None:
            self._kb.close()
            self._kb = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _require(self, capability: str):
        if not self.principal.can(capability):
            raise CapabilityDenied(
                f"主体 {self.principal.name} 缺少 {capability}")

    def _visible(self):
        return self.principal.visible

    def _add_prov_edge(self, child_id: str, parent_id: str,
                       relation: str = "derived_from", created_by: str = "local"):
        """provenance_edges 写入（§22.3）：派生关系一等公民化。"""
        import uuid
        from .util import now_iso
        kb = self._kb_inst()
        kb.catalog.conn.execute(
            "INSERT OR IGNORE INTO provenance_edges(edge_id,child_id,parent_id,"
            "relation,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex[:16], child_id, parent_id, relation,
             created_by, now_iso()))

    # ================= artifact =================
    class _Artifact:
        def __init__(self, outer: "Substrate"):
            self._o = outer

        def put_bytes(self, data: bytes, media: str = "application/octet-stream",
                      source_type: str = "external:api") -> str:
            """保存原始证据，返回 blob 引用（SHA-256）。Evidence 自动接纳。"""
            self._o._require(CAP_WRITE)
            kb = self._o._kb_inst()
            digest, _ = kb.blobs.put_bytes(data)
            return digest

        def get(self, ref: str) -> bytes | None:
            """按内容引用取原始证据（需要 REFLECT；private 不限——blob 层无密级）。"""
            self._o._require(CAP_REFLECT)
            kb = self._o._kb_inst()
            if not kb.blobs.exists(ref):
                return None
            with open(kb.blobs.path(ref), "rb") as f:
                return f.read()

        def inspect(self, ref: str) -> dict:
            """引用元信息（是否存在/大小/引用它的条目）。"""
            self._o._require(CAP_REFLECT)
            kb = self._o._kb_inst()
            return {
                "ref": ref,
                "exists": kb.blobs.exists(ref),
                "referenced_by": [r["id"] for r in kb.catalog.find_by_hash(ref)],
            }

    @property
    def artifact(self) -> "_Artifact":
        return self._Artifact(self)

    # ================= knowledge =================
    class _Knowledge:
        def __init__(self, outer: "Substrate"):
            self._o = outer

        def ingest(self, path: str, domain: str = "general", project: str = "default",
                   source_type: str = "external:file", **kw) -> dict:
            """Evidence Capture：摄取并自动接纳（ingestion_status=accepted）。
            epistemic_status 默认 unknown——接纳≠可信。"""
            self._o._require(CAP_WRITE)
            kb = self._o._kb_inst()
            stats = kb.add(path, collection=domain, origin=kw.pop("origin",
                           self._o.principal.name), **kw)
            kb.catalog.audit("knowledge.ingest", domain=domain, project=project,
                             source_type=source_type, **{
                                 k: v for k, v in stats.items() if k != "errors"})
            return stats

        def search(self, query: str, **kw) -> dict:
            self._o._require(CAP_REFLECT)
            return self._o._kb_inst().search(query, visible=self._o._visible(), **kw)

        def get(self, id_or_prefix: str) -> dict | None:
            self._o._require(CAP_REFLECT)
            return self._o._kb_inst().get(id_or_prefix)

        def assert_claim(self, entry_id: str, claim: str = "",
                         authority: str = "derived", confidence: float = 0.0,
                         epistemic_status: str = "asserted",
                         derived_from: str | None = None) -> str:
            """Knowledge Claim（架构文档 §22.2 独立表）：同时写 claims 表
            （可追溯的命题历史）与 entries 编目列（当前状态快照）。
            需要 APPROVE 能力（高权威晋升）。派生关系同步写 provenance_edges。"""
            import uuid
            from .util import now_iso
            self._o._require(CAP_APPROVE)
            kb = self._o._kb_inst()
            eid = kb.resolve_id(entry_id)
            fields = {"authority": authority, "confidence": float(confidence),
                      "epistemic_status": epistemic_status,
                      "created_by": self._o.principal.name}
            if derived_from:
                fields["derived_from"] = derived_from
            # 写序：先 claims（失败=无事发生），后 entries 快照（自带事务）。
            # 不做嵌套事务——Catalog.tx 不可重入（BEGIN IMMEDIATE 会撞）。
            kb.catalog.conn.execute(
                "INSERT INTO knowledge_claims(claim_id,entry_id,claim,authority,"
                "confidence,epistemic_status,created_by,derived_from,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:16], eid, claim or f"{eid} assertion",
                 authority, float(confidence), epistemic_status,
                 self._o.principal.name, derived_from, now_iso()))
            kb.catalog.update_entry(eid, fields)
            if derived_from:
                self._o._add_prov_edge(eid, derived_from,
                                       created_by=self._o.principal.name)
            kb.catalog.audit("knowledge.assert", id=eid, authority=authority,
                             confidence=confidence, epistemic_status=epistemic_status,
                             principal=self._o.principal.name)
            return eid

        def claims_of(self, entry_id: str) -> list[dict]:
            """条目的全部 claim 历史（认识论演化可追溯）。"""
            self._o._require(CAP_REFLECT)
            kb = self._o._kb_inst()
            eid = kb.resolve_id(entry_id)
            rows = kb.catalog.conn.execute(
                "SELECT * FROM knowledge_claims WHERE entry_id=? ORDER BY created_at",
                (eid,)).fetchall()
            return [dict(r) for r in rows]

        def retire(self, entry_id: str, reason: str = "") -> str:
            """逻辑退役：epistemic_status=obsolete + status 保留（可审计追溯）。"""
            self._o._require(CAP_APPROVE)
            kb = self._o._kb_inst()
            eid = kb.resolve_id(entry_id)
            kb.catalog.update_entry(eid, {"epistemic_status": "obsolete"})
            kb.catalog.audit("knowledge.retire", id=eid, reason=reason,
                             principal=self._o.principal.name)
            return eid

    @property
    def knowledge(self) -> "_Knowledge":
        return self._Knowledge(self)

    # ================= memory evidence =================
    class _Memory:
        def __init__(self, outer: "Substrate"):
            self._o = outer

        _DISCLOSURE_ORDER = {"public": 0, "internal": 1, "private": 2}

        def evidence_append(self, event: str, evidence_ref: str | None = None,
                            principal: str | None = None,
                            retention_class: str = "NORMAL") -> str:
            """Memory Pipeline 的持久证据层：追加一条事件记录。
            真正的 Memory 判定（什么成为记忆/何时想起）在上层。"""
            self._o._require(CAP_WRITE)
            kb = self._o._kb_inst()
            title = f"[event] {event[:60]}"
            eid = kb.note(title, event, collection="memory-evidence",
                          origin=self._o.principal.name)
            fields = {"kind": "event", "source_type": "memory_pipeline",
                      "source_principal": principal or self._o.principal.name,
                      "derived_from": evidence_ref,
                      "retention_class": retention_class}
            if evidence_ref:
                # §22.4 派生记录必须继承或收紧来源的 disclosure：
                # 从 derived_from 条目取 visibility，取两者更严的一档
                src = kb.catalog.get(evidence_ref) if kb.catalog.get(
                    evidence_ref) else None
                if src:
                    order = self._DISCLOSURE_ORDER
                    src_v = src.get("visibility", "internal")
                    new_v = fields.get("visibility", "internal")
                    stricter = src_v if order.get(src_v, 1) >= order.get(
                        new_v, 1) else new_v
                    fields["visibility"] = stricter
                self._o._add_prov_edge(eid, evidence_ref)
            kb.catalog.update_entry(eid, fields)
            return eid

        def search(self, query: str, **kw) -> dict:
            self._o._require(CAP_REFLECT)
            kb = self._o._kb_inst()
            return kb.search(query, collection="memory-evidence",
                             visible=self._o._visible(), **kw)

    @property
    def memory(self) -> "_Memory":
        return self._Memory(self)

    # ================= provenance =================
    class _Provenance:
        def __init__(self, outer: "Substrate"):
            self._o = outer

        def trace(self, entry_id: str) -> dict:
            """回溯派生链：derived_from 逐级上溯 + 审计时间线。"""
            self._o._require(CAP_REFLECT)
            kb = self._o._kb_inst()
            eid = kb.resolve_id(entry_id)
            chain = []
            cur, guard = eid, 0
            while cur and guard < 16:
                row = kb.catalog.get(cur)
                if row is None:
                    break
                chain.append({"id": row["id"], "kind": row["kind"],
                              "title": row["title"],
                              "source_type": row.get("source_type"),
                              "source_principal": row.get("source_principal"),
                              "created_by": row.get("created_by"),
                              "derived_from": row.get("derived_from"),
                              "created_at": row["created_at"]})
                cur = row.get("derived_from")
                guard += 1
            edges = []
            for r in kb.catalog.conn.execute(
                    "SELECT * FROM provenance_edges WHERE child_id=? OR parent_id=?",
                    (eid, eid)).fetchall():
                edges.append({"relation": r["relation"], "child": r["child_id"],
                              "parent": r["parent_id"], "created_by": r["created_by"]})
            audits = []
            with open(kb.dirs["logs"] + "/audit.jsonl", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("id") == eid:
                        audits.append({"ts": rec.get("ts"), "op": rec.get("op"),
                                       "principal": rec.get("principal")})
            return {"entry": eid, "chain": chain, "edges": edges,
                    "audit_timeline": audits[-50:]}

    @property
    def provenance(self) -> "_Provenance":
        return self._Provenance(self)

    # ================= retention =================
    class _Retention:
        def __init__(self, outer: "Substrate"):
            self._o = outer

        def classify(self, entry_id: str, retention_class: str) -> str:
            self._o._require(CAP_APPROVE)
            kb = self._o._kb_inst()
            eid = kb.resolve_id(entry_id)
            kb.catalog.update_entry(eid, {"retention_class": retention_class})
            kb.catalog.audit("retention.classify", id=eid,
                             retention_class=retention_class)
            return eid

        def retire(self, entry_id: str, reason: str = "") -> str:
            """CORE/LEGAL 不允许 retire（protected 类独立治理）。"""
            self._o._require(CAP_ADMIN)
            kb = self._o._kb_inst()
            eid = kb.resolve_id(entry_id)
            row = kb.catalog.get(eid)
            # 文档 §22.5：IMPORTANT 及以上不得走普通 retire（TEMPORARY/
            # REBUILDABLE/NORMAL 可）；IMPORTANT 降级后可 retire
            protected = ("IMPORTANT", "CORE", "LEGAL", "SYSTEM_PROTECTED")
            if row and row.get("retention_class") in protected:
                raise CapabilityDenied(
                    f"{eid} retention_class={row['retention_class']}，"
                    f"不进入普通 retire 流程")
            kb.catalog.update_entry(eid, {"status": "retired"})
            kb.catalog.audit("retention.retire", id=eid, reason=reason)
            return eid

    @property
    def retention(self) -> "_Retention":
        return self._Retention(self)

    # ================= ops =================
    def doctor_run(self) -> dict:
        return self._kb_inst().doctor()

    def backup_create(self, dest: str | None = None, **kw) -> dict:
        self._require(CAP_ADMIN)
        return self._kb_inst().backup(dest, **kw)

    def restore_verify(self, backup_dir: str) -> dict:
        """只验证备份，不执行恢复（restore 本身属 Kernel 运维流程）。"""
        import sqlite3
        db = os.path.join(backup_dir, "kb.db")
        if not os.path.exists(db):
            return {"ok": False, "reason": "缺少 kb.db"}
        conn = sqlite3.connect(db)
        ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        n = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        conn.close()
        return {"ok": ok, "entries": n}
