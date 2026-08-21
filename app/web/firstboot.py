"""Watch a freshly-started HAOS VM boot through four observable milestones."""

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from app.events import Event
from app.haclient import CANDIDATE_PORTS, Endpoint

# key, label -- in the order they are reached during a real boot.
MILESTONES: tuple[tuple[str, str], ...] = (
    ("running", "The VM is running"),
    ("agent", "Guest tools responding"),
    ("address", "Address obtained"),
    ("web", "Home Assistant is up"),
)


class VirshLike(Protocol):
    def state(self, name: str) -> str: ...
    def agent_ping(self, name: str) -> bool: ...
    def interface_addresses(self, name: str, mac: str | None = None) -> list[str]: ...


class HAClientLike(Protocol):
    def port_open(self, ip: str, port: int) -> bool: ...
    def resolve_endpoint(self, ip: str) -> Endpoint | None: ...


@dataclass(frozen=True, slots=True)
class Milestone:
    key: str
    label: str
    reached: bool = False
    detail: str = ""
    seconds: float = 0.0


class FirstBootWatcher:
    """Poll a VM's boot progress and publish one event per milestone reached.

    There is no server-side timer: polling is driven by the browser's own 2s
    fetch, through FirstBootRunner.state().
    """

    def __init__(
        self,
        virsh: VirshLike,
        ha: HAClientLike,
        emit: Callable[[Event], None],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._virsh = virsh
        self._ha = ha
        self._emit = emit
        self._clock = clock
        self._start_time = clock()
        self._milestones: dict[str, Milestone] = {
            key: Milestone(key, label) for key, label in MILESTONES
        }
        self.address: str | None = None
        self.resolved: Endpoint | None = None

    def state(self) -> list[Milestone]:
        return [self._milestones[key] for key, _ in MILESTONES]

    def poll_once(self, vm_name: str, mac: str) -> None:
        """Take one consistent look at the VM and latch any newly-true milestone."""
        elapsed = self._clock() - self._start_time
        try:
            state = self._virsh.state(vm_name)
            self._latch("running", elapsed, state == "running")
            self._latch("agent", elapsed, self._virsh.agent_ping(vm_name))

            addresses = self._virsh.interface_addresses(vm_name, mac=mac)
            if not self._milestones["web"].reached and addresses:
                self.address = addresses[0]
            self._latch("address", elapsed, bool(addresses), detail=self.address or "")

            if (
                self.address is not None
                and self.resolved is None
                and any(self._ha.port_open(self.address, port) for port in CANDIDATE_PORTS)
            ):
                self.resolved = self._ha.resolve_endpoint(self.address)
            self._latch("web", elapsed, self.resolved is not None, detail=self.address or "")
        except Exception:
            return

    def _latch(self, key: str, elapsed: float, condition: bool, detail: str = "") -> None:
        current = self._milestones[key]
        if current.reached or not condition:
            return
        self._milestones[key] = replace(current, reached=True, detail=detail, seconds=elapsed)
        message = f"{current.label} ({detail})" if detail else current.label
        self._emit(Event(key, "ok", message))
