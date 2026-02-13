import io
import threading
from collections import deque
from datetime import datetime
from typing import Iterable

_lock = threading.Lock()
_buffer: deque[str] = deque(maxlen=400)  # keep last ~400 lines


def log(*parts: object) -> None:
    msg = " ".join(str(p) for p in parts)
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {msg}"
    with _lock:
        _buffer.append(line)
    print(line)


def dump_lines(limit: int | None = None) -> Iterable[str]:
    with _lock:
        lines = list(_buffer)
    if limit is None:
        return lines
    return lines[-limit:]


def dump_text(limit: int | None = None) -> str:
    return "\n".join(dump_lines(limit=limit))


def dump_bytes(limit: int | None = None) -> bytes:
    return dump_text(limit=limit).encode("utf-8")


def dump_file(name: str = "logs.txt", limit: int | None = None) -> io.BytesIO:
    data = io.BytesIO(dump_bytes(limit=limit))
    data.name = name
    data.seek(0)
    return data
