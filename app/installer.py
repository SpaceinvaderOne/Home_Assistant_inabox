"""Sequence the install as discrete, individually resumable steps."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app import paths
from app.events import Event
from app.models import VMSettings
from app.releases import Release
from app.vmxml import build_domain_xml

STEPS = (
    "check_paths",
    "check_space",
    "download",
    "decompress",
    "resize",
    "nvram",
    "write_xml",
    "define",
    "start",
)

TERMINAL_STEP = "install"

CONFIG_PATH = "/config"

ESTIMATED_DECOMPRESSION_FACTOR = 5


class InstallError(Exception):
    def __init__(self, step: str, message: str):
        self.step = step
        super().__init__(f"{step}: {message}")


class Deps(Protocol):
    def free_bytes(self, path: str) -> int: ...
    def exists(self, path) -> bool: ...
    def sha256(self, path) -> str: ...
    def download(self, url, dest, sha256, progress=None): ...
    def decompress(self, src, dest, progress=None): ...
    def remove(self, path) -> None: ...
    def resize(self, image, size_gib) -> None: ...
    def check_ovmf_vars(self): ...
    def place_vm_icon(self, name: str): ...
    def write_xml(self, xml: str, path): ...
    def define(self, path) -> None: ...
    def start(self, name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class InstallPlan:
    settings: VMSettings
    release: Release
    size_gib: int
    domains_host_path: str
    domains_container_path: str = "/domains"
    webui: str = ""


def install(plan: InstallPlan, deps: Deps, emit: Callable[[Event], None]) -> None:
    """Run the install. Raises InstallError naming the step that failed."""
    container_disk = Path(paths.disk_path(plan.domains_container_path, plan.settings.name))
    host_disk = plan.settings.disk_path  # goes into the XML; never opened here
    archive = Path(CONFIG_PATH) / f"{plan.release.version}.qcow2.xz"
    xml_path = Path(CONFIG_PATH) / f"{plan.settings.name}.xml"

    def step(name: str, detail: str = ""):
        emit(Event(name, "start", detail))

    def done(name: str, detail: str = ""):
        emit(Event(name, "ok", detail))

    def fail(name: str, message: str):
        emit(Event(name, "fail", message))
        raise InstallError(name, message)

    def guard(name: str, action):
        try:
            return action()
        except InstallError:
            raise
        except Exception as err:
            emit(Event(name, "fail", str(err)))
            raise InstallError(name, str(err)) from err

    step("check_paths")
    expected_host_disk = paths.disk_path(plan.domains_host_path, plan.settings.name)
    if Path(host_disk) != Path(expected_host_disk):
        fail(
            "check_paths",
            f"the VM's disk_path is {host_disk} but the domains share is "
            f"{plan.domains_host_path}, which puts the disk at {expected_host_disk}. "
            "The XML and the file this container writes must be the same file.",
        )
    done("check_paths", host_disk)

    step("check_space")
    disk_ready = guard("check_space", lambda: deps.exists(container_disk))
    archive_present = guard("check_space", lambda: deps.exists(archive))
    domains_free = guard("check_space", lambda: deps.free_bytes(plan.domains_container_path))
    config_free = guard("check_space", lambda: deps.free_bytes(CONFIG_PATH))

    if not disk_ready:
        needed = plan.release.size * ESTIMATED_DECOMPRESSION_FACTOR
        if domains_free < needed:
            fail(
                "check_space",
                f"needs about {needed // 1024**3} GiB free on the domains share "
                f"({plan.domains_host_path}), {domains_free // 1024**3} GiB available",
            )
        # An archive already sitting in appdata is overwritten in place, so the room
        # it occupies is room we already have; only a fetch from nothing needs more.
        if not archive_present and config_free < plan.release.size:
            fail(
                "check_space",
                f"needs about {plan.release.size // 1024**2} MiB free on the appdata "
                f"share (this container's {CONFIG_PATH}) for the download, "
                f"{config_free // 1024**2} MiB available",
            )
    requested = plan.size_gib * 1024**3
    if domains_free < requested:
        emit(
            Event(
                "check_space",
                "info",
                f"the VM's virtual disk size ({plan.size_gib} GiB) is larger than the "
                f"{domains_free // 1024**3} GiB currently free on {plan.domains_host_path}; "
                "qcow2 is sparse so this is fine for now, but it may run out of room "
                "as Home Assistant grows to fill it.",
            )
        )
    done(
        "check_space",
        f"{domains_free // 1024**3} GiB free on {plan.domains_host_path}, "
        f"{config_free // 1024**3} GiB free on appdata",
    )

    # 3-4. fetch and expand, unless the disk is already there
    if disk_ready:
        for name in ("download", "decompress"):
            step(name, "image already present, skipping")
            done(name, "image already present, skipping")
    else:
        step("download", plan.release.asset_url)
        if guard("download", lambda: _archive_verifies(deps, archive, plan.release.sha256)):
            done("download", f"{archive} already here and verified, skipping the fetch")
        else:
            guard(
                "download",
                lambda: deps.download(
                    plan.release.asset_url,
                    archive,
                    plan.release.sha256,
                    progress=lambda d, t: emit(Event("download", "progress", "", d, t)),
                ),
            )
            done("download", f"verified sha256 {plan.release.sha256[:12]}…")

        step("decompress", str(container_disk))
        guard(
            "decompress",
            lambda: deps.decompress(
                archive,
                container_disk,
                progress=lambda d, t: emit(Event("decompress", "progress", "", d, t)),
            ),
        )
        try:
            deps.remove(archive)
        except OSError as err:
            emit(Event("decompress", "info", f"could not remove {archive}: {err}"))
        done("decompress")

    # 5. resize
    step("resize", f"{plan.size_gib} GiB")
    guard("resize", lambda: deps.resize(container_disk, plan.size_gib))
    done("resize")

    # 6. firmware. The domain names the OVMF variables template, and libvirt
    # creates the VM's own NVRAM from it on first start, so this only has to
    # confirm the template is there before defining something that needs it.
    step("nvram")
    template = guard("nvram", lambda: deps.check_ovmf_vars())
    done("nvram", f"libvirt will create this VM's NVRAM from {template}")

    try:
        icon = deps.place_vm_icon(plan.settings.icon)
        if icon.placed:
            emit(Event("write_xml", "info", f"placed the VM icon at {icon.reason}"))
    except Exception as err:
        emit(Event("write_xml", "info", f"could not place the VM icon: {err}"))

    # 7. xml
    step("write_xml")
    xml = build_domain_xml(plan.settings, webui=plan.webui)
    guard("write_xml", lambda: deps.write_xml(xml, xml_path))
    done("write_xml")

    # 8. define
    step("define")
    guard("define", lambda: deps.define(xml_path))
    done("define")

    # 9. start
    step("start")
    guard("start", lambda: deps.start(plan.settings.name))
    done("start")

    emit(Event(TERMINAL_STEP, "ok", plan.webui))


def _archive_verifies(deps: Deps, archive: Path, expected_sha256: str) -> bool:
    """Is a previous run's download still here and still intact?"""
    return deps.exists(archive) and deps.sha256(archive) == expected_sha256.lower()
