# agent-kb 路线图与决策记录

记录功能取舍、批次计划与需求审计结论。用法见 [README](../README.md)，
设计见 [ARCHITECTURE](ARCHITECTURE.md)。日期均为决策日。

## 1. 已决策事项（2026-09-03）

| 提案 | 决策 | 理由 |
|---|---|---|
| 文档分块索引 | **采纳，下一批首位** | 当前最大召回短板：长文档只有前 4KB 预览进索引，其余内容对检索不可见 |
| cross-encoder 重排 | **缓，API 优先** | 本地 reranker 依赖 torch（2GB+）顶到 3GB 内存红线；先等可用的 `/rerank` 风格 API 端点 |
| systemd 定时 verify/backup | **采纳，随下一批交付 deploy/** | 零代码风险，长期稳定性最便宜的保险 |
| 多机只读副本 | **不写代码，仅 runbook** | 单机部署下属于过度设计；分发 `kb backup` 一致性快照即是安全原语 |
| 行级加密 | **永久搁置** | 破坏 FTS/向量检索，数据无隐私诉求 |

## 2. 下一批（batch-1）—— ✅ 已交付 2026-09-03（提交 b3f3d83）

| # | 功能 | 交付物 | 状态 |
|---|---|---|---|
| 1 | 分块索引 | `kb add --chunk` / `kb rechunk`；schema v4（parent_id/chunk_no）；`util.chunk_text` 段落优先+滑窗重叠；块命中压制父条目 | ✅ |
| 2 | deploy/ | kb-backup/verify/watch 的 service+timer；[deploy/README.md](../deploy/README.md) 含 8 类灾备场景、多机分发食谱、季度演练脚本 | ✅ |
| 3 | 查询日志 + 缺口 | `logs/querys.jsonl`/`gets.jsonl`；`kb gaps`；`kb eval --from-log` 真实金标沉淀 | ✅ |
| 4 | `kb watch` | 轮询增量摄取，`--once` 供 cron/timer 驱动，复用 mtime 快跳零 IO | ✅ |
| 5 | `kb add --link` | 硬链接落库（同盘 O(1)），跨设备自动回退 copy | ✅ |
| 6 | 大体积警告 | 复制入库超 `ingest.warn_copy_mb`（默认 10GB）提示改用 --in-place/--link | ✅ |
| 7 | trash 老化 | `kb gc --empty-trash --older-than 30`；timer 每日执行 | ✅ |

测试：69 项 e2e 全绿（新增分块前后对比、watch --once、硬链接 nlink 校验、
缺口捕获、真实金标沉淀、老化只删过期文件等 13 项）。

实现中发现并记录的两个坑（已进 AGENTS.md）：块与父条目共享 source_path，
父条目查询必须用 `find_parent_by_source`（否则更新逻辑会错认块为父）；
验证"检索不到"必须按 `fts_rank` 判断关键词命中，不能看结果是否为空
（向量路永远返回最近邻）。

## 2.5 batch-2（✅ 已交付 2026-09-03，提交 0fc8e51）

来源：[DESIGN-V2](DESIGN-V2.md) 缺陷登记册。P0 三项（MCP 写白名单、向量过滤
下推、写锁）、P1 七项（stats 块分离、内容嗅探、原文 snippet、jieba 关键词、
代码签名摘要、日志轮转、vec_rows 索引）、缺失三项（告警 webhook、审计主体、
kb drill）全部完成；新增 policy.py 作为权限唯一裁决点。

审查中新发现并修复：**R1 可见性未级联到块的越权泄漏**（P0，分块×权限的组合
缺陷）、R2 写锁自锁、R3 畸形查询崩溃。e2e 从 69 增至 92 项。

仍挂账：C12 token 哈希存储、C13 PDF 页数可配、C14 面板实例复用、
C11 测试金字塔拆分。

## 2.6 真实数据验证与 P1 首批（✅ 2026-09-04）

生产库四本中文小说验证：意译查询策略重写（>4 字 CJK 段相邻二元组 OR）、
eval 指标去重、gaps 阈值与 RRF 换算对齐（0.012）、B1（max_file_mb 不再拦截
in-place/link）、pdf_max_pages 可配、link 零复制记账、CLI 分区显示。
金标 MRR 1.0，e2e 105 项。详见 [REAL-DATA-REPORT](REAL-DATA-REPORT-2026-09-04.md)。
P1 余项：e2e 拆分、C12 token 哈希、C14 面板实例复用。

## 2.7 P1 余项（✅ 2026-09-04 第二批）

- **测试金字塔**：新增 `tests/unit/test_unit.py` 27 项纯函数单测（policy 白名单/
  fts_query 两档策略/chunk_text 边界/GBK 截断解码/sniff/轮转/可重入锁），
  e2e 保持 116 项集成冒烟。
- **C12 token 哈希存储**：`kb token add/list/revoke/rotate/verify/migrate`，
  config 只存 SHA-256，明文仅创建时展示一次，hmac.compare_digest 防时序；
  旧明文条目兼容登录，`token migrate` 清除。
- **C14 面板实例池**：8 实例有界池循环出租（归还回滚事务），池满/写路径回退
  新建；实测 10 连发 200 + 备份 POST 正常。
- **附带发现并修复**：拆词阈值 5 字→3 字（4 字意译"注意力机制"在旧阈值下
  走整短语查不到近义表达），snippet 增加 3 字滑窗回退定位。

测试：e2e 116 项 + 单测 27 项全绿；生产库 doctor/金标 MRR 1.0/三类查询 sanity 通过。

## 2.8 语义嵌入与 rerank（✅ 2026-09-04 第三批）

硅基流动接入：embed=BAAI/bge-m3（1024 维，ApiEmbedder 加输入截断/429 退避/维度
自适应），rerank=BAAI/bge-reranker-v2-m3（新 rerank.py，RRF top-50 精排，失败
降级 RRF）。生产库 2194 行全量 reembed 完成。kb 金标 recall@3 0.33→0.67、MRR 1.0；
语义泛化查询（口语化描述技术功能）实测命中。检索 3.0s/RSS 82MB。
e2e 126 项（新增在线嵌入/精排/降级链 6 项）。详见 LLM-RELAY-REPORT。

## 3. 缓做（附触发条件）

| 功能 | 触发条件 |
|---|---|
| ~~rerank API 重排~~ | ✅ 已交付（bge-reranker-v2-m3，失败降级 RRF） |
| kb_answer 组合工具（检索→取原文→LLM 带引用回答） | ✅ 触发条件已满足（MRR 1.0）——下一批候选 |
| TTL 时效列（过期自动对检索隐藏） | 出现真实的时效性数据（价格/日程类） |
| sqlite-vec 迁移 | 摘要向量 > 百万行且单查 > 2s |
| 音视频 Whisper 转写 | 出现需要按内容检索音视频的真实场景 |

## 4. 永久不做（防止未来动摇）

- **使用信号排名加权**（谁被 kb_get 多谁排前）：把使用习惯编码进排序，
  是另一种形式的个性污染，与数据纯净性目标冲突。
- **blob 压缩**：破坏"哈希即地址"的简洁性；媒体文件本就不可压；引入解压
  失败新故障面。
- **行级加密**：见 §1。

## 5. 需求审计（2026-09-03）

### 5.1 相互矛盾/张力点及消解

| 张力 | 状态 |
|---|---|
| agent 自由写入 vs 数据不被污染 | 已消解（审核队列+origin 溯源），但有代价：agent 新写的笔记在 approve 前检索不到。运营建议：agent 工作笔记放独立 collection 且 review=false，正式资料才走审核 |
| 零依赖轻量 vs AI 整理/语义质量 | 不可消解，只能接受：离线档是刻意保底（"不难看"而非"好看"），质量上限取决于是否配置 API。这是属性，不是缺陷 |
| TB 级数据 vs 默认复制入库 | 默认 add 会让磁盘翻倍；batch-1 的大体积警告 + --link 缓解 |
| 多通道（CLI/MCP/Web）并发 vs 单写者软约定 | 短事务（审批/笔记）由 busy_timeout 兜底无碍；长时间批量 add 与面板审批并发可能报 database is locked。约定：批量摄取走 CLI 串行；真并发写需求出现时再加文件锁队列 |
| 删除永不直删 vs 磁盘有限 | trash 会无限增长；batch-1 加 30 天老化 |

### 5.2 "每个功能三套方案"的边界

功能层已做到（ARCHITECTURE §13 矩阵）。但基石（SQLite/numpy/文件系统）是
单选，无法三套——基石故障的"第三套"= export JSONL + blobs 目录的平台级
迁移。这是刻意接受的边界，不是遗漏。

### 5.3 优先级错判（自纠记录）

1. `restore` 命令一直没进 e2e——没恢复演练过的备份是薛定谔的备份。
   **本轮补测后当场抓到一个真 bug**：恢复时在连接未关闭的情况下换文件，
   进程退出的 WAL checkpoint 会把旧状态写回恢复后的库、静默撤销恢复
   （之前"结果非空"的弱断言被向量路永远返回最近邻的特性掩盖）。
   已修复（admin.restore 换文件前先 checkpoint(TRUNCATE)）并固化进 e2e。
2. 分块索引应第一轮就提出：长文档召回短板从 v0.1 就存在，拖到第五轮。
   batch-1 首位纠偏。
3. 查询日志/真实金标应更早：手写金标无法代表真实查询分布。batch-1。
4. deploy/ unit 文件在采纳决策后仍未落仓。batch-1。

### 5.4 需求实现状态总表

| 需求（历轮汇总） | 状态 |
|---|---|
| 幂等更新 / 版本化 / 内容寻址去重 | ✅ |
| 多模态摄取（文本/代码/图片/音视频元数据/PDF 文本） | ✅（音视频转写缓做） |
| 每条目摘要+关键词，摘要可检索 | ✅ |
| TB 级（--in-place / 快跳路径 / 索引只在摘要层） | ✅（大体积警告 batch-1） |
| 速度与内存（0.2s / 41MB / 零常驻 / 3GB 上限） | ✅ |
| 抗污染（不可变 blob / verify / doctor / 审计 / trash / 审核队列 / origin） | ✅ |
| 完全自控（零重依赖，纯 stdlib+numpy） | ✅ |
| 三级降级（每个功能多方案） | ✅（功能层；边界见 §5.2） |
| 备份保留策略 / 备份校验 / restore / 导出导入迁移 / 迁移前快照 | ✅（restore 已补测） |
| 权限分级（visibility 三级 / token 三级 / SQL 层强制过滤） | ✅ |
| 人类操作面板（浏览/检索/审批/备份） | ✅ |
| MCP agent 通道（默认只读，写入需显式开启且默认进审核） | ✅ |
| 远程使用 | ✅ SSH MCP / Web token；纯 HTTP API 刻意不做（ARCHITECTURE §10） |
| curate 整理（离线档 + AI 档） | ✅（AI 档需配置 API） |
| eval 检索质量（recall@k / MRR） | ✅（金标暂手写，真实流量生成 batch-1） |
| 跨系统兼容 | ✅ 代码层（无平台分支）；Linux 实测，macOS/Windows 文档声明未实测 |
| 文档统一标准 | ✅ README / ARCHITECTURE / AGENTS / ROADMAP 四件套 |
| git 管理与及时提交 | ✅ 逐功能提交 |
| 分块 / 重排 / watch / 查询日志 / --link / TTL / trash 老化 | 🗓 本文档 §2-3 |
