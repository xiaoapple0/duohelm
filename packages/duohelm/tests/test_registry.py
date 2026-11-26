from __future__ import annotations

from pathlib import Path

from duohelm.registry import PluginEntry, PluginRegistry, PluginStatus


def _scan(root: Path, sdk_version: str = "0.3.0") -> dict[str, PluginEntry]:
    return {e.id: e for e in PluginRegistry([root], sdk_version=sdk_version).scan()}


def test_ready_plugin(plugin_root, make_plugin) -> None:
    make_plugin("ops-basic")
    entries = _scan(plugin_root)
    assert entries["ops-basic"].status is PluginStatus.READY
    assert entries["ops-basic"].problems == ()


def test_missing_and_empty_dirs_are_ignored(tmp_path, plugin_root) -> None:
    (plugin_root / "not-a-plugin").mkdir()
    (plugin_root / "stray.txt").write_text("x", encoding="utf-8")
    assert PluginRegistry([plugin_root, tmp_path / "missing"]).scan() == []


def test_invalid_manifest(plugin_root, make_plugin) -> None:
    make_plugin("broken", raw="[plugin]\nid = 'broken'\n")
    entry = _scan(plugin_root)["broken"]
    assert entry.status is PluginStatus.INVALID
    assert entry.manifest is None
    assert entry.problems


def test_incompatible_sdk(plugin_root, make_plugin) -> None:
    make_plugin("ops-new", sdk=">=0.4")
    entry = _scan(plugin_root)["ops-new"]
    assert entry.status is PluginStatus.INCOMPATIBLE
    assert "0.3.0" in entry.problems[0]


def test_duplicate_ids_are_all_invalid(plugin_root, make_plugin) -> None:
    make_plugin("ops-basic", dirname="a")
    make_plugin("ops-basic", dirname="b")
    entries = PluginRegistry([plugin_root]).scan()
    assert [e.status for e in entries] == [PluginStatus.INVALID, PluginStatus.INVALID]
    assert all("重复" in e.problems[0] for e in entries)


def test_dependency_satisfied(plugin_root, make_plugin) -> None:
    make_plugin("ops-basic", version="1.2.0")
    make_plugin("ops-industry", requires={"ops-basic": ">=1.0"})
    assert _scan(plugin_root)["ops-industry"].status is PluginStatus.READY


def test_missing_dependency_blocks(plugin_root, make_plugin) -> None:
    make_plugin("ops-industry", requires={"ops-basic": ""})
    entry = _scan(plugin_root)["ops-industry"]
    assert entry.status is PluginStatus.BLOCKED
    assert entry.problems == ("缺少依赖 ops-basic",)


def test_dependency_version_mismatch_blocks(plugin_root, make_plugin) -> None:
    make_plugin("ops-basic", version="0.9.0")
    make_plugin("ops-industry", requires={"ops-basic": ">=1.0"})
    entry = _scan(plugin_root)["ops-industry"]
    assert entry.status is PluginStatus.BLOCKED
    assert "不满足" in entry.problems[0]


def test_unavailable_dependency_blocks_transitively(plugin_root, make_plugin) -> None:
    make_plugin("ops-basic", sdk=">=0.4")  # 不兼容
    make_plugin("ops-industry", requires={"ops-basic": ""})
    make_plugin("my-factors", requires={"ops-industry": ""})
    entries = _scan(plugin_root)
    assert entries["ops-basic"].status is PluginStatus.INCOMPATIBLE
    assert entries["ops-industry"].status is PluginStatus.BLOCKED
    assert entries["my-factors"].status is PluginStatus.BLOCKED
    assert "ops-industry" in entries["my-factors"].problems[0]


def test_dependency_cycle_blocks_members_and_dependents(plugin_root, make_plugin) -> None:
    make_plugin("ops-a", requires={"ops-b": ""})
    make_plugin("ops-b", requires={"ops-a": ""})
    make_plugin("ops-c", requires={"ops-a": ""})
    make_plugin("ops-d")
    entries = _scan(plugin_root)
    assert entries["ops-a"].problems == ("存在循环依赖",)
    assert entries["ops-b"].problems == ("存在循环依赖",)
    assert entries["ops-c"].status is PluginStatus.BLOCKED
    assert entries["ops-d"].status is PluginStatus.READY


def test_results_sorted_by_id(plugin_root, make_plugin) -> None:
    make_plugin("ops-z")
    make_plugin("ops-a")
    assert [e.id for e in PluginRegistry([plugin_root]).scan()] == ["ops-a", "ops-z"]
