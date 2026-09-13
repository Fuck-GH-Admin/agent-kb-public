"""kb 命令行包：入口 main()，子命令实现分散在 query/ingest/maint。"""
from .parser import build_parser, main

__all__ = ["main", "build_parser"]
