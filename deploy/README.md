# 部署与灾备 Runbook

面向运维执行，命令可直接复制。设计背景见 [ARCHITECTURE](../docs/ARCHITECTURE.md)
§13 降级矩阵 / §14 灾备场景。

## 1. 安装定时任务（每台部署机执行一次）

单元文件里的 `User=`、`Environment=KB_HOME=`、`ExecStart=` 路径按实际修改，然后：

```bash
sudo cp /home/miku/Bot/kb/deploy/kb-*.service /etc/systemd/system/
sudo cp /home/miku/Bot/kb/deploy/kb-*.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kb-backup.timer kb-verify.timer
# 需要自动摄取时再开（先确认 kb-watch.service 里的目录正确）
# sudo systemctl enable --now kb-watch.timer

systemctl list-timers 'kb-*'          # 确认下次触发时间
journalctl -u kb-backup.service -n 50 # 查看上次执行结果
```

- `kb-backup.timer`：每日备份（`--verify` 校验 + 保留 14 份）并清理 30 天前的回收站。
- `kb-verify.timer`：每周全量 SHA-256 复检，抓位腐（bit rot）。
- `kb-watch.timer`：每 15 分钟增量摄取 `~/inbox`（默认 `--review` 进审核队列）。
- 三者都是 `Nice=10/15 + IOSchedulingClass=idle`，不与前台任务抢 IO。

**离线校验清单（每月人工做一次，2 分钟）**：
```bash
kb doctor                      # quick_check / FTS / 缺 blob / 维度一致性
kb stats                       # 条目、待审核、容量、块数
ls -lt $KB_HOME/backups | head # 备份是否在按日增长
kb gaps --days 30              # 顺便看看该补什么资料
```

## 2. 灾备操作手册

### 2.1 误删条目（最常见）
```bash
grep '"op": *"delete"' $KB_HOME/logs/audit.jsonl | tail -20   # 确认删了什么
kb add /原始/路径                                              # blob 通常还在，重建索引
# 若 blob 也已 gc：kb restore $KB_HOME/backups/<日期> --yes
```

### 2.2 agent 批量污染
```bash
kb list --origin agent --limit 200        # 定位 agent 写入
kb reject <id> --yes                      # 或 kb rm <id> --yes --purge
kb restore $KB_HOME/backups/<污染前日期> --yes   # 大面积时整体回滚
```

### 2.3 kb.db 损坏（断电/磁盘异常）
```bash
kb doctor                                  # quick_check 非 ok 即确诊
kb restore $KB_HOME/backups/<最近一份> --yes
kb doctor && kb verify --limit 200        # 恢复后立即复检
# 无可用备份时的重建路线（blob 仍在）：
#   mv $KB_HOME/kb.db{,.broken} && kb init && kb import <上次 export.jsonl>
#   仍无 export 时：kb add 原始目录（重新编目，标签/审核状态会丢）
```

### 2.4 blob 位腐
```bash
kb verify --json > /tmp/verify.json        # corrupt/missing 列表
kb add /原始/路径 --force-hash             # 从源重新入库该文件
# 源已不存在：从带 blob 的备份取回
cp $KB_HOME/backups/<日期>/blobs/ab/cd/<hash> $KB_HOME/blobs/ab/cd/
kb verify                                  # 复检
```

### 2.5 误删整个 KB_HOME
```bash
kb init                                    # 建骨架
kb restore /备份所在/<日期> --yes          # 目录库+向量+审计
rsync -a /备份所在/<日期>/blobs/ $KB_HOME/blobs/   # 若备份含 blob
kb doctor && kb verify
```

### 2.6 迁移到新机器 / 更换 embedder
```bash
# 旧机
kb export /tmp/catalog.jsonl
rsync -a $KB_HOME/blobs/ newhost:/新KB_HOME/blobs/
scp /tmp/catalog.jsonl newhost:/tmp/
# 新机
kb init
kb config set embed.provider api    # 换 embedder 时才需要
kb import /tmp/catalog.jsonl
kb reembed                          # 换了 embedder 必跑
kb doctor && kb eval <金标>
```
`--in-place` 条目依赖源路径，新机路径不同的话需重新 `kb add`。

### 2.7 schema 升级失败
```bash
ls $KB_HOME/kb.db.pre-v*.bak               # 升级前自动快照
cp $KB_HOME/kb.db.pre-v4.bak $KB_HOME/kb.db   # 直接回退
rm -f $KB_HOME/kb.db-wal $KB_HOME/kb.db-shm
```

### 2.8 备份本身损坏
日常备份已带 `--verify`（quick_check + 条目计数），且 `--retention 14` 保留多份。
人工抽检：
```bash
sqlite3 $KB_HOME/backups/<日期>/kb.db 'PRAGMA quick_check; SELECT COUNT(*) FROM entries;'
```

## 3. 多机只读分发（不写代码的做法）

**原则：分发 `kb backup` 产出的一致性快照，不要 rsync 活动中的 KB_HOME**
（活目录的 kb.db + WAL 可能拷到撕裂状态）。

```bash
# 主机（写者）
kb backup /srv/kb-snapshot --blobs --verify
# 消费机（只读）
rsync -a --delete writer:/srv/kb-snapshot/ /var/lib/kb-replica/
KB_HOME=/var/lib/kb-replica kb doctor
KB_HOME=/var/lib/kb-replica kb search "..."    # 只读检索
```
消费机只做检索，不要写入（写入会在下次同步被覆盖）。需要 agent 远程访问时，
在消费机上跑 `kb serve-mcp`（经 SSH）或 `kb serve-web`（token 鉴权）。

## 4. 恢复演练（建议每季度一次，10 分钟）

真实发生过的教训：`restore` 曾有 WAL 写回 bug 导致"恢复被静默撤销"，
是补演练测试时才发现的。演练脚本化如下：

```bash
export KB_HOME=/tmp/kb-drill && rm -rf $KB_HOME
kb init && kb add ~/some-docs && kb backup --verify
ID=$(kb list --json | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["id"])')
kb rm $ID --yes
kb restore $KB_HOME/backups/$(ls -t $KB_HOME/backups | head -1) --yes
kb show $ID >/dev/null && echo "演练通过：条目已恢复" || echo "演练失败，需排查"
```