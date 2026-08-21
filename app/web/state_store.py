"""The three pieces of state that survive a container restart: the per-VM
watch flags, the watcher interval, and the record of webui URLs this project
has written. One JSON file in the config directory, written atomically.

Everything else (wizard progress, flap counters, the first-boot watch) is
deliberately in memory only.
"""

import contextlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

from app.installer import CONFIG_PATH

_LOG = logging.getLogger(__name__)

STATE_FILE_NAME = "state.json"
DEFAULT_STATE_PATH = Path(CONFIG_PATH) / STATE_FILE_NAME

_KNOWN_KEYS = frozenset({"watch_enabled", "interval_minutes", "webui"})


@dataclass
class PersistedState:
    """Exactly the three pieces this module persists, plus whatever a
    *future* version of this file might have added that this one does not
    understand yet -- `extra` carries any other top-level key verbatim so a
    reader that runs before this file is next saved does
    not have to lose it (see `_to_dict`).
    """

    watch_enabled: dict[str, bool] = field(default_factory=dict)
    interval_minutes: int | None = None
    webui: dict[str, str] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


def _from_dict(data: dict) -> PersistedState:
    """Defensive by field, not just by file: one malformed piece (a
    non-dict `watch_enabled`, a boolean masquerading as `interval_minutes`
    -- `bool` is a subclass of `int` in Python, so this must be checked
    before the plain `int` check or `True` would silently become `1`) is
    dropped back to that piece's own default rather than aborting the
    whole load. Never raises -- `load()` is the only caller, and it must be
    able to trust that whatever this returns is always safe to use."""
    watch_enabled: dict[str, bool] = {}
    raw_watch = data.get("watch_enabled")
    if isinstance(raw_watch, dict):
        watch_enabled = {
            name: value
            for name, value in raw_watch.items()
            if isinstance(name, str) and isinstance(value, bool)
        }

    interval_minutes = data.get("interval_minutes")
    if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int):
        interval_minutes = None

    webui: dict[str, str] = {}
    raw_webui = data.get("webui")
    if isinstance(raw_webui, dict):
        webui = {
            name: value
            for name, value in raw_webui.items()
            if isinstance(name, str) and isinstance(value, str)
        }

    extra = {key: value for key, value in data.items() if key not in _KNOWN_KEYS}

    return PersistedState(
        watch_enabled=watch_enabled,
        interval_minutes=interval_minutes,
        webui=webui,
        extra=extra,
    )


def _to_dict(state: PersistedState) -> dict:
    """The inverse of `_from_dict` -- `extra` is spread first so a known
    field always wins if some future version's key ever collided with it,
    though that should never happen in practice."""
    payload = dict(state.extra)
    payload["watch_enabled"] = dict(state.watch_enabled)
    if state.interval_minutes is not None:
        payload["interval_minutes"] = state.interval_minutes
    payload["webui"] = dict(state.webui)
    return payload


def load(path: str | Path) -> PersistedState:
    """Read `path` and parse it, never raising -- see this module's own
    docstring for exactly what "missing" vs "corrupt" each do. Pure: does
    not touch `_PATH`/`_CACHE` below, so this is safe to call directly
    (every test that wants to inspect exactly what was saved, or hand-write
    a fixture file and check what restoring it would produce, uses this
    rather than going through `enable`)."""
    p = Path(path)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return PersistedState()  # first run -- normal, not logged
    except (OSError, UnicodeDecodeError) as exc:
        _LOG.warning("state: could not read %s (%s) -- starting with fresh defaults", p, exc)
        return PersistedState()

    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object at the top level, got {type(data).__name__}")
    except ValueError as exc:
        _LOG.warning(
            "state: %s is corrupt or not valid JSON (%s) -- starting with fresh defaults; "
            "the file itself is left in place for inspection",
            p,
            exc,
        )
        return PersistedState()

    return _from_dict(data)


_SAVE_LOCK = threading.Lock()


def save(state: PersistedState, path: str | Path) -> None:
    """Atomically write `state` to `path` -- a temp file in the same
    directory, then `os.replace`, guarded by `_SAVE_LOCK` for the whole
    write-and-replace sequence so two concurrent callers can never leave a
    half-written or interleaved file behind (see the module docstring's own
    "Concurrency" section). Never raises: a failure here degrades to
    in-memory-only for this one write rather than taking down whichever
    route handler or watcher poll triggered it.
    """
    p = Path(path)
    text = json.dumps(_to_dict(state), indent=2, sort_keys=True) + "\n"
    with _SAVE_LOCK:
        tmp_name = None
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, p)
                tmp_name = None  # replaced -- nothing left to clean up
            finally:
                if tmp_name is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_name)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            _LOG.warning("state: could not save %s (%s) -- continuing in-memory only", p, exc)


# ---- the live mirror, shared by Watcher/webui_registry's own callers ------

_LOCK = threading.Lock()
_PATH: Path | None = None
_CACHE: PersistedState = PersistedState()


def enabled() -> bool:
    """Whether `record_*` below currently does anything -- test-visibility
    only; production code never needs to ask."""
    return _PATH is not None


def enable(path: str | Path) -> PersistedState:
    """Point every future `record_*` call at `path`, loading it now (never
    raising -- see `load`) to seed the in-memory mirror, and returning a
    defensive copy of what was loaded so the caller (`create_app`) can
    apply it to the live `Watcher`/`webui_registry` without either of them
    reaching back into this module's own internals.
    """
    global _PATH, _CACHE
    with _LOCK:
        _PATH = Path(path)
        _CACHE = load(_PATH)
        return replace(
            _CACHE,
            watch_enabled=dict(_CACHE.watch_enabled),
            webui=dict(_CACHE.webui),
            extra=dict(_CACHE.extra),
        )


def _save_locked() -> None:
    """Caller must already hold `_LOCK`. `_PATH` is never `None` here --
    every caller checks `enabled()`'s own condition first."""
    save(_CACHE, _PATH)


def record_watch(name: str, watch_enabled: bool) -> None:
    """`Watcher.set_enabled`'s own persistence half -- called for every
    invocation, on or off; a no-op whenever persistence itself is off (see
    `enabled`). Turning a VM off removes its key entirely rather than
    storing `false`, so the file only ever lists VMs currently opted in --
    smaller, and it means a hand-authored fixture that *does* include an
    explicit `false` (exercising forward-compatible tolerance, not
    something this module's own `save` ever produces) is still handled
    correctly by the restore step simply never turning it on.
    """
    with _LOCK:
        if _PATH is None:
            return
        if watch_enabled:
            _CACHE.watch_enabled[name] = True
        else:
            _CACHE.watch_enabled.pop(name, None)
        _save_locked()


def record_interval(minutes: int) -> None:
    """`Watcher.set_interval`'s own persistence half -- see `record_watch`
    for the no-op-when-disabled discipline this shares."""
    with _LOCK:
        if _PATH is None:
            return
        _CACHE.interval_minutes = minutes
        _save_locked()


def record_webui(name: str, url: str) -> None:
    """`webui_registry.remember`'s own persistence half -- see
    `record_watch` for the no-op-when-disabled discipline this shares."""
    with _LOCK:
        if _PATH is None:
            return
        _CACHE.webui[name] = url
        _save_locked()


def reset() -> None:
    """Test isolation only -- mirrors every other module-level registry's
    own reset() in this project (app.web.watcher.Watcher.reset,
    app.web.webui_registry.reset). Turns persistence back off entirely
    (`_PATH` back to `None`) rather than merely clearing the mirror, so a
    test that enabled it does not leak real file writes into whichever
    test runs next in the same process."""
    global _PATH, _CACHE
    with _LOCK:
        _PATH = None
        _CACHE = PersistedState()
