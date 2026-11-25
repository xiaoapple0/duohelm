"""插件注册表：发现插件、校验清单、检查 SDK 兼容性、解析依赖。

设计见 docs/subsystems/plugins.md 的"加载流程"。本模块只读清单，不执行任何插件代码。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from duohelm_sdk import MANIFEST_FILENAME, SDK_VERSION, Manifest, ManifestError, load_manifest


class PluginStatus(StrEnum):
    READY = "ready"
    """清单合法、SDK 兼容、依赖齐全。"""
    INVALID = "invalid"
    """清单无法解析或不合法，或 id 重复。"""
    INCOMPATIBLE = "incompatible"
    """不支持当前 SDK 版本。"""
    BLOCKED = "blocked"
    """依赖缺失、版本不满足、依赖不可用或循环依赖。"""


@dataclass(frozen=True)
class PluginEntry:
    path: Path
    status: PluginStatus
    manifest: Manifest | None = None
    problems: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return self.manifest.id if self.manifest else self.path.name


class PluginRegistry:
    def __init__(self, plugin_dirs: Sequence[Path], sdk_version: str = SDK_VERSION) -> None:
        self._plugin_dirs = tuple(plugin_dirs)
        self._sdk_version = sdk_version

    def scan(self) -> list[PluginEntry]:
        """扫描所有插件目录，返回按 id 排序的结果。不存在的目录被忽略。"""
        entries = [self._load(path) for path in self._candidates()]
        entries = _mark_duplicates(entries)
        entries = [self._check_sdk(e) for e in entries]
        entries = _resolve_dependencies(entries)
        return sorted(entries, key=lambda e: (e.id, str(e.path)))

    def _candidates(self) -> Iterable[Path]:
        for root in self._plugin_dirs:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if child.is_dir() and (child / MANIFEST_FILENAME).is_file():
                    yield child

    def _load(self, path: Path) -> PluginEntry:
        try:
            manifest = load_manifest(path)
        except ManifestError as exc:
            return PluginEntry(path, PluginStatus.INVALID, problems=(exc.message,))
        return PluginEntry(path, PluginStatus.READY, manifest)

    def _check_sdk(self, entry: PluginEntry) -> PluginEntry:
        if entry.status is not PluginStatus.READY or entry.manifest is None:
            return entry
        if entry.manifest.supports_sdk(self._sdk_version):
            return entry
        problem = f"需要 SDK {entry.manifest.plugin.sdk}，当前为 {self._sdk_version}"
        return replace(entry, status=PluginStatus.INCOMPATIBLE, problems=(problem,))


def _mark_duplicates(entries: list[PluginEntry]) -> list[PluginEntry]:
    paths_by_id: dict[str, list[Path]] = {}
    for e in entries:
        if e.manifest is not None:
            paths_by_id.setdefault(e.manifest.id, []).append(e.path)

    result: list[PluginEntry] = []
    for e in entries:
        paths = paths_by_id.get(e.id, []) if e.manifest is not None else []
        if len(paths) > 1:
            others = ", ".join(str(p) for p in paths if p != e.path)
            problem = f"插件 id 重复，另见: {others}"
            result.append(replace(e, status=PluginStatus.INVALID, problems=(problem,)))
        else:
            result.append(e)
    return result


def _resolve_dependencies(entries: list[PluginEntry]) -> list[PluginEntry]:
    by_id = {e.id: e for e in entries if e.manifest is not None}
    status = {pid: e.status for pid, e in by_id.items()}
    problems: dict[str, list[str]] = {pid: [] for pid in by_id}

    while True:
        # 反复传播，直到没有新的插件被阻塞（依赖不可用会向上传递）。
        changed = True
        while changed:
            changed = False
            for pid, e in by_id.items():
                if status[pid] is not PluginStatus.READY or e.manifest is None:
                    continue
                reasons = _requirement_problems(e.manifest, by_id, status)
                if reasons:
                    status[pid] = PluginStatus.BLOCKED
                    problems[pid] = reasons
                    changed = True

        cycle = _cycle_members(by_id, status)
        if not cycle:
            break
        for pid in cycle:
            status[pid] = PluginStatus.BLOCKED
            problems[pid] = ["存在循环依赖"]
        # 依赖环上插件的插件也要被阻塞，回到上面继续传播。

    result: list[PluginEntry] = []
    for e in entries:
        if e.manifest is not None and status[e.id] is not e.status:
            result.append(replace(e, status=status[e.id], problems=tuple(problems[e.id])))
        else:
            result.append(e)
    return result


def _requirement_problems(
    manifest: Manifest, by_id: dict[str, PluginEntry], status: dict[str, PluginStatus]
) -> list[str]:
    reasons: list[str] = []
    for req in manifest.requires:
        dep = by_id.get(req.id)
        if dep is None or dep.manifest is None:
            reasons.append(f"缺少依赖 {req.id}")
        elif not req.spec.contains(dep.manifest.version, prereleases=True):
            reasons.append(f"依赖 {req.id} 的版本 {dep.manifest.version} 不满足 {req.version}")
        elif status[req.id] is not PluginStatus.READY:
            reasons.append(f"依赖 {req.id} 不可用（{status[req.id]}）")
    return reasons


def _cycle_members(by_id: dict[str, PluginEntry], status: dict[str, PluginStatus]) -> set[str]:
    """在仍为 READY 的插件中找出处于依赖环上的插件。"""
    graph = {
        pid: [r.id for r in e.manifest.requires]
        for pid, e in by_id.items()
        if e.manifest is not None and status[pid] is PluginStatus.READY
    }
    members: set[str] = set()
    visiting: list[str] = []
    done: set[str] = set()

    def visit(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            members.update(visiting[visiting.index(node) :])
            return
        visiting.append(node)
        for dep in graph.get(node, []):
            if dep in graph:
                visit(dep)
        visiting.pop()
        done.add(node)

    for node in graph:
        visit(node)
    return members
