"""Read host capabilities out of `virsh capabilities` output."""

import re
from dataclasses import dataclass
from typing import Literal
from xml.etree import ElementTree

Q35_VERSION = re.compile(r"^pc-q35-(\d+)\.(\d+)$")


class NoMachineTypeError(Exception):
    """No usable q35 machine type for x86_64 in the capabilities XML."""


def _x86_64_machines(root: ElementTree.Element) -> list[ElementTree.Element]:
    machines: list[ElementTree.Element] = []
    for guest in root.findall("guest"):
        for arch in guest.findall("arch"):
            if arch.get("name") == "x86_64":
                machines.extend(arch.findall("machine"))
    return machines


def highest_q35(capabilities_xml: str) -> str:
    """Return the newest q35 machine type, e.g. 'pc-q35-10.2'."""
    root = ElementTree.fromstring(capabilities_xml)
    machines = _x86_64_machines(root)

    for machine in machines:
        if (machine.text or "").strip() == "q35":
            canonical = machine.get("canonical")
            if canonical and Q35_VERSION.match(canonical):
                return canonical

    versioned: list[tuple[tuple[int, int], str]] = []
    for machine in machines:
        name = (machine.text or "").strip()
        match = Q35_VERSION.match(name)
        if match:
            versioned.append(((int(match[1]), int(match[2])), name))

    if not versioned:
        raise NoMachineTypeError("no pc-q35-* machine type for x86_64 found in capabilities output")

    return max(versioned)[1]


@dataclass(frozen=True, slots=True)
class Core:
    """One physical core and the logical CPUs on it."""

    index: int
    threads: tuple[int, ...]
    kind: Literal["performance", "efficiency", "uniform"]


def _expand(spec: str) -> list[int]:
    """'0-1' -> [0, 1];  '4' -> [4];  '0-1,4' -> [0, 1, 4]."""
    values: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            start, end = part.split("-")
            values.extend(range(int(start), int(end) + 1))
        else:
            values.append(int(part))
    return values


def host_cpus(capabilities_xml: str) -> list[Core]:
    """Physical cores of the host, derived from the `siblings` attribute."""
    root = ElementTree.fromstring(capabilities_xml)
    groups: dict[tuple[int, ...], list[int]] = {}
    for cpu in root.iterfind("host/topology/cells/cell/cpus/cpu"):
        cpu_id = int(cpu.get("id", "0"))
        siblings = cpu.get("siblings")
        key = tuple(sorted(_expand(siblings))) if siblings else (cpu_id,)
        groups.setdefault(key, []).append(cpu_id)

    ordered = sorted(groups.values(), key=min)
    widths = {len(t) for t in ordered}
    hybrid = len(widths) > 1
    widest = max(widths) if widths else 0

    cores = []
    for index, threads in enumerate(ordered):
        if not hybrid:
            kind = "uniform"
        else:
            kind = "performance" if len(threads) == widest else "efficiency"
        cores.append(Core(index=index, threads=tuple(sorted(threads)), kind=kind))
    return cores
