"""Value objects describing a Home Assistant VM."""

from dataclasses import dataclass, field
from typing import Literal

from app import paths


@dataclass(frozen=True, slots=True)
class USBDevice:
    """A USB device selected for passthrough."""

    vendor_id: str
    product_id: str
    name: str


@dataclass(frozen=True, slots=True)
class NetworkSource:
    """Where the VM's NIC attaches."""

    name: str
    kind: Literal["bridge", "direct"]


@dataclass(frozen=True, slots=True)
class VMSettings:
    """Everything needed to render a domain XML."""

    name: str
    uuid: str
    machine: str
    memory_mib: int
    vcpus: int
    disk_path: str
    mac: str
    network: NetworkSource
    cpu_pins: tuple[int, ...] = field(default=())
    usb_devices: tuple[USBDevice, ...] = field(default=())
    # Shared with app.vmicon, which places the file this names.
    icon: str = paths.VM_ICON_NAME

    @property
    def memory_kib(self) -> int:
        return self.memory_mib * 1024

    @property
    def nvram_path(self) -> str:
        """Where Unraid keeps this VM's EFI variables."""
        return paths.nvram_path(self.uuid)
