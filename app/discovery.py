"""Find Home Assistant VMs by what they actually are, not by what they are called."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from xml.etree import ElementTree

from app import haosdisk, hostinfo
from app.haclient import CANDIDATE_PORTS, Endpoint, OnboardingState
from app.haosdisk import DiskIdentity

NETWORK_BUDGET_S = 3.0

Confidence = str


class VirshLike(Protocol):
    def list_domains(self) -> list[str]: ...
    def state(self, name: str) -> str: ...
    def dumpxml(self, name: str) -> str: ...
    def interface_addresses(self, name: str, mac: str | None = None) -> list[str]: ...


class HALike(Protocol):
    def port_open(self, ip: str, port: int) -> bool: ...
    def resolve_endpoint(self, ip: str, timeout: float) -> Endpoint | None: ...
    def onboarding_status(self, endpoint: Endpoint, timeout: float) -> OnboardingState: ...
    def manifest_name(self, endpoint: Endpoint, timeout: float) -> str | None: ...


@dataclass(frozen=True, slots=True)
class FoundVM:
    name: str
    state: str
    disk: str | None
    identity: DiskIdentity | None
    address: str | None
    endpoint: Endpoint | None
    confidence: Confidence
    evidence: str
    answered: bool


@dataclass(frozen=True, slots=True)
class _OtherDisk:
    """One `<disk>` element `_disk_sources` chose not to treat as a
    candidate file-backed HAOS disk -- its human-readable description,
    plus whether it could nonetheless be hiding a real HAOS root.
    """

    description: str
    could_carry_haos: bool


def _describe_other_disk(disk: ElementTree.Element) -> _OtherDisk:
    """A short, human-readable name for a `<disk>` element `_disk_sources`
    chose not to treat as a candidate HAOS disk -- real hardware, on the
    server, never silently folded into "no disk device was found" -- and
    whether it could actually be hiding a real HAOS root at all.
    """
    device = disk.get("device", "disk")
    source = disk.find("source")
    if device != "disk":
        location = source.get("file") if source is not None else None
        description = f"a {device} ({location})" if location else f"a {device} (no media)"
        return _OtherDisk(description, could_carry_haos=(device != "cdrom"))
    kind = disk.get("type", "file")
    location = None
    if source is not None:
        location = source.get("dev") or source.get("name") or source.get("volume")
    description = f"a {kind} disk ({location})" if location else f"a {kind} disk (no source)"
    return _OtherDisk(description, could_carry_haos=True)


def _disk_sources(domain_xml: str) -> tuple[list[str], list[_OtherDisk]]:
    """Every file-backed hard disk's `<source file="...">` (what this module
    can actually read), and every other `<disk>` element found, described
    and classified (see `_OtherDisk`), in document order -- so a VM whose
    only disk is a CD-ROM or a passed-through block device is never told
    "no disk device was found" about a disk that plainly exists.
    """
    root = ElementTree.fromstring(domain_xml)
    paths: list[str] = []
    other: list[_OtherDisk] = []
    for disk in root.iter("disk"):
        source = disk.find("source")
        file_path = source.get("file") if source is not None else None
        if disk.get("device", "disk") == "disk" and file_path:
            paths.append(file_path)
        else:
            other.append(_describe_other_disk(disk))
    return paths, other


HOME_ASSISTANT_MANIFEST_NAME = "Home Assistant"


def _onboarding_evidence(endpoint: Endpoint, onboarding: OnboardingState) -> str | None:
    """A sentence naming what HA's own API said, or None if the response
    was not shaped like Home Assistant's onboarding view at all.
    """
    if onboarding.steps:
        done = sum(1 for step in onboarding.steps if step.done)
        return (
            f"Home Assistant's onboarding API answered at {endpoint.base_url}/api/onboarding "
            f"with {len(onboarding.steps)} step(s), {done} done."
        )
    if onboarding.reachable and onboarding.complete:
        return (
            f"Home Assistant answered at {endpoint.base_url}/api/onboarding and reports "
            "itself already set up."
        )
    return None


def _inspect_running(
    name: str,
    virsh: VirshLike,
    ha: HALike,
    clock: Callable[[], float],
    deadline: float,
) -> tuple[str | None, Endpoint | None, str | None]:
    """Ask a *running* VM directly, over the network -- alongside reading its
    disk (see _inspect_domain), never instead of it. This call alone decides
    nothing about `confidence`; it hands the caller three independent facts
    (an address, an endpoint, and whether the API's answer was shaped like
    Home Assistant's own onboarding view) that _inspect_domain weighs
    against what the disk itself said.
    """
    address: str | None = None
    endpoint: Endpoint | None = None
    for candidate in virsh.interface_addresses(name):
        if clock() >= deadline:
            break
        if not any(ha.port_open(candidate, port) for port in CANDIDATE_PORTS):
            continue

        remaining = deadline - clock()
        if remaining <= 0:
            break
        resolved = ha.resolve_endpoint(candidate, timeout=remaining)
        if resolved is None:
            continue
        if address is None:
            address, endpoint = candidate, resolved

        remaining = deadline - clock()
        if remaining <= 0:
            break
        onboarding = ha.onboarding_status(resolved, timeout=remaining)
        evidence = _onboarding_evidence(resolved, onboarding)
        if evidence is not None and not onboarding.steps:
            remaining = deadline - clock()
            if remaining <= 0:
                break
            manifest = ha.manifest_name(resolved, timeout=remaining)
            if manifest != HOME_ASSISTANT_MANIFEST_NAME:
                evidence = None
        if evidence is not None:
            return candidate, resolved, evidence
    return address, endpoint, None


def _identify_disk(
    host_path: str,
    to_container_path: Callable[[str], str | None],
    identify: Callable[[str], DiskIdentity],
) -> DiskIdentity:
    """Translate `host_path` -- what virsh's own domain XML reports, since
    libvirt runs on the host and needs a path *it* can open -- to wherever
    this container can actually read it, then identify what is there.
    """
    container_path = to_container_path(host_path)
    if container_path is None:
        return DiskIdentity(
            False,
            (),
            "outside the folders mapped into this container; "
            "add a bind mount for its share to check it",
        )
    return identify(container_path)


def _inspect_disks(
    disk_paths: list[str],
    to_container_path: Callable[[str], str | None],
    identify: Callable[[str], DiskIdentity],
) -> tuple[str, DiskIdentity] | None:
    """Read each disk in `disk_paths` (non-empty; the caller handles "no disk
    at all" before this is reached) until one confirms HAOS, or all are
    read. `disk_paths` are host paths (see `_identify_disk`); the identity
    returned is keyed to the same host path, which is what `FoundVM.disk`
    reports -- what is actually in the VM's real definition.
    """
    checked: list[tuple[str, DiskIdentity]] = []
    for path in disk_paths:
        identity = _identify_disk(path, to_container_path, identify)
        if identity.is_haos:
            return path, identity
        checked.append((path, identity))

    for path, identity in checked:
        if not identity.partitions:
            return path, identity

    # Every disk that was read carried real, unrelated names -- positive
    # evidence against, on all of them, none of it merely absent.
    return None


def _inspect_domain(
    name: str,
    virsh: VirshLike,
    ha: HALike,
    identify: Callable[[str], DiskIdentity],
    to_container_path: Callable[[str], str | None],
    clock: Callable[[], float],
    deadline: float,
) -> FoundVM | None:
    """The disk answers "is this Home Assistant?"; the port answers "and is
    it up?" -- two independent facts, from the right source for each, never
    one standing in for the other.
    """
    state = virsh.state(name)
    address = endpoint = None
    api_evidence: str | None = None
    if state == "running":
        address, endpoint, api_evidence = _inspect_running(name, virsh, ha, clock, deadline)

    disk_paths, other_disks = _disk_sources(virsh.dumpxml(name))
    disk: str | None = None
    identity: DiskIdentity | None = None
    if not disk_paths:
        if other_disks:
            named = ", ".join(d.description for d in other_disks)
            disk_evidence = f"No file-backed hard disk was found; this VM has {named}."
        else:
            disk_evidence = "No disk device was found in this VM's definition."
    else:
        result = _inspect_disks(disk_paths, to_container_path, identify)
        if result is None:
            blocking = [d for d in other_disks if d.could_carry_haos]
            if not blocking:
                return None  # ruled out -- nothing left that could hide HAOS
            named = ", ".join(d.description for d in blocking)
            disk_evidence = (
                f"Every disk that could be read carried unrelated partitions; "
                f"this VM also has {named}, which could not be read."
            )
        else:
            disk, identity = result
            disk_evidence = f"Disk at {disk}: {identity.reason}."

    if identity is not None and identity.is_haos:
        return FoundVM(
            name=name,
            state=state,
            disk=disk,
            identity=identity,
            address=address,
            endpoint=endpoint,
            confidence="confirmed",
            evidence=disk_evidence,
            answered=api_evidence is not None,
        )

    if api_evidence is not None:
        return FoundVM(
            name=name,
            state=state,
            disk=disk,
            identity=identity,
            address=address,
            endpoint=endpoint,
            confidence="likely",
            evidence=api_evidence,
            answered=True,
        )

    return FoundVM(
        name=name,
        state=state,
        disk=disk,
        identity=identity,
        address=address,
        endpoint=endpoint,
        confidence="unknown",
        evidence=disk_evidence,
        answered=False,
    )


def find_home_assistant_vms(
    virsh: VirshLike,
    ha: HALike,
    identify: Callable[[str], DiskIdentity] = haosdisk.identify,
    to_container_path: Callable[[str], str | None] = hostinfo.container_path_for,
    clock: Callable[[], float] = time.monotonic,
    network_budget: float = NETWORK_BUDGET_S,
) -> list[FoundVM]:
    """Every VM on this server that is, or might be, Home Assistant -- by
    evidence, never by name.
    """
    deadline = clock() + network_budget
    results: list[FoundVM] = []
    for name in virsh.list_domains():
        try:
            found = _inspect_domain(name, virsh, ha, identify, to_container_path, clock, deadline)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            evidence = f"Could not inspect this VM: {exc}"
            found = FoundVM(name, "unknown", None, None, None, None, "unknown", evidence, False)
        if found is not None:
            results.append(found)
    return results
