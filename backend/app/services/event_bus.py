from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterator


EventCallback = Callable[[dict[str, Any]], None]


@dataclass
class RequestEventEmitter:
    callback: EventCallback
    seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def emit(self, event_type: str, **fields: Any) -> None:
        with self._lock:
            self.seq += 1
            event = {
                "type": str(event_type),
                "seq": self.seq,
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                **fields,
            }
        self.callback(event)


_EMITTER: ContextVar[RequestEventEmitter | None] = ContextVar(
    "request_event_emitter", default=None
)


@contextmanager
def capture(callback: EventCallback | None) -> Iterator[None]:
    if callback is None:
        yield
        return
    token = _EMITTER.set(RequestEventEmitter(callback=callback))
    try:
        yield
    finally:
        _EMITTER.reset(token)


def emit_event(event_type: str, **fields: Any) -> None:
    emitter = _EMITTER.get()
    if emitter is not None:
        emitter.emit(event_type, **fields)
