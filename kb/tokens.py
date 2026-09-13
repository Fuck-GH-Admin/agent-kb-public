"""token 管理（C12）：config 只存 SHA-256 哈希，明文仅在创建时展示一次。

存储形态（access.tokens）：[{"hash": "...", "name": "alice", "level": "admin",
"created_at": "..."}]。兼容读取旧明文条目（带 "token" 键的），登录时同时校验，
并提示迁移（kb token migrate）。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from .util import now_iso

LEVELS = ("admin", "operator", "viewer")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    # 32 字节 base64url：足够熵，且无特殊字符便于 URL/header 携带
    return secrets.token_urlsafe(32)


def verify_token(token: str, entry: dict) -> bool:
    """兼容哈希条目与历史明文条目（迁移期双读）。"""
    if entry.get("hash"):
        return hmac.compare_digest(entry["hash"], hash_token(token))
    if entry.get("token"):
        return hmac.compare_digest(entry["token"], token)
    return False


def add_token(cfg, name: str, level: str, token: str | None = None) -> str:
    """写入哈希条目，返回明文 token（仅此一次可见）。"""
    if level not in LEVELS:
        raise ValueError(f"level 必须是 {'/'.join(LEVELS)}")
    token = token or new_token()
    entries = [e for e in (cfg.get("access.tokens") or [])
               if e.get("name") != name]
    entries.append({"hash": hash_token(token), "name": name,
                    "level": level, "created_at": now_iso()})
    cfg.set("access.tokens", entries)
    return token


def revoke_token(cfg, name: str) -> bool:
    entries = cfg.get("access.tokens") or []
    kept = [e for e in entries if e.get("name") != name]
    if len(kept) == len(entries):
        return False
    cfg.set("access.tokens", kept)
    return True


def rotate_token(cfg, name: str, level: str | None = None) -> str:
    """轮换 = 先撤销再新增（level 沿用或指定）。返回新明文。"""
    old = next((e for e in (cfg.get("access.tokens") or [])
                if e.get("name") == name), None)
    lvl = level or (old or {}).get("level", "viewer")
    revoke_token(cfg, name)
    return add_token(cfg, name, lvl)


def lookup_token(cfg, token: str) -> tuple[str, str] | None:
    """返回 (level, name)；未命中 None。明文/哈希条目都校验。"""
    for e in (cfg.get("access.tokens") or []):
        if verify_token(token, e):
            return e.get("level", "viewer"), e.get("name") or "anon"
    return None


def needs_migration(cfg) -> bool:
    return any(e.get("token") for e in (cfg.get("access.tokens") or []))


def migrate_tokens(cfg) -> int:
    """把历史明文条目全部转为哈希（明文不可恢复，需 kb token add 重发）。"""
    entries = cfg.get("access.tokens") or []
    converted, dropped = [], 0
    for e in entries:
        if e.get("token"):
            dropped += 1     # 明文转哈希会让旧 token 失效，等价 revoke
        else:
            converted.append(e)
    cfg.set("access.tokens", converted)
    return dropped


def age_days(entry: dict) -> int | None:
    ts = entry.get("created_at")
    if not ts:
        return None
    try:
        import calendar
        st = time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
        return (time.time() - calendar.timegm(st)) // 86400
    except ValueError:
        return None
