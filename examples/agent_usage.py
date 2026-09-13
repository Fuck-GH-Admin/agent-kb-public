#!/usr/bin/env python3
"""agent 调用知识库的最小示例（Python API 方式）。

前置: KB_HOME 指向你的知识库目录（缺省 ~/.kb）
      cd /home/miku/Bot/kb-workspace/kb && python3 examples/agent_usage.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KB_HOME", "/tmp/kb_demo_home")

from kb import KnowledgeBase  # noqa: E402

kb = KnowledgeBase()

# 1) agent 把资料写进知识库（幂等，重复执行只会跳过）
stats = kb.add("/tmp/kb_e2e_data", tags=["demo"])
print("摄取:", {k: v for k, v in stats.items() if k != "errors"})

# 2) agent 记笔记（立即可检索）
kb.note("用户偏好", "用户喜欢简洁的中文回答，讨厌冗长列表。", tags=["profile"])

# 3) 检索：混合 BM25 + 向量
res = kb.search("注意力机制", limit=3)
for hit in res["results"]:
    print(f"[{hit['score']:.4f}] {hit['kind']:5} {hit['title']} -> {hit['source_path']}")

# 4) 拿到原文位置后，agent 自己读原文（大文件不进上下文，按需取）
top = res["results"][0]
loc = top["blob_path"] or top["source_path"]
print("原文位置:", loc)
if loc and os.path.exists(loc):
    with open(loc, encoding="utf-8", errors="replace") as f:
        print("原文开头:", f.read(80).replace("\n", " "))

kb.close()
