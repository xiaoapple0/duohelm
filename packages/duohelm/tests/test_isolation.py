"""插件隔离运行的测试。所有测试插件都写在 tmp_path 里，不访问外网。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import psutil
import pytest

from duohelm.isolation import Limits, PluginRunError, PluginSession, child_env, runner
from duohelm.isolation.protocol import (
    MAX_MESSAGE_BYTES,
    FrameStore,
    ProtocolError,
    decode,
    encode,
    read_message,
    write_message,
)
from duohelm.registry import PluginEntry, PluginRegistry, PluginStatus

# 一个插件覆盖所有场景：按 factor_id 选择行为，冒号后面是参数。
PLUGIN_CODE = r"""
import os
import os
import socket
import subprocess
from collections.abc import Iterator
from dataclasses import replace
import subprocess
import sys
import time
import urllib.request

import polars as pl


def _ok(data):
    return data.select("date", "symbol", (pl.col("close") * 2).alias("value"))


class Plugin:
    def __init__(self):
        self.calls = 0

    def compute(self, factor_id, data):
        self.calls += 1
        name, _, arg = factor_id.partition(":")
        if name == "double":
            return _ok(data)
        if name == "calls":
            return pl.DataFrame({"calls": [self.calls], "pid": [os.getpid()]})
        if name == "sleep":
            time.sleep(float(arg))
            return _ok(data)
        if name == "spawn_sleeper":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            with open(arg, "w") as f:
                f.write(str(child.pid))
            time.sleep(120)
        if name == "hog":
            chunks = []
            for _ in range(64):
                chunks.append(b"x" * (64 * 1024 * 1024))
                time.sleep(0.05)
            return _ok(data)
        if name == "print":
            print("hello from print")
            sys.stdout.flush()
            os.write(1, b"raw fd1 write\n")
            return _ok(data)
        if name == "env":
            return pl.DataFrame({"key": sorted(os.environ)})
        if name == "socket":
            s = socket.socket()
            s.connect(("127.0.0.1", int(arg)))
            return _ok(data)
        if name == "urllib":
            urllib.request.urlopen(f"http://127.0.0.1:{arg}/", timeout=2)
            return _ok(data)
        if name == "socketpair":
            a, b = socket.socketpair()
            a.sendall(b"ping")
            assert b.recv(4) == b"ping"
            return _ok(data)
        if name == "raise":
            raise ValueError("boom")
        if name == "exit":
            os._exit(3)
        if name == "wrong_type":
            return [1, 2, 3]
        if name == "bad_output":
            return data.select("date", "symbol", pl.col("close").cast(pl.Int64).alias("value"))
        if name == "stderr":
            for i in range(int(arg)):
                sys.stderr.write(f"line {i} " + "y" * 100 + "\n")
            return _ok(data)
        if name == "huge_result":
            return list(range(3_000_000))
        if name == "flags":
            return pl.DataFrame(
                {"isolated": [sys.flags.isolated], "cwd": [os.getcwd()], "path0": [sys.path[0]]}
            )
        raise KeyError(name)


plugin = Plugin()
"""

MANIFEST = """
[plugin]
id = "{plugin_id}"
name = "{plugin_id}"
version = "1.0.0"
kind = "factor"
sdk = ">=0.4,<0.5"
entrypoint = "{module}:plugin"

[[factors]]
id = "double"
name = "double"
fields = ["close"]
"""


def make_entry(root: Path, plugin_id: str = "iso-test", code: str = PLUGIN_CODE) -> PluginEntry:
    module = plugin_id.replace("-", "_")
    path = root / plugin_id
    path.mkdir(parents=True)
    (path / f"{module}.py").write_text(textwrap.dedent(code), encoding="utf-8")
    (path / "duohelm-plugin.toml").write_text(
        MANIFEST.format(plugin_id=plugin_id, module=module), encoding="utf-8"
    )
    [entry] = [e for e in PluginRegistry([root]).scan() if e.id == plugin_id]
    assert entry.status is PluginStatus.READY, entry.problems
    return entry


@pytest.fixture
def entry(tmp_path: Path) -> PluginEntry:
    return make_entry(tmp_path / "plugins")


@pytest.fixture
def data() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [date(2024, 1, 2), date(2024, 1, 2), date(2024, 1, 3)],
            "symbol": ["600000.SH", "000001.SZ", "600000.SH"],
            "close": [10.0, None, 11.5],
        }
    )


FAST = Limits(call_timeout_s=30, startup_timeout_s=60, poll_interval_s=0.1)


def run(entry: PluginEntry, factor_id: str, data: pl.DataFrame, **kw: object) -> pl.DataFrame:
    with PluginSession(entry, limits=kw.pop("limits", FAST), **kw) as s:  # type: ignore[arg-type]
        return s.compute(factor_id, data)


def wait_gone(pid: int, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.1)
    return False


# ---- 正常路径 ----


def test_compute_roundtrip(entry: PluginEntry, data: pl.DataFrame) -> None:
    out = run(entry, "double", data)
    assert out["value"].to_list() == [20.0, None, 23.0]
    assert out["date"].to_list() == data["date"].to_list()


def test_session_reuses_process(entry: PluginEntry, data: pl.DataFrame) -> None:
    with PluginSession(entry, limits=FAST) as s:
        first = s.call("compute", "calls", data)
        second = s.call("compute", "calls", data)
        assert first["pid"][0] == second["pid"][0] == s.pid
        assert second["calls"][0] == 2


def test_child_runs_isolated_in_workdir(entry: PluginEntry, data: pl.DataFrame) -> None:
    with PluginSession(entry, limits=FAST) as s:
        out = s.call("compute", "flags", data)
        assert s.workdir is not None
        assert out["isolated"][0] == 1
        assert Path(out["cwd"][0]).resolve() == s.workdir.resolve()
        assert Path(out["path0"][0]).resolve() == entry.path.resolve()


def test_close_removes_workdir_even_with_result_alive(
    entry: PluginEntry, data: pl.DataFrame
) -> None:
    with PluginSession(entry, limits=FAST) as s:
        out = s.compute("double", data)
        workdir = s.workdir
    assert workdir is not None
    assert not workdir.exists()
    assert out.height == 3  # 结果仍然可用，没有依赖临时文件


def test_call_after_close(entry: PluginEntry, data: pl.DataFrame) -> None:
    s = PluginSession(entry, limits=FAST)
    with s:
        pass
    with pytest.raises(PluginRunError):
        s.call("compute", "double", data)


# ---- 资源限制 ----


def test_timeout_kills_process(entry: PluginEntry, data: pl.DataFrame) -> None:
    limits = Limits(call_timeout_s=1, poll_interval_s=0.1)
    with PluginSession(entry, limits=limits) as s:
        pid = s.pid
        assert pid is not None
        start = time.monotonic()
        with pytest.raises(PluginRunError) as info:
            s.compute("sleep:60", data)
        assert info.value.reason == "timeout"
        assert time.monotonic() - start < 15
        assert wait_gone(pid)


def test_timeout_kills_grandchildren(
    entry: PluginEntry, data: pl.DataFrame, tmp_path: Path
) -> None:
    pid_file = tmp_path / "grandchild.pid"
    limits = Limits(call_timeout_s=5, poll_interval_s=0.1)
    with PluginSession(entry, limits=limits) as s:
        with pytest.raises(PluginRunError) as info:
            s.compute(f"spawn_sleeper:{pid_file}", data)
        assert info.value.reason == "timeout"
        # 在 close 之前检查：结束进程树必须当场生效，不能依赖会话关闭
        grandchild = int(pid_file.read_text())
        assert wait_gone(grandchild), "孙进程没有被结束"


def test_memory_limit(entry: PluginEntry, data: pl.DataFrame) -> None:
    limits = Limits(call_timeout_s=60, memory_bytes=400 * 1024**2, poll_interval_s=0.1)
    with PluginSession(entry, limits=limits) as s, pytest.raises(PluginRunError) as info:
        s.compute("hog", data)
    assert info.value.reason == "memory"


def test_cancel_kills_process(entry: PluginEntry, data: pl.DataFrame) -> None:
    cancel = threading.Event()
    with PluginSession(entry, limits=FAST, cancel=cancel) as s:
        pid = s.pid
        assert pid is not None
        threading.Timer(1.0, cancel.set).start()
        start = time.monotonic()
        with pytest.raises(PluginRunError) as info:
            s.compute("sleep:60", data)
        assert info.value.reason == "cancelled"
        assert time.monotonic() - start < 10
        assert wait_gone(pid)


# ---- 输出和日志 ----


def test_print_does_not_corrupt_protocol(entry: PluginEntry, data: pl.DataFrame) -> None:
    lines: list[str] = []
    with PluginSession(entry, limits=FAST, log=lines.append) as s:
        out = s.compute("print", data)
        s.compute("double", data)
    assert out.height == 3
    assert "hello from print" in lines
    assert "raw fd1 write" in lines


def test_stderr_is_capped(entry: PluginEntry, data: pl.DataFrame) -> None:
    lines: list[str] = []
    limits = Limits(call_timeout_s=30, stderr_bytes=2_000, poll_interval_s=0.1)
    with PluginSession(entry, limits=limits, log=lines.append) as s:
        s.compute("stderr:5000", data)  # 约 500 KB
        s.compute("double", data)  # 管道一直被读空，插件没有卡住
    notices = [line for line in lines if "后续输出已丢弃" in line]
    assert len(notices) == 1
    plugin_lines = [line for line in lines if line.startswith("line ")]
    assert plugin_lines
    # 至少读取了：各行内容 + 行与行之间的换行。最后一行可能是截断的残行，后面没有换行；
    # Windows 上每行还有一个被去掉的 \r，实际读取的只会更多。
    min_bytes_read = sum(len(line.encode()) for line in plugin_lines) + len(plugin_lines) - 1
    assert min_bytes_read <= 2_000


# ---- 环境变量和网络 ----


def test_secrets_not_inherited(
    entry: PluginEntry, data: pl.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DUOHELM_TEST_SECRET", "s3cret")
    monkeypatch.setenv("MY_API_TOKEN", "t0ken")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:1")
    with PluginSession(entry, limits=FAST) as s:
        keys = {k.upper() for k in s.call("compute", "env", data)["key"].to_list()}
    assert "DUOHELM_TEST_SECRET" not in keys
    assert "MY_API_TOKEN" not in keys
    assert "HTTPS_PROXY" not in keys
    assert "PATH" in keys


def test_child_env_allowlist(tmp_path: Path) -> None:
    env = child_env(tmp_path, {"PATH": "/bin", "AWS_SECRET_ACCESS_KEY": "x", "DUOHELM_HOME": "y"})
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "DUOHELM_HOME" not in env
    assert env["HOME"] == str(tmp_path)
    assert env["TMPDIR"] == str(tmp_path)


@pytest.fixture
def listener() -> Iterator[tuple[int, list[str]]]:
    """本机监听一个端口，记录收到的连接。"""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    server.settimeout(0.2)
    seen: list[str] = []
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                conn, addr = server.accept()
            except OSError:
                continue
            seen.append(str(addr))
            conn.close()

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield server.getsockname()[1], seen
    stop.set()
    t.join()
    server.close()


@pytest.mark.parametrize("how", ["socket", "urllib"])
def test_network_blocked(
    entry: PluginEntry, data: pl.DataFrame, listener: tuple[int, list[str]], how: str
) -> None:
    port, seen = listener
    with PluginSession(entry, limits=FAST) as s, pytest.raises(PluginRunError) as info:
        s.compute(f"{how}:{port}", data)
    assert info.value.reason == "plugin_error"
    assert "没有网络权限" in info.value.message
    time.sleep(0.5)
    assert seen == []


def test_socketpair_still_works(entry: PluginEntry, data: pl.DataFrame) -> None:
    assert run(entry, "socketpair", data).height == 3


# ---- 错误分类 ----


def test_plugin_exception(entry: PluginEntry, data: pl.DataFrame) -> None:
    with PluginSession(entry, limits=FAST) as s:
        with pytest.raises(PluginRunError) as info:
            s.compute("raise", data)
        assert info.value.reason == "plugin_error"
        assert "boom" in info.value.message
        assert info.value.traceback is not None
        assert "ValueError" in info.value.traceback
        # 插件自己的异常不影响会话继续使用
        assert s.compute("double", data).height == 3


def test_crash(entry: PluginEntry, data: pl.DataFrame) -> None:
    with PluginSession(entry, limits=FAST) as s, pytest.raises(PluginRunError) as info:
        s.compute("exit", data)
    assert info.value.reason == "crashed"
    assert "3" in info.value.message


def test_wrong_return_type(entry: PluginEntry, data: pl.DataFrame) -> None:
    with pytest.raises(PluginRunError) as info:
        run(entry, "wrong_type", data)
    assert info.value.reason == "contract"


def test_output_checked(entry: PluginEntry, data: pl.DataFrame) -> None:
    with pytest.raises(PluginRunError) as info:
        run(entry, "bad_output", data)
    assert info.value.reason == "contract"
    assert "value" in info.value.message


def test_result_too_large(entry: PluginEntry, data: pl.DataFrame) -> None:
    with PluginSession(entry, limits=FAST) as s, pytest.raises(PluginRunError) as info:
        s.call("compute", "huge_result", data)
    assert info.value.reason == "protocol"


def test_method_not_allowed(entry: PluginEntry, data: pl.DataFrame) -> None:
    with PluginSession(entry, limits=FAST) as s, pytest.raises(PluginRunError) as info:
        s.call("__init__")
    assert info.value.reason == "protocol"


def test_load_error(tmp_path: Path) -> None:
    broken = make_entry(tmp_path / "plugins", "iso-broken", "raise ImportError('nope')\n")
    with pytest.raises(PluginRunError) as info, PluginSession(broken, limits=FAST):
        pass
    assert info.value.reason == "load"
    assert "nope" in info.value.message


def test_not_ready_plugin_refused(entry: PluginEntry) -> None:

    with pytest.raises(PluginRunError) as info:
        PluginSession(replace(entry, status=PluginStatus.BLOCKED))
    assert info.value.reason == "load"


# ---- 协议 ----


class _Buffer:
    def __init__(self, data: bytes = b"") -> None:
        self.data = bytearray(data)

    def write(self, b: bytes) -> int:
        self.data += b
        return len(b)

    def flush(self) -> None:
        pass

    def read(self, n: int) -> bytes:
        chunk = bytes(self.data[:n])
        del self.data[:n]
        return chunk


def test_message_roundtrip() -> None:
    buf = _Buffer()
    write_message(buf, {"type": "x", "v": "中文"})  # type: ignore[arg-type]
    assert read_message(buf) == {"type": "x", "v": "中文"}  # type: ignore[arg-type]
    assert read_message(buf) is None  # type: ignore[arg-type]


def test_message_size_limit() -> None:
    with pytest.raises(ProtocolError):
        write_message(_Buffer(), {"v": "x" * MAX_MESSAGE_BYTES})  # type: ignore[arg-type]
    # 完整、合法的超长消息：只有长度检查能拦住它
    payload = b'"' + b"x" * (MAX_MESSAGE_BYTES - 1) + b'"'
    framed = len(payload).to_bytes(4, "big") + payload
    with pytest.raises(ProtocolError, match="超过上限"):
        read_message(_Buffer(framed))  # type: ignore[arg-type]


def test_truncated_message() -> None:
    with pytest.raises(ProtocolError):
        read_message(_Buffer((10).to_bytes(4, "big") + b"abc"))  # type: ignore[arg-type]


def test_frame_read_deletes_file(tmp_path: Path, data: pl.DataFrame) -> None:
    store = FrameStore(tmp_path, "t")
    encoded = encode({"df": data, "day": date(2024, 1, 2)}, store)
    decoded = decode(encoded, store)
    assert decoded["df"].equals(data)
    assert decoded["day"] == date(2024, 1, 2)
    # 读完立即可以删除整个目录（Windows 上被映射的文件删不掉）
    assert list(tmp_path.iterdir()) == []


def test_frame_name_cannot_escape(tmp_path: Path) -> None:
    store = FrameStore(tmp_path / "work", "t")
    with pytest.raises(ProtocolError):
        decode({"$frame": "../secret.arrow"}, store)


def test_limits_from_env() -> None:
    limits = Limits.from_env({"DUOHELM_PLUGIN_TIMEOUT": "12", "DUOHELM_PLUGIN_MEMORY_MB": "512"})
    assert limits.call_timeout_s == 12
    assert limits.memory_bytes == 512 * 1024**2
    with pytest.raises(ValueError, match="DUOHELM_PLUGIN_MEMORY_MB"):
        Limits.from_env({"DUOHELM_PLUGIN_MEMORY_MB": "0"})


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")
def test_windows_interpreter_in_job(entry: PluginEntry) -> None:
    # venv 的 python.exe 是启动器：真正运行插件的解释器也必须在 Job 里
    with PluginSession(entry, limits=FAST) as s:
        job = s._job  # pyright: ignore[reportPrivateUsage]
        assert job is not None
        assert s.pid is not None
        assert job.contains(s.pid)


def test_child_start_flags(entry: PluginEntry, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 必须挂起启动（先加入 Job 再恢复）；POSIX 必须自成进程组。"""
    seen: list[dict[str, object]] = []
    real_popen = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)  # runner 用的就是这个模块
    with PluginSession(entry, limits=FAST):
        pass
    assert len(seen) == 1
    flags = seen[0]
    if sys.platform == "win32":
        assert flags["creationflags"] == runner._CREATE_SUSPENDED  # pyright: ignore[reportPrivateUsage]
        assert flags["creationflags"] == 0x00000004  # Win32 CREATE_SUSPENDED
        assert flags["start_new_session"] is False
    else:
        assert flags["start_new_session"] is True
        assert flags["creationflags"] == 0


def test_job_assign_failure_kills_suspended_process(
    entry: PluginEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows Job Object")
    from duohelm.isolation import _windows  # noqa: PLC0415

    attempted: list[tuple[int, str]] = []
    resumed: list[int] = []

    def failing_assign(self: _windows.Job, pid: int) -> None:
        attempted.append((pid, psutil.Process(pid).status()))
        raise OSError(5, "模拟 AssignProcessToJobObject 失败")

    monkeypatch.setattr(_windows.Job, "assign", failing_assign)
    monkeypatch.setattr(runner, "_resume", resumed.append)

    session = PluginSession(entry, limits=FAST)
    try:
        with pytest.raises(PluginRunError) as info:
            session.start()
        assert info.value.reason == "load"
        assert "Job" in info.value.message
        assert resumed == [], "加入 Job 失败后不得恢复进程"
        [(pid, status)] = attempted
        assert status == psutil.STATUS_STOPPED, "加入 Job 时进程应处于挂起状态"
        # 在 close() 之前检查：start() 抛错之前就必须结束这个进程，不能依赖会话关闭
        assert wait_gone(pid, timeout=2), "挂起的进程没有在抛错前被结束"
    finally:
        session.close()


def test_job_refuses_closed_handle() -> None:
    if sys.platform != "win32":
        pytest.skip("Windows Job Object")
    from duohelm.isolation._windows import Job, JobClosedError  # noqa: PLC0415

    job = Job(64 * 1024**2)
    job.close()
    # 当前进程很可能在某个 Job 里（例如被 IDE 或终端启动）：空句柄会被当作“任意 Job”
    with pytest.raises(JobClosedError):
        job.contains(os.getpid())
    with pytest.raises(JobClosedError):
        job.assign(os.getpid())


DATASOURCE_CODE = r"""
from datetime import date

import polars as pl

from duohelm_sdk import Capabilities, Freq
from duohelm_sdk.schema import SECURITIES_SCHEMA

ROWS = [
    ("600000.SH", "浦发银行", "SH", "stock", date(1999, 11, 10), None),
    ("600087.SH", "退市长油", "SH", "stock", date(1997, 6, 12), date(2014, 6, 4)),
]


class Source:
    def capabilities(self):
        return Capabilities(markets=("CN",), freqs=(Freq.DAY,), fields=("open",))

    def securities(self, *, include_delisted=False):
        rows = ROWS if include_delisted else [r for r in ROWS if r[5] is None]
        return pl.DataFrame(rows, schema=SECURITIES_SCHEMA, orient="row")

    def lookup_securities(self, symbols):
        rows = [r for r in ROWS if r[0] in symbols]
        return pl.DataFrame(rows, schema=SECURITIES_SCHEMA, orient="row")

    def trading_calendar(self, market, start, end):
        return pl.DataFrame({"date": [start]})

    def bars(self, symbols, freq, start, end):
        raise NotImplementedError

    def adj_factors(self, symbols, start, end):
        raise NotImplementedError

    def index_constituents(self, index, start, end):
        raise NotImplementedError


plugin = Source()
"""

DATASOURCE_MANIFEST = """
[plugin]
id = "iso-source"
name = "iso-source"
version = "1.0.0"
kind = "datasource"
sdk = ">=0.4,<0.5"
entrypoint = "iso_source:plugin"
"""


def test_datasource_security_methods(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    path = root / "iso-source"
    path.mkdir(parents=True)
    (path / "iso_source.py").write_text(DATASOURCE_CODE, encoding="utf-8")
    (path / "duohelm-plugin.toml").write_text(DATASOURCE_MANIFEST, encoding="utf-8")
    [entry] = PluginRegistry([root]).scan()
    assert entry.status is PluginStatus.READY, entry.problems
    with PluginSession(entry, limits=FAST) as s:
        listed = s.call("securities")
        everything = s.call("securities", include_delisted=True)
        found = s.call("lookup_securities", ["600087.SH", "609999.SH"])
    assert listed["symbol"].to_list() == ["600000.SH"]
    assert everything["symbol"].to_list() == ["600000.SH", "600087.SH"]
    assert found["symbol"].to_list() == ["600087.SH"]
    assert found["delist_date"].to_list() == [date(2014, 6, 4)]


def test_capabilities_roundtrip_and_unknown_freq_skipped(tmp_path: Path) -> None:
    from duohelm_sdk import Capabilities, Freq  # noqa: PLC0415

    store = FrameStore(tmp_path, "t")
    caps = Capabilities(markets=("CN",), freqs=(Freq.DAY,), fields=("open",), lists_delisted=True)
    assert decode(encode(caps, store), store) == caps
    # 新版 SDK 的插件可能声明当前版本不认识的频率：跳过，不能报错
    newer = {"$capabilities": {"markets": ["CN"], "freqs": ["1d", "5s"], "fields": []}}
    decoded = decode(newer, store)
    assert decoded.freqs == (Freq.DAY,)
    assert decoded.lists_delisted is False
