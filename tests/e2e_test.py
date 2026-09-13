#!/usr/bin/env python3
"""端到端测试：构造混合样例数据 → 摄取 → 去重/更新 → 检索 → 维护命令 → MCP 握手。
用法: KB_HOME=/tmp/kb_e2e_home python3 tests/e2e_test.py
"""
import json
import os

import numpy as np
import shutil
import subprocess
import sys
import tempfile
from time import time as _time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

HOME = os.environ.get("KB_HOME") or tempfile.mkdtemp(prefix="kb_home_")
DATA = os.environ.get("KB_TEST_DATA") or tempfile.mkdtemp(prefix="kb_data_")

passed = failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def make_data():
    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(os.path.join(DATA, "img"), exist_ok=True)
    with open(os.path.join(DATA, "ml_notes.md"), "w", encoding="utf-8") as f:
        f.write("# 机器学习学习笔记\n\n注意力机制是 Transformer 的核心。"
                "自注意力让模型在处理每个词时都能关注输入序列中的所有位置，"
                "通过查询、键、值三个矩阵实现。多头部注意力并行运行多个注意力函数。\n\n"
                "梯度下降优化器从 SGD 发展到 Adam，学习率调度对收敛速度影响很大。\n")
    with open(os.path.join(DATA, "recipes.txt"), "w", encoding="utf-8") as f:
        f.write("红烧肉做法。五花肉切块焯水，锅中放冰糖炒糖色，下肉翻炒上色，"
                "加料酒、生抽、老抽和开水，小火焖四十分钟，最后大火收汁。\n")
    with open(os.path.join(DATA, "transformer_intro.txt"), "w", encoding="utf-8") as f:
        f.write("The Transformer architecture replaced recurrence with self-attention. "
                "Each layer attends to all positions in the sequence, enabling much "
                "better parallelism during training than RNNs.\n")
    with open(os.path.join(DATA, "app.py"), "w", encoding="utf-8") as f:
        f.write("def add(a, b):\n    return a + b\n\n# 简单的加法工具函数\n")
    try:
        from PIL import Image
        Image.new("RGB", (64, 48), (30, 120, 200)).save(os.path.join(DATA, "img", "chart.png"))
    except ImportError:
        with open(os.path.join(DATA, "img", "chart.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + os.urandom(64))
    with open(os.path.join(DATA, "data.csv"), "w", encoding="utf-8") as f:
        f.write("date,value\n2026-01-01,10\n2026-01-02,12\n")
    with open(os.path.join(DATA, "blob.bin"), "wb") as f:
        f.write(os.urandom(1024))


def main():
    os.environ["KB_HOME"] = HOME
    make_data()
    from kb import KnowledgeBase

    print("== 1. 初始化与摄取 ==")
    shutil.rmtree(HOME, ignore_errors=True)
    kb = KnowledgeBase(HOME)
    st = kb.add(DATA, tags=["demo"])
    check("7 个文件全部新增", st["added"] == 7, str(st))
    check("无失败", st["failed"] == 0, str(st["errors"]))
    st2 = kb.add(DATA)
    check("重复摄取全部跳过(幂等)", st2["added"] == 0 and st2["skipped"] == 7, str(st2))

    print("== 2. 内容寻址去重 ==")
    shutil.copy(os.path.join(DATA, "app.py"), os.path.join(DATA, "app_copy.py"))
    st3 = kb.add(DATA)
    check("同内容新文件复用 blob", st3["added"] == 1, str(st3))
    row = kb.catalog.find_by_source(os.path.join(DATA, "app_copy.py"))[0]
    blob = kb.catalog.find_by_hash(row["content_hash"])
    check("两目录项共享同一 blob", len(blob) == 2 and blob[0]["blob"] == blob[1]["blob"])

    print("== 3. 更新（版本化） ==")
    with open(os.path.join(DATA, "recipes.txt"), "a", encoding="utf-8") as f:
        f.write("小贴士：加几颗山楂能让肉更快软烂。\n")
    st4 = kb.add(DATA)
    check("修改后重摄取触发更新", st4["updated"] == 1, str(st4))
    row = kb.catalog.find_by_source(os.path.join(DATA, "recipes.txt"))[0]
    check("版本号递增", row["version"] == 2, str(row["version"]))
    check("摘要包含新增内容", "山楂" in row["summary"] or "山楂" in row["preview"],
          row["summary"])

    print("== 4. 中文/英文检索 ==")
    r = kb.search("注意力机制")
    check("中文检索命中 ml_notes.md",
          r["results"] and r["results"][0]["title"].endswith("ml_notes.md") or
          (r["results"] and "ml_notes" in (r["results"][0]["source_path"] or "")),
          json.dumps([x["title"] for x in r["results"]], ensure_ascii=False))
    r2 = kb.search("self-attention parallelism")
    check("英文检索命中 transformer_intro.txt",
          r2["results"] and "transformer_intro" in (r2["results"][0]["source_path"] or ""),
          json.dumps([x["title"] for x in r2["results"]], ensure_ascii=False))
    r3 = kb.search("红烧肉")
    check("中文全文检索命中 recipes.txt",
          r3["results"] and "recipes" in (r3["results"][0]["source_path"] or ""),
          json.dumps([x["title"] for x in r3["results"]], ensure_ascii=False))

    print("== 5. 笔记与过滤 ==")
    eid = kb.note("会议纪要 2026-09", "讨论了知识库选型：决定用 SQLite+blob 方案，自己掌控。")
    r4 = kb.search("知识库选型", kind="note")
    check("note 类型过滤检索", r4["results"] and r4["results"][0]["id"] == eid)
    r5 = kb.search("红烧肉", tag="demo")
    check("标签过滤", r5["results"] and "recipes" in (r5["results"][0]["source_path"] or ""))
    r6 = kb.search("chart", kind="image")
    check("图片按文件名可检索", r6["results"] and r6["results"][0]["kind"] == "image",
          json.dumps([x["title"] for x in r6["results"]], ensure_ascii=False))

    print("== 6. 维护命令 ==")
    doc = kb.doctor()
    check("doctor: quick_check ok", doc["quick_check"] == "ok", str(doc))
    check("doctor: fts ok", doc["fts_integrity"] == "ok", str(doc["fts_integrity"]))
    ver = kb.verify()
    check("verify 全部完好", ver["checked"] >= 7 and not ver["corrupt"] and not ver["missing"],
          str(ver))
    stt = kb.stats()
    check("stats 条目数 9（7文件+副本+笔记）", stt["entries_total"] == 9,
          str(stt["entries_total"]))
    rep = kb.remove(eid, purge=True)
    g = kb.gc(commit=True)
    check("gc 把孤儿 blob 移入回收站", g["trashed"] >= 1, str(g))
    bk = kb.backup(verify=True)
    check("备份目录生成", os.path.exists(os.path.join(bk["dest"], "kb.db")))
    ri = kb.reindex_fts()
    check("FTS 重建后仍可检索",
          kb.search("红烧肉")["results"], "rebuild 后查询失败")
    kb.close()

    print("== 7. MCP stdio 握手与工具调用 ==")
    env = dict(os.environ, KB_HOME=HOME)
    env_viewer = dict(env, KB_ACCESS_LEVEL="viewer")
    proc = subprocess.Popen([sys.executable, "-m", "kb.mcp_server"], cwd=ROOT,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env_viewer, text=True)
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "kb_search", "arguments": {"query": "注意力机制", "limit": 3}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "kb_add", "arguments": {"path": "/tmp"}}},
    ]
    try:
        for m in msgs:
            proc.stdin.write(json.dumps(m, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        outs = []
        for _ in range(4):
            line = proc.stdout.readline()
            if not line:
                break
            outs.append(json.loads(line))
        by_id = {o.get("id"): o for o in outs}
        init = by_id.get(1, {}).get("result", {})
        check("initialize 返回 serverInfo",
              init.get("serverInfo", {}).get("name") == "agent-kb", str(init))
        tools = [t["name"] for t in
                 by_id.get(2, {}).get("result", {}).get("tools", [])]
        check("viewer 级别不暴露写工具（Capability 映射）",
              "kb_add" not in tools and "kb_search" in tools, str(tools))
        sr = by_id.get(3, {}).get("result", {})
        text = sr.get("content", [{}])[0].get("text", "")
        # viewer 只见 public：ml_notes 是 internal，返回空是正确的权限行为
        check("viewer kb_search 权限过滤（internal 不可见）",
              "[]" == text.strip() or "ml_notes" not in text, text[:120])
        add_resp = by_id.get(4, {}).get("result", {})
        check("viewer 调 kb_add 被拒（capability 或路径边界）",
              add_resp.get("isError") is True, str(add_resp)[:120])
    finally:
        proc.kill()

    print("== 8. 主题分区 / 审核队列 / 溯源 / snippet ==")
    kb2 = KnowledgeBase(HOME)
    with open(os.path.join(DATA, "pasta.txt"), "w", encoding="utf-8") as f:
        f.write("奶油培根意大利面做法：煮面，煎培根，蛋黄和芝士拌成酱，"
                "离火拌面，加面汤调节浓稠度。\n")
    kb2.add(os.path.join(DATA, "pasta.txt"), collection="cooking")
    rc = kb2.search("意大利面", collection="cooking")
    check("分区过滤命中 cooking",
          rc["results"] and "pasta" in (rc["results"][0]["source_path"] or ""))
    rd = kb2.search("意大利面", collection="default")
    check("default 分区查不到 cooking 内容",
          not rd["results"] or "pasta" not in (rd["results"][0]["source_path"] or ""),
          json.dumps([x["title"] for x in rd["results"]], ensure_ascii=False))
    check("FTS 命中带 snippet 片段", bool(rc["results"][0].get("snippet")))

    agent_note = kb2.note("agent 观察记录", "用户在晚间时段提问较多，建议缓存常用答案。",
                          collection="agent-notes", review=True, origin="agent")
    rh = kb2.search("观察记录")
    check("待审核条目默认不可检索",
          not rh["results"] or all(r["id"] != agent_note for r in rh["results"]))
    check("stats 统计 pending", kb2.stats()["pending"] >= 1)
    kb2.approve(agent_note)
    ra = kb2.search("观察记录")
    check("approve 后可检索", ra["results"] and ra["results"][0]["id"] == agent_note)
    check("origin 标记为 agent", ra["results"][0]["origin"] == "agent")
    agent_note2 = kb2.note("待拒笔记", "这条应该被拒绝。", review=True, origin="agent")
    kb2.reject(agent_note2)
    try:
        gone = kb2.get(agent_note2) is None
    except Exception:
        gone = True
    check("reject 后条目消失", gone)
    kb2.close()

    print("== 9. v1 → v2 迁移（旧库无损升级） ==")
    import sqlite3 as s3
    old_home = tempfile.mkdtemp(prefix="kb_v1_")
    conn = s3.connect(os.path.join(old_home, "kb.db"))
    v1_cols = ("id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',"
               "summary TEXT NOT NULL DEFAULT '', keywords TEXT NOT NULL DEFAULT '',"
               "source_path TEXT, blob TEXT NOT NULL DEFAULT '', mime TEXT NOT NULL DEFAULT '',"
               "ext TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0,"
               "meta_json TEXT NOT NULL DEFAULT '{}', preview TEXT NOT NULL DEFAULT '',"
               "content_hash TEXT NOT NULL DEFAULT '', in_place INTEGER NOT NULL DEFAULT 0,"
               "version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,"
               "updated_at TEXT NOT NULL, title_fts TEXT NOT NULL DEFAULT '',"
               "summary_fts TEXT NOT NULL DEFAULT '', keywords_fts TEXT NOT NULL DEFAULT '',"
               "preview_fts TEXT NOT NULL DEFAULT ''")
    conn.execute(f"CREATE TABLE entries ({v1_cols})")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES('schema_version','1')")
    conn.execute("INSERT INTO meta VALUES('created_at','2025-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO entries(id,kind,title,summary,content_hash,created_at,"
                 "updated_at,title_fts,summary_fts) VALUES('v1test0000000001','note',"
                 "'旧库笔记','迁移测试内容','','x','x','旧 库 笔 记','迁 移 测 试 内 容')")
    conn.commit()
    conn.close()
    kbv = KnowledgeBase(old_home)
    check("旧库打开自动升级 schema v6", kbv.catalog.get_meta("schema_version") == "6")
    hit = kbv.get("v1test0000000001")
    check("旧条目保留且分区为 default", hit is not None and
          hit["collection"] == "default" and hit["status"] == "active")
    kbv.reindex_fts()  # v1 手工灌的数据未进 FTS，重建后一致
    check("旧条目可检索", kbv.search("迁移测试")["results"])
    kbv.close()
    check("迁移前自动生成 .pre-v6.bak 快照",
          os.path.exists(os.path.join(old_home, "kb.db.pre-v6.bak")))

    print("== 10. 权限分级 / 快跳 / 备份策略 / 迁移 / 整理 / 降级 / eval ==")
    kb3 = KnowledgeBase(HOME)
    # -- 权限分级 --
    with open(os.path.join(DATA, "secret.txt"), "w", encoding="utf-8") as f:
        f.write("内部机密：root 密码策略为每季度轮换。\n")
    kb3.add(os.path.join(DATA, "secret.txt"), visibility="private")
    rv = kb3.search("root 密码", visible=("public",))
    check("viewer 看不到 private 条目", not rv["results"])
    rv2 = kb3.search("root 密码")  # CLI/admin 无限制
    check("admin 可见 private 条目", rv2["results"])
    sid = kb3.resolve_id(os.path.basename("secret") ) if False else None
    secret_id = kb3.catalog.find_by_source(os.path.join(DATA, "secret.txt"))[0]["id"]
    kb3.set_visibility(secret_id, "public")
    rv3 = kb3.search("root 密码", visible=("public",))
    check("改 public 后 viewer 可见", rv3["results"])

    # -- 快跳路径（size+mtime 未变则不重算哈希）与 --force-hash 揪出篡改 --
    p_mod = os.path.join(DATA, "recipes.txt")
    old_ns = os.stat(p_mod).st_mtime_ns
    with open(p_mod, "r+", encoding="utf-8") as f:  # 同长度篡改：五花肉→五化肉
        content = f.read()
        f.seek(0)
        f.write(content.replace("五花肉", "五化肉", 1))
    os.utime(p_mod, ns=(old_ns, old_ns))
    r_fast = kb3.add(p_mod)
    check("同长度篡改+还原mtime → 快路径跳过（预期行为）", r_fast["skipped"] == 1,
          str(r_fast))
    r_force = kb3.add(p_mod, force_hash=True)
    check("--force-hash 强制重算揪出篡改", r_force["updated"] == 1, str(r_force))

    # -- 备份保留策略与校验 --
    for _ in range(3):
        rep_b = kb3.backup(retention=2, verify=True)
    n_backups = len([d for d in os.listdir(kb3.dirs["backups"])])
    check("retention=2 保留最近两份", n_backups == 2, str(n_backups))
    check("备份校验通过", rep_b["verified"]["quick_check"] is True)

    # -- restore 灾备演练（审计补测：没恢复过的备份是薛定谔的备份） --
    bk_r = kb3.backup()
    victim = kb3.catalog.find_by_source(
        os.path.join(DATA, "transformer_intro.txt"))[0]["id"]
    kb3.remove(victim)
    res_rm = kb3.search("self-attention")
    check("删除后该条目不再出现",
          not any("transformer_intro" in (r.get("source_path") or "")
                  for r in res_rm["results"]),
          "(向量路总会返回最近邻，断言按条目判断)")
    kb3.restore(bk_r["dest"], force=True)
    kb3.close()
    kb3 = KnowledgeBase(HOME)  # 恢复覆盖了 kb.db，需重开拿新库
    check("restore 后条目与检索恢复",
          any("transformer_intro" in (r.get("source_path") or "")
              for r in kb3.search("self-attention parallelism")["results"]))

    # -- 导出/导入迁移（跨机模拟）--
    exp = os.path.join(HOME, "export.jsonl")
    kb3.export_entries(exp)
    new_home = tempfile.mkdtemp(prefix="kb_mig_")
    import shutil as _sh
    _sh.copytree(os.path.join(HOME, "blobs"), os.path.join(new_home, "blobs"))
    kb_new = KnowledgeBase(new_home)
    rep_i = kb_new.import_entries(exp)
    check("迁移导入条目数一致",
          rep_i["imported"] + rep_i["updated"] >= kb3.stats()["entries_total"] - 1,
          json.dumps(rep_i, ensure_ascii=False))
    check("迁移后检索可用", kb_new.search("红烧肉")["results"])
    kb_new.close()

    # -- 整理（离线档）--
    kb3.catalog.update_entry(secret_id, {"summary": "", "keywords": ""})
    kb3.catalog.set_tags(secret_id, [" Foo ", "foo", "bar"])
    rep_c = kb3.curate(apply=True)
    row_c = kb3.catalog.get(secret_id)
    check("空摘要被补齐", bool(row_c["summary"]))
    check("标签规范化去重", set(kb3.catalog.tags_of(secret_id)) == {"foo", "bar"},
          str(kb3.catalog.tags_of(secret_id)))

    # -- 第三级降级：FTS 损坏时 LIKE 扫描兜底 --
    orig_fts = kb3.catalog.search_fts
    kb3.catalog.search_fts = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    r_dg = kb3.search("红烧肉")
    kb3.catalog.search_fts = orig_fts
    check("FTS 不可用时降级 LIKE 仍有结果", r_dg["results"] and r_dg["degraded"],
          json.dumps([x["title"] for x in r_dg["results"]], ensure_ascii=False))

    # -- eval 金标评估 --
    qfile = os.path.join(HOME, "queries.jsonl")
    with open(qfile, "w", encoding="utf-8") as f:
        f.write(json.dumps({"query": "红烧肉", "relevant": ["recipes"]},
                           ensure_ascii=False) + "\n")
        f.write(json.dumps({"query": "注意力机制", "relevant": ["ml_notes"]},
                           ensure_ascii=False) + "\n")
    rep_e = kb3.evaluate(qfile, k=5)
    check("金标全命中 recall=1", rep_e["recall@k_avg"] == 1.0, str(rep_e))
    kb3.close()

    # -- Web 面板 --
    print("== 11. Web 操作面板 ==")
    kb4 = KnowledgeBase(HOME)
    kb4.cfg.set("access.tokens", [{"token": "tok-op", "name": "op", "level": "operator"},
                                  {"token": "tok-view", "name": "v", "level": "viewer"}])
    kb4.close()
    import threading
    import urllib.request
    from kb.web import serve_web
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    th = threading.Thread(
        target=serve_web, args=(HOME,), kwargs={"port": port, "host": "127.0.0.1"},
        daemon=True)
    th.start()
    import time as _t
    _t.sleep(0.6)
    base = f"http://127.0.0.1:{port}"
    try:
        r0 = urllib.request.urlopen(f"{base}/?token=tok-op", timeout=5)
        check("面板带 token 可访问", r0.status == 200 and "agent-kb" in
              r0.read().decode())
        r1 = urllib.request.urlopen(f"{base}/search?q=%E7%BA%A2%E7%83%A7%E8%82%89&token=tok-op",
                                    timeout=5)
        check("面板可检索", "recipes" in r1.read().decode())
        try:
            urllib.request.urlopen(f"{base}/", timeout=5)
            unauthorized = False
        except urllib.error.HTTPError as e:
            unauthorized = e.code == 401
        check("无 token 返回 401", unauthorized)
        rv_ = urllib.request.urlopen(f"{base}/?token=tok-view", timeout=5)
        check("viewer 可进面板", rv_.status == 200)
    except Exception as e:
        check("Web 面板异常", False, str(e))

    print("== 12. batch-1：分块 / watch / --link / 缺口 / 金标 / trash老化 ==")
    kb5 = KnowledgeBase(HOME)
    # -- 分块索引：文档须超过 4KB 预览上限，才能演示块化前后的差别 --
    long_path = os.path.join(DATA, "long_doc.md")
    tail_note = "尾注：ZK 共识协议使用拜占庭容错，需要三分之二节点保持诚实。\n"
    with open(long_path, "w", encoding="utf-8") as f:
        f.write("# 长文档\n\n" + ("填充段落，介绍分布式系统与一致性模型的背景知识。\n\n" * 400)
                + tail_note)
    kb5.add(long_path)  # 不开块
    r_nochunk = kb5.search("拜占庭容错")
    # 注意：向量路总返回最近邻，长文档可能作为语义近邻出现；
    # 真正要验证的是"关键词没命中"（fts_rank 为空），即尾注不在索引里。
    check("未块化时尾注（>4KB 外）不产生关键词命中",
          not any("long_doc" in (r.get("source_path") or "")
                  and r.get("fts_rank") for r in r_nochunk["results"]),
          json.dumps([(x["title"], x["fts_rank"]) for x in r_nochunk["results"]],
                     ensure_ascii=False))
    rc1 = kb5.rechunk()
    check("rechunk 生成块", rc1["chunks"] >= 2, str(rc1))
    r_chunk = kb5.search("拜占庭容错")
    hit_c = next((r for r in r_chunk["results"]
                  if r.get("parent_id") and r.get("fts_rank")), None)
    check("块化后尾注产生关键词命中且带块定位",
          hit_c is not None and hit_c["chunk_no"] > 0,
          json.dumps([(x["title"], x["fts_rank"], x.get("chunk_no"))
                      for x in r_chunk["results"]], ensure_ascii=False))
    if hit_c:
        check("块命中时父条目被压制",
              not any(r["id"] == hit_c["parent_id"] for r in r_chunk["results"]))
        p = kb5.get(hit_c["parent_id"])
        check("块可回溯父条目", p is not None and "long_doc" in p["source_path"])
    rc2 = kb5.add(long_path, chunk=True)  # 内容未变 → 快跳，块不动
    check("同内容重摄取跳过且不重建块", rc2["skipped"] == 1)

    # -- kb watch --once（cron 模式）--
    with open(os.path.join(DATA, "watch_new.txt"), "w", encoding="utf-8") as f:
        f.write("watch 模式新增的文件内容，关于 ZooKeeper 的选主机制。\n")
    import subprocess as _sp
    wr = _sp.run([sys.executable, "-m", "kb.cli", "watch", DATA, "--once",
                  "--collection", "watched"],
                 capture_output=True, text=True,
                 env=dict(os.environ, KB_HOME=HOME), cwd=ROOT)
    check("watch --once 增量摄取", wr.returncode == 0 and "新增 1" in wr.stdout,
          wr.stdout + wr.stderr)

    # -- --link 硬链接摄取 --
    lp = os.path.join(DATA, "link_target.txt")
    with open(lp, "w", encoding="utf-8") as f:
        f.write("硬链接摄取的文件。\n")
    kb5.add(lp, link=True)
    import stat as _stat
    blob_of = kb5.catalog.find_parent_by_source(os.path.abspath(lp))["blob"]
    nlink = os.stat(kb5.blobs.path(blob_of)).st_nlink
    check("硬链接落库且源文件仍在", os.path.exists(lp) and nlink >= 2, str(nlink))

    # -- 查询日志 → 缺口报告 → 真实金标 --
    kb5.search("根本不存在的量子啤酒")          # 零相关查询 → 缺口
    kb5.search("红烧肉")                        # 真实查询
    top = kb5.search("红烧肉")["results"][0]
    kb5.get(top["id"])                          # agent 跟进 → 沉淀金标
    g = kb5.gaps(days=365, min_score=0.02)
    check("缺口报告捕获零结果查询",
          any("量子啤酒" in x["query"] for x in g["gaps"]),
          json.dumps(g["gaps"], ensure_ascii=False)[:200])
    gf = os.path.join(HOME, "auto_goldens.jsonl")
    kg = kb5.goldens_from_log(gf)
    check("真实金标沉淀 >=1 条", kg["goldens"] >= 1, str(kg))
    if kg["goldens"]:
        ev = kb5.evaluate(gf, k=5)
        check("真实金标可评估", ev["queries"] == kg["goldens"], str(ev))

    # -- trash 30 天老化 --
    kb5.remove(kb5.catalog.find_parent_by_source(os.path.abspath(lp))["id"],
               purge=True)
    old_trash = os.path.join(kb5.dirs["trash"], "aged_test")
    with open(old_trash, "w") as f:
        f.write("x")
    past = _time() - 40 * 86400
    os.utime(old_trash, (past, past))
    g30 = kb5.gc(empty_trash=True, older_than=30)
    check("老化清理只删 30 天前文件",
          not os.path.exists(old_trash)
          and os.listdir(kb5.dirs["trash"]) != [], str(g30))
    g_all = kb5.gc(empty_trash=True)
    check("不带 older-than 清空全部", g_all["trash_files_removed"] >= 1)
    kb5.close()

    print("== 13. batch-2：P0 安全/并发 + P1 质量 + 缺失件 ==")
    kb6 = KnowledgeBase(HOME)

    # -- C1 MCP 写路径白名单（默认 $HOME 非隐藏目录）--
    from kb.policy import WritePathDenied, resolve_write_path
    denied = []
    for bad in ("/etc/shadow", "/proc/self/environ",
                os.path.expanduser("~/.ssh"), os.path.expanduser("~/../etc")):
        try:
            resolve_write_path(bad, [])
            denied.append((bad, "未拦截"))
        except WritePathDenied:
            pass
    check("C1 系统路径/隐藏目录/穿越全部拒绝", not denied, str(denied))
    ok_path = os.path.expanduser("~")
    check("C1 $HOME 正常目录放行",
          resolve_write_path(ok_path, []) == os.path.realpath(ok_path))
    check("C1 显式白名单外拒绝",
          _raises(WritePathDenied, resolve_write_path, DATA, ["/nonexistent-root"]))
    check("C1 显式白名单内放行", resolve_write_path(DATA, [DATA]) == os.path.realpath(DATA))

    # -- C1 端到端：MCP 只读拒绝 + 写模式下越权路径被挡 --
    env_w = dict(os.environ, KB_HOME=HOME, KB_ALLOW_WRITE="1")
    proc = subprocess.Popen([sys.executable, "-m", "kb.mcp_server"], cwd=ROOT,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env_w, text=True)
    try:
        for m in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "kb_add", "arguments": {"path": "/etc"}}}):
            proc.stdin.write(json.dumps(m) + "\n")
        proc.stdin.flush()
        outs = [json.loads(proc.stdout.readline()) for _ in range(2)]
        r = {o.get("id"): o for o in outs}.get(2, {}).get("result", {})
        text = r.get("content", [{}])[0].get("text", "")
        check("C1 MCP 写通道拒绝 /etc", r.get("isError") is True and "拒绝" in text,
              str(r)[:200])
    finally:
        proc.kill()

    # -- C3 并发写互斥 --
    from kb.util import WriteLock
    lock_a = WriteLock(HOME)
    lock_a.__enter__()
    check("C3 第二个写者被拒绝（flock）",
          _raises(RuntimeError, lambda: WriteLock(HOME).__enter__()))
    lock_a.__exit__()
    with WriteLock(HOME):  # 释放后可再获取
        pass
    check("C3 锁释放后可重新获取", True)

    # -- C2 向量过滤下推：小分区不被全局 top-N 淹没 --
    noise_dir = os.path.join(DATA, "noise")
    os.makedirs(noise_dir, exist_ok=True)
    for i in range(60):
        with open(os.path.join(noise_dir, f"n{i}.txt"), "w", encoding="utf-8") as f:
            f.write(f"噪声文档 {i}：与查询无关的填充内容，用于挤占全局候选位。\n")
    kb6.add(noise_dir, collection="noise")
    rare_dir = os.path.join(DATA, "rare")
    os.makedirs(rare_dir, exist_ok=True)
    with open(os.path.join(rare_dir, "rare.txt"), "w", encoding="utf-8") as f:
        f.write("稀有分区文档：讨论量子退火算法在组合优化中的应用。\n")
    kb6.add(rare_dir, collection="rare")
    kb6.cfg.set("search.vec_candidates", 5)   # 极端收紧全局候选，放大后过滤缺陷
    r_scoped = kb6.search("量子退火", collection="rare")
    check("C2 小分区检索有向量路命中（过滤下推）",
          any(h.get("vec_rank") for h in r_scoped["results"]),
          json.dumps([(h["title"], h["vec_rank"]) for h in r_scoped["results"]],
                     ensure_ascii=False))
    kb6.cfg.set("search.vec_candidates", 200)

    # -- C4 stats 不把块算进资料条目 --
    st6 = kb6.stats()
    check("C4 entries_total 不含块",
          st6["entries_total"] + st6["chunks"] == st6["rows_total"],
          f"entries={st6['entries_total']} chunks={st6['chunks']} rows={st6['rows_total']}")

    # -- C5 内容嗅探：.ts 是 TypeScript 不是视频 --
    ts_path = os.path.join(DATA, "loader.ts")
    with open(ts_path, "w", encoding="utf-8") as f:
        f.write("export function parseConfig(raw: string): Config {\n"
                "  return JSON.parse(raw);\n}\nexport class Loader {\n  load() {}\n}\n")
    kb6.add(ts_path)
    ts_row = kb6.catalog.find_parent_by_source(os.path.abspath(ts_path))
    check("C5 .ts 判为 code 而非 video", ts_row["kind"] == "code", ts_row["kind"])
    # -- C8 代码摘要是签名而非正文前几行 --
    check("C8 代码摘要抽出函数/类签名",
          "parseConfig" in ts_row["summary"] and "return JSON.parse" not in ts_row["summary"],
          ts_row["summary"])

    # -- C6 中文 snippet 无逐字空格 --
    r_cn = kb6.search("红烧肉")
    snip = next((h.get("snippet") for h in r_cn["results"]
                 if "recipes" in (h.get("source_path") or "")), None)
    check("C6 中文 snippet 取自原文（无逐字空格）",
          snip is not None and "红烧肉" in snip and "红 烧 肉" not in snip, repr(snip))

    # -- C9 日志轮转 --
    from kb.util import append_jsonl
    log_p = os.path.join(kb6.dirs["logs"], "rotate_test.jsonl")
    for _ in range(300):
        append_jsonl(log_p, {"pad": "x" * 4000}, max_mb=1, keep=2)
    check("C9 日志按大小轮转", os.path.exists(log_p + ".1")
          and os.path.getsize(log_p) < 2 * 1024 * 1024,
          f"size={os.path.getsize(log_p)}")

    # -- D2 审计带主体 --
    kb_p = KnowledgeBase(HOME, principal=__import__(
        "kb.policy", fromlist=["Principal"]).Principal(level="operator", name="web:alice"))
    nid = kb_p.note("审计主体测试", "检查 audit 是否记录 principal。")
    kb_p.close()
    with open(os.path.join(kb6.dirs["logs"], "audit.jsonl"), encoding="utf-8") as f:
        audit_lines = [json.loads(x) for x in f if x.strip()]
    check("D2 审计记录 principal",
          any(a.get("principal") == "web:alice" and a.get("id") == nid
              for a in audit_lines[-20:]),
          str(audit_lines[-3:])[:200])

    # -- R1 可见性级联到块（防止经块读到 private 文档正文）--
    leak_doc = os.path.join(DATA, "confidential.md")
    with open(leak_doc, "w", encoding="utf-8") as f:
        f.write("# 机密文档\n\n" + ("无关填充段落。\n\n" * 400)
                + "尾部机密：数据库 root 口令为 hunter2-prod。\n")
    kb6.add(leak_doc, chunk=True, visibility="internal")
    leak_id = kb6.catalog.find_parent_by_source(os.path.abspath(leak_doc))["id"]
    kb6.set_visibility(leak_id, "private")
    r_leak = kb6.search("root 口令 hunter2", visible=("public", "internal"))
    check("R1 父转 private 后块不再泄漏正文",
          not any("confidential" in (h.get("source_path") or "")
                  for h in r_leak["results"]),
          json.dumps([(h["visibility"], (h.get("snippet") or "")[:40])
                      for h in r_leak["results"]], ensure_ascii=False))
    check("R1 admin 仍可见该文档",
          any("confidential" in (h.get("source_path") or "")
              for h in kb6.search("root 口令 hunter2")["results"]))

    # -- R2 嵌套写路径不自锁（reject → remove 都持有同一把可重入锁）--
    nested = kb6.note("嵌套写测试", "reject 内部会再调用 remove。", review=True)
    try:
        kb6.reject(nested)
        nested_ok = True
    except RuntimeError as e:
        nested_ok = f"写锁自锁: {e}"
    check("R2 嵌套写路径不自锁", nested_ok is True, str(nested_ok))

    # -- R3 特殊查询不崩溃（"*"/纯标点会让 LIKE 降级路拼出空条件）--
    crashed = []
    for q in ("*", "\") OR 1=1--", "   ", "。。。", "NEAR/2", "\"unclosed", "^%$#@!"):
        try:
            kb6.search(q, limit=2)
        except Exception as e:
            crashed.append((q, str(e)[:60]))
    check("R3 特殊/畸形查询不抛异常", not crashed, str(crashed))

    # -- R4 损坏 blob：verify 能报告，检索不受影响 --
    okp = os.path.join(DATA, "verify_target.txt")
    with open(okp, "w", encoding="utf-8") as f:
        f.write("用于校验损坏检测的内容。\n")
    kb6.add(okp)
    tgt = kb6.catalog.find_parent_by_source(os.path.abspath(okp))
    with open(kb6.blobs.path(tgt["blob"]), "w") as f:
        f.write("CORRUPTED")
    ver6 = kb6.verify()
    check("R4 verify 检出 blob 损坏", tgt["blob"] in ver6["corrupt"], str(ver6)[:120])
    check("R4 损坏后检索仍可用（索引在目录库）",
          bool(kb6.search("校验损坏检测")["results"]))

    # -- D3 kb drill 灾备演练 --
    drill = kb6.drill()
    check("D3 灾备演练全链路通过", drill["ok"],
          json.dumps(drill["steps"], ensure_ascii=False))

    # -- D1 告警：未配置时不投递但留痕；配置后可投递 --
    check("D1 未配置 webhook 时不投递", kb6.notify("test", {}) is False)
    import http.server
    import threading as _th
    received = []

    class _Sink(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(
                int(self.headers.get("Content-Length") or 0)).decode())
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Sink)
    _th.Thread(target=srv.serve_forever, daemon=True).start()
    kb6.cfg.set("alerts.webhook", f"http://127.0.0.1:{srv.server_port}/hook")
    ok_notify = kb6.notify("verify_failed", {"corrupt": 1})
    srv.shutdown()
    check("D1 告警可投递并带事件内容",
          ok_notify and received and "verify_failed" in received[0], str(received)[:150])
    kb6.cfg.set("alerts.webhook", "")
    kb6.close()

    print("== 14. batch-3：杂类数据类型（模型/Office/ipynb/git/分区保留）==")
    import struct
    import zipfile
    kb7 = KnowledgeBase(HOME)
    misc = os.path.join(DATA, "misc")
    os.makedirs(misc, exist_ok=True)

    # -- 模型权重：safetensors 头部元数据，绝不加载权重本体 --
    hdr = json.dumps({"w1": {"dtype": "F32", "shape": [2, 3],
                             "data_offsets": [0, 24]},
                      "__metadata__": {"architecture": "llama"}}).encode()
    with open(os.path.join(misc, "m.safetensors"), "wb") as f:
        f.write(struct.pack("<Q", len(hdr)) + hdr + b"\x00" * 24)
    with open(os.path.join(misc, "legacy.pt"), "wb") as f:
        f.write(b"\x80\x02" + os.urandom(32))   # pickle-legacy
    kb7.add(misc)
    st_row = kb7.catalog.find_parent_by_source(
        os.path.join(misc, "m.safetensors"))
    check("模型判为 kind=model 且头部元数据入摘要",
          st_row["kind"] == "model" and "llama" in st_row["summary"]
          and "1个张量" in st_row["summary"], st_row["summary"])
    pt_row = kb7.catalog.find_parent_by_source(os.path.join(misc, "legacy.pt"))
    check("legacy pickle 模型带安全警告", "任意代码" in pt_row["summary"],
          pt_row["summary"])

    # -- Office（zip 容器）抽正文 --
    with zipfile.ZipFile(os.path.join(misc, "r.docx"), "w") as z:
        z.writestr("word/document.xml",
                   "<w:p><w:t>项目验收结论：全部指标达成。</w:t></w:p>")
    kb7.add(os.path.join(misc, "r.docx"))
    r_docx = kb7.search("项目验收结论")
    check("docx 正文可检索（不再判 archive）",
          any("r.docx" in (h.get("source_path") or "") and h["kind"] == "text"
              for h in r_docx["results"]),
          json.dumps([(h["title"], h["kind"]) for h in r_docx["results"]],
                     ensure_ascii=False))

    # -- ipynb 抽 markdown+code --
    nb = {"cells": [
        {"cell_type": "markdown", "source": ["# 调参记录\n", "学习率热身很关键。"]},
        {"cell_type": "code", "source": ["def warmup(step):\n", "    return step/100"]},
        {"cell_type": "code", "source": [""], "outputs": [{"data": {
            "image/png": "iVBORw0KGgo=" * 100}}]},   # base64 输出不应进索引
    ]}
    with open(os.path.join(misc, "tune.ipynb"), "w", encoding="utf-8") as f:
        json.dump(nb, f, ensure_ascii=False)
    kb7.add(os.path.join(misc, "tune.ipynb"))
    r_nb = kb7.search("学习率热身")
    nb_row = kb7.catalog.find_parent_by_source(os.path.join(misc, "tune.ipynb"))
    check("ipynb 单元可检索且 base64 输出不进索引",
          any("tune.ipynb" in (h.get("source_path") or "") for h in r_nb["results"])
          and "iVBORw0KGgo" not in nb_row["preview"], nb_row["preview"][:80])

    # -- git 仓库：.git 默认排除，工作区代码正常入库 --
    repo = os.path.join(misc, "repo")
    os.makedirs(os.path.join(repo, ".git", "objects"), exist_ok=True)
    with open(os.path.join(repo, ".git", "objects", "blob1"), "wb") as f:
        f.write(b"\x78\x9c" + os.urandom(32))
    with open(os.path.join(repo, "train.py"), "w") as f:
        f.write("def fit(model):\n    return model\n")
    rep_git = kb7.add(repo)
    git_indexed = kb7.catalog.find_by_source(
        os.path.join(repo, ".git", "objects", "blob1"))
    check("git 内部对象默认排除、工作区代码入库",
          not git_indexed and rep_git["added"] == 1, str(rep_git))

    # -- 重摄取不重置分区（b3-3 修复回归）--
    ptxt = os.path.join(misc, "paper.txt")
    with open(ptxt, "w", encoding="utf-8") as f:
        f.write("论文初稿。")
    kb7.add(ptxt, collection="papers")
    with open(ptxt, "a", encoding="utf-8") as f:
        f.write("二稿修订。")
    kb7.add(ptxt)   # 不指定 collection
    prow = kb7.catalog.find_parent_by_source(os.path.abspath(ptxt))
    check("更新时未指定 collection 则保留原分区",
          prow["collection"] == "papers" and prow["version"] == 2,
          f"collection={prow['collection']} v={prow['version']}")
    kb7.close()

    print("== 15. 真实数据回归：CJK 意译查询 / 截断可配 / 大文件模式 ==")
    kb8 = KnowledgeBase(HOME)

    # -- CJK 长查询的相邻二元组策略：意译文本可命中 --
    # 注：≤4 字的段仍按整短语精确匹配（生产原文即"多年以后"）；
    # 这里验证的是 >4 字段"面对行刑队"能命中"站在行刑队面前"这类意译。
    para = os.path.join(DATA, "paraphrase.txt")
    with open(para, "w", encoding="utf-8") as f:
        f.write("多年以后，上校站在行刑队面前，回忆起那个遥远的下午，天空布满灰云。\n")
    kb8.add(para, chunk=True)
    r_p = kb8.search("多年以后 面对行刑队")
    check("意译查询命中（相邻二元组 OR）",
          any("paraphrase" in (h.get("source_path") or "") and h.get("fts_rank")
              for h in r_p["results"]),
          json.dumps([(h["title"], h["fts_rank"]) for h in r_p["results"]],
                     ensure_ascii=False))
    # 短语语义不回归：≤4 字 CJK 串仍按整短语精确匹配
    short = os.path.join(DATA, "phrase.txt")
    with open(short, "w", encoding="utf-8") as f:
        f.write("方鸿渐在欧洲留学四年，最后拿到一张克莱登大学的假文凭。\n")
    kb8.add(short)
    r_s = kb8.search("方鸿渐 留学")
    check("≤4字短语匹配不回归",
          any("phrase" in (h.get("source_path") or "") and h.get("fts_rank")
              for h in r_s["results"]))

    # -- max_text_kb 截断可配：放开后重摄取可见尾部内容 --
    big_t = os.path.join(DATA, "truncated_doc.txt")
    with open(big_t, "w", encoding="utf-8") as f:
        f.write("开头标记。" + ("正文填充内容。" * 1500) + "尾部独有词：星辰大海。\n")
    kb8.cfg.set("ingest.max_text_kb", 1)
    kb8.add(big_t, chunk=True)
    r_t1 = kb8.search("星辰大海")
    check("1KB 上限时尾部内容不可检索",
          not any("truncated_doc" in (h.get("source_path") or "")
                  and h.get("fts_rank") for h in r_t1["results"]))
    kb8.cfg.set("ingest.max_text_kb", 64)
    kb8.add(big_t, chunk=True, force_hash=True)
    r_t2 = kb8.search("星辰大海")
    check("放开 max_text_kb 并 force-hash 后尾部可检索",
          any("truncated_doc" in (h.get("source_path") or "")
              and h.get("fts_rank") for h in r_t2["results"]))
    kb8.cfg.set("ingest.max_text_kb", 256)

    # -- B1：max_file_mb 只约束复制模式，不拦 in-place/link --
    big_b = os.path.join(DATA, "big_model.bin")
    with open(big_b, "wb") as f:
        f.seek(2 * 1024 * 1024)
        f.write(b"\0")
    kb8.cfg.set("ingest.max_file_mb", 1)   # 1MB
    r_b1 = kb8.add(big_b)
    check("B1 复制模式超限仍被拒",
          r_b1["failed"] == 1 and any("复制上限" in e for e in r_b1["errors"]),
          str(r_b1["errors"])[:120])
    r_b2 = kb8.add(big_b, in_place=True)
    check("B1 in-place 不受复制上限限制", r_b2["added"] == 1, str(r_b2))
    link_b = os.path.join(DATA, "link_model.bin")
    with open(link_b, "wb") as f:
        f.seek(2 * 1024 * 1024)
        f.write(b"\0")
    r_b3 = kb8.add(link_b, link=True)
    check("B1 link 不受复制上限限制且零复制",
          r_b3["added"] == 1 and r_b3["bytes_stored"] == 0, str(r_b3))
    kb8.cfg.set("ingest.max_file_mb", 512)
    kb8.close()

    print("== 16. P1：token 哈希生命周期 / 面板哈希登录 ==")
    kb9 = KnowledgeBase(HOME)
    from kb.tokens import (add_token, lookup_token, migrate_tokens,
                           needs_migration, revoke_token, rotate_token)
    # add -> 只存哈希
    t1 = add_token(kb9.cfg, "p1user", "operator")
    toks = kb9.cfg.get("access.tokens")
    new_e = next(e for e in toks if e.get("name") == "p1user")
    check("C12 新条目只存哈希",
          "hash" in new_e and "token" not in new_e, str(new_e))
    check("C12 lookup 命中", lookup_token(kb9.cfg, t1) == ("operator", "p1user"))
    check("C12 错 token 拒绝", lookup_token(kb9.cfg, t1[:-1] + "x") is None)
    # rotate -> 旧失效新有效
    t2 = rotate_token(kb9.cfg, "p1user")
    check("C12 rotate 后旧 token 失效", lookup_token(kb9.cfg, t1) is None)
    check("C12 rotate 后新 token 有效", lookup_token(kb9.cfg, t2) == ("operator", "p1user"))
    # 明文兼容读取 + migrate
    kb9.cfg.set("access.tokens", [{"token": "plain-legacy", "name": "legacy",
                                   "level": "viewer"}])
    check("C12 明文兼容登录", lookup_token(kb9.cfg, "plain-legacy") == ("viewer", "legacy"))
    check("C12 needs_migration 检出明文", needs_migration(kb9.cfg) is True)
    n = migrate_tokens(kb9.cfg)
    check("C12 migrate 移除明文条目", n == 1 and
          lookup_token(kb9.cfg, "plain-legacy") is None)
    revoke_token(kb9.cfg, "p1user")
    kb9.close()

    # 面板端到端：哈希 token 登录
    import socket as _socket
    kb_w = KnowledgeBase(HOME)
    web_tok = add_token(kb_w.cfg, "panel", "admin")
    kb_w.close()
    from kb.web import serve_web
    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        wport = s.getsockname()[1]
    _th.Thread(target=serve_web, args=(HOME,),
               kwargs={"port": wport, "host": "127.0.0.1"}, daemon=True).start()
    _t.sleep(0.6)
    import urllib.request
    base = f"http://127.0.0.1:{wport}"
    ok200 = False
    for _ in range(2):  # 两次请求同线程复用实例
        r = urllib.request.urlopen(f"{base}/?token={web_tok}", timeout=5)
        ok200 = r.status == 200
    check("P1 面板哈希 token 登录且复用实例无异常", ok200)
    r2 = urllib.request.urlopen(
        f"{base}/search?q=%E7%BA%A2%E7%83%A7%E8%82%89&token={web_tok}", timeout=5)
    check("P1 面板检索正常", "recipes" in r2.read().decode())
    try:
        urllib.request.urlopen(f"{base}/?token={web_tok[:-1]}x", timeout=5)
        check("P1 错 token 401", False)
    except urllib.error.HTTPError as e:
        check("P1 错 token 401", e.code == 401)

    print("== 17. LLM 中转接入：编目链路/故障转移/解析器 ==")
    kb10 = KnowledgeBase(HOME)
    # 单元级：_parse_json_obj 各种模型输出形态
    from kb.summarize import _parse_json_obj
    check("解析 ```json 块", _parse_json_obj(
        '好的，这是结果：\n```json\n{"title": "T", "summary": "S", "keywords": ["k"]}\n```') ==
        {"title": "T", "summary": "S", "keywords": ["k"]})
    check("解析裸 JSON", _parse_json_obj('{"title":"a","summary":"b"}') is not None)
    check("JSON 前后噪声容忍", _parse_json_obj('说明文字 {"title":"x","summary":"y"} 后缀') is not None)
    check("无 JSON 返回 None", _parse_json_obj("完全没有结构化内容") is None)

    # LLM 链路（在线集成：走本地中转，不可用时跳过）
    api = os.environ.get("KB_TEST_LLM_BASE")
    key = os.environ.get("KB_TEST_LLM_KEY")
    if api and key:
        from kb.summarize import LLM
        llm = LLM(api, key, "qwen3.8-flash,mimo-v2.5,hy3,glm-5.3-flash")
        out = llm.chat_json(
            "你是知识库编目员。只输出 JSON："
            '{"title":"...","summary":"不超过60字","keywords":["k1","k2"]}。',
            "文件名: unit.txt\n注意力机制是 Transformer 的核心。", 100)
        check("LLM 编目返回结构化结果",
              bool(out) and out.get("title") and out.get("summary") is not None, str(out))
    else:
        print("  SKIP  LLM 在线编目（未设 KB_TEST_LLM_BASE/KEY）")
    kb10.close()

    print("== 18. 语义嵌入与 rerank（在线集成，可跳过） ==")
    kb11 = KnowledgeBase(HOME)
    emb_base = os.environ.get("KB_TEST_EMB_BASE")
    emb_key = os.environ.get("KB_TEST_EMB_KEY")
    if emb_base and emb_key:
        from kb.embed import ApiEmbedder
        emb = ApiEmbedder(emb_base, emb_key, "BAAI/bge-m3")
        vecs = emb.embed(["注意力机制", "attention mechanism"])
        check("bge-m3 返回 1024 维且归一化",
              vecs.shape == (2, 1024)
              and abs(float(np.linalg.norm(vecs[0])) - 1.0) < 1e-3,
              f"{vecs.shape}")
        # 中英同义向量相似度应显著高于无关对
        sim_rel = float(vecs[0] @ vecs[1])
        v_other = emb.embed(["红烧肉的做法"])[0]
        sim_irr = float(vecs[0] @ v_other)
        check("语义区分度：同义对 > 无关对", sim_rel > sim_irr + 0.1,
              f"rel={sim_rel:.3f} irr={sim_irr:.3f}")
    else:
        print("  SKIP  在线嵌入（未设 KB_TEST_EMB_BASE/KEY）")
    rr_base = os.environ.get("KB_TEST_RR_BASE")
    rr_key = os.environ.get("KB_TEST_RR_KEY")
    if rr_base and rr_key:
        from kb.rerank import Reranker
        rr = Reranker(rr_base, rr_key, "BAAI/bge-reranker-v2-m3")
        scored = rr.rerank("灾备演练", [
            {"text": "KnowledgeBase 门面提供灾备演练功能", "id": "a"},
            {"text": "红烧肉的做法", "id": "b"},
            {"text": "向量索引分片设计", "id": "c"}])
        check("rerank 相关文档排第一", scored[0]["id"] == "a"
              and scored[0]["rerank_score"] > 0.5,
              str([(s['id'], round(s['rerank_score'],3)) for s in scored]))
        # 检索降级链：错误 key → rerank 失败 → 降级 RRF 且有 warning
        kb11.cfg.set("rerank.provider", "api")
        kb11.cfg.set("rerank.api_base", rr_base)
        kb11.cfg.set("rerank.model", "BAAI/bge-reranker-v2-m3")
        kb11.cfg.set("rerank.api_key", rr_key)   # 上次运行可能残留无效 key
        kb11._reranker = None      # 清缓存让新配置生效
        r_ok = kb11.search("红烧肉")
        check("rerank 正常生效", r_ok.get("rerank") is True)
        kb11.cfg.set("rerank.api_key", "sk-invalid")
        kb11._reranker = None
        r_dg = kb11.search("红烧肉", use_cache=False)  # 避开缓存，测真实降级
        check("rerank 故障自动降级 RRF",
              r_dg["results"] and not r_dg.get("rerank")
              and "rerank" in (r_dg.get("warning") or ""), str(r_dg.get("warning"))[:80])
        kb11.cfg.set("rerank.api_key", rr_key)
    else:
        print("  SKIP  在线 rerank（未设 KB_TEST_RR_BASE/KEY）")
    kb11.close()

    print("== 19. 语义缓存：L1/L2/失效/scope ==")
    kb12 = KnowledgeBase(HOME)
    t0 = _time(); r_c1 = kb12.search("红烧肉", limit=5); t_cold = _time() - t0
    t0 = _time(); r_c2 = kb12.search("红烧肉", limit=5); t_warm = _time() - t0
    check("L1 精确命中且结果一致",
          r_c2.get("cache") == "hit"
          and [h["id"] for h in r_c1["results"]] == [h["id"] for h in r_c2["results"]])
    check("缓存命中显著更快", t_warm <= t_cold + 0.005,
          f"cold={t_cold*1000:.1f}ms warm={t_warm*1000:.1f}ms")
    # 近义阈值行为：>0.90 的微调命中，0.75-0.85 的意译变体不命中（防跨意图）
    r_near = kb12.search("红烧肉！", limit=5)
    check("L2 标点级微调命中", r_near.get("cache") == "hit")
    r_far = kb12.search("红烧肉到底应该怎么做才好吃呢", limit=5)
    check("L2 意译变体不误命中（sim<0.90）",
          r_far.get("cache") is None)
    # 写后失效
    n_before = len(kb12.search("红烧肉", limit=5)["results"])
    kb12.note("缓存失效测试", "全新条目：缓存失效后的红烧肉高压锅做法。")
    r_inv = kb12.search("红烧肉", limit=5)
    check("数据变更后缓存失效", r_inv.get("cache") is None)
    check("失效后新条目可见",
          any("缓存失效测试" in (h.get("title") or "") for h in r_inv["results"]))
    stats_cache = kb12.catalog.conn.execute(
        "SELECT COUNT(*) FROM semantic_cache").fetchone()[0]
    check("缓存有界存储正常", stats_cache >= 1, str(stats_cache))
    kb12.close()

    print("== 20. Information Substrate：schema v5 / Capability API / Secret 句柄 ==")
    import shutil as _sh
    _sh.rmtree("/tmp/kb_is_test", ignore_errors=True)
    _osenv = dict(os.environ, KB_HOME="/tmp/kb_is_test")
    from kb.substrate import Substrate, CapabilityDenied
    from kb.policy import local_principal, limited_principal, CAP_REFLECT

    sub = Substrate(principal=local_principal("ops"))
    sub.knowledge.ingest(DATA, domain="ml", project="is-test")
    res = sub.knowledge.search("注意力机制")
    check("IS: knowledge.ingest+search", len(res["results"]) >= 1)
    eid = res["results"][0]["id"]
    sub.knowledge.assert_claim(eid, authority="derived", confidence=0.9,
                               epistemic_status="corroborated")
    row = sub._kb_inst().catalog.get(eid)
    check("IS: assert_claim 写认识论字段",
          row["epistemic_status"] == "corroborated"
          and row["authority"] == "derived" and row["confidence"] == 0.9)
    check("IS: 接纳≠可信（ingestion accepted vs epistemic 独立）",
          row["status"] == "active" and row["epistemic_status"] == "corroborated")
    sub.retention.classify(eid, "IMPORTANT")
    try:
        sub.retention.retire(eid); check("IS: IMPORTANT retire 保护", False)
    except CapabilityDenied:
        check("IS: IMPORTANT retire 保护", True)
    ref = sub.artifact.put_bytes(b"raw evidence bytes")
    check("IS: artifact.put/get 往返",
          sub.artifact.get(ref) == b"raw evidence bytes")
    mid = sub.memory.evidence_append("用户偏好简洁回答", evidence_ref=ref)
    check("IS: memory evidence 层",
          len(sub.memory.search("偏好")["results"]) >= 1)
    tr = sub.provenance.trace(eid)
    check("IS: provenance 链+审计时间线",
          len(tr["chain"]) >= 1 and len(tr["audit_timeline"]) >= 1)

    limited = Substrate(principal=limited_principal("agent-7", level="viewer"))
    try:
        limited.artifact.put_bytes(b"x"); check("IS: 无 CAP_WRITE 拒绝", False)
    except CapabilityDenied:
        check("IS: 无 CAP_WRITE 拒绝", True)
    try:
        limited.knowledge.assert_claim(eid, epistemic_status="asserted")
        check("IS: claim 需 APPROVE", False)
    except CapabilityDenied:
        check("IS: claim 需 APPROVE", True)
    sub.close(); limited.close()

    # Secret env 句柄
    os.environ["KB_TEST_SECRET"] = "sk-handle-test"
    kb13 = KnowledgeBase(HOME)
    kb13.cfg.set("summarize.api_key", "env:KB_TEST_SECRET")
    check("IS: secret env 句柄解析", kb13.cfg.get("summarize.api_key") == "sk-handle-test")
    check("IS: config 只存句柄", kb13.cfg.data["summarize"]["api_key"] == "env:KB_TEST_SECRET")
    del os.environ["KB_TEST_SECRET"]
    check("IS: env 缺失时空串不抛异常", kb13.cfg.get("summarize.api_key") == "")
    kb13.close()

    print("== 21. Phase B：claims 表 / provenance edges / disclosure 继承 / 三类 gap ==")
    import shutil as _sh2
    _sh2.rmtree("/tmp/kb_is_pb", ignore_errors=True)
    from kb.substrate import Substrate, CapabilityDenied
    from kb.policy import local_principal
    os.environ["KB_HOME"] = "/tmp/kb_is_pb"
    sub = Substrate(principal=local_principal("ops"))
    sub.knowledge.ingest(DATA, domain="ml", visibility="public")
    eid = sub.knowledge.search("注意力机制")["results"][0]["id"]
    sub.knowledge.assert_claim(eid, claim="初判", epistemic_status="asserted")
    sub.knowledge.assert_claim(eid, claim="佐证", confidence=0.9,
                               epistemic_status="corroborated")
    claims = sub.knowledge.claims_of(eid)
    check("PB: claim 历史可追溯", len(claims) == 2
          and [c["epistemic_status"] for c in claims] == ["asserted", "corroborated"])
    ref = sub.artifact.put_bytes(b"evidence bytes")
    mid = sub.memory.evidence_append("读了文档", evidence_ref=ref)
    tr = sub.provenance.trace(mid)
    check("PB: provenance edge 写入", any(
        e["relation"] == "derived_from" and e["parent"] == ref
        for e in tr["edges"]))
    src_id = sub.knowledge.search("注意力机制")["results"][0]["id"]
    sub._kb_inst().catalog.update_entry(src_id, {"visibility": "private"})
    mid2 = sub.memory.evidence_append("私密笔记", evidence_ref=src_id)
    row2 = sub._kb_inst().catalog.get(mid2)
    check("PB: disclosure 继承收紧为 private",
          row2["visibility"] == "private")
    kb_pb = sub._kb_inst()
    kb_pb.record_gap("memory", "方鸿渐出场", "本应召回", expected_id=eid)
    kb_pb.record_gap("awareness", "备份失败", "晚知道")
    check("PB: 三类 gap 分别汇总",
          len(sub._kb_inst().gaps(gap_type="memory")["gaps"]) == 1
          and len(sub._kb_inst().gaps(gap_type="awareness")["gaps"]) == 1)
    check("PB: schema v6", kb_pb.catalog.get_meta("schema_version") == "6")
    sub.close()
    os.environ["KB_HOME"] = HOME  # 恢复

    print(f"\n结果: {passed} 通过, {failed} 失败  (KB_HOME={HOME}, DATA={DATA})")
    return 1 if failed else 0


def _raises(exc, fn, *args, **kw) -> bool:
    try:
        fn(*args, **kw)
    except exc:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())
