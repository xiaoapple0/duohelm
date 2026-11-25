"""父进程侧：在子进程里运行插件，负责超时、内存上限、取消和结束整棵进程树。

隔离的强度和威胁模型见 docs/subsystems/plugins.md 的"隔离"一节：M0 的隔离用于防止
有错误的插件拖垮平台，不防恶意代码。
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Literal, Self, cast

import polars as pl
import psutil

from duohelm.isolation.protocol import (
    FrameStore,
    ProtocolError,
    decode,
    encode,
    read_message,
    write_message,
)
from duohelm.registry import PluginEntry, PluginStatus
from duohelm_sdk.checks import check_factor_output

logger = logging.getLogger(__name__)

FailureReason = Literal[
    "timeout", "memory", "cancelled", "crashed", "load", "plugin_error", "protocol", "contract"
]
SUPPORTED_KINDS = frozenset({"factor", "datasource"})

# 子进程只继承这些环境变量。凭据、DUOHELM_* 配置、代理设置等一律不传。
_ENV_ALLOWLIST_WINDOWS = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
)
_ENV_ALLOWLIST_POSIX = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
_ENV_PASSTHROUGH = ("POLARS_MAX_THREADS",)

_CLOSE_GRACE_S = 5.0
_CREATE_SUSPENDED = 0x00000004


class PluginRunError(Exception):
    def __init__(self, reason: FailureReason, message: str, traceback: str | None = None) -> None:
        super().__init__(f"[{reason}] {message}")
        self.reason: FailureReason = reason
        self.message = message
        self.traceback = traceback


@dataclass(frozen=True)
class Limits:
    call_timeout_s: float = 300.0
    """每次调用的超时。"""
    startup_timeout_s: float = 60.0
    """启动子进程、导入插件的超时。"""
    memory_bytes: int = 2 * 1024**3
    """整棵进程树的内存上限。"""
    stderr_bytes: int = 256 * 1024
    """每次会话写入日志的 stderr 上限，超出部分丢弃。"""
    poll_interval_s: float = 0.5
    """内存和取消的检查间隔。"""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Limits:
        """读取 DUOHELM_PLUGIN_TIMEOUT（秒）和 DUOHELM_PLUGIN_MEMORY_MB。"""
        env = os.environ if env is None else env
        limits = cls()
        timeout = env.get("DUOHELM_PLUGIN_TIMEOUT", "").strip()
        memory = env.get("DUOHELM_PLUGIN_MEMORY_MB", "").strip()
        if timeout:
            value = float(timeout)
            if value <= 0:
                raise ValueError(f"DUOHELM_PLUGIN_TIMEOUT 必须为正数，当前为 {timeout}")
            limits = cls(call_timeout_s=value, memory_bytes=limits.memory_bytes)
        if memory:
            value_mb = int(memory)
            if value_mb <= 0:
                raise ValueError(f"DUOHELM_PLUGIN_MEMORY_MB 必须为正整数，当前为 {memory}")
            limits = cls(call_timeout_s=limits.call_timeout_s, memory_bytes=value_mb * 1024**2)
        return limits


def child_env(workdir: Path, parent: Mapping[str, str] | None = None) -> dict[str, str]:
    """子进程的环境变量：白名单里的系统变量，外加指向会话临时目录的 HOME 和临时目录。"""
    parent = os.environ if parent is None else parent
    allow = _ENV_ALLOWLIST_WINDOWS if sys.platform == "win32" else _ENV_ALLOWLIST_POSIX
    upper = {k.upper(): v for k, v in parent.items()} if sys.platform == "win32" else parent
    env = {name: upper[name] for name in (*allow, *_ENV_PASSTHROUGH) if name in upper}
    tmp = str(workdir)
    env.update({"TEMP": tmp, "TMP": tmp, "TMPDIR": tmp, "HOME": tmp, "USERPROFILE": tmp})
    env["PYTHONIOENCODING"] = "utf-8"
    return env


_EOF = object()


class PluginSession:
    """一个插件子进程。用 with 语句管理生命周期；会话内可以多次调用。

    ``cancel`` 被设置时立即结束子进程；``log`` 接收插件写到 stdout / stderr 的输出。
    """

    def __init__(
        self,
        entry: PluginEntry,
        *,
        deps: Sequence[PluginEntry] = (),
        limits: Limits | None = None,
        cancel: threading.Event | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if entry.status is not PluginStatus.READY or entry.manifest is None:
            raise PluginRunError("load", f"插件 {entry.id} 的状态是 {entry.status}，不能运行")
        info = entry.manifest.plugin
        if info.kind not in SUPPORTED_KINDS:
            raise PluginRunError("load", f"M0 不支持在隔离进程里运行 {info.kind} 插件")
        if info.entrypoint is None:
            raise PluginRunError("load", f"插件 {entry.id} 没有 entrypoint")
        self.entry = entry
        self.deps = tuple(deps)
        self.limits = limits or Limits()
        self._cancel = cancel
        self._log: Callable[[str], None] = log or self._default_log
        self._kind = info.kind
        self._entrypoint = info.entrypoint

        self.workdir: Path | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._interpreter_pid: int | None = None
        self._job: Any = None
        self._messages: queue.Queue[object] = queue.Queue()
        self._abort: PluginRunError | None = None
        self._abort_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._inputs: FrameStore | None = None
        self._outputs: FrameStore | None = None
        self._closed = False

    # ---- 生命周期 ----

    def __enter__(self) -> Self:
        try:
            self.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _default_log(self, line: str) -> None:
        logger.info("[plugin %s] %s", self.entry.id, line)

    @property
    def pid(self) -> int | None:
        """运行插件代码的解释器进程。

        Windows 上 venv 的 python.exe 是一个启动器，会再启动真正的解释器，所以它和
        Popen 拿到的 pid 不一定相同。
        """
        return self._interpreter_pid

    def start(self) -> None:
        workdir = Path(tempfile.mkdtemp(prefix="duohelm-plugin-"))
        self.workdir = workdir
        self._inputs = FrameStore(workdir, "in")
        self._outputs = FrameStore(workdir, "out")
        windows = sys.platform == "win32"
        proc = subprocess.Popen(
            [sys.executable, "-I", "-m", "duohelm.isolation.child"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=child_env(workdir),
            # POSIX：自成进程组，结束时整组一起结束。
            start_new_session=not windows,
            # Windows：先挂起，加入 Job 之后再恢复。venv 的 python.exe 是启动器，会再启动
            # 真正的解释器；只有在它运行之前加入 Job，后面启动的进程才会全部继承这个 Job。
            creationflags=_CREATE_SUSPENDED if windows else 0,
        )
        self._proc = proc
        # 直接写 sys.platform 判断：pyright 只认这种写法，在 Linux / macOS 上跳过这段代码。
        if sys.platform == "win32":
            from duohelm.isolation._windows import Job  # noqa: PLC0415

            try:
                job = Job(self.limits.memory_bytes)
                self._job = job
                job.assign(proc.pid)
            except OSError as exc:
                # 没有加入 Job 的进程不受内存上限约束、也不会随 Job 一起结束：
                # 绝不恢复它，直接结束这个仍处于挂起状态的进程。
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=_CLOSE_GRACE_S)
                if self._job is not None:
                    self._job.close()
                    self._job = None
                raise PluginRunError("load", f"无法把插件进程加入 Job Object：{exc}") from exc
            _resume(proc.pid)
        self._spawn(self._read_messages, "messages")
        self._spawn(self._drain_stderr, "stderr")
        self._spawn(self._monitor, "monitor")

        ready = self._receive(self.limits.startup_timeout_s)
        if ready.get("type") != "ready":
            raise self._fatal("protocol", f"子进程启动消息不对：{ready.get('type')!r}")
        interpreter = int(ready.get("pid", 0))
        if interpreter != proc.pid and interpreter not in _descendants(proc.pid):
            raise self._fatal("protocol", f"子进程报告的 pid {interpreter} 不是它自己")
        self._interpreter_pid = interpreter

        self._send(
            {
                "type": "start",
                "workdir": str(workdir),
                "sys_path": [str(e.path) for e in (self.entry, *self.deps)],
                "entrypoint": self._entrypoint,
                "kind": self._kind,
            }
        )
        started = self._receive(self.limits.startup_timeout_s)
        if started.get("type") == "error":
            raise self._fatal_from(started)
        if started.get("type") != "started":
            raise self._fatal("protocol", f"子进程启动消息不对：{started.get('type')!r}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None:
            if proc.poll() is None and self._abort is None:
                try:
                    self._send({"type": "close"})
                    proc.wait(timeout=_CLOSE_GRACE_S)
                except (OSError, PluginRunError, subprocess.TimeoutExpired):
                    pass
            # 插件可能留下了孙进程：不管主进程是否已经退出，都把整棵树结束掉。
            self._kill_tree()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
        self._stop.set()
        for t in self._threads:
            t.join(timeout=_CLOSE_GRACE_S)
        if self._job is not None:
            self._job.close()
            self._job = None
        if self.workdir is not None:
            _remove_tree(self.workdir)

    # ---- 调用 ----

    def call(self, method: str, *args: object, **kwargs: object) -> Any:
        """调用插件方法。DataFrame 参数和返回值经 Arrow IPC 文件传递。"""
        if self._closed or self._proc is None:
            raise PluginRunError("protocol", "会话已关闭")
        if self._abort is not None:
            raise self._abort
        assert self._inputs is not None and self._outputs is not None
        self._send(
            {
                "type": "call",
                "method": method,
                "args": encode(list(args), self._inputs),
                "kwargs": encode(dict(kwargs), self._inputs),
            }
        )
        reply = self._receive(self.limits.call_timeout_s)
        if reply.get("type") == "error":
            error = _from_error(reply)
            if error.reason in ("memory", "protocol"):
                self._set_abort(error)  # 子进程状态不可信，不再继续使用
                self._kill_tree()
            raise error
        if reply.get("type") != "result":
            raise self._fatal("protocol", f"意外的消息：{reply.get('type')!r}")
        return decode(reply.get("value"), self._outputs)

    def compute(self, factor_id: str, data: pl.DataFrame) -> pl.DataFrame:
        """计算一个因子。返回前用 check_factor_output 校验输出格式。"""
        out = self.call("compute", factor_id, data)
        if not isinstance(out, pl.DataFrame):
            raise PluginRunError(
                "contract", f"compute 应返回 DataFrame，实际为 {type(out).__name__}"
            )
        result = check_factor_output(out)
        if not result.passed:
            raise PluginRunError("contract", "；".join(result.details))
        return out

    # ---- 内部：收发 ----

    def _send(self, message: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            write_message(cast(BinaryIO, self._proc.stdin), message)
        except OSError as exc:
            raise self._exited() from exc

    def _receive(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            if self._abort is not None:
                raise self._abort
            if self._cancel is not None and self._cancel.is_set():
                raise self._fatal("cancelled", "任务已取消，插件进程已结束")
            try:
                item = self._messages.get(timeout=0.05)
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise self._fatal("timeout", f"超过 {timeout:g} 秒没有返回") from None
                continue
            if item is _EOF:
                raise self._exited()
            if isinstance(item, ProtocolError):
                raise self._fatal("protocol", str(item))
            return cast(dict[str, Any], item)

    def _exited(self) -> PluginRunError:
        """子进程已经退出：可能是被我们结束的（超时、内存、取消），也可能是自己崩溃。"""
        assert self._proc is not None
        try:
            code = self._proc.wait(timeout=_CLOSE_GRACE_S)
        except subprocess.TimeoutExpired:
            code = None
        if self._abort is not None:
            return self._abort
        return self._fatal("crashed", f"插件进程意外退出，退出码 {code}")

    # ---- 内部：线程 ----

    def _spawn(self, target: Callable[[], None], name: str) -> None:
        t = threading.Thread(target=target, name=f"plugin-{self.entry.id}-{name}", daemon=True)
        t.start()
        self._threads.append(t)

    def _read_messages(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream = cast(BinaryIO, self._proc.stdout)
        try:
            while True:
                message = read_message(stream)
                if message is None:
                    break
                self._messages.put(message)
        except ProtocolError as exc:
            self._messages.put(exc)
            return
        except (OSError, ValueError):
            pass
        self._messages.put(_EOF)

    def _drain_stderr(self) -> None:
        """把 stderr 写进日志，总量不超过上限；超出后只记一条说明，但继续读空管道。"""
        assert self._proc is not None and self._proc.stderr is not None
        stream = cast(io.BufferedReader, self._proc.stderr)
        budget = self.limits.stderr_bytes
        pending = b""
        truncated = False
        while True:
            try:
                chunk = stream.read1(65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            if truncated:
                continue
            take = chunk[:budget]
            budget -= len(take)
            pending += take
            *lines, pending = pending.split(b"\n")
            for line in lines:
                self._emit(line)
            if budget <= 0:
                if pending:
                    self._emit(pending)
                    pending = b""
                truncated = True
                self._log(f"[DuoHelm] 插件输出超过 {self.limits.stderr_bytes} 字节，后续输出已丢弃")
        if pending and not truncated:
            self._emit(pending)

    def _emit(self, line: bytes) -> None:
        text = line.decode("utf-8", errors="replace").rstrip("\r")
        if text:
            self._log(text)

    def _monitor(self) -> None:
        assert self._proc is not None
        pid = self._proc.pid
        while not self._stop.wait(self.limits.poll_interval_s):
            if self._proc.poll() is not None:
                return
            if self._cancel is not None and self._cancel.is_set():
                self._set_abort(PluginRunError("cancelled", "任务已取消，插件进程已结束"))
                self._kill_tree()
                return
            used = _tree_rss(pid)
            if used > self.limits.memory_bytes:
                limit_mb = self.limits.memory_bytes // 1024**2
                self._set_abort(
                    PluginRunError(
                        "memory", f"内存占用 {used // 1024**2} MiB，超过上限 {limit_mb} MiB"
                    )
                )
                self._kill_tree()
                return

    # ---- 内部：结束 ----

    def _set_abort(self, error: PluginRunError) -> None:
        with self._abort_lock:
            if self._abort is None:
                self._abort = error

    def _fatal(self, reason: FailureReason, message: str) -> PluginRunError:
        self._set_abort(PluginRunError(reason, message))
        self._kill_tree()
        assert self._abort is not None
        return self._abort

    def _fatal_from(self, reply: Mapping[str, Any]) -> PluginRunError:
        self._set_abort(_from_error(reply))
        self._kill_tree()
        assert self._abort is not None
        return self._abort

    def _kill_tree(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if self._job is not None:
            self._job.terminate()  # Windows：Job 里的整棵树一起结束
        family: list[psutil.Process] = []
        # 只在子进程还没被回收时按 pid 找进程树：回收之后 pid 可能被别的进程复用。
        if proc.poll() is None:
            with contextlib.suppress(psutil.Error):
                root = psutil.Process(proc.pid)
                family = [*root.children(recursive=True), root]
        if sys.platform != "win32":
            # 进程组里还有成员时，组 id 不会被复用；主进程退出后留下的孙进程也在组里。
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        for p in family:
            with contextlib.suppress(psutil.Error):
                p.kill()
        try:
            proc.wait(timeout=_CLOSE_GRACE_S)
        except subprocess.TimeoutExpired:
            logger.warning("插件进程 %s 在 %s 秒内没有退出", proc.pid, _CLOSE_GRACE_S)


def _from_error(reply: Mapping[str, Any]) -> PluginRunError:
    kind = str(reply.get("kind", "plugin_error"))
    reason: FailureReason
    if kind in ("memory", "load", "protocol", "plugin_error"):
        reason = cast(FailureReason, kind)
    else:
        reason = "plugin_error"
    return PluginRunError(reason, str(reply.get("message", "")), reply.get("traceback"))


def _resume(pid: int) -> None:
    """恢复以 CREATE_SUSPENDED 启动的进程（psutil 在 Windows 上用 NtResumeProcess）。"""
    psutil.Process(pid).resume()


def _descendants(pid: int) -> set[int]:
    try:
        return {p.pid for p in psutil.Process(pid).children(recursive=True)}
    except psutil.Error:
        return set()


def _tree_rss(pid: int) -> int:
    try:
        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.Error:
        return 0
    total = 0
    for p in procs:
        with contextlib.suppress(psutil.Error):
            total += p.memory_info().rss
    return total


def _remove_tree(path: Path) -> None:
    # Windows 上进程刚退出时文件句柄可能还没释放，稍等重试。
    for _ in range(20):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.1)
    logger.warning("无法删除插件会话临时目录 %s", path)
