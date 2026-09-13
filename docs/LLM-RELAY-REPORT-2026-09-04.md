# LLM 中转接入与 AI 编目报告（2026-09-04）

端点：`http://127.0.0.1:3000/api/v1`（OpenAI 兼容 chat，**无 embeddings 路由**）。
模型优先级（用户指定）：**qwen3.8-flash > mimo-v2.5 / hy3 > glm-5.3-flash**。
key 存于生产库 config（chmod 600）。

> **2026-09-04 更新（硅基流动接入）**：新增 `https://api.siliconflow.cn/v1`——
> **BAAI/bge-m3 嵌入（1024 维）+ BAAI/bge-reranker-v2-m3 精排**均已接入生产库。
> 三代检索质量对比（kb 技术金标，k=3）：
>
> | 配置 | recall@3 | MRR |
> |---|---|---|
> | hash 嵌入（上一代） | 0.33 | 0.44→1.0（靠 OR 修复） |
> | **bge-m3 语义嵌入** | **0.67** | 1.0 |
> | **bge-m3 + rerank 精排** | **0.67** | **1.0**（排序更稳） |
>
> 语义泛化实测（hash 时代不可能命中）："怎么防止两个人同时改数据库" → 写锁文档
> sim 0.52 第一；"如何恢复被误删的数据" → 备份恢复文档 sim 0.57 第一。
> 代价：全量 reembed 2194 行 10.5 分钟（一次性的）；单次检索 3.0s / RSS 82MB
> （含 rerank 网络往返，仍远低于 3GB）。rerank 失败自动降级 RRF（有 warning）。
> 生产配置：embed=api/bge-m3；rerank=api/bge-reranker-v2-m3 top_n=50。

## 1. 接入结论

- **嵌入**：中转不支持 `/embeddings`（Route Not Found），生产库继续用 hash
  embedder（字面相似 + BM25F）。语义嵌入待有真实 embedding 端点后
  `config set embed.provider api && kb reembed`。
- **摘要/编目**：`summarize.provider=api`，
  `summarize.model="qwen3.8-flash,mimo-v2.5,hy3,glm-5.3-flash"`（逗号链即故障转移序）。

## 2. 三个真实问题与修复（全部有回归测试）

| # | 问题 | 根因 | 修复 | 验证 |
|---|---|---|---|---|
| R1 | `chat_json` 返回 None | 中转的 chat 模型**忽略 system 指令**，把编目任务当"文件内容解释"回答 | 指令直写 user 消息 + `response_format=json_object` 优先 + 失败转 plain + 解析器支持 ```json 块 | 编目单条 2.2s 返回结构化 JSON；e2e §17 |
| R2 | 主模型不可用全链失败 | 旧实现单模型单尝试 | `summarize.models` 逗号链按序故障转移（每模型 json_mode→plain 两策略） | 把主模型改为 not-exist 后自动落到 mimo，实测 16.3s 完成编目 |
| R3 | AI 摘要漏掉 core.py 的 drill 功能 | 双重截断：curate 虽读全文，但 chat_json 只取 12k 字符（drill 在 18.9k）；且 curate 的 `limit` 切片是 iter_all 顺序，core.py 在 46 位，`--limit 25` 根本轮不到 | 截断上限提至 60k；新增 `--force`（连已编目条目重做）；排查时用 monkeypatch 追踪确认全链 | 摘要含"灾备演练"，FTS 可召回 |

## 3. AI 编目质量（生产库 35 父条目，已编目 30+）

抽检样例（qwen3.8-flash 输出）：

- 围城 → "钱钟书长篇小说《围城》全本文本……描写方鸿渐、苏文纨、鲍小姐等留学生群像"
  关键词：围城,钱钟书,方鸿渐,留学生,假文凭
- 盗墓笔记 → "开篇讲述50年前长沙土夫子盗战国帛书遭遇血尸……前往山东临沂"
  关键词：盗墓笔记,七星鲁王宫,战国帛书,吴邪,三叔
- tokens.py → "配置仅存储 SHA-256 哈希，明文 token 只在创建时展示一次……"
- core.py → "……提供……策展、评估和灾备演练等命令。通过写锁保证并发写安全……"

模型输出有采样随机性（同提示是否提到某功能约 3/3~4/4 命中，偶有遗漏），
`--force` 可重跑。

## 4. 期间发现的检索算法问题（第 4 个真修复）

金标评估暴露：`灾备演练 drill` 查询 **0 命中**。根因是 fts_query 把中英文段
用 AND 连接——core.py 摘要有"灾备演练"没有"drill"，DESIGN-V2.md 反之，
**没有任何文档同时包含两种语言的词**。修复：`fts_query` 顶级段改为 **OR**
（召回优先，噪声由双路 RRF 融合压制）。复测：

| 金标集 | 修复前 | 修复后 |
|---|---|---|
| kb 技术文档 3 条（k=3） | recall 0.33 | recall 0.5，**MRR 1.0** |
| 小说 5 条 | recall 1.0 / MRR 1.0 | 不回归（1.0/1.0） |

同时修正了金标自身两处错误（eval 按条目去重后 `relevant` 应写同义文档集合，
"drill"的 relevant 应含 DESIGN-V2.md——实现文档与代码同义）。

## 5. 验收状态

- e2e **121 项**（新增 §17：JSON 解析器 4 形态 + 在线编目集成）+ 单测 27 项全绿
- 生产库 doctor ok；备份校验通过（2194 行）；两套金标 MRR 1.0
- 生产配置已固化：summarize api + 四模型链（key 已在 config，600 权限）

## 6. 剩余与建议

- 每次代码/文档更新后跑 `kb curate --ai --apply --force --limit N --collection X`
  刷新编目（模型采样偶有遗漏，重跑即好）。
- 查询日志 + `kb gaps` 现在有了真实的 LLM 编目文本，缺口报告的判据更可靠。
- 若中转将来开放 `/embeddings`，切 `embed.provider=api` + `kb reembed` 后
  需重新评估两套金标（hash→语义嵌入的 MRR 变化是 rerank 决策依据）。
