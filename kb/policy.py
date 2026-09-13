"""Kernel Policy Adapter（原"唯一访问裁决点"，架构文档 §15/§22.1 职责变更）。

Information Substrate 不再拥有独立权限语义；调用方（Kernel 或本机使用者）
注入 Principal + Capability，本模块只做 **enforcement**：
  - sql_scope / row_pass：可见性与状态的 SQL/行级过滤；
  - resolve_write_path：写路径边界（Kernel 未注入时按保守默认执行）。

Capability 约定（第一阶段，Kernel 接管后只需替换注入来源）：
  CAP_REFLECT   可读 private
  CAP_WRITE     可写入 / 摄取
  CAP_APPROVE   可执行高权威晋升（approve/claim）
  CAP_ADMIN     可执行运维（restore/retire/GC）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

LEVEL_VISIBILITY: dict[str, tuple | None] = {
    "admin": None,                       # None = 不限制
    "operator": ("public", "internal"),
    "viewer": ("public",),
}

CAP_REFLECT = "substrate.reflect"
CAP_WRITE = "substrate.write"
CAP_APPROVE = "substrate.approve"
CAP_ADMIN = "substrate.admin"


@dataclass(frozen=True)
class Principal:
    """访问主体：level 提供向后兼容的可见级别；capabilities 是授予的能力集。"""
    level: str = "admin"
    name: str = "local"
    capabilities: frozenset = field(default_factory=lambda: frozenset({
        CAP_REFLECT, CAP_WRITE, CAP_APPROVE, CAP_ADMIN}))

    @property
    def visible(self) -> tuple | None:
        # 未知级别一律按 viewer 处理（fail-safe）
        return LEVEL_VISIBILITY.get(self.level, LEVEL_VISIBILITY["viewer"])

    def can(self, capability: str) -> bool:
        return capability in self.capabilities


def local_principal(name: str = "local") -> Principal:
    """本机使用者（CLI 通道）：完整能力。"""
    return Principal(level="admin", name=name)


def limited_principal(name: str, level: str = "viewer",
                      capabilities: frozenset | None = None) -> Principal:
    """受限主体：MCP/Web 或未来 Kernel 注入。默认只有读能力（CAP_REFLECT），
    无写/晋升/运维——与旧 viewer 语义兼容但显式化。"""
    caps = frozenset({CAP_REFLECT}) if capabilities is None else frozenset(capabilities)
    return Principal(level=level, name=name, capabilities=caps)


def sql_scope(visible: tuple | None, include_pending: bool = False,
              include_chunks: bool = True, alias: str = "e") -> tuple[list[str], list]:
    """返回 (where 条件列表, 参数)。检索默认含块（块本身可命中），
    浏览列表默认不含块（由调用方传 include_chunks=False）。"""
    where, params = [], []
    if not include_chunks:
        where.append(f"{alias}.parent_id IS NULL")
    if not include_pending:
        where.append(f"{alias}.status='active'")
    if visible is not None:
        where.append(f"{alias}.visibility IN (%s)" % ",".join("?" * len(visible)))
        params.extend(visible)
    return where, params


def row_pass(visible: tuple | None, row: dict, include_pending: bool = False) -> bool:
    """单行复检（用于无法走 SQL 的路径）。与 sql_scope 语义必须一致。"""
    if not include_pending and row.get("status", "active") != "active":
        return False
    if visible is not None and row.get("visibility", "internal") not in visible:
        return False
    return True


class WritePathDenied(Exception):
    """写通道试图摄取边界外路径。"""


def resolve_write_path(raw: str, write_roots: list[str] | None,
                       home: str | None = None) -> str:
    """校验并规范化写通道的摄取路径（enforcement，不定义身份）。

    默认策略：仅允许 $HOME 下的非隐藏目录。配置 access.write_roots 可收紧
    为显式白名单。先 realpath 再前缀判定，防 .. 与软链穿越。
    """
    home = os.path.realpath(home or os.path.expanduser("~"))
    target = os.path.realpath(os.path.expanduser(raw))

    roots = [os.path.realpath(os.path.expanduser(r)) for r in (write_roots or [])]
    if roots:
        for root in roots:
            if target == root or target.startswith(root + os.sep):
                return target
        raise WritePathDenied(f"路径不在 access.write_roots 白名单内: {raw}")

    if target != home and not target.startswith(home + os.sep):
        raise WritePathDenied(
            f"默认策略只允许摄取 $HOME 下的目录，被拒绝: {raw}。"
            f"如需其他位置请配置 access.write_roots")
    rel = os.path.relpath(target, home)
    if rel != "." and any(part.startswith(".") for part in rel.split(os.sep)):
        raise WritePathDenied(
            f"隐藏目录/文件不允许经远程通道摄取（可能含密钥与凭据）: {raw}")
    return target
