"""Wizard step rendering and the preflight checks behind the welcome screen."""

import os
import re
from dataclasses import dataclass

from app.capabilities import host_cpus
from app.discovery import FoundVM, find_home_assistant_vms
from app.haclient import (
    CANDIDATE_PORTS,
    ONBOARDING_TIMEOUT,
    PORT_PROBE_TIMEOUT,
    Endpoint,
    OnboardingState,
    manifest_name,
    onboarding_status,
    port_open,
    resolve_endpoint,
)
from app.hostinfo import (
    MOUNT_HINTS,
    Bridge,
    BridgeNotFoundError,
    bridges,
    free_bytes,
    host_path_for,
)
from app.libvirtctl import Virsh
from app.models import USBDevice
from app.paths import DOMAIN_CFG_PATH
from app.releases import RateLimitedError, fetch_releases
from app.usbdevices import UsbDevice, enumerate_devices, flash_identity_known
from app.web.session import MAX_VCPUS, MIN_DISK_GIB, MIN_MEMORY_MIB, SESSION, ValidationError
from app.web.steps import next_step

LIBVIRT_SOCKET = "/var/run/libvirt/libvirt-sock"
DOMAINS_CONTAINER_PATH = "/domains"

DOMAIN_CFG_LINE = re.compile(r"^([A-Za-z_]\w*)\s*=\s*(.*)$")

UNQUOTED_COMMENT = re.compile(r"\s#")

VM_MANAGER_HEALTHY = {"SERVICE": "enable", "DISABLE": "no"}
VM_MANAGER_BLOCKING = {"SERVICE": "disable", "DISABLE": "yes"}


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str


def _domain_cfg_value(raw: str) -> str:
    """Extract a domain.cfg value, quoted or not."""
    if raw[:1] in "\"'":
        quote = raw[0]
        end = raw.find(quote, 1)
        if end != -1:
            return raw[1:end]
    comment = UNQUOTED_COMMENT.search(raw)
    if comment is not None:
        raw = raw[: comment.start()]
    return raw.strip()


@dataclass(frozen=True, slots=True)
class DomainCfgReading:
    """What read_vm_manager_settings() found -- and whether it can be
    trusted as CURRENT.
    """

    values: dict[str, str] | None
    stale: bool


def _domain_cfg_fstat(handle):
    """The real, default `stat` seam for read_vm_manager_settings: the
    link count of the file *actually open*, not a fresh lookup by path.
    """
    return os.fstat(handle.fileno())


def read_vm_manager_settings(
    path: str = DOMAIN_CFG_PATH, *, stat=_domain_cfg_fstat
) -> DomainCfgReading:
    """Best-effort read of Unraid's VM Manager settings (domain.cfg)."""
    try:
        # utf-8-sig quietly drops a leading BOM if one is present (some editors
        # add one on save) and behaves exactly like utf-8 when there isn't one.
        with open(path, encoding="utf-8-sig", errors="replace") as handle:
            try:
                nlink = stat(handle).st_nlink
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                return DomainCfgReading(values=None, stale=True)
            if not isinstance(nlink, int) or isinstance(nlink, bool) or nlink < 0:
                return DomainCfgReading(values=None, stale=True)
            if nlink == 0:
                return DomainCfgReading(values=None, stale=True)
            text = handle.read()
    except OSError:
        return DomainCfgReading(values=None, stale=False)

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = DOMAIN_CFG_LINE.match(line)
        if not match:
            continue
        key, raw_value = match.group(1), match.group(2).strip()
        values[key] = _domain_cfg_value(raw_value)

    return DomainCfgReading(values=(values or None), stale=False)


def _existing_vm_check(virsh) -> tuple[Check, list[FoundVM]]:
    """Discover this server's VMs once, building both the welcome step's
    "existing" Check and the raw FoundVM evidence routes_status needs to
    decide whether the status panel applies -- from a single scan, not two.
    welcome_context() is the one caller that needs both halves of this in
    the same request; preflight() below still gets only the Check, unchanged
    """
    try:
        found = _discover(virsh)
        confirmed = [vm for vm in found if vm.confidence in ("confirmed", "likely")]
        if confirmed:
            names = ", ".join(vm.name for vm in confirmed)
            detail = f"Found an existing Home Assistant VM: {names}"
        else:
            detail = "No existing Home Assistant VM"
        return Check("existing", True, detail), found
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return Check("existing", True, "Could not list existing VMs"), []


def welcome_context(virsh) -> dict:
    """Everything the welcome step's template needs, from exactly one
    discovery scan (see _existing_vm_check) -- computed here, once, rather
    than inside preflight() and a second time beside it, so GET / does not
    ask the server about its VMs twice in the same request.
    """
    existing, _found = _existing_vm_check(virsh)
    checks = preflight(virsh, existing=existing)
    return {
        "checks": checks,
        "all_ok": all(c.ok for c in checks),
        "next_key": "version",
    }


def preflight(
    virsh,
    host_path=host_path_for,
    free=free_bytes,
    vm_manager_settings=read_vm_manager_settings,
    *,
    existing: Check | None = None,
) -> list[Check]:
    """Everything that decides whether an install can succeed, as evidence."""
    checks: list[Check] = []

    try:
        virsh.capabilities()
        checks.append(Check("libvirt", True, "Connected to the VM manager"))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as err:
        checks.append(
            Check(
                "libvirt",
                False,
                f"Add -v {LIBVIRT_SOCKET}:{LIBVIRT_SOCKET} to your docker run. ({err})",
            )
        )

    try:
        resolved = host_path(DOMAINS_CONTAINER_PATH)
        checks.append(Check("domains", True, f"Your VMs are stored in {resolved}"))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as err:
        domains_host_path = MOUNT_HINTS[DOMAINS_CONTAINER_PATH]
        checks.append(
            Check(
                "domains",
                False,
                f"Add -v {domains_host_path}:{DOMAINS_CONTAINER_PATH} to your docker run. ({err})",
            )
        )

    try:
        gib = free(DOMAINS_CONTAINER_PATH) // 1024**3
        checks.append(Check("space", gib > 8, f"{gib} GiB free"))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as err:
        checks.append(Check("space", False, f"Cannot read free space ({err})"))

    if existing is None:
        existing, _found = _existing_vm_check(virsh)
    checks.append(existing)

    try:
        cfg = vm_manager_settings()
        if cfg.stale:
            checks.append(
                Check(
                    "vm_manager",
                    True,
                    f"{DOMAIN_CFG_PATH} changed on the host since this container "
                    "started, so its VM Manager settings can no longer be checked "
                    "from here. Restart the container to see the current value, "
                    "or just check Settings -> VM Manager yourself before "
                    "installing.",
                )
            )
        elif cfg.values is None:
            checks.append(
                Check(
                    "vm_manager",
                    True,
                    f"Could not read {DOMAIN_CFG_PATH} to check whether Unraid's VM "
                    "Manager will allow a start; continuing without that check. If "
                    "the install fails at the last step, check Settings -> VM Manager.",
                )
            )
        else:
            settings = cfg.values
            service = settings.get("SERVICE", VM_MANAGER_HEALTHY["SERVICE"]).strip()
            disable = settings.get("DISABLE", VM_MANAGER_HEALTHY["DISABLE"]).strip()
            unrecognised = {
                key: value
                for key, value in (("SERVICE", service), ("DISABLE", disable))
                if value not in (VM_MANAGER_HEALTHY[key], VM_MANAGER_BLOCKING[key])
            }
            if service == VM_MANAGER_BLOCKING["SERVICE"]:
                checks.append(
                    Check(
                        "vm_manager",
                        False,
                        "VMs are switched off in Unraid entirely (Settings -> VM "
                        "Manager -> Enable VMs is set to No). Set it to Yes, then "
                        "try again.",
                    )
                )
            elif disable == VM_MANAGER_BLOCKING["DISABLE"]:
                checks.append(
                    Check(
                        "vm_manager",
                        False,
                        "Unraid will refuse to start any VM (Settings -> VM Manager "
                        "-> Disable Autostart/Start option for VMs is set to Yes). "
                        "Set it to No, then try again.",
                    )
                )
            elif unrecognised:
                spelled = ", ".join(
                    f"{key}={value!r}" for key, value in sorted(unrecognised.items())
                )
                checks.append(
                    Check(
                        "vm_manager",
                        True,
                        f"Read {DOMAIN_CFG_PATH}, but could not interpret {spelled}, so "
                        "whether Unraid's VM Manager will allow a start was not "
                        "checked. If the install fails at the last step, check "
                        "Settings -> VM Manager.",
                    )
                )
            else:
                checks.append(
                    Check(
                        "vm_manager",
                        True,
                        f"Read {DOMAIN_CFG_PATH}; nothing in VM Manager's settings "
                        "looks like it would block a start.",
                    )
                )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as err:
        checks.append(
            Check(
                "vm_manager",
                True,
                f"Could not read {DOMAIN_CFG_PATH} to check whether Unraid's VM "
                f"Manager will allow a start ({err}); continuing without that check.",
            )
        )

    return checks


def next_step_key(key: str) -> str | None:
    """Re-exports steps.next_step so both the routes below and the
    navigation-chain test have one place to ask what comes after a step."""
    return next_step(key)


_releases = fetch_releases


def _bridges() -> list[Bridge]:
    return bridges(Virsh())


def _capabilities() -> str:
    return Virsh().capabilities()


def _domains() -> list[str]:
    return Virsh().list_domains()


def _usb_devices() -> list[UsbDevice]:
    """A fresh enumeration -- app.usbdevices.enumerate_devices reads real
    sysfs (and var.ini, for the boot-flash exclusion) by default, exactly
    like every other free-function dependency in this module. Both
    usb_context() (GET, and the "rescan" affordance, which is just another
    GET of this same route) and apply_usb() (POST) call this bare name, not
    app.usbdevices.enumerate_devices(...) qualified, so tests can
    monkeypatch routes_wizard._usb_devices the same way every other seam
    here already is -- no real filesystem I/O in the test suite."""
    return enumerate_devices()


def _flash_identity_known() -> bool:
    """Whether the boot-flash exclusion actually had anything to run
    against -- app.usbdevices.flash_identity_known's own seam, kept bare for
    the same monkeypatching reason as `_usb_devices` above."""
    return flash_identity_known()


DISCOVERY_PORT_PROBE_TIMEOUT = 0.3


class _HAClient:
    """Adapts app.haclient's bare port_open()/resolve_endpoint()/
    onboarding_status()/manifest_name() to discovery.HALike's bound-method
    shape -- the same adaptation routes_firstboot._HAClient makes for
    FirstBootWatcher (see that class's own docstring). Calls the bare
    module-level names below, not app.haclient.resolve_endpoint(...)
    qualified, so tests can monkeypatch routes_wizard.resolve_endpoint /
    .onboarding_status / .manifest_name / .port_open the same way every
    other free-function dependency in this module is already stubbed,
    without a real network round trip."""

    def port_open(self, ip: str, port: int) -> bool:
        return port_open(ip, port, timeout=DISCOVERY_PORT_PROBE_TIMEOUT)

    def resolve_endpoint(self, ip: str, timeout: float) -> Endpoint | None:
        per_port = min(timeout / len(CANDIDATE_PORTS), PORT_PROBE_TIMEOUT)
        return resolve_endpoint(ip, timeout=per_port)

    def onboarding_status(self, endpoint: Endpoint, timeout: float) -> OnboardingState:
        return onboarding_status(endpoint, timeout=min(timeout, ONBOARDING_TIMEOUT))

    def manifest_name(self, endpoint: Endpoint, timeout: float) -> str | None:
        return manifest_name(endpoint, timeout=min(timeout, ONBOARDING_TIMEOUT))


def _discover(virsh) -> list[FoundVM]:
    return find_home_assistant_vms(virsh, _HAClient())


# ---- Version step -----------------------------------------------------------


def version_context() -> dict:
    """Build the version step's template context."""
    try:
        releases = _releases()
    except RateLimitedError as err:
        return {
            "releases": (),
            "rate_limited": True,
            "rate_limit_message": str(err),
            "selected": SESSION.version or "latest",
        }
    selected = SESSION.version or (releases[0].version if releases else "latest")
    return {"releases": releases, "rate_limited": False, "selected": selected}


def apply_version(form) -> None:
    """Store the chosen version. Any non-empty value is accepted, including
    "latest" -- the fallback when the release list could not be fetched."""
    SESSION.version = (form.get("version") or "").strip() or "latest"


# ---- Placement step ----------------------------------------------------------

NAT_BRIDGE = re.compile(r"^virbr\d*$")


def _is_nat(bridge: Bridge) -> bool:
    return bool(NAT_BRIDGE.match(bridge.name))


def _name_conflicts(name: str) -> bool:
    """Does an existing libvirt domain already have this exact name?"""
    try:
        domains = _domains()
    except Exception:
        return False
    return name in domains


NAME_SUFFIX_LIMIT = 20


def available_name(base: str, limit: int = NAME_SUFFIX_LIMIT) -> str:
    """`base`, or the first "`base` N" that no existing domain is using."""
    if not _name_conflicts(base):
        return base
    for suffix in range(2, limit + 1):
        candidate = f"{base} {suffix}"
        if not _name_conflicts(candidate):
            return candidate
    return base


def placement_context(**overrides) -> dict:
    try:
        found = _bridges()
    except BridgeNotFoundError as err:
        context = {
            "bridges": (),
            "bridge_error": str(err),
            "selected": "",
            "default_name": "",
            "mac": SESSION.to_settings(machine="", domains_host_path="").mac,
            "nat_warning": False,
            "name": SESSION.name,
            "errors": {},
        }
        context.update(overrides)
        context["name_conflict"] = _name_conflicts(context["name"])
        return context

    default = next((b for b in found if b.is_default), found[0])
    selected_name = SESSION.bridge or default.name
    selected = next((b for b in found if b.name == selected_name), default)
    # machine and domains_host_path only shape the disk path and the XML's
    # machine type, neither of which this step shows -- only .mac is used.
    mac = SESSION.to_settings(machine="", domains_host_path="").mac
    context = {
        "bridges": found,
        "selected": selected_name,
        "default_name": default.name,
        "mac": mac,
        "nat_warning": _is_nat(selected),
        "bridge_error": "",
        "name": SESSION.name,
        "errors": {},
    }
    context.update(overrides)
    context["name_conflict"] = _name_conflicts(context["name"])
    return context


def apply_placement(form) -> bool:
    """Validate, store, and report whether the choice needs a second look."""
    SESSION.set_name(form.get("name") or "")
    bridge_name = (form.get("bridge") or "").strip()
    found = _bridges()
    match = next((b for b in found if b.name == bridge_name), None)
    if match is None:
        raise ValidationError("bridge", "Choose one of the networks listed above.")
    SESSION.bridge = match.name
    SESSION.bridge_kind = match.kind
    confirmed = form.get("confirm_nat") == "1"
    return _is_nat(match) and not confirmed


# ---- Resources step -----------------------------------------------------------


def resources_context(**overrides) -> dict:
    cores = host_cpus(_capabilities())
    threads = sum(len(core.threads) for core in cores)
    pinned_threads = set(SESSION.cpu_pins)
    context = {
        "vcpus": SESSION.vcpus,
        "memory_mib": SESSION.memory_mib,
        "size_gib": SESSION.size_gib,
        "cores": cores,
        "physical": len(cores),
        "threads": threads,
        "pinned": bool(SESSION.cpu_pins),
        "pinned_cores": {c.index for c in cores if set(c.threads) & pinned_threads},
        "overprovision_warning": SESSION.vcpus > threads,
        "min_disk_gib": MIN_DISK_GIB,
        "max_vcpus": MAX_VCPUS,
        "min_memory_mib": MIN_MEMORY_MIB,
        "errors": {},
    }
    context.update(overrides)
    return context


def _parse_int(raw: str | None, field: str, label: str) -> int:
    try:
        return int((raw or "").strip())
    except ValueError as err:
        raise ValidationError(field, f"{label} must be a whole number.") from err


def apply_resources(form) -> bool:
    """Validate, store, and report whether the choice needs a second look."""
    vcpus = _parse_int(form.get("vcpus"), "vcpus", "vCPU count")
    memory = _parse_int(form.get("memory"), "memory", "Memory")
    disk = _parse_int(form.get("disk"), "disk", "Disk size")
    SESSION.set_vcpus(vcpus)
    SESSION.set_memory_mib(memory)
    SESSION.set_size_gib(disk)

    selected_cores = {int(v) for v in form.getlist("pin_core")}
    if selected_cores:
        cores = host_cpus(_capabilities())
        SESSION.cpu_pins = tuple(
            sorted(t for c in cores if c.index in selected_cores for t in c.threads)
        )
    else:
        SESSION.cpu_pins = ()

    threads = sum(len(core.threads) for core in host_cpus(_capabilities()))
    confirmed = form.get("confirm_overprovision") == "1"
    return vcpus > threads and not confirmed


def _usb_tier(device: UsbDevice) -> str:
    """Which single rung of the honesty ladder wins for this device -- the
    first rung (top to bottom, per the pinned order above) whose own
    trigger is true, or "none" if nothing claims it at all. `_usb_badge`,
    `_usb_note`, and `_usb_hint` all read this instead of the raw UsbDevice
    fields directly, so "which rung wins" is decided in exactly one place."""
    if device.recognised:
        return "recognised"
    if device.curated_key is not None:
        return "curated"
    if device.ambiguous:
        return "ambiguous"
    if device.bluetooth:
        return "bluetooth"
    if device.name_hint is not None:
        return "name_hint"
    if device.not_typically_useful is not None:
        return "not_typically_useful"
    return "none"


def _usb_badge(device: UsbDevice) -> str | None:
    """The honesty-ladder label shown next to a device's own sysfs name
    (option-title already shows display_name -- this never repeats it).
    """
    tier = _usb_tier(device)
    if tier == "recognised":
        return device.category or device.domain
    if tier == "curated":
        return device.curated_category
    if tier == "ambiguous":
        return "USB serial adapter -- may be a Zigbee or Z-Wave stick"
    if tier == "bluetooth":
        return "Bluetooth adapter"
    return None


_NAME_HINT_TEXT: dict[str, str] = {
    "zigbee": "Its name suggests a Zigbee device -- may be a coordinator.",
    "zwave": "Its name suggests a Z-Wave device -- may be a coordinator/controller.",
    "coordinator": "Its name suggests it may be a coordinator/controller.",
}


def _usb_hint(device: UsbDevice) -> str | None:
    """The honesty ladder's name-hint rung, below every stronger rung
    `_usb_tier` checks first -- so a hint can never appear alongside a badge
    or a note, even for a hand-built fixture claiming both.
    """
    if _usb_tier(device) != "name_hint":
        return None
    return _NAME_HINT_TEXT[device.name_hint]


_CURATED_NOTE_TEXT: dict[str, str] = {
    "rtl_sdr": (
        "Pairs with the rtl_433 add-on to receive many 433 MHz sensors "
        "(weather stations, doorbells, and similar)."
    ),
    "coral": ("Usually better left on Unraid for a Frigate container than passed into this VM."),
    "ups": (
        "Unraid usually monitors the UPS itself for its own shutdown "
        "protection -- passing it through here takes it away from Unraid."
    ),
}

_BLUETOOTH_NOTE = (
    "Home Assistant can use this for Bluetooth devices (sensors, presence). "
    "Support varies by chipset."
)

_NOT_TYPICALLY_USEFUL_TEXT: dict[str, str] = {
    "hid_keyboard_mouse": "Keyboard/mouse receiver -- not typically useful to Home Assistant.",
    "hid_other": "USB input device -- not typically useful to Home Assistant.",
    "storage": "Mass storage device -- not typically useful to Home Assistant.",
}


def _usb_note(device: UsbDevice) -> str | None:
    """The curated/Bluetooth/not-typically-useful rungs' own note --
    entirely separate from `hint` (the name-hint rung, below all of these)
    and from `auto_setup_note`/`zigbee_interference_note` (the HA-table-
    recognised rung's own notes, computed in `usb_context` directly from
    `device.recognised`, unaffected by anything added here).
    """
    tier = _usb_tier(device)
    if tier == "curated":
        return _CURATED_NOTE_TEXT[device.curated_key]
    if tier == "bluetooth":
        return _BLUETOOTH_NOTE
    if tier == "not_typically_useful":
        return _NOT_TYPICALLY_USEFUL_TEXT[device.not_typically_useful]
    return None


def _is_zigbee_category(category: str | None) -> bool:
    """The USB-3.0-interference note is Zigbee-specific: USB 3.0's
    SuperSpeed signalling emits 2.4 GHz noise, the same band Zigbee's radio
    uses, which is a documented cause of a flaky mesh. Z-Wave's radio is
    sub-GHz (908 MHz US / 868 MHz EU) and shares no spectrum with USB 3.0 at
    all, so it must never get this note just for being "recognised" too.
    A substring check catches both `_CATEGORY_BY_DOMAIN` labels that name a
    Zigbee radio -- the plain "Zigbee coordinator" (zha/deconz) and the combo
    "Zigbee/Thread coordinator" (Sky Connect, Connect ZBT-2) -- without
    hand-listing those domains a second time here.
    """
    return category is not None and "Zigbee" in category


@dataclass(frozen=True, slots=True)
class UsbOption:
    """One checkbox row on the usb step -- app.usbdevices.UsbDevice plus
    everything the template needs to render its badge and notes, computed
    once here so step_usb.html stays free of the honesty-ladder logic
    itself (the same split placement_context/resources_context already keep
    between "what was found" and "how to validate/store it")."""

    bus_port: str
    display_name: str
    vendor_id: str
    product_id: str
    serial: str | None
    badge: str | None
    hint: str | None
    note: str | None
    auto_setup_note: bool
    zigbee_interference_note: bool
    checked: bool


def usb_context() -> dict:
    """Every USB device on this server right now (app.usbdevices' own
    exclusions already applied -- boot flash and hubs are never even in this
    list), annotated for the template, with any box SESSION already
    remembers pre-ticked.
    """
    devices = _usb_devices()
    selected = {(d.vendor_id, d.product_id) for d in SESSION.usb_devices}
    options = [
        UsbOption(
            bus_port=d.bus_port,
            display_name=d.display_name,
            vendor_id=d.vendor_id,
            product_id=d.product_id,
            serial=d.serial,
            badge=_usb_badge(d),
            hint=_usb_hint(d),
            note=_usb_note(d),
            auto_setup_note=d.recognised,
            zigbee_interference_note=d.recognised and _is_zigbee_category(d.category),
            checked=(d.vendor_id, d.product_id) in selected,
        )
        for d in devices
    ]
    return {"options": options, "flash_identity_known": _flash_identity_known()}


def apply_usb(form) -> None:
    """Store the selection, replacing whatever was ticked before -- the same
    full re-derive-from-this-POST discipline apply_resources() already
    applies to cpu_pins, never an accumulate-on-top.
    """
    fresh = {d.bus_port: d for d in _usb_devices()}
    SESSION.usb_devices = tuple(
        USBDevice(vendor_id=d.vendor_id, product_id=d.product_id, name=d.display_name)
        for bus_port in form.getlist("usb")
        if (d := fresh.get(bus_port)) is not None
    )
