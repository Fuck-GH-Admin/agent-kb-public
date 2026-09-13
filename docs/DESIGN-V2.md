# DESIGN-V2 —— 从零重设计推演与现系统差距评估

日期：2026-09-03。方法：抛开现有实现，只保留需求，以架构师+用户双视角重新设计；
再用重设计结果反照现系统，产出缺陷登记册与缺失清单。所有缺陷均附可复现证据。

## Part A. 需求的本质（用户视角重述）

剥掉实现细节，这个系统对用户只有五个承诺：

| # | 承诺 | 对应需求原文 |
|---|---|---|
| P1 | **放进去就能找到**：丢文件即入库，重复执行无副作用 | 便于更新、便于存储、自动摄取 |
| P2 | **找到的是对的**：检索质量可度量、可改进 | 摘要可检索、检索准确、语义检索 |
| P3 | **永远不坏不丢**：任何单点故障都有恢复路径 | 稳定可靠、备份、TB 级 |
| P4 | **谁都污染不了**：agent/人的每次写入可控可溯可撤 | 不被污染、权限分级、审核 |
| P5 | **永远轻**：资源边界是特性不是妥协 | 内存<3G、零常驻、完全自控 |

架构师视角的推论：P3/P4/P5 决定骨架（不可变存储、审计、零依赖），
P1/P2 决定管线（摄取、索引、检索），两组解耦——骨架错了要重写，管线错了可迭代。

## Part B. 从零设计（如果今天重来）

### B.1 不变的部分（被验证正确的骨架）

重推演后结论：**存储骨架与现系统一致**。SQLite（目录+FTS5）+ SHA-256 内容寻址
blob + 摘要层索引 + numpy 分片向量 + RRF 混合检索——这套组合在 25→69 项测试、
四个参考项目对照、以及多轮审计中没有出现根本性反例。重写它没有收益。

### B.2 会改的部分（重设计的真正价值）

**1. Principal 贯穿的单一策略层（最大差异）。**
现系统 visibility/status 过滤逻辑散在 search.py(3处)/store.py(6处)/mcp_server.py/
web.py——每加一个访问面就要再抄一遍过滤，漏一处就是越权。重设计会立一个
`policy.py`：所有访问面先构造 `Principal(level, origin)`，一切读写走
`policy.scope(principal) -> SQL 片段 + 向量过滤器`，全库只有这一个裁决点。

**2. 向量预过滤（partition-aware）。**
现系统是后过滤：先取全局 top-200 再按 collection/visibility 筛。缺陷：小分区
的条目可能完全挤不进全局 top-200 → 过滤后向量路零结果（召回黑洞）。
重设计把 (collection, visibility, status) 随 vec_rows 存表，检索时**先筛
候选行集再算相似度**——过滤下推到相似度计算之前。

**3. 写者互斥内建。**
现系统"单写者"是文档约定。两个进程同时 add 时 vec_next 元数据竞态会导致
向量错位（悄悄写坏，doctor 都查不出）。重设计在 KnowledgeBase 写路径入口
拿 `KB_HOME/.lock` 文件锁（flock，非阻塞失败即报错），零成本消灭整类事故。

**4. 内容嗅探而非仅扩展名。**
证据：`guess_kind('x.ts') == 'video'`（TypeScript 被判成 MPEG-TS）。
重设计以 magic bytes 嗅探为主、扩展名为辅。

**5. FTS 展示列与索引列分离。**
证据：中文 snippet 输出 `[红 烧 肉] 做 法 五 化 肉…`——因为 snippet() 跑在
CJK 逐字切分的索引列上。重设计 snippet 从原文 preview 定位，索引列只做检索。

**6. 块/父在统计与展示层严格分离。**
证据：stats 显示 entries_total=31，其中 18 是块——用户以为库里有 31 份资料。

**7. 日志内建轮转**（audit/querys/gets 无限增长）；**8. 测试金字塔**
（单元测试按模块 + e2e 冒烟，替代 475 行顺序依赖的单脚本）。

### B.3 判定：演进，不重写

骨架判定正确 + 69 项测试是现成的安全网 + B.2 的 8 项全部可以增量落地
（策略层收敛是重构不是重写）。推倒重来会丢掉测试资产和已修复的 6 个真 bug
（每个都值一次事故）。**结论：以 batch-2 形式演进。**

## Part C. 缺陷登记册（带证据，按严重度）

> **修复状态（2026-09-03，提交 0fc8e51 / 后续）**：P0 三项、P1 七项全部修复并有
> 回归测试；审查过程中又发现两个更严重的问题（R1 越权泄漏、R3 崩溃），见 §C.4。
> P2 四项：C11 已随 e2e 分节推进，C12/C13/C14 仍挂账（见 ROADMAP）。

### P0 —— 正确性/安全，立即修

| # | 缺陷 | 证据 | 修法 |
|---|---|---|---|
| C1 | **MCP 写通道无路径白名单**：开 KB_ALLOW_WRITE 后 agent 可 `kb_add /etc/shadow`，机密被复制进 blob 并被摘要——变相任意文件读 | grep 无 allowed_roots | 增 `access.write_roots` 配置，kb_add 仅接受白名单内路径 |
| C2 | **向量后过滤召回黑洞**：小分区过滤后向量路可能零结果 | search.py:126 先 top-N 后过滤 | 过滤下推（B.2-2） |
| C3 | **并发写无锁**：vec_next 竞态 → 向量静默错位 | vectors.py 无任何锁 | 写路径 flock（B.2-3） |

### P1 —— 质量缺陷，batch-2 内修

| # | 缺陷 | 证据 |
|---|---|---|
| C4 | stats 把块算进条目总数（31 显示 vs 13 真条目） | stats 实测 |
| C5 | .ts→video 等扩展名冲突，无内容嗅探 | guess_kind 实测 |
| C6 | 中文 snippet 带空格、观感差 | `[红 烧 肉] 做 法…` |
| C7 | 中文关键词是二元组碎片（"肉做""化肉"） | keywords 实测 |
| C8 | 代码条目摘要=原文前几行，无结构感知（应提取函数/类签名） | `def add(a,b):return a+b#…` |
| C9 | audit/querys/gets 日志无轮转，无限增长 | grep 无 rotate |
| C10 | all_vec_rows 每查询全量物化：百万行时每查白付数百 ms 与几十 MB | vectors.py:88 |

### P2 —— 技术债，挂 ROADMAP

C11 e2e 单文件 475 行顺序依赖（拆测试金字塔）；C12 Web token 明文存储
（chmod 600 缓解，改存哈希更稳）；C13 PDF 只抽前 8 页（可配置化）；
C14 web.py 每请求新建 KB 实例（ThreadingHTTPServer 下正确但浪费，量大再改）。

### C.4 修复过程中新发现的缺陷（比原登记册更严重）

按 software-development-code-review.md 的方法追踪完整数据路径时发现：

| # | 缺陷 | 触发条件 | 实际影响 | 根因 | 修复 |
|---|---|---|---|---|---|
| **R1** | **可见性未级联到块（越权泄漏，P0）** | 文档以 `--chunk` 入库后，把父条目设为 private | operator/viewer 仍能通过块检索到 private 文档正文——实测泄漏出"数据库 root 口令为 hunter2-prod" | 块是独立 entries 行、携带父文档正文，`set_visibility` 只改父行 | `set_children_visibility` 级联；e2e 加 R1 两项回归 |
| **R2** | 写锁自锁（可用性，P1） | `reject` 内部调用 `remove`，两者都取写锁 | 审核拒绝直接报"另一个写进程正在运行"，功能不可用 | WriteLock 每次 new 一个新 fd，自己和自己冲突 | 改为实例级可重入锁（depth 计数） |
| **R3** | LIKE 降级路空条件崩溃（可用性，P1） | 查询为 `*`、纯标点等无有效词项，且走 LIKE 路 | `sqlite3.OperationalError: near ")"` 直接抛栈 | `" OR ".join([])` 拼出 `WHERE ()` | 无词项时退化为 `1=1` 按时间列出；e2e 加 7 种畸形查询 |
| R4 | （验证通过，非缺陷） | blob 被外部损坏 | verify 正确检出、检索不受影响（索引在目录库） | — | 补回归测试固化该行为 |

R1 是本轮最有价值的发现：它由 batch-1 的分块功能引入，而分块与权限是两个
独立提交，单看任一提交都不会发现——**跨特性的组合缺陷只有系统性审查能抓到**。

### C.5 实测数据（3000 条目规模）

| 指标 | 实测 |
|---|---|
| 摄取 3000 个文件 | 2.4s |
| 幂等重跑（全部走 mtime 快跳） | 0.26s |
| 检索（全库）| 0.20s / 峰值 64.5MB |
| 检索（分区过滤，走下推）| 0.19s / 峰值 45.3MB |
| 目录库 / 向量 | 4.7MB / 5.9MB |

过滤下推不仅修了召回黑洞，还让带过滤的检索内存更低（候选集更小）。

## Part C.6 batch-3：杂类数据类型审查（2026-09-03）

用户问题"git/富文本/音视频/模型权重能否合理处理"触发的专项探测，
真实样例实测（每条先复现后修复）：

| 场景 | 探测结果 | 处置 |
|---|---|---|
| 模型权重 (.safetensors/.gguf/.pt) | 判成 binary，摘要无信息 | 新 kind=**model**：只读头部（张量数/dtype/架构），绝不加载本体；legacy pickle 附安全警告 |
| docx/xlsx（zip 容器） | 判成 archive，正文不可检索 | Office 抽取器：zip 内 XML 剥标签，正文进索引 |
| .ipynb | 整个 JSON 当代码，base64 输出会污染索引 | 专用抽取器：只取 markdown+code 单元 |
| git 仓库 | .git 已被 exclude_names 排除（原设计正确） | 补回归测试固化 |
| zip/tar | 判 archive 仅元数据（原设计正确：防 zip 炸弹不解包） | 文档声明 |
| >512MB 大文件 --in-place | 被 max_file_mb 拒绝——但 in-place 本不复制，上限没道理 | **B1 缺陷**，见下 |
| 重摄取不带 --collection | **分区被静默重置回 default**（数据治理事故级） | **B2 缺陷**，已修：None 语义=保留原分区 |

**B1（挂账 P2）**：max_file_mb 对 --in-place/--link 模式无意义（不复制字节），
应只约束 copy 模式；当前 workaround 是调大配置。
**B2（已修）**：add(collection=None) 更新时保留原分区、新增时 default；
CLI --collection 缺省改为 None。回归测试"更新时未指定 collection 则保留原分区"。

## Part D. 缺失部分（需求层面尚未覆盖）

| # | 缺失 | 说明 | 建议 |
|---|---|---|---|
| D1 | **告警通道** | verify/backup 失败只进 journalctl，没人看=没告警。"稳定可靠"缺最后一环 | `kb notify` 钩子：失败时 POST webhook（ntfy/TG bot 均可接） |
| D2 | **审计缺主体** | audit 记了 op 没记"谁"：approve 是哪个 token 干的查不到（多管理员需求下是盲区） | audit 增 principal 字段，Web/MCP 通道填 token name |
| D3 | **恢复演练未命令化** | runbook 里是手工脚本，"每季度演练"大概率被遗忘 | `kb drill`：备份→删→恢复→校验一键完成，可挂 timer |
| D4 | 面板缺使用视图 | gaps/查询频次在 CLI 有、面板没有 | 面板加一页 |
| D5 | kb_answer | 已在 ROADMAP 缓做（触发条件 MRR≥0.8）| 维持 |

## Part E. batch-2 交付记录（✅ 完成 2026-09-03，提交 0fc8e51 起）

| 项 | 交付内容 | 验证 |
|---|---|---|
| policy.py 策略层 | `Principal` + `sql_scope` + `row_pass` + `resolve_write_path`；search/store/web/mcp 全部改为经此裁决，不再各自拼可见性 SQL | 92 项 e2e |
| C1 MCP 写白名单 | 默认只允许 `$HOME` 非隐藏目录（realpath 后前缀判定，挡 `..` 与软链穿越）；`access.write_roots` 可收紧为显式白名单 | 拒绝 /etc/shadow、/proc、~/.ssh、~/../etc；MCP 端到端拒绝 /etc |
| C2 向量过滤下推 | `scoped_vec_rows` 先按策略筛候选行再算相似度 | vec_candidates 收到 5 的极端条件下小分区仍有向量命中 |
| C3 写锁 | 实例级可重入 flock，覆盖 add/note/approve/reject/remove/rechunk/reembed/compact/restore/import/curate | 跨进程并发 add：一方成功一方被拒，doctor 一致 |
| C4~C9 | stats 块分离、magic bytes 嗅探、原文 snippet、jieba 可选、代码签名摘要、日志轮转 | 各有 e2e 断言 |
| D1/D2/D3 | 告警 webhook（`kb notify-test`）、审计 principal、`kb drill` 一键灾备演练 | 本地 HTTP sink 收到告警；audit 出现 `web:alice`；drill 五步全过 |
| R1~R3 | 见 §C.4 | 各有回归测试 |

**未做（明确挂账）**：C12 token 哈希存储、C13 PDF 页数可配、C14 面板 KB 实例复用、
C11 测试金字塔拆分（当前 e2e 已按 13 个场景分节，暂够用）。