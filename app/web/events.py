"""Fan-out from the installer's synchronous emit callback to async SSE subscribers."""

import asyncio
import html
from collections import deque
from collections.abc import AsyncIterator

from app.events import Event

HISTORY_LIMIT = 500

_STATUS_CLASS = {
    "ok": "ev-ok",
    "fail": "ev-fail",
    "info": "ev-info",
    "start": "ev-cmd",
    "progress": "",
}


def _running_loop_here() -> asyncio.AbstractEventLoop | None:
    """The event loop driving the *current* thread, if any."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class EventBus:
    """Thread-safe publish, async subscribe."""

    def __init__(self, history_limit: int = HISTORY_LIMIT):
        self._history: deque[Event] = deque(maxlen=history_limit)
        self._queues: list[asyncio.Queue[Event]] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, event: Event) -> None:
        self._history.append(event)
        if not self._queues:
            return

        if self._loop is not None and self._loop.is_running():
            for queue in list(self._queues):
                self._loop.call_soon_threadsafe(queue.put_nowait, event)
            return

        if _running_loop_here() is not None:
            for queue in list(self._queues):
                queue.put_nowait(event)
            return

        raise RuntimeError(
            "EventBus.publish() has a subscriber waiting but no loop was ever "
            "bound via bind_loop(), and this thread has none of its own. Call "
            "EventBus.bind_loop() during application startup before publishing "
            "from a worker thread."
        )

    def history(self) -> list[Event]:
        return list(self._history)

    async def subscribe(self) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._queues.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._queues.remove(queue)


def format_line(event: Event) -> str:
    """One dock line. Everything from an Event is escaped — details carry URLs,
    libvirt stderr and user-supplied VM names.
    """
    step_attr = html.escape(event.step, quote=True)
    status_attr = html.escape(event.status, quote=True)
    data = f' data-step="{step_attr}" data-status="{status_attr}"'

    if event.status == "progress":
        if not event.total:
            return f"<div{data}>  {html.escape(event.step)}: {event.done} bytes</div>"
        pct = event.done * 100 // event.total
        return f"<div{data}>  {html.escape(event.step)}: {pct}%</div>"

    marker = {"start": "->", "ok": "OK", "fail": "FAIL", "info": "  "}[event.status]
    css = _STATUS_CLASS.get(event.status, "")
    if event.detail:
        detail = ": " + "<br>".join(html.escape(line) for line in event.detail.splitlines())
    else:
        detail = ""
    return f'<div class="{css}"{data}>{marker} {html.escape(event.step)}{detail}</div>'


def sse_frame(payload: str) -> str:
    """`payload` as one complete Server-Sent Events frame."""
    return "".join(f"data: {line}\n" for line in payload.splitlines()) + "\n"
