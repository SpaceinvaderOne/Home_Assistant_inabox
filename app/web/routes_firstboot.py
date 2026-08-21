"""The first-boot step: watch a freshly-started HAOS VM boot, and hand off
to the ready step once it answers.
"""

import re
import threading
import time
from dataclasses import replace

from app.events import Event
from app.haclient import (
    CANDIDATE_PORTS,
    Endpoint,
    OnboardingState,
    onboarding_status,
    port_open,
    resolve_endpoint,
)
from app.libvirtctl import Virsh
from app.web import webui_registry
from app.web.firstboot import MILESTONES, FirstBootWatcher, Milestone
from app.web.routes_install import WEBUI_URL
from app.web.routes_wizard import available_name
from app.web.session import SESSION
from app.web.session import reset as reset_session
from app.web.steps import next_step
from app.web.steps import step as wizard_step

_ONBOARDING_UNKNOWN = OnboardingState(reachable=False, complete=False, steps=())

STALL_GRACE_PERIOD_S = 300.0

_HOSTNAME_LABEL_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _usable_hostname(value: object) -> bool:
    """Is this something safe to put in a URL and show to the user?"""
    return isinstance(value, str) and _HOSTNAME_LABEL_RE.fullmatch(value) is not None


class _HAClient:
    """Adapts app.haclient's bare port_open()/resolve_endpoint() functions to
    FirstBootWatcher's HAClientLike protocol (an object with bound methods)
    -- the same shape RealDeps in routes_install.py gives the installer's
    free-function dependencies.
    """

    def port_open(self, ip: str, port: int) -> bool:
        return port_open(ip, port)

    def resolve_endpoint(self, ip: str) -> Endpoint | None:
        return resolve_endpoint(ip)


class FirstBootRunner:
    """Tracks the one boot-watch this container runs at a time."""

    def __init__(self) -> None:
        self._watcher: FirstBootWatcher | None = None
        self._vm_name: str | None = None
        self._mac: str | None = None
        self._started_at: float | None = None
        self._emit = None
        self._hostname: str | None = None
        self._hostname_raw: object = None
        self._hostname_attempted = False
        self._webui_ok = False
        self._webui_attempted = False
        self._onboarding_state: OnboardingState | None = None
        self._known_onboarding_steps: tuple = ()
        self._announced_onboarding_steps: set[str] = set()
        self._onboarding_ever_complete = False
        self._onboarding_complete_announced = False
        self._onboarding_asked = False
        # Whether Home Assistant has ever actually replied -- see answered().
        self._ha_answered = False
        self._poll_lock = threading.Lock()
        self._onboarding_lock = threading.Lock()
        self._hostname_lock = threading.Lock()

    def start(self, vm_name: str, mac: str, emit) -> None:
        """Idempotent: a second terminal install event (a retried install, a
        duplicated signal) while a watch is already running must not spawn a
        second poller racing the first.
        """
        if self._watcher is not None:
            return
        self._watcher = FirstBootWatcher(Virsh(), _HAClient(), emit)
        self._vm_name = vm_name
        self._mac = mac
        self._started_at = time.monotonic()
        self._emit = emit

    def state(self) -> list[Milestone]:
        if self._watcher is None or self._vm_name is None:
            return [Milestone(key, label) for key, label in MILESTONES]
        if self._poll_lock.acquire(blocking=False):
            try:
                self._watcher.poll_once(self._vm_name, self._mac)
                self._write_webui_if_needed()
            finally:
                self._poll_lock.release()
        return self._watcher.state()

    def _write_webui_if_needed(self) -> None:
        """Correct the VM's WebUI attribute once Home Assistant's resolved
        address is known.
        """
        if self._webui_attempted or self._watcher is None or self._vm_name is None:
            return
        agent = next(m for m in self._watcher.state() if m.key == "agent")
        if not agent.reached:
            return
        resolved = self.resolved_endpoint()
        if resolved is None:
            return
        address = self.address()
        if address is None:
            return

        self._webui_attempted = True
        self._correct_webui(_endpoint_url(resolved, address))

    def _correct_webui(self, url: str) -> None:
        """Point Unraid's own "Open WebUI" link at `url`, through
        Virsh.set_webui's existing admin-safety rule -- never reimplemented
        here, only reused.
        """
        if self._vm_name is None:
            return
        try:
            known = webui_registry.known_for(self._vm_name)
            defaults = (WEBUI_URL, known) if known is not None else WEBUI_URL
            outcome = Virsh().set_webui(self._vm_name, url, default=defaults)
        except Exception as err:
            if self._emit is not None:
                self._emit(Event("webui", "fail", f"could not correct the WebUI link: {err}"))
            return
        if outcome is False:
            return
        if outcome is None:
            if self._emit is not None:
                self._emit(
                    Event(
                        "webui",
                        "info",
                        "leaving the existing WebUI link as it is",
                    )
                )
            return
        self._webui_ok = True
        webui_registry.remember(self._vm_name, url)
        if self._emit is not None:
            self._emit(Event("webui", "ok", f"WebUI link corrected to {url}"))

    def resume_key(self) -> str | None:
        """Which step a bare `GET /` should land on, or None if this container
        has never started watching a VM.
        """
        if self._watcher is None:
            return None
        web = next(m for m in self._watcher.state() if m.key == "web")
        return "ready" if web.reached else "firstboot"

    def onboarding_complete(self) -> bool:
        """Has this watch's own onboarding genuinely been observed complete?"""
        return self._onboarding_ever_complete

    def watching(self) -> bool:
        """Is there a live boot-watch at all?"""
        return self._watcher is not None

    def elapsed(self) -> float:
        if self._started_at is None:
            return 0.0
        return time.monotonic() - self._started_at

    def address(self) -> str | None:
        """The VM's LAN IP, once the watcher has found one -- see
        FirstBootWatcher.address's own docstring for how it is kept live
        until "web" latches and frozen after."""
        return None if self._watcher is None else self._watcher.address

    def resolved_endpoint(self) -> Endpoint | None:
        """Where Home Assistant actually answered, once poll_once has
        resolved it -- see FirstBootWatcher.resolved. The
        single source the ready step's onboarding mirror, both of its links,
        and the WebUI write all read through, so none of them can ever show
        or write a different port than the others. None until resolution
        succeeds, and reset() clears it the same way it clears address(): by
        dropping the watcher it's read through.
        """
        return None if self._watcher is None else self._watcher.resolved

    def hostname(self) -> str | None:
        """The guest agent's reported hostname, for the ready step's
        `<hostname>.local` address -- or None if it isn't one this code is
        willing to hand onward.
        """
        if self._hostname_attempted or self._vm_name is None:
            return self._hostname
        if self._hostname_lock.acquire(blocking=False):
            raw: object = None
            try:
                raw = Virsh().guest_hostname(self._vm_name)
            except Exception:
                raw = None
            finally:
                self._hostname_raw = raw
                self._hostname = raw if _usable_hostname(raw) else None
                self._hostname_attempted = True
                self._hostname_lock.release()
        return self._hostname

    def answered(self) -> bool:
        """Has Home Assistant itself ever replied to this container?"""
        if self._ha_answered:
            return True
        if self._watcher is None:
            return False
        return next(m for m in self._watcher.state() if m.key == "web").reached

    def webui_ok(self) -> bool:
        """Is Unraid's own "Open WebUI" link for this VM known to reach Home
        Assistant? See the field's docstring in __init__ -- False whenever
        this container hasn't established that, including before the agent
        milestone has been reached at all."""
        return self._webui_ok

    def onboarding_asked(self) -> bool:
        """Has Home Assistant ever actually been asked about its onboarding?
        See the field's docstring in __init__: "not asked yet" and "asked and
        got nothing" both look like reachable=False, and the ready step has
        to tell them apart before it says anything about HA's health."""
        return self._onboarding_asked

    def onboarding(self) -> OnboardingState:
        """Home Assistant's own onboarding progress, mirrored for the ready
        step -- the same non-blocking-poll discipline as state() above, for
        the same reason: onboarding_status() can itself block for seconds,
        and a second concurrent caller (another tab, or this step's own
        reload racing its poll) must get the last-known state back
        immediately rather than queue behind a real network round-trip.
        """
        ip = self.address()
        if ip is not None and self._onboarding_lock.acquire(blocking=False):
            try:
                endpoint = self._resolve_if_needed(ip)
                if endpoint is not None:
                    try:
                        self._absorb_onboarding_poll(self._ask(ip, endpoint))
                    except Exception:
                        pass
                    finally:
                        self._onboarding_asked = True
            finally:
                self._onboarding_lock.release()
        return self._mirrored_onboarding_state()

    def _ask(self, ip: str, endpoint: Endpoint) -> OnboardingState:
        """Poll `endpoint`, and -- only if it has stopped answering -- try
        to find where Home Assistant went before concluding it
        cannot be reached at all. See `onboarding`'s own docstring for why
        this exists.
        """
        state = onboarding_status(endpoint)
        if state.reachable:
            return state
        fresh = self._reresolve(ip)
        if fresh is None:
            return state
        if fresh != endpoint:
            self._adopt_endpoint(fresh)
        return onboarding_status(fresh)

    def _reresolve(self, ip: str) -> Endpoint | None:
        """The same two-step probe `_resolve_if_needed` uses for the very
        first resolution, run again regardless of what is already latched
        -- see `_ask`'s own docstring for when this runs.
        """
        try:
            if not any(port_open(ip, port) for port in CANDIDATE_PORTS):
                return None
            return resolve_endpoint(ip)
        except Exception:
            return None

    def _adopt_endpoint(self, endpoint: Endpoint) -> None:
        """Home Assistant answered somewhere new: make every consumer of
        the old address follow, not just this one poll's own result.
        """
        if self._watcher is not None:
            self._watcher.resolved = endpoint
        address = self.address()
        if address is not None:
            self._correct_webui(_endpoint_url(endpoint, address))

    def _resolve_if_needed(self, ip: str) -> Endpoint | None:
        """Where Home Assistant answered, resolving it here if nothing has yet."""
        watcher = self._watcher
        if watcher is None:
            return None
        if watcher.resolved is not None:
            return watcher.resolved
        try:
            if not any(port_open(ip, port) for port in CANDIDATE_PORTS):
                return None
            resolved = resolve_endpoint(ip)
        except Exception:
            return None
        if resolved is not None and watcher.resolved is None:
            watcher.resolved = resolved
        return watcher.resolved

    def _mirrored_onboarding_state(self) -> OnboardingState:
        """What the ready step actually shows: reachable/complete as of the
        most recent poll, but with steps that survive a poll that came back
        empty (review F2/F8) -- a stalled read, a refused connection during
        a mid-onboarding HA restart, or the 404 HA answers once its own
        onboarding views are gone for good all report `steps=()`, and none
        of them should blank a checklist this runner already knows the real
        contents of.
        """
        latest = self._onboarding_state or _ONBOARDING_UNKNOWN
        complete = latest.complete or (self._onboarding_ever_complete and not latest.steps)
        steps = latest.steps or self._known_onboarding_steps
        if complete and steps:
            steps = tuple(replace(step, done=True) for step in steps)
        return OnboardingState(reachable=latest.reachable, complete=complete, steps=steps)

    def _absorb_onboarding_poll(self, new_state: OnboardingState) -> None:
        """Record one poll's result, then announce it -- in that order
        (review F6): committing state first means a broken `emit()` raising
        out of the announce step below still leaves the *next* read of
        onboarding() with accurate reachable/complete/steps, rather than
        permanently stuck on whatever the state was at the last poll (a
        real repro: four consecutive successful polls with a raising emit
        left the screen reading "not responding" forever)."""
        if new_state.steps:
            self._known_onboarding_steps = new_state.steps
        self._onboarding_state = new_state
        if new_state.reachable:
            self._ha_answered = True
        if new_state.complete:
            self._onboarding_ever_complete = True
        elif new_state.steps:
            self._onboarding_ever_complete = False

        self._publish_onboarding_changes(new_state)
        if new_state.complete and not self._onboarding_complete_announced:
            self._onboarding_complete_announced = True
            if self._emit is not None:
                self._emit(Event("onboarding", "ok", "Home Assistant setup complete"))

    def _publish_onboarding_changes(self, new_state: OnboardingState) -> None:
        """Emit one event per step that just finished, onto the same bus
        firstboot's own milestones use -- so the activity log picks up
        onboarding's own progress too, the way design intent #2 asks for
        ("pushed to the browser over the existing SSE stream").
        """
        if self._emit is None:
            return
        for step in new_state.steps:
            if step.done and step.key not in self._announced_onboarding_steps:
                self._announced_onboarding_steps.add(step.key)
                self._emit(Event("onboarding", "ok", step.label))

    def reset(self) -> None:
        """Forget everything -- called before a fresh install starts, so a
        retry never leaves a stale watcher's state bleeding into the new
        one."""
        self._watcher = None
        self._vm_name = None
        self._mac = None
        self._started_at = None
        self._emit = None
        self._hostname = None
        self._hostname_raw = None
        self._hostname_attempted = False
        self._webui_attempted = False
        self._webui_ok = False
        self._onboarding_asked = False
        self._ha_answered = False
        self._onboarding_state = None
        self._known_onboarding_steps = ()
        self._announced_onboarding_steps = set()
        self._onboarding_ever_complete = False
        self._onboarding_complete_announced = False


WATCHER = FirstBootRunner()


def next_step_key(key: str) -> str | None:
    """Re-exports steps.next_step so both the routes below and the
    navigation-chain test have one place to ask what comes after a step,
    matching routes_wizard.py's own next_step_key."""
    return next_step(key)


_state = WATCHER.state
_watching = WATCHER.watching
_elapsed = WATCHER.elapsed
_address = WATCHER.address
_hostname = WATCHER.hostname
_resolved_endpoint = WATCHER.resolved_endpoint
_onboarding = WATCHER.onboarding
_onboarding_asked = WATCHER.onboarding_asked
_answered = WATCHER.answered
_webui_ok = WATCHER.webui_ok
_resume_key = WATCHER.resume_key


def _endpoint_url(resolved: Endpoint | None, host: str) -> str:
    """The URL to show for `host` -- the port Home Assistant actually
    resolved to, once known (so the IP and hostname links always agree, by
    construction, via Endpoint.with_host), or a bare guess with no port at
    all before that: never a hardcoded ":8123" this wizard cannot back up
    with an observation. A bare guess defaults to the scheme's own port
    (80 for http), which is also what app.web.routes_install.WEBUI_URL's
    own pre-boot default assumes -- correct for every install this wizard
    creates today, per Home Assistant Core 2026.8's move off 8123.
    """
    if resolved is not None:
        return resolved.with_host(host).base_url
    return f"http://{host}"


def resume_step_key() -> str | None:
    """Where `GET /` should land, or None when no VM is being watched -- see
    FirstBootRunner.resume_key. Also what tells POST /install that accepting
    an install would abandon a VM this container is already watching.
    """
    return _resume_key()


def home_resume_key() -> str | None:
    """Like resume_step_key(), except it stops resuming once this watch's
    own onboarding has genuinely been observed complete -- change
    #3: "bare GET / stops resuming to Ready once onboarding is complete --
    the panel takes over," per the spec's own words (§6.3: "wizard flips to
    Done, status panel takes over"). Used by GET /'s own landing decision
    (app.web.main.index) and by the status panel's activity banner
    (app.web.routes_status) -- nowhere else.
    """
    if WATCHER.onboarding_complete():
        return None
    return _resume_key()


def start_another() -> None:
    """Clear the watch and the session: the explicit "I'm done with that
    one" action a live watch otherwise has no way out of short of
    restarting the container (see resume_step_key's own docstring for why
    GET / resumes on it in the first place). Called from POST /another-vm
    once app.web.main has confirmed no install is actually running -- a
    real install in progress is a different situation, covered by POST
    /install's own busy refusal, not this one.
    """
    WATCHER.reset()
    reset_session()
    SESSION.name = available_name(SESSION.name)


def watch_conflict_context() -> dict:
    """The page shown when an install is asked for while a watch is live."""
    key = _resume_key() or "firstboot"
    return {"vm_name": SESSION.name, "watch_step": key, "watch_label": wizard_step(key).label}


def firstboot_context() -> dict:
    milestones = _state()
    elapsed = _elapsed()
    running = next(m for m in milestones if m.key == "running")
    agent = next(m for m in milestones if m.key == "agent")
    web = next(m for m in milestones if m.key == "web")
    address = _address()
    grace_exceeded = elapsed >= STALL_GRACE_PERIOD_S
    return {
        "watching": _watching(),
        "milestones": milestones,
        "unseen": grace_exceeded and not running.reached,
        "stalled": grace_exceeded and running.reached and not agent.reached,
        "no_web": grace_exceeded and running.reached and agent.reached and not web.reached,
        "web_reached": web.reached,
        "address": address,
        "address_url": _endpoint_url(None, address) if address else None,
        "vm_name": SESSION.name,
    }


def ready_context() -> dict:
    """The handoff step: Home Assistant's own address(es) to open, and its
    onboarding mirrored from /api/onboarding -- never driven from here, only
    observed (design intent #4)."""
    resolved = _resolved_endpoint()
    address = _address()
    hostname = _hostname()
    return {
        "watching": _watching(),
        "onboarding": _onboarding(),
        "onboarding_asked": _onboarding_asked(),
        "answered": _answered(),
        "address": address,
        "hostname": hostname,
        "ip_url": _endpoint_url(resolved, address) if address else None,
        "hostname_url": _endpoint_url(resolved, f"{hostname}.local") if hostname else None,
        # Whether Unraid's own VM manager is known to link at Home Assistant,
        "webui_ok": _webui_ok(),
        "vm_name": SESSION.name,
    }
