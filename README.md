# Information Substrate

长期 BOT 系统的信息基础设施子系统：负责**证据、资料、来源、索引、检索、保留和恢复**。
Knowledge、Memory、Attention 与 Subject Runtime 在它之上形成主体语义；它自己不解释"主体是谁"。

> 由 agent-kb 演化而来（v0.6，38.6 万真实语料验证）。substrate 扩展的数据结构与
> Capability 设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §2.2/§22，
> 演进推演见 [docs/DESIGN-V2.md](docs/DESIGN-V2.md)；旧版使用文档保留在
> [docs/README-agent-kb.md](docs/README-agent-kb.md)。

## 设计定位（与旧 agent-kb 的本质区别）

| 旧 agent-kb | Information Substrate |
|---|---|
| 独立知识库产品，自带用户权限体系 | BOT 系统的共享底座，权限来自 Kernel 注入的 Capability |
| collection 同时表达主题/项目/权限/密级 | domain(→collection 分区) / project(→审计记录) / topic_tags(→tags) / visibility+派生收紧(代 disclosure_scope) / retention_class |
| origin=human/agent 二元 | 具体 Principal/Producer（human:user_A / worker:W103 / external:web / system:kernel…） |
| 所有 agent 写入默认人工审批 | 按数据类型分治理：Raw Evidence 自动收，Knowledge Claim 记录认识论状态 |
| ingestion_status 混同"可信" | 接纳（accepted）与可信（epistemic_status）分离 |
| `<3GB` 全局红线 / 零常驻教条 | 模块级资源预算，非必要不常驻 |

不变的骨架（被 38.6 万语料验证）：SHA-256 内容寻址不可变 blob、SQLite(WAL) 目录、
FTS5+向量 RRF 混合检索、三级降级链、审计、verify/doctor/backup/restore/drill。

## 快速开始

```bash
cd ~/Bot/kb-workspace/kb
export KB_HOME=~/.kb

./bin/kb init
./bin/kb add ~/docs --tags 资料            # 摄取 = Evidence Capture（自动接纳）
./bin/kb search "注意力机制" --limit 5
./bin/kb claim <id> --status corroborated   # 声明认识论状态（Knowledge 层）
./bin/kb stats && ./bin/kb doctor
```

## Capability API（供上层 BOT 组件调用）

上层组件不直接访问 SQLite，经 `substrate.py` 的稳定接口：

```python
from kb.substrate import Substrate
from kb.policy import Principal, local_principal
# 本机脚本直接用完整能力；Kernel 注入能力集的目标态见 ARCHITECTURE §22.1：
#   principal = Principal(level="operator", name="subject_runtime",
#                         capabilities=frozenset({"substrate.reflect", "substrate.write"}))
sub = Substrate(home="~/.kb", principal=local_principal("subject_runtime"))

# Artifact（原始证据层）
ref = sub.artifact.put_bytes(b"...", media="text/plain")
hit = sub.artifact.get(ref)

# Knowledge（编目与命题层）
sub.knowledge.ingest("/path/to/dir", domain="ml", project="kb")
res = sub.knowledge.search("注意力机制", limit=5)          # 两阶段检索：先摘要后原文
sub.knowledge.assert_claim(entry_id, authority="derived", confidence=0.8,
                           epistemic_status="corroborated")
sub.knowledge.retire(entry_id, reason="superseded")

# Memory Evidence（事件/经历证据层——真正的 Memory 判定在上层）
sub.memory.evidence_append(event="用户表达了偏好", evidence_ref=ref,
                           principal="human:user_A")

# Provenance / Retention / 运维
sub.provenance.trace(entry_id)                             # 派生链回溯到原始证据
sub.retention.classify(entry_id, retention_class="IMPORTANT")
sub.doctor_run(); sub.backup_create()   # 运维能力：flat 方法（backup 需 CAP_ADMIN）
```

## CLI（维护用途为主）

```bash
./bin/kb --help            # 完整命令表（摄取/检索/审核/备份/迁移/体检/…）
./bin/kb serve-mcp         # agent MCP 通道（stdio）
./bin/kb serve-web         # 人类操作面板（token 鉴权）
```

## 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) —— 数据结构、检索算法、降级矩阵、安全模型、运维
- [docs/ROADMAP.md](docs/ROADMAP.md) —— 功能取舍与批次记录
- [docs/HANDOFF-2026-09-04.md](docs/HANDOFF-2026-09-04.md) —— 交接记录
- 测试：`tests/unit/test_unit.py`（27）+ `tests/e2e_test.py`（144）
