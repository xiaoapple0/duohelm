"""Windows Job Object：进程树内存上限、一次结束整棵进程树。

子进程（以及它启动的所有孙进程）加入同一个 Job。关闭 Job 句柄或调用
``terminate`` 时，Job 里的所有进程一起结束。
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class _BasicLimits(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _IoCounters(ctypes.Structure):
        _fields_ = tuple(
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        )

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.IsProcessInJob.restype = wintypes.BOOL
    _kernel32.IsProcessInJob.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    )
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    class JobClosedError(RuntimeError):
        """Job 句柄已经关闭。"""

    def _check(ok: object, what: str) -> None:
        if not ok:
            raise OSError(ctypes.get_last_error(), f"{what} 失败")

    class Job:
        """一个带内存上限、关闭即结束所有进程的 Job。"""

        def __init__(self, memory_bytes: int) -> None:
            handle = _kernel32.CreateJobObjectW(None, None)
            _check(handle, "CreateJobObjectW")
            self._handle: int | None = handle
            info = _ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = (
                _JOB_OBJECT_LIMIT_JOB_MEMORY
                | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            )
            info.JobMemoryLimit = memory_bytes
            _check(
                _kernel32.SetInformationJobObject(
                    handle,
                    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                ),
                "SetInformationJobObject",
            )

        def _open_handle(self) -> int:
            """当前的 Job 句柄。已关闭时报错：空句柄传给 Win32 API 会被当作“任意 Job”。"""
            if self._handle is None:
                raise JobClosedError("Job 已关闭")
            return self._handle

        def assign(self, pid: int) -> None:
            handle = self._open_handle()
            process = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            _check(process, "OpenProcess")
            try:
                _check(
                    _kernel32.AssignProcessToJobObject(handle, process),
                    "AssignProcessToJobObject",
                )
            finally:
                _kernel32.CloseHandle(process)

        def contains(self, pid: int) -> bool:
            # IsProcessInJob 的 Job 参数为空时，含义是“是否在任意一个 Job 里”，结果会悄悄出错。
            handle = self._open_handle()
            process = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            _check(process, "OpenProcess")
            try:
                result = wintypes.BOOL()
                _check(
                    _kernel32.IsProcessInJob(process, handle, ctypes.byref(result)),
                    "IsProcessInJob",
                )
                return bool(result.value)
            finally:
                _kernel32.CloseHandle(process)

        def terminate(self) -> None:
            if self._handle is not None:
                _kernel32.TerminateJobObject(self._handle, 1)

        def close(self) -> None:
            """关闭句柄。KILL_ON_JOB_CLOSE 保证此时 Job 里剩下的进程全部结束。"""
            if self._handle is not None:
                _kernel32.CloseHandle(self._handle)
                self._handle = None
