"""Build a libvirt domain XML that matches what Unraid 7 generates itself."""

import random
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr

from app.models import VMSettings

# The QEMU/KVM OUI, as used by Unraid. v2 used AC:87:A3 — an Apple OUI left over
# from Macinabox, which causes routers to identify the VM as Apple hardware.
QEMU_OUI = (0x52, 0x54, 0x00)


def generate_mac(rng: random.Random | None = None) -> str:
    """Return a random locally-assigned MAC using the QEMU OUI."""
    rng = rng or random.Random()
    octets = [*QEMU_OUI, rng.randrange(256), rng.randrange(256), rng.randrange(256)]
    return ":".join(f"{octet:02x}" for octet in octets)


# New VMs use this. Older ones may still carry the legacy form; rc.libvirt
# migrates them at libvirt startup.
UNRAID_NS = "http://unraid"
LEGACY_UNRAID_NS = "unraid"


def metadata_attributes(
    icon: str,
    webui: str = "",
    namespace: str = UNRAID_NS,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the attribute dict for <vmtemplate> element."""
    attributes: dict[str, str] = {
        "xmlns": namespace,
        "name": "Linux",
        "iconold": icon,
        "icon": icon,
        "os": "linux",
    }
    if webui:
        attributes["webui"] = webui
    attributes["storage"] = "default"
    if extra:
        attributes.update(extra)
    return attributes


def build_metadata_element(
    icon: str,
    webui: str = "",
    namespace: str = UNRAID_NS,
    extra: dict[str, str] | None = None,
) -> str:
    """Render the <vmtemplate> element Unraid stores inside <metadata>."""
    attributes = metadata_attributes(icon=icon, webui=webui, namespace=namespace, extra=extra)
    rendered = " ".join(f"{key}={quoteattr(value)}" for key, value in attributes.items())
    return f"<vmtemplate {rendered}/>"


def read_metadata_namespace(domain_xml: str) -> str | None:
    """Return the namespace URI on an existing VM's <vmtemplate>, or None."""
    parser = ElementTree.XMLPullParser(events=("start-ns", "start"))
    parser.feed(domain_xml)
    parser.close()

    declarations: dict[str, str] = {}
    for event, payload in parser.read_events():
        if event == "start-ns":
            prefix, uri = payload
            declarations[prefix] = uri
            continue
        namespace, _, local_name = payload.tag.rpartition("}")
        if local_name != "vmtemplate":
            continue
        if namespace:
            return namespace.removeprefix("{")
        # Unqualified: the URI lives in the prefix declaration virsh wrote,
        # whose key is the element name.
        return declarations.get(local_name)
    return None


OVMF_CODE = "/usr/share/qemu/ovmf-x64/OVMF_CODE-pure-efi.fd"
# Host path, resolved by libvirtd rather than by this container: naming it in
# the domain lets libvirt create the VM's NVRAM itself on first start.
OVMF_VARS_TEMPLATE = "/usr/share/qemu/ovmf-x64/OVMF_VARS-pure-efi.fd"
EMULATOR = "/usr/local/sbin/qemu"

# Order matters — this is the order Unraid emits them in.
CLOCK_TIMERS = (
    ("hpet", {"present": "no"}),
    ("hypervclock", {"present": "no"}),
    ("pit", {"tickpolicy": "delay"}),
    ("rtc", {"tickpolicy": "catchup"}),
)


def _pci_address(parent, *, slot: str, function: str, multifunction: bool = False):
    """An explicit PCI address on bus 0x00, as Unraid's VM manager writes them.

    Unraid pins its USB controllers (slot 0x07) and video (slot 0x1e) to fixed
    addresses and re-emits those same pins on every edit, so a VM it created
    never changes shape under its own form. Anything we leave unpinned must be
    left unpinned for the same reason: the host's libvirtd then assigns it
    exactly as it does for Unraid's own VMs. Pinning what Unraid pins, and no
    more, makes our layout the fixed point of Unraid's regeneration -- an edit
    in its VM manager moves nothing, so the guest's view of its hardware (and
    UEFI's stored path to the boot disk) never shifts.
    """
    attributes = {
        "type": "pci",
        "domain": "0x0000",
        "bus": "0x00",
        "slot": slot,
        "function": function,
    }
    if multifunction:
        attributes["multifunction"] = "on"
    return ElementTree.SubElement(parent, "address", attributes)


def _sub(parent, tag, text=None, **attributes):
    element = ElementTree.SubElement(parent, tag, {k: str(v) for k, v in attributes.items()})
    if text is not None:
        element.text = str(text)
    return element


def build_domain_xml(settings: VMSettings, webui: str = "") -> str:
    """Render a complete libvirt domain XML for a Home Assistant VM."""
    domain = ElementTree.Element("domain", {"type": "kvm"})

    _sub(domain, "name", settings.name)
    _sub(domain, "uuid", settings.uuid)

    metadata = _sub(domain, "metadata")
    attrs = metadata_attributes(icon=settings.icon, webui=webui)
    ElementTree.SubElement(metadata, "vmtemplate", attrs)

    _sub(domain, "memory", settings.memory_kib, unit="KiB")
    _sub(domain, "currentMemory", settings.memory_kib, unit="KiB")
    memory_backing = _sub(domain, "memoryBacking")
    _sub(memory_backing, "nosharepages")

    _sub(domain, "vcpu", settings.vcpus, placement="static")

    if settings.cpu_pins:
        cputune = _sub(domain, "cputune")
        for index, host_cpu in enumerate(settings.cpu_pins):
            _sub(cputune, "vcpupin", vcpu=index, cpuset=host_cpu)

    os_element = _sub(domain, "os")
    _sub(os_element, "type", "hvm", arch="x86_64", machine=settings.machine)
    _sub(os_element, "loader", OVMF_CODE, readonly="yes", type="pflash", format="raw")
    _sub(
        os_element,
        "nvram",
        settings.nvram_path,
        template=OVMF_VARS_TEMPLATE,
        format="raw",
    )

    features = _sub(domain, "features")
    _sub(features, "acpi")
    _sub(features, "apic")

    threads = 2 if settings.vcpus % 2 == 0 else 1
    cores = settings.vcpus // threads

    cpu = _sub(domain, "cpu", mode="host-passthrough", check="none", migratable="on")
    _sub(cpu, "topology", sockets=1, dies=1, clusters=1, cores=cores, threads=threads)
    _sub(cpu, "cache", mode="passthrough")

    clock = _sub(domain, "clock", offset="utc")
    for name, attributes in CLOCK_TIMERS:
        _sub(clock, "timer", name=name, **attributes)

    _sub(domain, "on_poweroff", "destroy")
    _sub(domain, "on_reboot", "restart")
    _sub(domain, "on_crash", "restart")

    _build_devices(domain, settings)

    ElementTree.indent(domain, space="  ")
    return ElementTree.tostring(domain, encoding="unicode")


def _build_devices(domain, settings: VMSettings) -> None:
    devices = _sub(domain, "devices")
    _sub(devices, "emulator", EMULATOR)

    disk = _sub(devices, "disk", type="file", device="disk")
    _sub(disk, "driver", name="qemu", type="qcow2", cache="writeback", discard="unmap")
    _sub(disk, "source", file=settings.disk_path)
    _sub(disk, "target", dev="hdc", bus="virtio")
    _sub(disk, "serial", "vdisk1")
    _sub(disk, "boot", order=1)

    _sub(devices, "controller", type="sata", index=0)
    _sub(devices, "controller", type="pci", index=0, model="pcie-root")
    for index in range(1, 6):
        port = _sub(devices, "controller", type="pci", index=index, model="pcie-root-port")
        _sub(port, "model", name="pcie-root-port")
        _sub(port, "target", chassis=index, port=hex(0x7 + index))
    _sub(devices, "controller", type="virtio-serial", index=0)

    ehci = _sub(devices, "controller", type="usb", index=0, model="ich9-ehci1")
    _pci_address(ehci, slot="0x07", function="0x7")
    for number, startport, function in ((1, 0, "0x0"), (2, 2, "0x1"), (3, 4, "0x2")):
        uhci = _sub(devices, "controller", type="usb", index=0, model=f"ich9-uhci{number}")
        _sub(uhci, "master", startport=startport)
        _pci_address(uhci, slot="0x07", function=function, multifunction=number == 1)

    interface = _sub(
        devices,
        "interface",
        **(
            {"type": "direct", "trustGuestRxFilters": "yes"}
            if settings.network.kind == "direct"
            else {"type": "bridge"}
        ),
    )
    _sub(interface, "mac", address=settings.mac)
    if settings.network.kind == "direct":
        _sub(interface, "source", dev=settings.network.name, mode="bridge")
    else:
        _sub(interface, "source", bridge=settings.network.name)
    _sub(interface, "model", type="virtio-net")

    serial = _sub(devices, "serial", type="pty")
    serial_target = _sub(serial, "target", type="isa-serial", port=0)
    _sub(serial_target, "model", name="isa-serial")
    console = _sub(devices, "console", type="pty")
    _sub(console, "target", type="serial", port=0)

    channel = _sub(devices, "channel", type="unix")
    _sub(channel, "target", type="virtio", name="org.qemu.guest_agent.0")

    _sub(devices, "input", type="tablet", bus="usb")
    _sub(devices, "input", type="mouse", bus="ps2")
    _sub(devices, "input", type="keyboard", bus="ps2")

    graphics = _sub(
        devices,
        "graphics",
        type="vnc",
        port=-1,
        autoport="yes",
        websocket=-1,
        listen="0.0.0.0",
        sharePolicy="ignore",
    )
    _sub(graphics, "listen", type="address", address="0.0.0.0")

    _sub(devices, "audio", id=1, type="none")

    video = _sub(devices, "video")
    _sub(
        video,
        "model",
        type="qxl",
        ram=65536,
        vram=16384,
        vgamem=16384,
        heads=1,
        primary="yes",
    )
    _pci_address(video, slot="0x1e", function="0x0")

    for device in settings.usb_devices:
        hostdev = _sub(devices, "hostdev", mode="subsystem", type="usb", managed="no")
        source = _sub(hostdev, "source", startupPolicy="optional")
        _sub(source, "vendor", id=f"0x{device.vendor_id}")
        _sub(source, "product", id=f"0x{device.product_id}")

    _sub(devices, "watchdog", model="itco", action="reset")
    _sub(devices, "memballoon", model="virtio")
