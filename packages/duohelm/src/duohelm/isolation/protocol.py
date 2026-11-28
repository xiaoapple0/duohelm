"""父子进程之间的通信协议。

控制消息：4 字节大端长度 + UTF-8 JSON，长度不超过 ``MAX_MESSAGE_BYTES``。
数据表：写成 Arrow IPC 文件放在会话临时目录，消息里只传文件名。

值的编码（``encode`` / ``decode``）：
- ``polars.DataFrame`` -> ``{"$frame": "<文件名>"}``
- ``datetime.date`` -> ``{"$date": "YYYY-MM-DD"}``
- ``Capabilities`` -> ``{"$capabilities": {...}}``
- ``StrEnum`` -> 字符串；tuple / list -> 列表；dict 的键必须是字符串
"""

from __future__ import annotations

import itertools
import json
import logging
import struct
from datetime import date, datetime
from pathlib import Path
from typing import Any, BinaryIO, cast

import polars as pl

from duohelm_sdk import Capabilities, ConnectionStatus, Freq

logger = logging.getLogger(__name__)

DATA_CATEGORIES = frozenset({"securities", "calendar", "bars_1d", "adj_factors"})
"""平台认识的数据类别 id，见 ``Capabilities.categories`` 和 docs/subsystems/data-sources.md。"""

MAX_MESSAGE_BYTES = 8 * 1024 * 1024
_HEADER = struct.Struct(">I")


class ProtocolError(Exception):
    """消息格式不对、超长，或者对方提前关闭了管道。"""


def write_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"消息长度 {len(payload)} 字节，超过上限 {MAX_MESSAGE_BYTES}")
    stream.write(_HEADER.pack(len(payload)) + payload)
    stream.flush()


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """读一条消息。对方正常关闭（在消息边界处 EOF）时返回 None。"""
    header = _read_exact(stream, _HEADER.size, allow_eof=True)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    if length > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"消息长度 {length} 字节，超过上限 {MAX_MESSAGE_BYTES}")
    payload = _read_exact(stream, length, allow_eof=False)
    assert payload is not None
    try:
        message: object = json.loads(payload.decode("utf-8"))
    except ValueError as exc:
        raise ProtocolError(f"消息不是合法的 JSON：{exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("消息必须是 JSON 对象")
    return cast(dict[str, Any], message)


def _read_exact(stream: BinaryIO, size: int, *, allow_eof: bool) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if allow_eof and remaining == size:
                return None
            raise ProtocolError("管道在消息中途关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class FrameStore:
    """会话临时目录里的数据表文件。每一方用不同的前缀，避免文件名冲突。"""

    def __init__(self, directory: Path, prefix: str) -> None:
        self.directory = directory
        self._prefix = prefix
        self._counter = itertools.count(1)

    def write(self, df: pl.DataFrame) -> str:
        name = f"{self._prefix}-{next(self._counter)}.arrow"
        df.write_ipc(self.directory / name)
        return name

    def read(self, name: str) -> pl.DataFrame:
        path = self._resolve(name)
        # 不对文件做内存映射：先整个读进内存再解析，文件句柄立即关闭。Windows 上被映射
        # 的文件无法删除，会话结束时临时目录会删不掉。polars 2.x 的 read_ipc 已经没有
        # memory_map 参数，从 bytes 读取是能明确保证不映射文件的方式。
        df = pl.read_ipc(path.read_bytes())
        path.unlink()
        return df

    def _resolve(self, name: str) -> Path:
        path = (self.directory / name).resolve()
        if path.parent != self.directory.resolve() or not name.endswith(".arrow"):
            raise ProtocolError(f"数据文件名不合法：{name!r}")
        return path


def encode(value: object, frames: FrameStore) -> Any:  # noqa: PLR0911  按类型分派
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return str(value)  # StrEnum 也是 str，转成普通字符串
    if isinstance(value, pl.DataFrame):
        return {"$frame": frames.write(value)}
    if isinstance(value, datetime):
        raise ProtocolError("M0 只支持日频，不支持跨进程传递 datetime")
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Capabilities):
        return {
            "$capabilities": {
                "markets": list(value.markets),
                "freqs": [str(f) for f in value.freqs],
                "fields": list(value.fields),
                "lists_delisted": value.lists_delisted,
                "exact_delist_date": value.exact_delist_date,
                "categories": (
                    None if value.categories is None else [str(c) for c in value.categories]
                ),
            }
        }
    if isinstance(value, ConnectionStatus):
        return {"$connection_status": {"ok": bool(value.ok), "message": str(value.message)}}
    if isinstance(value, list | tuple):
        return [encode(v, frames) for v in cast(list[object] | tuple[object, ...], value)]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in cast(dict[object, object], value).items():
            if not isinstance(k, str) or k.startswith("$"):
                raise ProtocolError(f"字典的键必须是不以 $ 开头的字符串：{k!r}")
            out[k] = encode(v, frames)
        return out
    raise ProtocolError(f"不支持跨进程传递的类型：{type(value).__name__}")


def decode(value: Any, frames: FrameStore) -> Any:  # noqa: PLR0911  按类型分派
    if isinstance(value, list):
        return [decode(v, frames) for v in cast(list[Any], value)]
    if not isinstance(value, dict):
        return value
    obj = cast(dict[str, Any], value)
    if "$frame" in obj:
        return frames.read(str(obj["$frame"]))
    if "$date" in obj:
        return date.fromisoformat(str(obj["$date"]))
    if "$capabilities" in obj:
        caps = cast(dict[str, Any], obj["$capabilities"])
        known_freqs = {f.value for f in Freq}
        return Capabilities(
            markets=tuple(cast(list[str], caps["markets"])),
            # Freq 是可扩展的枚举：不认识的频率跳过，不能报错
            freqs=tuple(Freq(f) for f in cast(list[str], caps["freqs"]) if f in known_freqs),
            fields=tuple(cast(list[str], caps["fields"])),
            lists_delisted=bool(caps.get("lists_delisted", False)),
            exact_delist_date=bool(caps.get("exact_delist_date", True)),
            categories=_decode_categories(caps.get("categories")),
        )
    if "$connection_status" in obj:
        status = cast(dict[str, Any], obj["$connection_status"])
        return ConnectionStatus(ok=bool(status["ok"]), message=str(status.get("message", "")))
    return {k: decode(v, frames) for k, v in obj.items()}


def _decode_categories(raw: object) -> tuple[str, ...] | None:
    """``Capabilities.categories``：缺字段或为 null 时是 None（插件没有声明）。

    类别 id 以后可能新增（同枚举新增取值的规则）：不认识的 id 跳过并记日志，不能报错。
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ProtocolError(f"Capabilities.categories 必须是列表：{raw!r}")
    known: list[str] = []
    for item in cast(list[object], raw):
        if isinstance(item, str) and item in DATA_CATEGORIES:
            known.append(item)
        else:
            logger.warning("插件声明了不认识的数据类别 %r，已跳过", item)
    return tuple(known)
