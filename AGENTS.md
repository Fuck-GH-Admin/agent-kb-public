# AGENTS.md —— 给在本仓库工作的编码 agent 的约定

## 项目定位
agent 本地知识库，长期运行的服务。每次改动都要以"稳定性 > 功能"为前提。

## 硬性约束（违反即返工）
0. **权限只在 policy.py 裁决**：任何访问面（CLI/MCP/Web/未来的 HTTP）都必须
   构造 Principal 并经 sql_scope/row_pass/resolve_write_path，禁止自行拼
   visibility/status 条件。新增可级联字段时同步块子条目（见已知陷阱）。
1. 运行依赖只允许 numpy + 标准库；可选依赖必须 guarded import 并有降级路径。
2. 不引入常驻服务进程；不监听端口。
3. 原始数据永不入库；SQLite 只存编目/索引层。
4. 一切写操作必须写审计（catalog.audit）；删除只能进 trash，禁止直接 unlink 内容。
5. 所有 SQL 参数化；FTS MATCH 由 util.fts_query 白名单构造。
6. entries 表新增列必须同时：
   - 加进 `store._SCHEMA` 与 `_ENTRY_COLS`（漏掉会导致落库静默丢字段——已发生过）；
   - 加进 `store._ensure_columns` 幂等迁移，并 bump `SCHEMA_VERSION`；
   - 同步 `search.to_hit` / CLI / MCP 输出；
   - 在 tests/e2e_test.py 补迁移用例。

## 常用命令
```bash
KB_HOME=/tmp/kb_home KB_TEST_DATA=/tmp/kb_data python3 tests/e2e_test.py   # 全量 e2e
./bin/kb --home /tmp/kb_home search "..."     # 手工验证
./bin/kb --home /tmp/kb_home doctor           # 改动后体检
./bin/kb --home /tmp/kb_home rechunk          # 改了分块算法后重建块
```

## 模块职责（新增代码前先确认放哪）
- `store.py` 存储与 schema（最核心，改动最少）；`ingest.py` 摄取与分块同步；
  `search.py` 三级降级检索；`admin.py` 备份/恢复/迁移；`curate.py` 整理；
  `eval.py` 评估与金标沉淀；`web.py`/`mcp_server.py` 两个访问面；
  `substrate.py` 上层组件 Capability API（设计见 ARCHITECTURE §22）；
  `core.py` 只做薄门面委派。
- 命令行是 `cli/` 包：`parser.py` 装配，子命令按 query/ingest/maint 三类分文件。
  新增子命令 = 在对应类别文件写 `cmd_xxx` + 在 parser.py 注册 + import。
- 新功能优先放进已有模块的对应职责区；`core.py` 只加委派方法，不写业务逻辑。

## 设计参考（本地克隆，勿提交）
kb-research/ 下有 mem0 / txtai / sqlite-vec / knowledge-mcp 浅克隆，用于对照设计。
借鉴要点已沉淀在 docs/ARCHITECTURE.md §5/§6/§8。

## 提交规范
- 每完成一个可验证的改进就 commit（用户要求防丢进度）。
- message 用一行中文，动词开头，注明影响面（如"检索: BM25 列加权"）。
- 提交前必须跑全量 e2e 且全部通过（当前 144 项，以 tests/e2e_test.py 实际输出为准）。
- 涉及安全/并发/数据完整性的改动，按 bkyexam-work/software-development-code-review.md
  的方法自审：追踪完整数据路径、检查失败路径与组合场景，而不只看新增代码。

## 已知陷阱
- numpy memmap 分片是稀疏文件，`ls` 表观大小虚高，统计用 st_blocks。
- 向量路可能对某些条目无结果（FTS-only 命中），融合处一律用 dict.get。
- FTS 外部内容表依赖触发器；绕过 Catalog 手工 INSERT 后必须 reindex_fts。
- Python 3.14 下 sentence-transformers 等重依赖未验证，保持 optional。
- 向量路永远返回最近邻：断言"检索不到"必须按"目标条目不在结果中"或
  "fts_rank 为空（无关键词命中）"写，不能写"结果为空"
  （曾因此放过 restore 的 WAL 写回 bug）。
- 任何"换掉 kb.db 文件"的操作（restore 等）必须先对活连接执行
  wal_checkpoint(TRUNCATE)，否则退出时 checkpoint 会把旧状态写回新文件。
- 块子条目与父条目共享 source_path：查父条目一律用 `find_parent_by_source`
  （`parent_id IS NULL` 过滤），用 find_by_source 会错把块当父条目。
- **块的审批/删除/可见性必须由父条目级联**：块携带父文档正文，漏级联
  visibility 会造成越权泄漏（真实发生过，见 DESIGN-V2 §C.4 R1）。
- 写锁是实例级可重入的（`kb._wlock`）：嵌套写路径必须共用同一实例，
  每次 new WriteLock 会导致自锁（真实发生过，R2）。
- 拼 SQL 条件列表时必须处理空列表：`" OR ".join([])` 会产出 `WHERE ()`
  语法错误（真实发生过，R3）。
- 组合特性（如分块 × 权限）要单独测：单看任一提交都发现不了跨特性缺陷。
