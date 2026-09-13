# agent-kb 架构文档

本文面向维护者，说明数据结构、分类存储模型、检索算法、数据治理、安全模型、
运维方式与兼容性。使用说明见 [README.md](../README.md)。

## 1. 架构总览

```
                     写路径（人/agent）
  文件/目录/笔记 ──► extract 多模态抽取 ──► summarize 摘要+关键词
        │                                        │
        ▼                                        ▼
  BlobStore（SHA-256 不可变 blob）        SQLite 目录库（WAL）
        │                                  ├─ entries（条目+摘要+FTS列）
        │  content_hash 关联                ├─ entries_fts（FTS5 外部内容表）
        │                                  ├─ tags / vec_rows / meta
        ▼                                  └─ audit.jsonl（审计）
  无重复存储、原子写入                       每文件一个事务，失败回滚
                                                   │
                     向量 ──► vectors.py 分片 memmap（float32，65536行/片）

                     读路径（人/agent）
  query ──┬── FTS5 BM25（列加权）──► 候选+命中片段 snippet ──┐
          └── 向量 cosine（分块流式）──► top-k ──────────────┴──► RRF 融合 ──► 结果
```

设计原则：**零常驻服务、零重依赖（仅 numpy）、原始数据永不进数据库、
一切写操作有审计、删除永不直删**。

## 1.1 代码结构与依赖

```
入口层   bin/kb ──► kb/cli/（parser.py 装配 + query/ingest/maint 三类子命令）
                        │ python -m kb.cli / pyproject console_scripts 同入口
访问面   kb/mcp_server.py（agent, stdio, 默认 operator 级别）  kb/web.py（人类面板, token）
                        │ 两者都必须经 policy.py 裁决，禁止自行拼可见性 SQL
                        ▼
门面     kb/core.py  KnowledgeBase（装配 + 委派，无业务逻辑；写锁/缓存/告警在此）
         kb/substrate.py  Substrate（上层 BOT 组件的 Capability API，见 §22）
                        │
存储层   kb/store.py  BlobStore（SHA-256 不可变 blob）+ Catalog（SQLite WAL：
         entries/semantic_cache/vec_rows/tags/meta + FTS5 外部内容表 + 触发器）
         kb/vectors.py  numpy memmap 分片（add_many 批量 / compact 压实）
管线层   kb/extract.py（多模态抽取） → kb/summarize.py（离线/LLM 双档）
         → kb/embed.py（hash/api/local） → kb/ingest.py（去重/快跳/分块/攒批嵌入）
检索层   kb/search.py（三级降级 + RRF + 语义缓存 L1/L2） ← kb/policy.py（唯一裁决点）
         ← kb/rerank.py（可选第四路精排，失败降级）
治理层   kb/curate.py（离线/AI 整理） kb/eval.py（金标评估+真实金标沉淀）
         kb/admin.py（备份/恢复/导出导入） kb/tokens.py（哈希 token） kb/util.py
```

依赖方向（箭头=import）：cli/web/mcp_server → core → {store, ingest, search,
vectors, admin, curate, eval, tokens}；substrate → {core, policy}；{ingest, search}
→ {policy, embed, summarize, rerank, util}；**禁止反向 import**（如 store 不得
import core）。运行时依赖仅 numpy + 标准库；pypdf/sentence-transformers 为可选
增强（guarded import）。

## 2. 数据结构

### 2.1 KB_HOME 目录

```
$KB_HOME/
├── kb.db / kb.db-wal / kb.db-shake  SQLite 目录库（WAL 模式）
├── config.json                      配置（chmod 600；密钥只存 env:VAR 句柄，见 §22.6）
├── blobs/aa/bb/<sha256>             不可变原始内容（两级扇出）
├── vectors/shard_NNNN.npy           向量分片（float32 [65536, dim]）
├── logs/audit.jsonl                 审计日志（每次写操作一行 JSON）
├── trash/<时间戳>_<hash16>          GC/删除的 blob 回收站
└── backups/<时间戳>/                kb backup 输出
```

### 2.2 entries 表（核心）

| 列 | 说明 |
|---|---|
| id | 16 位 hex 随机 id（支持前缀引用） |
| kind | 类型：text / code / image / audio / video / pdf / note / binary |
| title / summary / keywords | 编目信息（LLM 或抽取式生成） |
| source_path | 摄取时的绝对路径（笔记为 NULL） |
| blob / content_hash | blob 库中的 SHA-256；in_place 时 blob 为空 |
| in_place | 1 = 不复制，索引原始位置（TB 级模式） |
| size / ext / mime / meta_json | 元数据（尺寸、时长、页数、截断标记等） |
| preview | 正文前 N 字（默认 4096），可配置 |
| **collection** | **主题分区**（缺省 default），见 §3 |
| **status** | **active / pending**（审核队列），见 §6 |
| **origin** | **human / agent**（写入来源溯源），见 §8 |
| **visibility** | **public / internal / private**（权限分级），见 §15 |
| **mtime** | 源文件 mtime（秒），配合 size 实现"快跳路径"避免重复全量哈希（§17） |
| **parent_id / chunk_no** | 块子条目指向父条目与块序号（NULL = 父条目本身），见 §19 |
| version | 内容变更次数（+1/次） |
| created_at / updated_at | UTC ISO 时间 |
| title_fts / summary_fts / keywords_fts / preview_fts | FTS 专用预处理列（CJK 逐字切分） |

辅助表：`tags(entry_id, tag)`；`vec_rows(entry_id, shard, row)`；`meta(key,value)`
（schema_version / embedder / vec_dim / vec_shards / vec_next / data_version——后者
驱动语义缓存失效）。

**Information Substrate 扩展（schema v5/v6）**：

- entries 追加列：`source_type` / `source_principal` / `created_by` /
  `derived_from`（Provenance，§22.3）；`authority` / `confidence` /
  `epistemic_status`（认识论状态：asserted/corroborated/disputed/obsolete/unknown，
  接纳≠可信）；`retention_class`（保留分级，§22.5）；`valid_from` / `valid_until`
  （时效）；
- `knowledge_claims(claim_id, entry_id, claim, authority, confidence,
  epistemic_status, created_by, derived_from, …)`：认识论声明历史表，entries 列
  只是当前快照（写入路径见 §22.2）；
- `provenance_edges(edge_id, child_id, parent_id, relation, …)`：派生关系边表，
  provenance.trace 返回链+边双视图（§22.3）；
- `semantic_cache(qkey, qvec_blob, result_json, data_version, scope)`：语义缓存。

**Capability 门面**：上层组件（Knowledge/Memory/Subject Runtime）经
`kb/substrate.py` 的 `Substrate` 使用底座，五组资源命名空间（artifact/knowledge/
memory/provenance/retention）+ 三个 flat 运维方法（doctor_run/backup_create/
restore_verify），不直接访问 SQLite；权限由注入的 Principal.capabilities 决定
（CAP_REFLECT/WRITE/APPROVE/ADMIN），policy.py 只做 enforcement（§15/§22.1）。

### 2.3 FTS 与向量

- `entries_fts`：FTS5 **外部内容表**（content='entries'），unicode61 分词器，
  由 AFTER INSERT/DELETE/UPDATE 触发器同步；`rebuild`/`integrity-check` 可用。
  CJK 以"逐字+短语查询"方式处理（见 §4）。
- `vectors/shard_NNNN.npy`：仅存**摘要层向量**（title+summary+keywords），
  追加式写入，删除留洞由 `kb compact` 压实；查询按 16384 行分块流式计算，
  内存占用与库规模无关。

## 3. 分类存储模型（不同主题的数据如何落位）

条目同时具备五个正交维度，按需组合过滤：

```
kind（物类型）  text/code/image/audio/video/pdf/note/binary   ← 自动识别
collection（主题分区）  papers / cooking / agent-notes …       ← 摄取时指定，顶层隔离
tags（自由标签）  任意多值                                     ← 随时增删
status（状态）    active / pending                              ← 治理
origin（来源）    human / agent                                 ← 溯源
```

- **collection 是物理意义的分区**：不同主题、不同项目、不同密级建议分开放
  （`kb add ~/papers --collection papers`），查询时限定分区可避免跨主题噪声，
  也便于将来整区导出/删除。列表：`kb list --collection papers`。
- **tags 是逻辑标签**：同一条目可挂多个标签，跨分区聚合（如 `todo`）。
- 落库位置与分区无关：blob 按 content_hash 寻址，天然全局去重；分区只是
  目录库里的一个可索引字段，切换分区零拷贝（重复 add 指定新 collection 即可）。

## 4. 检索算法（查询时怎么做）

1. **查询预处理**（`util.fts_query`）：拉丁词保留并做前缀匹配（≥3 字符加 `*`）；
   CJK 串分两档——≤3 字转逐字相邻**短语**（查准），>3 字拆词/二元组后取
   **"相邻项 AND、组间 OR"**（文档命中查询的连续 3 字子串即可召回，
   兼顾意译查询的查全与高 IDF 噪声的抑制，见 REAL-DATA-REPORT §2）。
   兼顾中文无分词器下的查准与查全。
2. **BM25 关键词路**：FTS5 `bm25()` **列加权**（title 6 / summary 3 / keywords 2 /
   preview 1，即 BM25F 思想：标题命中比正文命中更可信），候选 200 条，
   同时用 `snippet()` 抽出命中片段。
3. **向量语义路**：查询向量与摘要层向量做 cosine（分块流式，每块 16384 行，
   峰值内存约 32MB/块），取 top-200；同一 embedder 校验（不一致则降级仅关键词路并告警）。
4. **RRF 融合**：score = Σ 1/(60 + rank)，两路各自的名次融合成最终排序；
   输出附带 fts_rank / vec_rank / vec_sim 便于调试。
5. **rerank 精排（可选第四路）**（v0.6）：`rerank.provider=api` 时（如硅基流动
   BAAI/bge-reranker-v2-m3），RRF top-N（默认 50）候选用 cross-encoder 逐对打分
   重排，结果带 rerank_rank/rerank_score；**失败自动降级 RRF 并告警**，
   绝不阻断检索。粒度文本 = title|summary|snippet|preview 拼接。
6. 过滤下推：kind / collection / tag / path GLOB / status / visibility 在 SQL 层
   完成；向量候选集在相似度计算前预过滤（`scoped_vec_rows`）。

**"永远有结果"特性（重要）**：向量路按相似度返回最近邻，因此除非过滤器把
候选全部排除，检索几乎不会返回空。判断"查没查到"不能看结果数量，应看
snippet 是否命中、vec_sim/score 是否显著——这一点已写进 kb_search 工具描述，
避免 agent 把最近邻误当命中。

**规模预期**：FTS5 百万级摘要条目毫秒级；向量路百万行单查约 1-2s（纯 CPU），
十万行内 <300ms。速度不够时的升级路径见 §5。

## 5. 更优算法的演进路线（按性价比排序）

1. **换真语义嵌入**（收益最大）：`embed.provider=api`（OpenAI 兼容）或本地
   bge-small（约 400MB 内存）。hash 嵌入是字面相似度，语义泛化有限。
2. **sqlite-vec / 量化**：vec0 支持 partition key（对应我们的 collection）、
   int8/bit 量化、辅助列。截至本文，其 ANN 分支（IVF/DiskANN）仍在开发分支、
   Python 3.14 轮子未声明支持（见 kb-research/sqlite-vec/TODO.md），且
   `enable_load_extension` 在部分系统 Python 上被禁用。**暂缓**，百万行以上再评估。
3. **cross-encoder 重排**：对 RRF 前 50 条用 bge-reranker 类模型精排，显著提升
   top-5 质量；作为可选依赖接入 search.py。
4. **MMR 多样性**：结果去冗余（λ 加权选取），适合"给我几篇不同角度的资料"场景。
5. **融合策略升级**：txtai 的 Hybrid 模块（kb-research/txtai/.../search/hybrid.py）
   在"归一化分数"时用凸组合、未归一化时用 RRF——我们的嵌入已归一化，
   可加 `search.fusion=convex` 选项做 A/B 对比；当前 RRF 对分数尺度最稳健。
6. **块级索引**：超长文档按语义分块入库（哈希去重保证存储不翻倍），
   条目级 → 段落级检索。

## 6. 数据整理与验证（存入的东西如何保证质量）

摄取即校验链：
1. 大小上限（默认 512MB/文件）；文本按 utf-8→gb18030 解码，失败兜底 replace。
2. SHA-256 流式计算 → **内容寻址去重**（同内容只存一份）；与 mem0 的两级去重
   思路一致但更强（mem0 用 MD5，我们是全库 SHA-256 寻址）。
3. 写 blob：临时文件 → fsync → 校验哈希 → 原子改名；失败不留半截文件。
4. 识别不了的格式只存元数据进索引（**隔离而非乱编**），summary 明确标注
   "未识别出可索引文本"。
5. 单文件一个事务，失败计入 errors 不拖垮整批。

治理通道：
- **审核队列**：`kb add --review` / agent 的 MCP 写入（默认 pending）→ 人工
  `kb approve <id>` / `kb reject <id>`。pending 条目对一切检索不可见。
- **幂等更新**：同路径同内容跳过；内容变化 version+1 重摘要素引。
- **退役**：`kb rm` 解除索引；`kb gc --commit` 把孤儿 blob 移入回收站；
  `kb gc --empty-trash` 才真正删除。
- **体检**：`kb verify`（全量重算哈希）、`kb doctor`（quick_check / FTS 完整性 /
  缺 blob / 缺源文件 / embedder 一致性 / pending 数）、`kb backup`。
- **迁移**：schema 版本化（meta.schema_version），打开旧库自动幂等补列
  （v1→v2 已实现并有测试），杜绝"打开即坏"。

## 7. agent 如何自由且准确地调取

- **MCP 工具面**（级别决定工具面，默认 operator=读写可见）：`kb_search`（返回
  id/标题/摘要/`snippet` 命中片段/标签/分区/分数，可选 `include_preview` 附正文
  前 400 字）、`kb_get`（按 id 看全量信息与原文路径）、`kb_list`、`kb_stats`；
  operator/admin 级别另暴露 `kb_add` / `kb_note` / `kb_approve` / `kb_reject`
  （写入默认进待审队列，见 §8）。级别与能力映射见 §22.1。
- **两段式取用**：先 `kb_search` 拿摘要+snippet 判断相关性 → `kb_get` 或直接读
  `source_path`/`blob_path` 取原文。大文件不进上下文，按需取。
- **精确定位**：snippet 是 FTS5 计算的命中上下文，agent 可直接引用定位到段落；
  id 支持前缀唯一缩写。
- **scope 不信任客户端**：collection/status 过滤在服务端 SQL 下推，agent 无法
  通过构造元数据越权（借鉴 mem0 对 #6655 的修复思路）。
- Python API 同构（见 examples/agent_usage.py），便于嵌进自有 agent 循环。

## 8. 数据纯净性：agent 个性会不会污染库

机制上四层防护：
1. **origin 溯源**：每条目记录 human/agent；`kb list --origin agent` 一键列出
   agent 写过的所有内容，可批量复核（借鉴 mem0 的 actor_id/role 溯源）。
2. **默认进审核队列**：MCP 的 kb_add/kb_note 默认 `review=true`，agent 产出
   必须经人 approve 才对检索可见——个性化、幻觉内容到不了检索层。
3. **提示词约束**：kb_note 工具描述明确要求"客观事实口吻、注明依据"。
4. **分区隔离**：建议 agent 产写入独立 collection（如 agent-notes），
   与原始资料物理分开，整区可丢弃重来。

治理之外，工作流上建议：原始资料用 `--collection 资料区`（human）；agent 的
总结/观察放 `--collection agent-notes`；需要晋升为"事实"的，approve 后挪回资料区。

## 9. 安全模型

- **无网络面**：MCP 走 stdio 由客户端拉起，不开端口，远程使用见 §10。
- **文件权限**：KB_HOME chmod 700；config.json（含 API key）chmod 600。
- **token 哈希存储**（C12，v0.5）：面板 token 只存 SHA-256（`kb token add/list/
  revoke/rotate/migrate` 管理，明文仅创建时展示一次）；历史明文条目兼容登录并
  由 `kb token migrate` 一键清除。比对用 hmac.compare_digest 防时序侧信道。
- **SQL 注入**：全部参数化查询；FTS MATCH 语句由白名单字符构造。
- **写权限**：MCP 写能力由级别映射（默认 operator 含写；写入仍默认进待审队列），
  `KB_ALLOW_WRITE=1` 仅为兼容逃生阀；远程写路径一律受白名单约束（§22.1）；
  CLI 本身就是本机使用者。
- **完整性**：SHA-256 全链（入库校验 + verify 全量复检 + 备份 manifest）。
- **边界声明**：数据在本机为明文，防"物理/磁盘泄露"请依赖磁盘加密（LUKS）与
  备份保管；KB 不做行级加密（会影响 FTS/向量检索，属于未来可选模块）。

## 10. 远程部署与稳定性

- **推荐：SSH 远程 MCP**。客户端配置（本机 Claude/ZCode/Cursor 均支持）：

```json
{ "mcpServers": { "agent-kb": {
    "command": "ssh",
    "args": ["user@server", "/opt/kb/bin/kb --home /data/kb serve-mcp"] } } }
```

  stdio 经 SSH 透传，服务端零端口、鉴权复用 SSH；这是最稳的远程形态。
- **CLI 远程**：`ssh server kb search ...` 即可，agent 也能这样用。
- **HTTP API**：暂不内置（引入常驻 Web 服务违背资源目标）；确需多机共享时，
  建议先 NFS/同步盘放 KB_HOME 单写者方案，或将来加薄 HTTP 层（roadmap）。
- **稳定性机制**：WAL + busy_timeout 支持多读单写并发；**约定只有一个写者进程**
  （人或 cron），多机写入暂不支持；备份用 `kb backup` + blobs rsync。
- **定时体检**（systemd timer 示例，防患于未然）：

```ini
# /etc/systemd/system/kb-verify.service  → /opt/kb/bin/kb --home /data/kb verify
# /etc/systemd/system/kb-verify.timer    → OnCalendar=weekly
# /etc/systemd/system/kb-backup.timer    → OnCalendar=daily（kb backup + rsync blobs）
```

## 11. 兼容性

| 项 | 要求 | 说明 |
|---|---|---|
| Python | ≥3.10（实测 3.14） | 仅标准库 + numpy |
| SQLite | ≥3.34 | FTS5 外部内容表 / trigram 无依赖；实测 3.46 |
| 操作系统 | Linux（实测）/ macOS / Windows | 纯 os.path + os.replace，无 fcntl 等 POSIX 专有调用；os.link 失败自动回退复制 |
| 文件系统 | ext4/xfs/APFS/NTFS | blob 两级扇出避免单目录海量文件；稀疏预分配在不支持时也仅浪费表观大小 |
| 旧库升级 | v1 自动迁到 v2 | 打开即迁移，幂等可重入 |

Windows 注意事项：路径 GLOB 过滤大小写敏感（SQLite GLOB 语义）；CRLF 文本已由
解码与分词兼容；其余无平台分支代码。

## 12. 资源预算（<3GB 约束）

| 场景 | 实测/估算 |
|---|---|
| CLI 单次检索（小库） | 0.2s，峰值 RSS 41.6MB |
| 检索（38.6 万条目生产库，无 rerank） | 1.05-1.16s，RSS 82-92MB |
| 检索（同上，含 rerank 网络往返） | 2.8-4.2s，RSS 740-900MB（top_n=50 候选入内存） |
| MCP 常驻服务 | 空载约 40-60MB RSS |
| hash 嵌入器 | 忽略不计（纯 CPU 哈希） |
| 本地 bge-small 嵌入（可选） | 约 400-500MB（仍远低于 3GB） |
| 百万摘要条目 | 目录库 ~5-10GB 磁盘、向量 ~2GB；检索内存不变（分块流式） |
| TB 级原始数据 | 不占内存；--in-place 零拷贝索引 |

## 13. 功能降级矩阵（每个能力至少三条路）

| 功能 | 首选 | 降级 1 | 降级 2（保底） | 切换机制 |
|---|---|---|---|---|
| 摘要 | LLM API（summarize.provider=api） | 抽取式（分句+关键词） | 元数据描述 | LLM 配置缺失/调用失败自动回退，不阻断摄取 |
| 嵌入 | OpenAI 兼容 API | 本地 sentence-transformers | hash 字符特征哈希（零依赖） | 配置切换；检索时 embedder 不匹配自动降为仅关键词路并告警 |
| 检索 | BM25 加权 + 向量 RRF | 仅 BM25 | LIKE 全表扫描（FTS 损坏时兜底） | 每次查询 try/except 逐级降级，结果标 degraded+warning |
| 原文存储 | 复制进 blob 库 | --in-place 零拷贝索引 | --move 摄取后删源 | 按空间/来源可靠性选择 |
| PDF 文本 | pypdf | pdftotext（poppler） | 仅元数据 | 按可用工具自动探测 |
| 音视频 | ffprobe 元数据 | （装 Whisper 后转写，roadmap） | 仅文件名/大小 | 自动探测 |
| 全文索引 | FTS5 正常增量 | kb reindex 重建 | 导出→重建库（kb export/import） | doctor 报告异常时人工触发 |
| 访问通道 | CLI 本机 | MCP stdio（agent） | Web 面板（token 远程）/SSH | 并存，互为备份 |

## 14. 灾备场景与数据迁移

备份体系：`kb backup [--blobs] [--retention N] [--verify]` 快照 kb.db+向量+审计+配置
（config 含 token 不进面板可见面）；`kb restore <dir> --yes` 恢复（当前库自动
另存 .pre-restore.bak）；`kb export/import` 编目 JSONL + blobs rsync 做跨机迁移；
每次 schema 迁移前自动生成 kb.db.pre-vN.bak；关闭连接时 WAL checkpoint(TRUNCATE)。

| 场景 | 后果 | 恢复路径 |
|---|---|---|
| 误删单条目 | 索引丢失 | 审计日志有记录；源文件重 add 即可（blob 仍在库内） |
| agent 批量污染 | 检索质量下降 | list --origin agent 定位 → reject/rm；极端时从备份恢复 kb.db |
| kb.db 损坏（断电/磁盘） | 目录不可用 | WAL 自动恢复 → quick_check 失败则 restore 备份 → 无备份则 blobs+源目录 re-add 重建 |
| blob 位腐（bit rot） | 单条原文损坏 | kb verify 报告损坏哈希 → 从源目录重 add（copy 模式）或备份恢复该 blob |
| 误删 KB_HOME | 全部丢失 | restore 备份；或 blobs rsync + export.jsonl import 重建 |
| 迁移新机器/新 embedder | — | ① rsync blobs/ ② export.jsonl 随行 ③ kb init+import ④ kb reembed |
| 升级失败（schema 迁移中断） | 旧库不可开 | kb.db.pre-vN.bak 直接回拷即回到升级前 |
| 备份本身损坏 | — | --verify 在备份时即校验 quick_check；多份 retention 兜底 |

## 15. 权限分级与访问控制

**策略裁决唯一入口是 `policy.py`**（v0.3 收敛；v0.6 重定位为 Kernel Policy
Adapter）：访问面（CLI/MCP/Web/substrate）构造 `Principal(level, name,
capabilities)`，读路径经 `sql_scope()` 拿 SQL 条件、`row_pass()` 做行级复检，
写路径经 `resolve_write_path()` 校验白名单；本模块只做 **enforcement**，身份与
能力授予来自注入方（本机=完整能力，MCP/Web=级别映射，未来=Kernel 注入，见
§22.1）。新增访问面禁止自行拼可见性 SQL——历史上过滤逻辑散在 4 个文件，
漏一处就是越权。

三条必须记住的级联规则（都出过真实缺陷）：
- 块继承父条目的 **status**（审批）与 **visibility**（权限）；改父必须级联子，
  否则可经块读到 private 正文。
- 父条目删除时级联删块。
- 写锁是**实例级可重入**的：嵌套写路径（reject→remove）共用一把锁。

```
级别        可见 visibility          写/审权限        通道
admin       全部                     完全             CLI（本机即 admin）、MCP(KB_ACCESS_LEVEL)、Web token
operator    public + internal        审批/添加         Web token、MCP
viewer      仅 public                无               Web token、MCP
```

- 条目级：`kb add --visibility private|internal|public`，随时 `kb visibility <id> <level>`。
- 检索/浏览 SQL 层强制 `visibility IN (...)` 过滤，越权条目等同于不存在（fail-safe：
  未知级别按 viewer 处理）。
- Web 面板 token 存于 config（chmod 600）；MCP 默认 **operator**（写工具可见但
  写入默认进待审队列），级别经 `KB_ACCESS_LEVEL` 环境变量或 `access.mcp_level`
  配置调整（§22.1）。数据不含隐私，但权限边界仍按最小可见执行。

## 16. 人类操作面板

`kb serve-web --host 0.0.0.0 --port 7800`：纯标准库实现（无前端依赖），浏览器即可：
仪表盘（统计/分区/待审核/知识缺口）、检索、条目详情、待审核队列通过/拒绝按钮、
一键备份。token 鉴权（`?token=` 或 `X-KB-Token` 头，哈希存储见 §9），级别映射见 §15。
连接管理：8 实例有界对象池循环出租（C14，归还时回滚未提交事务），池满或写路径
回退为按需新建；写锁保证写互斥。

## 17. 磁盘 IO 与索引设计（为什么不炸 IO）

- **索引只建在摘要层**：每条目进 FTS 的是标题+摘要+关键词+预览前 4KB（可调），
  与原始文件大小无关；TB 级视频也只是几百字节索引行。
- **摄取读放大控制**：新文件读 1 次算哈希 + 写 1 次 blob（或 --in-place 零拷贝）；
  重复摄取走 size+mtime **快跳路径**，未变更文件一个字节的读 IO 都没有
  （防篡改需求用 --force-hash 全量重算）。
- **写入模式**：blob 顺序写+原子改名；SQLite WAL 顺序追加；向量分片 memmap 追加
  不整片重写；关闭连接时一次性 checkpoint。
- **查询读放大控制**：FTS 倒排索引命中即取；向量路只把 16384 行/块的 memmap 页
  调入页缓存，不整库装载；候选数上限（默认 200）双路封顶。
- 需要进一步降 IO 的场景：把 preview_chars 调小、检索走 --kind/--collection
  过滤（索引下推）、大量小文件入库时批量 add（每文件事务已做合并写优化空间）。

## 18. 模块地图与代码健康

```
kb/
├── policy.py      **访问策略唯一裁决点**（Principal/能力/可见性/写路径白名单）
├── core.py        门面：装配 + 增查 + 委派维护命令（薄层）
├── substrate.py   Information Substrate Capability API（上层组件入口，§22）
├── store.py       BlobStore + Catalog（schema/FTS/迁移/审计）—— 最核心，改动最少
├── ingest.py      摄取管线（去重/快跳/版本化/分块同步）
├── extract.py     多模态抽取        ├── summarize.py 摘要（离线/LLM）
├── embed.py       嵌入三后端        ├── vectors.py   向量分片
├── search.py      三级降级检索+RRF   ├── curate.py    整理（离线/AI）
├── admin.py       备份/恢复/导出/导入 ├── eval.py      评估 + 真实金标沉淀
├── web.py         人类面板（stdlib）  ├── mcp_server.py agent 面板（stdlib）
├── cli/           命令行包（拆分后）
│   ├── parser.py  argparse 装配 + main()
│   ├── query.py   检索浏览类子命令   ├── ingest.py 摄取治理类子命令
│   └── maint.py   维护备份服务类子命令
└── config.py / util.py
```

健康度自评（防屎山）：模块间无循环依赖，只经 core 门面与明确参数交互；测试
144 e2e + 27 单测全绿是重构安全网。store.py（657 行）与 core.py（625 行）已超
520 行预算（substrate 扩展所致），拆分挂账 ROADMAP；cli.py 曾涨到 625 行触发
预案，已拆为 `cli/` 包（parser/query/ingest/maint 四文件，最大 229 行）。
新增条目字段必须遵守 AGENTS.md 的四步清单（schema/迁移/输出/测试），这是
历史上唯一出过"静默丢字段" bug 的地方。

## 18.5 数据类型支持矩阵（v0.4）

| 数据类型 | kind | 抽取内容 | 检索粒度 | 说明 |
|---|---|---|---|---|
| 纯文本/Markdown/配置 | text | 全文（截断 256KB 可配） | 条目/块 | |
| 代码（40+ 扩展名） | code | 全文 + **签名摘要**（函数/类） | 条目/块 | .ts 等歧义扩展名靠内容嗅探 |
| Office/ODF（docx/xlsx/pptx/odt…） | text | zip 内 XML 正文剥标签 | 条目/块 | 零依赖；复杂排版丢失但编目够用 |
| Jupyter (.ipynb) | code | markdown+code 单元 | 条目/块 | base64 输出刻意不进索引 |
| PDF | pdf | pypdf/pdftotext 前 8 页 | 条目/块 | 无工具时仅元数据 |
| 图片 | image | 尺寸/EXIF | 条目 | 内容检索靠文件名/标签/AI 编目 |
| 音/视频 | audio/video | ffprobe 时长/编码/分辨率 | 条目 | 转写挂 ROADMAP |
| **模型权重**（safetensors/gguf/pt/onnx…） | **model** | **头部元数据**：张量数/dtype/架构 | 条目 | **绝不加载权重本体**；legacy pickle 摘要带"反序列化可执行任意代码"警告 |
| git 仓库 | — | 工作区正常入库 | — | .git 目录默认排除（exclude_names），内部对象不污染索引 |
| 归档（zip/tar/7z…） | archive | 仅元数据 | 条目 | 刻意不解包（防 zip 炸弹/递归）；需要内容就先解开再 add |
| 其他未知二进制 | binary | 仅元数据 | 条目 | 隔离不污染 |

设计原则：**结构不同、原理相似的容器格式，按"内容语义"归类而非按容器归类**
（docx 是 zip 但归 text；safetensors 是二进制但归 model），归类逻辑集中在
`util.guess_kind + sniff_kind`，新格式只需在这两处登记。

## 19. 分块索引（长文档段落级检索）

**解决的问题**：条目级索引只把正文前 4KB 写进 FTS，长文档后半部分对关键词
检索不可见（向量路仍会因整体语义返回它，但那是最近邻不是命中）。

- **数据模型**：块是 entries 表里的子条目，`parent_id` 指父、`chunk_no` 记序号；
  与父条目共享 source_path/collection/visibility/status（审批与删除级联）。
  块不占 blob（`blob=''`），正文存在 preview 列并整段进 FTS 与向量。
- **切分算法**（`util.chunk_text`，离线确定性、无模型依赖）：段落优先合并到
  `size`（默认 1200 字），超长段内滑窗 + `overlap`（默认 200 字）重叠，
  上限 2000 块/文档防失控。
- **同步策略**：块的 meta 记录指纹 `{size, overlap, content_hash}`；重新摄取时
  指纹与块数一致则原样保留，否则整体重建。快跳路径命中（文件未变）时完全不碰块。
- **检索行为**：块与父条目同池竞争，命中块时**压制其父条目**避免同文档占两席；
  结果里 `parent_id`+`chunk_no` 给出定位，agent 需要全文时按 parent_id 取父条目。
- **开关**：`kb add --chunk` 单次开启，或 `config set ingest.chunk.enabled true`
  全局开启；`kb rechunk [--collection X]` 对存量条目补建，`kb rechunk --disable`
  移除全部块。
- **代价**（务必知情）：向量行数随块数增长 10-50 倍，暴力向量检索线性变慢
  （10 万块约 0.2s，百万块 1-2s）；目录库体积增加约每块 1-2KB。建议只对
  真正需要段落级检索的 collection 开启。

## 20. 查询日志与知识飞轮

- **记录**（`logs/querys.jsonl` / `logs/gets.jsonl`）：每次 search 记 query、
  结果数、top 分数与 id、是否降级；每次 get 记条目 id/标题/路径/是否块。
  写入是 append-only 且异常吞掉，绝不影响检索主流程。
- **`kb gaps --days 30`**：汇总零结果与低分（top_score < 阈值）查询，
  按频次排序——这就是"该补什么资料"的清单。
- **`kb eval --from-log out.jsonl`**：把"search 后 5 分钟内 kb_get 过的条目"
  视为该查询的相关项，自动沉淀成金标集再评估。真实流量金标比手写金标
  更能代表实际查询分布，用得越久越准。
- **隐私边界**：日志只在本机 KB_HOME 下，随 `kb backup` 一起备份；
  不含条目正文，只有 query 文本与 id。

## 21. 运维部署（deploy/）

`deploy/` 提供开箱即用的 systemd 单元与灾备手册（[deploy/README.md](../deploy/README.md)）：

| 单元 | 周期 | 动作 |
|---|---|---|
| kb-backup.timer | 每日 | `backup --retention 14 --verify` + `gc --empty-trash --older-than 30` |
| kb-verify.timer | 每周 | `verify` 全量 SHA-256 复检（抓位腐） |
| kb-watch.timer | 每 15 分钟 | `watch <inbox> --once --review` 增量摄取 |

全部 `Nice` + `IOSchedulingClass=idle`，不与前台抢 IO。手册含 8 类灾备场景的
逐条命令、多机只读分发食谱（分发 backup 快照而非 rsync 活目录）、
季度恢复演练脚本。

## 22. Information Substrate 扩展（v0.6，schema v5/v6）

substrate.py 的 Capability API 与 schema 扩展（§2.2）的设计约定。实现见
`kb/substrate.py`，测试见 e2e "IS/Phase B" 分节。

### 22.1 Capability 模型与 MCP 级别映射

- `Principal(level, name, capabilities)`：四个能力常量——`substrate.reflect`
  （读）、`substrate.write`（写/摄取）、`substrate.approve`（高权威晋升：
  approve/claim/classify）、`substrate.admin`（运维：restore/retire/GC/backup）。
  本机 CLI 用 `local_principal()`（完整能力）；受限主体用 `limited_principal()`
  （缺省仅 CAP_REFLECT）。能力校验在 substrate 各入口 `_require()`。
- 可见级别沿旧三级：admin=不限 / operator=public+internal / viewer=public，
  未知级别按 viewer（fail-safe）。
- **MCP 默认 operator**（写工具可见，写入默认进待审队列）。级别来源：
  `KB_ACCESS_LEVEL` 环境变量 → `access.mcp_level` 配置 → 默认 operator。
  写工具暴露条件：`KB_ALLOW_WRITE=1`（兼容逃生阀）或当前级别含
  `substrate.write`。已知边界：kb_approve/kb_reject 目前同受"写工具暴露"单闸门
  约束，未按 CAP_APPROVE 单独细分（挂账）。

### 22.2 Knowledge Claims（认识论历史）

- `substrate.knowledge.assert_claim()` **双写**：先写 `knowledge_claims` 历史
  （失败即无事发生），再更新 entries 快照列——不做嵌套事务（Catalog.tx 不可
  重入）；带 derived_from 时同步写 provenance 边。需要 CAP_APPROVE。
- `claims_of()` 返回条目全部 claim 历史；`knowledge.retire()` 是认识论退役
  （epistemic_status=obsolete，status 不动，可审计追溯）。
- CLI `kb claim` 目前只更新 entries 快照列，**不写 claims 历史**；需要完整
  历史请走 assert_claim（两条路径的差异挂账待统一）。

### 22.3 Provenance（派生关系一等公民）

- `provenance_edges(child_id, parent_id, relation, created_by)`：派生边表，
  INSERT OR IGNORE 幂等；默认 relation=derived_from。
- `provenance.trace(id)` 返回三视图：derived_from 链上溯（guard 16 级防环）、
  关联边、该条目的审计时间线（最近 50 条）。

### 22.4 Memory Evidence 与 disclosure 继承

- `memory.evidence_append(event, evidence_ref, principal, retention_class)`：
  落一条 `[event]` 笔记到 `memory-evidence` 分区（kind=event），真正的记忆判定
  在上层。带 evidence_ref 时：写 provenance 边，且 **disclosure 继承收紧**——
  新记录 visibility 取"来源条目"与"目标默认(internal)"更严一档（public <
  internal < private），派生记录永不比来源更开放。
- `memory.search()` 恒定限定 memory-evidence 分区。

### 22.5 Retention 分级

- `retention.classify(id, retention_class)`：自由分级（缺省 NORMAL），需
  CAP_APPROVE。
- `retention.retire(id)`：置 status=retired，需 CAP_ADMIN；**protected 类
  （IMPORTANT/CORE/LEGAL/SYSTEM_PROTECTED）拒绝普通 retire**——IMPORTANT 须先
  降级；TEMPORARY/REBUILDABLE/NORMAL 可走普通流程。与 22.2 的认识论退役是
  两条独立通道。

### 22.6 Secret 外移（env: 句柄）

config.json 不再存明文凭据：值形如 `env:VAR_NAME` 时运行时从环境变量解析；
doctor 对明文凭据告警。备份快照中的 config 因此不携带机密。迁移：把明文换成
`env:` 句柄并存入环境变量即可。
