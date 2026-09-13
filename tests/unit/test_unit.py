"""单元测试：纯函数级（util/policy），无库状态。运行: python3 tests/unit/test_unit.py"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    from kb.policy import Principal, WritePathDenied, resolve_write_path, sql_scope, row_pass
    from kb.util import (chunk_text, fts_query, fts_index_text, make_snippet,
                         read_text, sniff_kind)
    from kb.util import keywords, append_jsonl, WriteLock

    home = tempfile.mkdtemp(prefix="kb_unit_")

    # ---- policy: Principal / sql_scope / row_pass ----
    check("admin 无限制", Principal(level="admin").visible is None)
    check("operator 可见 public+internal",
          Principal(level="operator").visible == ("public", "internal"))
    check("未知级别 fail-safe 为 viewer", Principal(level="hacker").visible == ("public",))
    w, p = sql_scope(("public",), include_chunks=False)
    check("sql_scope 条件", "parent_id IS NULL" in " ".join(w)
          and "status='active'" in " ".join(w)
          and "visibility IN" in " ".join(w) and p == ["public"], f"{w} {p}")
    check("row_pass 拒绝 pending/private",
          not row_pass(("public",), {"status": "active", "visibility": "private"})
          and not row_pass(None, {"status": "pending", "visibility": "public"})
          and row_pass(("public",), {"status": "active", "visibility": "public"}))

    # ---- policy: 写路径白名单 ----
    me = os.path.expanduser("~")
    try:
        resolve_write_path("/etc/shadow", [])
        check("拒绝 /etc/shadow", False)
    except WritePathDenied:
        check("拒绝 /etc/shadow", True)
    try:
        resolve_write_path(me + "/.ssh", [])
        check("拒绝隐藏目录", False)
    except WritePathDenied:
        check("拒绝隐藏目录", True)
    try:
        resolve_write_path(me + "/../etc", [])
        check("拒绝 .. 穿越", False)
    except WritePathDenied:
        check("拒绝 .. 穿越", True)
    check("显式白名单内放行", resolve_write_path(home, [home]) == home)
    try:
        resolve_write_path(home, ["/nonexistent"])
        check("显式白名单外拒绝", False)
    except WritePathDenied:
        check("显式白名单外拒绝", True)

    # ---- util: fts_query 两档策略 ----
    check("≤3字 CJK 整短语", fts_query("红烧肉") == '"红 烧 肉"')
    q4 = fts_query("注意力机制")
    check("4字拆相邻对 OR（意译子串召回）",
          '"注 意" AND "意 力"' in q4 and " OR " in q4, q4)
    q5 = fts_query("面对行刑队出击")
    check(">3字拆相邻对 OR", '"面 对" AND "对 行"' in q5 and " OR " in q5, q5)
    ql = fts_query("hello world 注意")
    check("拉丁词前缀", '"hello *"' in ql and '"world *"' in ql and '"注 意"' in ql, ql)
    check("空查询安全", fts_query("   ") == "" and fts_query("?!") == "")

    # ---- util: make_snippet 从原文定位 ----
    snip = make_snippet("他站在行刑队面前。" * 3, "面对行刑队")
    check("snippet 容忍意译（无则回退 None）", snip is None or "行刑队" in snip)
    snip2 = make_snippet("方鸿渐在欧洲留学四年。", "方鸿渐")
    check("snippet 精确定位", snip2 is not None and "方鸿渐" in snip2)

    # ---- util: chunk_text 边界 ----
    cs = chunk_text("a" * 3000, size=1200, overlap=200)
    check("滑窗块数与重叠", len(cs) == 3 and len(cs[0]) == 1200, str([len(c) for c in cs]))
    check("空文本安全", chunk_text("") == [])
    cs2 = chunk_text("段落一。\n\n段落二。", size=1200)
    check("段落保留", len(cs2) == 1 and "段落一" in cs2[0] and "段落二" in cs2[0])

    # ---- util: read_text 增量解码（GBK 截断不乱码）----
    p = os.path.join(home, "gbk.txt")
    raw = ("方鸿渐留学。" * 100).encode("gb18030")
    with open(p, "wb") as f:
        f.write(raw[:4096])   # 恰好切在多字节中间
    t = read_text(p, max_bytes=4096)
    check("GBK 截断解码含正文字符", "方鸿渐" in t, t[:40])

    # ---- util: sniff_kind ----
    p2 = os.path.join(home, "x.ts")
    with open(p2, "w") as f:
        f.write("export function f(): void {}")
    check(".ts 文本判 code", sniff_kind(p2, "video") == "code")
    p3 = os.path.join(home, "x.safetensors")
    with open(p3, "wb") as f:
        f.write(b"PK\x03\x04zzzz")
    check("模型扩展名优先于 magic", sniff_kind(p3, "model") == "model")

    # ---- util: append_jsonl 轮转 ----
    lp = os.path.join(home, "r.jsonl")
    for _ in range(300):
        append_jsonl(lp, {"pad": "x" * 4000}, max_mb=1, keep=2)
    check("日志轮转", os.path.exists(lp + ".1") and os.path.getsize(lp) < 2 << 20)

    # ---- util: WriteLock 可重入 ----
    lock = WriteLock(home)
    with lock:
        with lock:  # 嵌套不自锁
            pass
    check("WriteLock 可重入", True)
    lock2 = WriteLock(home)
    with lock2:
        try:
            WriteLock(home).__enter__()
            check("第二写者被拒", False)
        except RuntimeError:
            check("第二写者被拒", True)

    # ---- util: keywords（jieba 缺席时二元组保底）----
    kw = keywords("红烧肉做法五花肉切块焯水")
    check("关键词非空且为词/二元组", bool(kw) and "," in kw, kw)

    print(f"\n单测结果: {passed} 通过, {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
