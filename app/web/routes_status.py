"""The status panel: what this container has to say once it knows
about a Home Assistant VM, instead of nothing at all.
"""

import logging
from dataclasses import dataclass, replace

from app.discovery import FoundVM
from app.web import routes_firstboot as rf
from app.web import routes_install as ri
from app.web import routes_wizard as rw
from app.web import watcher as wt
from app.web.session import SESSION

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VMStatus:
    """What the status panel shows for one VM -- derived from a single
    FoundVM, never re-deriving anything discovery did not itself observe.
    """

    name: str
    state: str
    running: bool
    stopped: bool
    address: str | None
    url: str | None
    answered: bool
    watch_enabled: bool
    watch_gave_up: bool
    watch_gave_up_reason: str | None


def _status_of(vm: FoundVM, watcher: wt.Watcher) -> VMStatus:
    return VMStatus(
        name=vm.name,
        state=vm.state,
        running=vm.state == "running",
        stopped=vm.state == "shut off",
        address=vm.address,
        url=vm.endpoint.base_url if vm.endpoint is not None else None,
        answered=vm.answered,
        watch_enabled=watcher.is_enabled(vm.name),
        watch_gave_up=watcher.gave_up(vm.name),
        watch_gave_up_reason=watcher.gave_up_reason(vm.name),
    )


@dataclass(frozen=True, slots=True)
class ActivityBanner:
    """The one thing worth telling the panel's visitor
    about something already in progress elsewhere in this container --
    never more than one at a time (the container runs one wizard), and
    never about a VM's own row, which is what the ordinary VM list already
    narrates.
    """

    kind: str
    step_key: str
    vm_name: str


def _activity_banner() -> ActivityBanner | None:
    """Which one banner (if any) belongs on the panel right now."""
    watch_key = rf.home_resume_key()
    if watch_key is not None:
        return ActivityBanner(kind="firstboot", step_key=watch_key, vm_name=SESSION.name)
    if ri.RUNNER.busy():
        return ActivityBanner(kind="installing", step_key="installing", vm_name=SESSION.name)
    if SESSION.version and not rf.WATCHER.watching():
        return ActivityBanner(kind="wizard", step_key="welcome", vm_name=SESSION.name)
    return None


def _shown_on_panel(found: list[FoundVM]) -> list[FoundVM]:
    """What this panel is sure enough about to list at all -- confirmed or
    likely, never "unknown". See this module's own docstring for why a
    the panel's own bar and why it is the one it is
    anymore."""
    return [vm for vm in found if vm.confidence in ("confirmed", "likely")]


def status_context(found: list[FoundVM], watcher: wt.Watcher | None = None) -> dict:
    """The status step's own template context, from a discovery scan the
    caller already has in hand -- this function never scans on its own
    account, so a caller that already scanned once for this request (GET /
    deciding whether to land here at all, or POST /vm/start re-confirming
    the VM it just started) never pays for a second scan just to render the
    page afterwards.
    """
    watcher = watcher if watcher is not None else wt.WATCHER
    shown = _shown_on_panel(found)
    watched = [vm for vm in shown if watcher.is_enabled(vm.name)]
    return {
        "vms": [_status_of(vm, watcher) for vm in shown],
        "watcher_interval": watcher.interval_minutes(),
        "multiple_watched": len(watched) > 1,
        "banner": _activity_banner(),
    }


def home_context(virsh) -> tuple[str, dict]:
    """What `GET /` shows once no in-progress boot-watch or install already
    claimed the front door directly (see app.web.main.index()'s own
    watch-then-busy shortcuts): the status panel
    whenever there is anything at all to show it for -- a confirmed or
    likely Home Assistant VM (see `_shown_on_panel`), or an activity banner
    worth narrating (see `_activity_banner`) -- the ordinary welcome step
    only when genuinely neither applies. The rule is that
    the status panel is home: a server with no VMs and
    nothing running keeps welcome as its front page, unchanged.
    """
    existing, found = rw._existing_vm_check(virsh)
    context = status_context(found)
    if _shown_on_panel(found) or context["banner"] is not None:
        return "status", context

    checks = rw.preflight(virsh, existing=existing)
    welcome = {
        "checks": checks,
        "all_ok": all(c.ok for c in checks),
        "next_key": "version",
    }
    return "welcome", welcome


def start_vm(virsh, name: str, watcher: wt.Watcher | None = None) -> list[FoundVM]:
    """Start `name`, if -- and only if -- a fresh discovery scan still
    considers it Home Assistant (confirmed or likely -- the identical bar
    `_shown_on_panel` already uses) and it
    is genuinely shut off. Returns the scan used to make that decision, for
    the caller to render the panel from without a second scan of its own.
    """
    watcher = watcher if watcher is not None else wt.WATCHER
    found = rw._discover(virsh)
    target = next(
        (vm for vm in found if vm.name == name and vm.confidence in ("confirmed", "likely")), None
    )
    if target is None or target.state != "shut off":
        return found

    lock = watcher.action_lock(name)
    if not lock.acquire(blocking=False):
        _LOG.warning(
            "start_vm: %r is currently locked by the watcher's own action "
            "(possibly a hung virsh call) -- this click is a no-op; try again shortly",
            name,
        )
        return found
    try:
        virsh.start(name)
        started = replace(target, state=virsh.state(name))
    finally:
        lock.release()
    return [started if vm is target else vm for vm in found]


def set_watch(virsh, name: str, enabled: bool, watcher: wt.Watcher | None = None) -> list[FoundVM]:
    """Flip one VM's watch checkbox -- the central safety property,
    "watching is per-VM and opt-in", enforced exactly the way start_vm()
    already enforces "never trust the posted name alone": the page that
    rendered this checkbox and this POST are two different requests, and
    the only thing ever allowed to turn watching ON for a VM is a fresh
    discovery scan considering it Home Assistant *right now* -- confirmed
    or likely, the identical bar `_shown_on_panel` already uses.
    """
    watcher = watcher if watcher is not None else wt.WATCHER
    found = rw._discover(virsh)
    if enabled:
        target = next(
            (vm for vm in found if vm.name == name and vm.confidence in ("confirmed", "likely")),
            None,
        )
        if target is not None:
            watcher.set_enabled(name, True)
    else:
        watcher.set_enabled(name, False)
    return found


def set_watcher_interval(minutes: int, watcher: wt.Watcher | None = None) -> None:
    """How often the watcher checks, in minutes -- "configurable
    1-60". Clamping itself lives in Watcher.set_interval; this is just the
    seam POST /watcher/interval calls through, mirroring set_watch's own
    injectable-watcher shape for the same testability reason."""
    watcher = watcher if watcher is not None else wt.WATCHER
    watcher.set_interval(minutes)
