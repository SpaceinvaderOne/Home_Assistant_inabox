"""Run the install as a background task, and the state the browser polls for."""

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from app import download as dl
from app.capabilities import highest_q35
from app.events import Event
from app.hostinfo import free_bytes, host_path_for
from app.installer import STEPS, TERMINAL_STEP, Deps, InstallError, InstallPlan, install
from app.libvirtctl import Virsh
from app.releases import Release, fetch_latest, fetch_releases
from app.vmicon import place_vm_icon
from app.web.routes_wizard import DOMAINS_CONTAINER_PATH
from app.web.session import SESSION

WEBUI_URL = "http://homeassistant.local"

# "Not more than about twice a second" for the dock -- see throttle_progress.
PROGRESS_MIN_INTERVAL = 0.5


def throttle_progress(
    emit: Callable[[Event], None],
    *,
    min_interval: float = PROGRESS_MIN_INTERVAL,
    now: Callable[[], float] = time.monotonic,
) -> Callable[[Event], None]:
    """Wrap `emit` so "progress" events are collapsed before they become dock lines."""
    last_pct: dict[str, int] = {}
    last_emitted: dict[str, float] = {}

    def wrapped(event: Event) -> None:
        if event.status != "progress":
            emit(event)
            return

        step = event.step
        if event.total:
            pct = event.done * 100 // event.total
            if last_pct.get(step) == pct:
                return
        else:
            pct = None

        moment = now()
        last_time = last_emitted.get(step)
        if last_time is not None and moment - last_time < min_interval:
            return

        if pct is not None:
            last_pct[step] = pct
        last_emitted[step] = moment
        emit(event)

    return wrapped


class InstallRunner:
    """Tracks the one install this container may run at a time."""

    def __init__(self) -> None:
        self._busy = False
        self._completed_steps: list[str] = []
        self._current_step: str | None = None
        self._failed_step: str | None = None

    def busy(self) -> bool:
        return self._busy

    def state(self) -> dict:
        return {
            "busy": self._busy,
            "completed_steps": list(self._completed_steps),
            "current_step": self._current_step,
            "failed_step": self._failed_step,
        }

    def reset(self) -> None:
        self._busy = False
        self._completed_steps = []
        self._current_step = None
        self._failed_step = None

    def start(self, plan: InstallPlan, deps: Deps, emit: Callable[[Event], None]) -> None:
        """Launch the install and return immediately -- the route that calls
        this must not block on a job that can take minutes."""
        if self._busy:
            raise RuntimeError("an install is already running")
        self._busy = True
        self._completed_steps = []
        self._current_step = None
        self._failed_step = None

        throttled = throttle_progress(emit)

        def tracking_emit(event: Event) -> None:
            if event.status == "start":
                self._current_step = event.step
            elif event.status == "ok":
                if event.step not in self._completed_steps:
                    self._completed_steps.append(event.step)
                if self._current_step == event.step:
                    self._current_step = None
            elif event.status == "fail":
                self._failed_step = event.step
                if self._current_step == event.step:
                    self._current_step = None
            throttled(event)

        asyncio.create_task(self._run(plan, deps, tracking_emit))

    async def _run(self, plan: InstallPlan, deps: Deps, emit: Callable[[Event], None]) -> None:
        try:
            await asyncio.to_thread(_install, plan, deps, emit)
        except InstallError as err:
            self._failed_step = err.step
        finally:
            self._busy = False


RUNNER = InstallRunner()


class RealDeps:
    """Binds the installer's Protocol to the real modules, for a genuine install."""

    def __init__(self, virsh: Virsh):
        self._virsh = virsh

    def free_bytes(self, path):
        return free_bytes(path)

    def exists(self, path):
        return Path(path).exists()

    def sha256(self, path):
        return dl.file_sha256(Path(path))

    def download(self, url, dest, sha256, progress=None):
        return dl.download(url, Path(dest), sha256, progress=progress)

    def decompress(self, src, dest, progress=None):
        return dl.decompress(Path(src), Path(dest), progress=progress)

    def remove(self, path):
        Path(path).unlink(missing_ok=True)

    def resize(self, image, size_gib):
        dl.resize(Path(image), size_gib)

    def check_ovmf_vars(self):
        return dl.check_ovmf_vars()

    def place_vm_icon(self, name):
        return place_vm_icon(name)

    def write_xml(self, xml, path):
        Path(path).write_text(xml)
        return path

    def define(self, path):
        self._virsh.define(path)

    def start(self, name):
        self._virsh.start(name)


_install = install


def _machine() -> str:
    return highest_q35(Virsh().capabilities())


def _domains_host_path() -> str:
    return host_path_for(DOMAINS_CONTAINER_PATH)


def _release_for(version: str) -> Release:
    """Resolve the version step's choice to a full Release."""
    if version and version != "latest":
        for release in fetch_releases(30):
            if release.version == version:
                return release
    return fetch_latest()


def _current_plan() -> InstallPlan:
    """Build the plan the review step displays and /install acts on, from one
    place, so the two can never show and install different things.
    """
    machine = _machine()
    domains_host_path = _domains_host_path()
    release = _release_for(SESSION.version)
    settings = SESSION.to_settings(machine=machine, domains_host_path=domains_host_path)
    return InstallPlan(
        settings=settings,
        release=release,
        size_gib=SESSION.size_gib,
        domains_host_path=domains_host_path,
        domains_container_path=DOMAINS_CONTAINER_PATH,
        webui=WEBUI_URL,
    )


# ---- Review step ------------------------------------------------------------


def review_context() -> dict:
    plan = _current_plan()
    return {
        "settings": plan.settings,
        "release": plan.release,
        "size_gib": plan.size_gib,
    }


# ---- Installing step ---------------------------------------------------------


def installing_context() -> dict:
    return {"install_steps": STEPS, "state": RUNNER.state(), "terminal_step": TERMINAL_STEP}
