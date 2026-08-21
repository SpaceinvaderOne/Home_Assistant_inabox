"""The wizard's answers, and the rules about what a valid answer is."""

import uuid as uuidlib
from dataclasses import dataclass, field, fields

from app import paths
from app.models import NetworkSource, USBDevice, VMSettings
from app.vmxml import generate_mac

# The shipped HAOS image is a 32 GiB disk and qemu-img cannot shrink it.
MIN_DISK_GIB = 32
MAX_VCPUS = 128
MIN_MEMORY_MIB = 2048


class ValidationError(Exception):
    def __init__(self, field_name: str, message: str):
        self.field_name = field_name
        super().__init__(message)


@dataclass
class WizardState:
    name: str = "Home Assistant"
    version: str = ""
    bridge: str = ""
    bridge_kind: str = "bridge"
    vcpus: int = 2
    memory_mib: int = 4096
    size_gib: int = 64
    cpu_pins: tuple[int, ...] = ()
    usb_devices: tuple[USBDevice, ...] = ()
    _mac: str = field(default_factory=generate_mac)
    _uuid: str = field(default_factory=lambda: str(uuidlib.uuid4()))

    def set_name(self, value: str) -> None:
        value = value.strip()
        if not value:
            raise ValidationError("name", "Give the VM a name.")
        if "/" in value or "\\" in value or value.startswith("."):
            raise ValidationError(
                "name",
                "The name becomes a folder on your domains share, so it "
                "cannot contain slashes or start with a dot.",
            )
        self.name = value

    def set_vcpus(self, value: int) -> None:
        if not 1 <= value <= MAX_VCPUS:
            raise ValidationError("vcpus", f"Choose between 1 and {MAX_VCPUS} vCPUs.")
        self.vcpus = value

    def set_memory_mib(self, value: int) -> None:
        if value < MIN_MEMORY_MIB:
            raise ValidationError("memory", f"Home Assistant needs at least {MIN_MEMORY_MIB} MiB.")
        self.memory_mib = value

    def set_size_gib(self, value: int) -> None:
        if value < MIN_DISK_GIB:
            raise ValidationError(
                "disk",
                f"The Home Assistant image is a {MIN_DISK_GIB} GiB disk, "
                "so it cannot be made smaller.",
            )
        self.size_gib = value

    def to_settings(self, machine: str, domains_host_path: str) -> VMSettings:
        return VMSettings(
            name=self.name,
            uuid=self._uuid,
            machine=machine,
            memory_mib=self.memory_mib,
            vcpus=self.vcpus,
            disk_path=paths.disk_path(domains_host_path, self.name),
            mac=self._mac,
            network=NetworkSource(name=self.bridge, kind=self.bridge_kind),
            cpu_pins=self.cpu_pins,
            usb_devices=self.usb_devices,
        )


SESSION = WizardState()


def reset() -> None:
    """Return SESSION to its defaults in place."""
    fresh = WizardState()
    for f in fields(WizardState):
        setattr(SESSION, f.name, getattr(fresh, f.name))
