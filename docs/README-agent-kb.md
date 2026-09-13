# agent-kb —— 给 agent 用的本地知识库

为本地 LLM/agent 设计的**轻量、可控、抗损坏**知识库。核心只有两个久经考验的基石：
**SQLite（FTS5 全文索引）** + **内容寻址（SHA-256）不可变文件库**，向量检索用 numpy
内存映射分片实现，无任何常驻服务。

```
检索 = BM25 关键词(FTS5)  ×  语义向量(numpy 分片)   →  RRF 融合排序
存储 = 原始数据(文件)     +  摘要/元数据(SQLite)     +  摘要向量(分片 memmap)
```

## 设计目标与现状

| 需求 | 实现方式 |
|---|---|
| 便于更新 | `kb add` 幂等：重复摄取自动跳过（size+mtime 快跳，零 IO）；文件变了自动版本化更新；`kb watch` 可自动增量摄取 |
| 便于存储 | 内容寻址去重：相同内容只存一份；`--in-place` 零拷贝、`--link` 硬链接（同盘 O(1)） |
| 多模态 | 文本/代码/图片/音视频/PDF 各自抽取器；识别不了的格式只存元数据、不污染索引 |
| 摘要可检索 | 每条目都有摘要+关键词，摘要/标题/关键词/正文预览全部进全文索引和向量索引 |
| 长文档检索 | `--chunk` 分块索引：段落级命中并回溯父条目（`kb rechunk` 可对存量补建） |
| 分类存储 | `--collection` 主题分区 × `--tag` 自由标签 × kind 类型，多维正交过滤 |
| 内容可治理 | `--review` 审核队列（agent 写入默认待审），`approve/reject` 放行或拒绝 |
| 摘要可溯源 | 每条目记录 origin（human/agent），agent 写入的内容一键甄别 |
| 检索可评估 | `kb eval` 算 recall@k/MRR；`--from-log` 用真实查询日志自动沉淀金标 |
| 知识缺口 | `kb gaps` 汇总零结果/低分查询，直接告诉你该补什么资料 |
| TB 级数据 | 原始数据不进数据库；索引只针对摘要层；向量分片流式查询，内存占用恒定 |
| 速度不卡 | 小库检索 0.2s/41MB；38.8 万条目生产库 1.1s（rerank 关）/2.8-4.2s（含 rerank）/RSS 82-900MB（见下文实测） |
| 资源有限 | 零常驻进程；默认嵌入器纯 CPU 毫秒级；整套依赖只有 numpy |
| 不被污染 | 不可变 blob+写入校验；verify/doctor 体检；审计日志；GC 只进回收站；**MCP 默认只读** |
| 长期运维 | `deploy/` 提供 systemd 定时备份/校验/摄取 + 8 类灾备场景手册 |

架构、数据结构、算法演进、安全模型、远程部署等深入说明见
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**；功能取舍与计划见
**[docs/ROADMAP.md](docs/ROADMAP.md)**；部署与灾备见 **[deploy/README.md](deploy/README.md)**。

## 目录

```
kb/
├── kb/                  # Python 包
│   ├── core.py          # KnowledgeBase 门面（所有能力从这里进）
│   ├── store.py         # BlobStore + SQLite 目录（WAL、FTS5、审计）
│   ├── ingest.py        # 摄取管线（去重/更新/向量化）
│   ├── extract.py       # 多模态内容抽取
│   ├── summarize.py     # 摘要（离线抽取式 / 可选 LLM）
│   ├── embed.py         # 嵌入（hash / API / 本地模型）
│   ├── vectors.py       # numpy 分片向量索引
│   ├── search.py        # 混合检索 + RRF
│   ├── cli.py           # 命令行
│   └── mcp_server.py    # MCP stdio 服务（stdlib 实现）
├── bin/kb               # 可执行入口（免安装）
├── examples/agent_usage.py
└── tests/e2e_test.py    # 25 项端到端测试
```

数据目录（`KB_HOME`，缺省 `~/.kb`）：

```
~/.kb/
├── kb.db          # SQLite 目录库（WAL）：条目/摘要/标签/FTS/向量行映射
├── blobs/aa/bb/<sha256>   # 不可变原始内容
├── vectors/shard_XXXX.npy # 摘要向量分片
├── logs/audit.jsonl       # 全部写操作审计
├── trash/                 # GC 回收站（永不直删）
└── config.json
```

## 快速开始

```bash
cd ~/Bot/kb-workspace/kb
export KB_HOME=~/.kb            # 可选，缺省即 ~/.kb

./bin/kb init                          # 初始化
./bin/kb add ~/docs --tags 资料        # 摄取目录（幂等，可反复执行）
./bin/kb add ~/papers --collection papers --review  # 分主题分区 + 待审核
./bin/kb add ~/books --chunk           # 长文档分块，段落级检索
./bin/kb add ~/大文件库 --link          # 硬链接入库（同盘 O(1)，不占额外空间）
./bin/kb approve <id>                  # 审核通过（reject <id> 则拒绝删除）
./bin/kb add /mnt/nas/视频库 --in-place # TB 级数据：只索引原位置，不复制
./bin/kb watch ~/inbox --once          # 增量自动摄取（cron 友好；去掉 --once 常驻）
./bin/kb note --title "备忘" --text "..." --tags memo
./bin/kb search "注意力机制" --limit 5
./bin/kb search "红烧肉" --tag demo --kind text --collection cooking
./bin/kb list --origin agent           # 甄别 agent 写入的内容
./bin/kb gaps --days 30                # 知识缺口：该补什么资料
./bin/kb curate --apply                # 整理：补摘要/规范标签/查重复（--ai 用 LLM）
./bin/kb eval --from-log auto.jsonl    # 用真实查询日志沉淀金标并评估
./bin/kb backup --retention 7 --verify # 备份+校验+保留策略（restore 可恢复）
./bin/kb export catalog.jsonl          # 迁移：配合 rsync blobs/ + import
./bin/kb serve-web --port 7800         # 人类操作面板（浏览器 + token 鉴权）
./bin/kb stats && ./bin/kb doctor
```

## 给 agent 用

### 方式一：MCP（推荐，Claude/ZCode/Cursor 等通用）

`--home` 换成你的实际 KB 目录：

```json
{
  "mcpServers": {
    "agent-kb": {
      "command": "/home/miku/Bot/kb-workspace/kb/bin/kb",
      "args": ["--home", "/home/miku/.kb", "serve-mcp"]
    }
  }
}
```

agent 拿到的工具（**默认只读**，不会被 agent 误写污染）：

| 工具 | 说明 |
|---|---|
| `kb_search` | 混合检索，返回 id/标题/摘要/**命中片段 snippet**/标签/分区/分数/原文位置 |
| `kb_get` | 按 id 看条目完整信息与原文路径 |
| `kb_list` / `kb_stats` | 浏览与统计（含待审核数） |

确实要让 agent 写入时，在 MCP 配置的环境变量里加 `"env": {"KB_ALLOW_WRITE": "1"}`，
会额外暴露 `kb_add` / `kb_note` / `kb_approve` / `kb_reject`。注意：**agent 的写入
默认进入待审核队列**（pending），经人工 approve 后才会出现在检索结果里，
从机制上杜绝 agent 个性和幻觉内容污染知识库。

### 方式二：Python API（嵌进你自己的 agent 循环）

见 `examples/agent_usage.py`，核心就四步：

```python
from kb import KnowledgeBase
kb = KnowledgeBase()                       # 读 KB_HOME
kb.add("/path/to/dir", tags=["docs"])      # 摄取（幂等）
res = kb.search("查询词", limit=5)          # 混合检索
hit = res["results"][0]
loc = hit["blob_path"] or hit["source_path"]  # agent 按需读原文
```

## 摘要与嵌入后端

默认**完全离线可用**：摘要用内置抽取式（分句+关键词加权），向量用字符 3-gram 特征
哈希（确定性、零依赖，捕捉字面相似度，与 BM25 互补）。想要更强的语义效果，配置一个
OpenAI 兼容端点即可（中转/本地 vLLM/Ollama 都行）：

```bash
./bin/kb config set summarize.provider api
./bin/kb config set summarize.api_base https://your-relay/v1
./bin/kb config set summarize.api_key sk-xxx
./bin/kb config set summarize.model gpt-4o-mini

./bin/kb config set embed.provider api
./bin/kb config set embed.api_base https://your-relay/v1
./bin/kb config set embed.model text-embedding-3-small
./bin/kb reembed                       # 按新后端重建全部向量
```

- 换嵌入后端后必须 `kb reembed`；doctor 会检查 embedder 与库内向量是否一致。
- LLM/API 不可用或配置不完整时自动**降级**到离线路径，绝不阻断摄取。
- 本地嵌入模型（机器有富余时）：`pip install sentence-transformers` 后
  `config set embed.provider local`（默认 bge-small-zh，约 100MB）。

## TB 级数据策略

原始数据永远不进 SQLite，索引只建在摘要层（标题+摘要+关键词+正文预览前 4KB）。
容量预算（100 万条目量级）：

| 项 | 估算 |
|---|---|
| 原始数据（blob 或 in-place） | 就是你数据的实际大小 |
| kb.db（含 FTS） | 约 5-10 GB |
| 向量分片（512 维） | 100 万条 ≈ 2 GB 磁盘 |

三种摄取模式：

- 默认：复制进 blob 库（有去重，同内容只存一份）。安全，与源文件解耦。
- `--in-place`：只算哈希+索引，**零拷贝**，推荐大库首选用法；doctor 会报告源文件丢失。
- `--move`：入库后删源文件（空间不翻倍）。

## 防污染与抗损坏设计

1. **不可变 blob**：内容寻址，写入时先落临时文件、fsync、校验哈希、原子改名。
2. **`kb verify`**：全量重算 SHA-256，报告损坏/丢失（建议每月跑一次，或 cron）。
3. **`kb doctor`**：SQLite quick_check、FTS 完整性、缺 blob/缺源文件、维度一致性。
4. **审计日志** `logs/audit.jsonl`：每个写操作都有记录，出问题可追溯。
5. **删除永不直删**：`rm` 解除索引；`gc --commit` 只把孤儿 blob 移入 `trash/`；
   确认无误后才 `gc --empty-trash`。
6. **MCP 默认只读**：agent 检索不受限，写入需要显式开关。
7. **单文件失败不拖垮批量**：摄取逐文件事务，错误汇总报告。
8. **备份**：`kb backup [目录]` 快照目录库+向量+审计+配置；blob 本身内容寻址，
   用 `rsync blobs/` 即可增量备份（`kb backup --blobs` 也可，硬链接优先）。
9. **恢复**：新机器 `kb init` → 还原 config.json → 恢复 kb.db/vectors/audit →
   `rsync` 回 blobs/ → `kb doctor && kb verify --limit 100`。

## 实测资源占用（本机，12 核/30G/无 GPU）

```
小库（<100 条目）:
  单次检索   0.2s 墙钟, 峰值 RSS 41.6MB（/usr/bin/time -v, 含 Python 启动）
生产库（38.6 万条目 / 134k 向量行 / 目录库 2.2GB）:
  BM25+向量+RRF     1.05-1.16s, RSS 82-92MB
  语义检索(+rerank) 2.8-4.2s,  RSS 740-900MB（rerank 网络往返 + 候选文本入内存）
摄取: 3000 文件 2.4s；幂等重跑 0.26s；批量嵌入 API 攒批 14x 提速
常驻进程:   无（CLI/MCP 均为按需拉起；kb serve-web/serve-mcp 为显式服务）
```

## 局限与后续

- 当前检索粒度是"文件/条目"级；超长文档建议拆分后入库（哈希去重保证无额外存储）。
- 音视频默认只索引元数据；配置 Whisper（`pip install faster-whisper`）后可在
  `extract.py:_inspect_av` 挂上转写。
- hash 嵌入是字面相似度；中文语义检索建议尽早切 API 或本地 bge。
- 向量为暴力检索（分块流式），百万摘要条目下单次向量路约 1-2s；再大可换 sqlite-vec。
