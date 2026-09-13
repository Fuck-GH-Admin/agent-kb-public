"""agent 本地知识库：SQLite + 内容寻址 blob + 混合检索，为 LLM agent 设计。"""
from .core import AmbiguousId, KnowledgeBase, NotFound

__version__ = "0.1.0"
__all__ = ["KnowledgeBase", "NotFound", "AmbiguousId", "__version__"]
