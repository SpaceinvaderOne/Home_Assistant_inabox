"""Keep a Home Assistant VM running, without ever pretending to know whether
Home Assistant itself is healthy.

Watching is opt-in per VM. A VM that keeps dying is given up on rather than
restarted forever, and the reason is surfaced rather than acted on.
"""

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.discovery import FoundVM
from app.events import Event
from app.libvirtctl import Virsh
from app.notify import Importance
from app.notify import notify as _notify_file
from app.web import routes_firstboot as rf
from app.web import routes_wizard as rw
from app.web import state_store, webui_registry
from app.web.routes_install import WEBUI_URL

DEFAULT_INTERVAL_MINUTES = 15
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 60

MAX_CONSECUTIVE_FAILURES = 5

MAX_CONSECUTIVE_RESTARTS = 5

RESTART_WINDOW_POLLS = MAX_CONSECUTIVE_RESTARTS * 3

UNREACHABLE_NOTIFY_AFTER = 3

_ACTION_FOR_STATE: dict[str, str] = {
    "shut off": "start",
    "paused": "resume",
}
_PAST_TENSE = {"start": "started", "resume": "resumed"}

_LOG = logging.getLogger(__name__)


async def _default_wait(seconds: float, wake: asyncio.Event) -> None:
    """Wait up to `seconds`, or until `wake` is set -- whichever comes
    first.
    """
    try:
        await asyncio.wait_for(wake.wait(), timeout=seconds)
    except TimeoutError:
        pass  # the ordinary case: the interval elapsed with no change
    finally:
        wake.clear()


@dataclass
class _WatchEntry:
    """Everything this module remembers about one VM's watch, keyed by
    name in Watcher._entries. Never constructed with enabled=True by
    anything but an explicit Watcher.set_enabled(name, True) call -- see
    this module's own docstring on why that must stay true."""

    enabled: bool = False
    consecutive_failures: int = 0
    gave_up: bool = False
    gave_up_reason: str | None = None
    consecutive_unreachable: int = 0
    unreachable_notified: bool = False
    restart_window: deque[bool] = field(default_factory=lambda: deque(maxlen=RESTART_WINDOW_POLLS))
    stuck_state: str | None = None
    action_lock: threading.Lock = field(default_factory=threading.Lock)


class Watcher:
    """One process-lifetime watcher, the same shape as
    app.web.routes_install.InstallRunner and
    app.web.routes_firstboot.FirstBootRunner: app-level state that outlives
    any single request, built once as the module-level WATCHER below.
    """

    def __init__(
        self,
        *,
        virsh_factory: Callable[[], Virsh] = Virsh,
        discover: Callable[[Virsh], list[FoundVM]] | None = None,
        notify: Callable[[str, str, Importance], bool] = _notify_file,
    ) -> None:
        self._virsh_factory = virsh_factory
        self._discover = discover
        self._notify = notify
        self._entries: dict[str, _WatchEntry] = {}
        self._entries_guard = threading.Lock()
        self._interval_minutes = DEFAULT_INTERVAL_MINUTES
        self._wake = asyncio.Event()

    # ---- per-VM watch state -------------------------------------------

    def _entry_for(self, name: str) -> _WatchEntry:
        """The entry for `name`, creating one if this is the first time
        anything has ever asked about it. Guarded (see `_entries_guard`'s
        own docstring) because creation -- specifically, the fresh
        threading.Lock() a new entry carries -- must happen exactly once
        per name, not once per concurrent caller."""
        entry = self._entries.get(name)
        if entry is not None:
            return entry
        with self._entries_guard:
            return self._entries.setdefault(name, _WatchEntry())

    def is_enabled(self, name: str) -> bool:
        entry = self._entries.get(name)
        return entry is not None and entry.enabled

    def gave_up(self, name: str) -> bool:
        entry = self._entries.get(name)
        return entry is not None and entry.gave_up

    def gave_up_reason(self, name: str) -> str | None:
        """What was actually observed to make this VM's watch give up --
        see _WatchEntry.gave_up_reason's own docstring. None whenever
        `gave_up` is False, so a caller never has to check both."""
        entry = self._entries.get(name)
        return None if entry is None else entry.gave_up_reason

    def failures(self, name: str) -> int:
        entry = self._entries.get(name)
        return 0 if entry is None else entry.consecutive_failures

    def action_lock(self, name: str) -> threading.Lock:
        """One lock per VM name, shared with
        app.web.routes_status.start_vm.
        """
        return self._entry_for(name).action_lock

    def set_enabled(self, name: str, enabled: bool) -> None:
        """The one path that may ever turn watching on for a VM -- the
        checkbox POST (app.web.routes_status.set_watch), which re-confirms
        the VM is currently `confirmed` before ever calling this, and the
        one install-terminal-event caller pre-ticking the VM it just
        installed. Nothing in this class calls this on its own account.
        """
        entry = self._entry_for(name)
        entry.enabled = enabled
        if enabled:
            entry.gave_up = False
            entry.gave_up_reason = None
            entry.consecutive_failures = 0
            entry.restart_window.clear()
        state_store.record_watch(name, enabled)

    def reset(self) -> None:
        """Test isolation only -- see FirstBootRunner.reset/InstallRunner's
        own reset() for the identical purpose. Also drops the interval back
        to its default, so one test's set_interval() call can never leak
        into the next."""
        self._entries.clear()
        self._interval_minutes = DEFAULT_INTERVAL_MINUTES
        self._wake.clear()

    # ---- the shared interval --------------------------------------------

    def interval_minutes(self) -> int:
        return self._interval_minutes

    def set_interval(self, minutes: int) -> None:
        """Clamped, never rejected: configurable from 1 to 60 minutes.
        A number input can send anything; there is no value here wrong
        enough to deserve an error page, only one worth correcting.
        """
        self._interval_minutes = max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, int(minutes)))
        self._wake.set()
        state_store.record_interval(self._interval_minutes)

    # ---- the poll itself -------------------------------------------------

    def poll_once(self, emit: Callable[[Event], None] | None = None, virsh=None) -> None:
        """One pass over every VM this watcher is currently enabled for."""
        virsh = virsh if virsh is not None else self._virsh_factory()
        discover = self._discover if self._discover is not None else rw._discover
        try:
            found = discover(virsh)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            _LOG.warning("watcher: discovery scan failed; skipping this poll: %s", exc)
            return

        by_name = {vm.name: vm for vm in found}
        for name in list(self._entries):
            entry = self._entries[name]
            if not entry.enabled:
                continue
            vm = by_name.get(name)
            if vm is None or vm.confidence not in ("confirmed", "likely"):
                continue
            try:
                self._poll_vm(virsh, vm, entry, emit)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:
                _LOG.warning("watcher: polling %r failed; continuing: %s", name, exc)

    def _poll_vm(self, virsh, vm: FoundVM, entry: _WatchEntry, emit) -> None:
        action = _ACTION_FOR_STATE.get(vm.state)
        if action is not None:
            self._attempt_action(virsh, vm, entry, action, emit)
            return
        if vm.state == "running":
            entry.restart_window.append(False)
            entry.stuck_state = None
            self._track_reachability(vm, entry, emit)
            self._recheck_webui(virsh, vm)
            return
        self._track_stuck(vm, entry, emit)

    def _attempt_action(self, virsh, vm: FoundVM, entry: _WatchEntry, action: str, emit) -> None:
        with entry.action_lock:
            try:
                getattr(virsh, action)(vm.name)
                new_state = virsh.state(vm.name)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:
                self._record_failure(vm, entry, action, str(exc), emit)
                return

        if new_state != "running":
            self._record_failure(vm, entry, action, f"still {new_state!r} after {action}", emit)
            return

        entry.consecutive_failures = 0
        verb = _PAST_TENSE[action]
        _LOG.info("watcher: %s %r -> running", verb, vm.name)

        entry.restart_window.append(True)
        restart_count = sum(entry.restart_window)
        if restart_count >= MAX_CONSECUTIVE_RESTARTS:
            entry.gave_up = True
            entry.enabled = False
            entry.gave_up_reason = (
                f"it kept coming back up and dying again ({restart_count} times in its last "
                f"{len(entry.restart_window)} polls), not failing to start"
            )
            self._emit_safely(
                emit,
                Event(
                    "watcher",
                    "fail",
                    f"{vm.name}: needed restarting {restart_count} times in its last "
                    f"{len(entry.restart_window)} polls -- giving up",
                ),
            )
            self._notify_safely(
                subject=f"{vm.name} will not stay up",
                description=(
                    f"{vm.name} has needed restarting {restart_count} times in its last "
                    f"{len(entry.restart_window)} polls -- it keeps coming back up and dying "
                    "again, not failing to start. This container is not going to keep "
                    "restarting it indefinitely; re-enable watching from the status panel "
                    "once you've checked what's wrong."
                ),
                importance="alert",
            )
            return

        self._emit_safely(emit, Event("watcher", "ok", f"{vm.name}: was {vm.state}, {verb} it"))
        self._notify_safely(
            subject=f"{vm.name} {verb}",
            description=f"{vm.name} was {vm.state}; this container {verb} it.",
            importance="normal",
        )

    def _record_failure(
        self, vm: FoundVM, entry: _WatchEntry, action: str, reason: str, emit
    ) -> None:
        entry.consecutive_failures += 1
        _LOG.warning(
            "watcher: could not %s %r (attempt %d/%d): %s",
            action,
            vm.name,
            entry.consecutive_failures,
            MAX_CONSECUTIVE_FAILURES,
            reason,
        )
        self._emit_safely(
            emit,
            Event(
                "watcher",
                "fail",
                f"{vm.name}: could not {action} it "
                f"(attempt {entry.consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {reason}",
            ),
        )
        if entry.consecutive_failures < MAX_CONSECUTIVE_FAILURES:
            return

        entry.gave_up = True
        entry.enabled = False
        entry.gave_up_reason = (
            f"it would not {action} after {MAX_CONSECUTIVE_FAILURES} attempts in a row "
            f"(last error: {reason})"
        )
        self._notify_safely(
            subject=f"Giving up on {vm.name}",
            description=(
                f"This container could not {action} {vm.name} after "
                f"{MAX_CONSECUTIVE_FAILURES} attempts in a row. Last error: {reason}. "
                "Watching is now off for this VM -- re-enable it from the status "
                "panel once you've checked what's wrong."
            ),
            importance="alert",
        )

    def _track_reachability(self, vm: FoundVM, entry: _WatchEntry, emit) -> None:
        """Surfaces, never acts -- the more important rule. This
        method's only possible side effects are a log line and, once an
        outage has lasted UNREACHABLE_NOTIFY_AFTER consecutive polls, a
        `warning` notification -- never a virsh call of any kind, however
        long `vm.answered` stays False. Home Assistant can be legitimately
        unreachable for minutes during its own core updates and database
        migrations; restarting a running VM over that alone is exactly the
        v2-shaped mistake this rule exists to prevent.
        """
        if vm.answered:
            entry.consecutive_unreachable = 0
            entry.unreachable_notified = False
            return

        entry.consecutive_unreachable += 1
        _LOG.warning(
            "watcher: %r is running but Home Assistant is not answering (%d consecutive polls)",
            vm.name,
            entry.consecutive_unreachable,
        )
        if entry.consecutive_unreachable < UNREACHABLE_NOTIFY_AFTER or entry.unreachable_notified:
            return
        entry.unreachable_notified = True
        self._emit_safely(
            emit,
            Event(
                "watcher",
                "info",
                f"{vm.name}: running, but Home Assistant hasn't answered for "
                f"{entry.consecutive_unreachable} checks in a row",
            ),
        )
        self._notify_safely(
            subject=f"{vm.name} is running, but Home Assistant is not answering",
            description=(
                f"{vm.name}'s VM has stayed up, but this container has not been able "
                "to reach Home Assistant on it for several checks in a row. This is "
                "not being treated as a reason to restart the VM -- Home Assistant is "
                "legitimately unreachable for minutes during its own core updates and "
                "database migrations, and restarting it mid-update would risk its "
                "database."
            ),
            importance="warning",
        )

    def _track_stuck(self, vm: FoundVM, entry: _WatchEntry, emit) -> None:
        """A state this module has no safe verb for at all
        -- crashed, in shutdown, pmsuspended, or anything else libvirt
        might report -- is never actioned, but must never be silent
        either. A guest hung on ACPI shutdown, or a crashed VM this project
        deliberately cannot revive without the destroy it does not have,
        sitting there unattended and unremarked is exactly the failure
        this whole feature exists to catch, not a state worth ignoring
        just because there is nothing to *do* about it automatically.
        """
        if vm.state == entry.stuck_state:
            return  # already reported this exact streak
        entry.stuck_state = vm.state
        _LOG.warning("watcher: %r is %r; no safe action to take automatically", vm.name, vm.state)
        self._emit_safely(
            emit,
            Event(
                "watcher",
                "info",
                f"{vm.name}: is {vm.state}; nothing safe to do about that automatically",
            ),
        )
        self._notify_safely(
            subject=f"{vm.name} is {vm.state} and needs attention",
            description=(
                f"{vm.name} is currently {vm.state}. This container has no safe way to "
                "recover it automatically -- check it in Unraid's own VM manager."
            ),
            importance="warning",
        )

    def _recheck_webui(self, virsh, vm: FoundVM) -> None:
        """`webui` is written once at first boot and never revisited on its
        own, so a later port change (or a
        DHCP-moved address) leaves Unraid's own "Open WebUI" button pointing
        nowhere. This watcher is already polling every running, watched VM
        anyway, so it re-derives the correct URL on each poll and hands it
        to Virsh.set_webui -- which alone owns the "only overwrite
        empty-or-our-own-default" rule (see its own docstring); nothing here
        reimplements that rule, only builds the URL the same way
        app.web.routes_firstboot.FirstBootRunner._write_webui_if_needed
        does for the identical reason -- via the identical shared
        `rf._endpoint_url`, so the two writers cannot drift apart.
        """
        if vm.endpoint is None or vm.address is None:
            return  # nothing resolved to build a URL from this poll
        url = rf._endpoint_url(vm.endpoint, vm.address)
        known = webui_registry.known_for(vm.name)
        defaults = (WEBUI_URL, known) if known is not None else WEBUI_URL
        try:
            outcome = virsh.set_webui(vm.name, url, default=defaults)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            _LOG.warning("watcher: could not correct %r's webui: %s", vm.name, exc)
            return
        if outcome is True:
            webui_registry.remember(vm.name, url)

    def _emit_safely(self, emit: Callable[[Event], None] | None, event: Event) -> None:
        """Mirrors `_notify_safely`'s own discipline for the
        identical reason -- `emit` is normally `bus.publish`, called from
        run_forever's own worker thread, and a raising callable here must
        not be able to abort whichever caller is mid-decision (most
        pointedly `_record_failure`, which calls this *before* checking
        whether this poll is the one that escalates)."""
        if emit is None:
            return
        try:
            emit(event)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            _LOG.warning("watcher: emit failed: %s", exc)

    def _notify_safely(self, *, subject: str, description: str, importance: Importance) -> None:
        """A notification failure must never break the watcher (
        and the brief's own words). app.notify.notify already guarantees it
        never raises on its own account -- this is the second, independent
        guarantee against whatever `self._notify` actually is, since tests
        are free to inject one that
        does not share that discipline."""
        try:
            self._notify(subject, description, importance)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            _LOG.warning("watcher: notification failed (%r): %s", subject, exc)

    # ---- the background loop --------------------------------------------

    async def run_forever(
        self,
        emit: Callable[[Event], None] | None = None,
        *,
        wait: Callable[[float, asyncio.Event], Awaitable[None]] = _default_wait,
        to_thread: Callable[..., Awaitable[None]] = asyncio.to_thread,
    ) -> None:
        """Poll on an interval, forever, without blocking the request path."""
        while True:
            try:
                await to_thread(self.poll_once, emit)
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except BaseException:
                _LOG.exception("watcher: poll_once failed; the watcher is still running")
            try:
                await wait(self._interval_minutes * 60, self._wake)
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except BaseException:
                _LOG.exception("watcher: wait failed; the watcher is still running")


WATCHER = Watcher()
