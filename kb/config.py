"""配置：KB_HOME 目录布局与 config.json 读写。"""
from __future__ import annotations

import copy
import json
import os

DEFAULTS = {
    "embed": {
        "provider": "hash",          # hash | api | local
        "dim": 512,
        "api_base": "",              # OpenAI 兼容 /v1 根地址
        "api_key": "",
        "model": "",
        "local_model": "BAAI/bge-small-zh-v1.5",
    },
    "summarize": {
        "provider": "extractive",    # extractive | api
        "api_base": "",
        "api_key": "",
        "model": "",
        "max_chars": 400,
    },
    "ingest": {
        "max_file_mb": 512,
        "preview_chars": 4096,
        "max_text_kb": 256,
        "follow_symlinks": False,
        "exclude_names": [".git", "node_modules", "__pycache__", ".venv", "venv",
                          ".kb", ".svn", ".idea", ".vscode", ".hqx"],
        "chunk": {"enabled": False, "size": 1200, "overlap": 200},
        "pdf_max_pages": 8,      # PDF 抽取页数上限（0 = 不限，慎用）
        "warn_copy_mb": 10240,   # 未用 in-place/link 摄取超过此体积提示
        # 批量摄取时的摘要档位：offline（快，AI 编目交给 curate --ai）| ai（逐文件
        # LLM 摘要，~2.5s/文件，只适合小批量）。真实教训：40 万文件 × LLM = 不可行。
        "summarize_on_ingest": "offline",
    },
    "search": {"fts_candidates": 200, "vec_candidates": 200, "rrf_k": 60,
               "cache": {"enabled": True, "max_entries": 5000}},
    "access": {
        # Web 面板 token（MCP/CLI 为本机信任通道，不受此表约束）
        # [{"token": "长随机串", "name": "alice", "level": "admin|operator|viewer"}]
        "tokens": [],
        "mcp_level": "operator",      # MCP 通道默认级别（旧值 admin 已按架构文档降级）
        # MCP 写通道可摄取的根目录白名单。空 = 只允许 $HOME 下的非隐藏目录
        # （见 policy.resolve_write_path）。设为具体路径可进一步收紧。
        "write_roots": [],
    },
    "alerts": {"webhook": ""},      # 失败事件 POST 目标（ntfy/TG bot 等）
    "rerank": {
        "provider": "off",          # off | api
        "api_base": "",             # 如 https://api.siliconflow.cn/v1
        "api_key": "",
        "model": "",                # 如 BAAI/bge-reranker-v2-m3
        "top_n": 50,                # 参与精排的 RRF 候选数
    },
}


def default_home() -> str:
    return os.environ.get("KB_HOME") or os.path.expanduser("~/.kb")


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, home: str | None = None):
        self.home = os.path.abspath(home or default_home())
        self.path = os.path.join(self.home, "config.json")
        self.data = _deep_merge(DEFAULTS, self._load())

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def save(self) -> None:
        os.makedirs(self.home, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        try:  # 配置里可能存 API key，限制为本人可读
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, dotted: str):
        cur = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        # Secret 外移（架构文档 §22.6）：值形如 "env:VAR_NAME" 时从环境变量取，
        # config 只存句柄不存明文。非 str 值不受影响。
        if isinstance(cur, str) and cur.startswith("env:") and len(cur) > 4:
            return os.environ.get(cur[4:], "")
        return cur

    def set(self, dotted: str, value) -> None:
        parts = dotted.split(".")
        cur = self.data
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
        self.save()


def ensure_dirs(home: str) -> dict[str, str]:
    dirs = {
        "home": home,
        "blobs": os.path.join(home, "blobs"),
        "vectors": os.path.join(home, "vectors"),
        "logs": os.path.join(home, "logs"),
        "trash": os.path.join(home, "trash"),
        "backups": os.path.join(home, "backups"),
    }
    for p in dirs.values():
        os.makedirs(p, exist_ok=True)
    try:  # 知识库属于私有数据，目录权限收紧
        os.chmod(home, 0o700)
    except OSError:
        pass
    return dirs
