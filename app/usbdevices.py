"""Which USB devices are safe to offer for passthrough, and what we honestly
know about each one.
"""

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from app.usbmatchers import USB_MATCHERS, UsbMatcher

SYSFS_ROOT = "/sys"
VAR_INI_PATH = "/usr/local/emhttp/state/var.ini"

# Relative to sysfs_root. Every device (and interface, and root hub) sysfs
# knows about shows up as one entry directly under here.
_DEVICES_SUBPATH = "bus/usb/devices"

_HUB_DEVICE_CLASS = "09"

_CATEGORY_BY_DOMAIN = {
    "zha": "Zigbee coordinator",
    "deconz": "Zigbee coordinator",
    "zwave_js": "Z-Wave controller",
    "homeassistant_sky_connect": "Zigbee/Thread coordinator",
    "homeassistant_connect_zbt2": "Zigbee/Thread coordinator",
}


@dataclass(frozen=True, slots=True)
class UsbDevice:
    """One USB device that survived exclusion, annotated as honestly as
    app.usbmatchers' table allows.
    """

    bus_port: str
    vendor_id: str
    product_id: str
    serial: str | None
    display_name: str
    category: str | None
    recognised: bool
    ambiguous: bool
    domain: str | None
    name_hint: str | None
    bluetooth: bool
    curated_key: str | None
    curated_category: str | None
    not_typically_useful: str | None


def _read_attr(entry: Path, name: str) -> str | None:
    """One sysfs attribute file, stripped -- or None if it is absent,
    unreadable, or a directory (a garbled entry degrades to "unknown", never
    a crash). `errors="replace"` means undecodable bytes never raise either;
    they just do not compare equal to anything meaningful downstream, which
    is the safe direction for every attribute this module reads."""
    try:
        text = (entry / name).read_text(errors="replace")
    except OSError:
        return None
    text = text.strip()
    return text or None


def _is_hub(device_class: str | None) -> bool:
    return device_class is not None and device_class.strip().lower() == _HUB_DEVICE_CLASS


def _is_boot_flash(vendor_id: str, product_id: str, flash_guid: str | None) -> bool:
    """Unraid's own test, exactly: `stripos(flashGUID, "vid-pid") === 0` --
    the candidate's uppercased `vendor-product` pair is a case-insensitive
    PREFIX of flashGUID, never a substring/contains check. `flash_guid=None`
    (no var.ini, no key, or a flashGUID that is not even a vendor-product
    shape at all -- e.g. a non-USB boot's disk identifier) makes this always
    False: there is nothing to exclude against, which is the correct,
    non-error outcome (see _read_flash_guid)."""
    if flash_guid is None:
        return False
    prefix = f"{vendor_id}-{product_id}".upper()
    return flash_guid.upper().startswith(prefix)


_INI_LINE = re.compile(r"^([A-Za-z_]\w*)\s*=\s*(.*)$")


def _ini_value(raw: str) -> str:
    raw = raw.strip()
    if raw[:1] in "\"'":
        quote = raw[0]
        end = raw.find(quote, 1)
        if end != -1:
            return raw[1:end]
    return raw


def _read_flash_guid(path: str) -> str | None:
    """flashGUID from var.ini, or None if the file is unreadable, carries no
    flashGUID, or carries an empty one.

    None always means "no exclusion evidence", never "the boot flash was found
    and excluded".
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _INI_LINE.match(line)
        if not match or match.group(1) != "flashGUID":
            continue
        value = _ini_value(match.group(2))
        return value or None
    return None


def flash_identity_known(var_ini_path: str = VAR_INI_PATH) -> bool:
    """Did we manage to read this server's boot-flash identity at all?"""
    return _read_flash_guid(var_ini_path) is not None


@dataclass(frozen=True, slots=True)
class _Match:
    recognised: bool
    ambiguous: bool
    category: str | None
    domain: str | None


_NO_MATCH = _Match(recognised=False, ambiguous=False, category=None, domain=None)


def _glob_match(value: str | None, pattern: str) -> bool:
    """HA's own semantics, exactly (homeassistant/components/usb/utils.py
    `_fnmatch_lower`): fnmatch, both sides lowercased. `value=None` (the
    sysfs attribute this glob checks was absent) can never match a pattern
    -- an absent product/manufacturer/serial is not evidence for anything."""
    if value is None:
        return False
    return fnmatch.fnmatch(value.lower(), pattern.lower())


def _hard_fields_match(matcher: UsbMatcher, vendor_id: str, product_id: str) -> bool:
    """vid/pid are HA's "hard" fields -- exact match, case-insensitive, and
    only checked when the matcher actually carries them. A matcher with no
    `pid` at all (HA's real "insteon" entry: vid alone) matches every
    product id for that vendor, mirroring homeassistant.components.usb.utils
    .usb_device_matches_matcher's own `"pid" in matcher` check exactly --
    not merely a matcher whose pid happens to equal the device's."""
    fields = ((matcher.vid, vendor_id), (matcher.pid, product_id))
    return all(pattern is None or pattern.lower() == value for pattern, value in fields)


def _glob_fields_match(
    matcher: UsbMatcher, product: str | None, manufacturer: str | None, serial: str | None
) -> bool:
    """Every glob field the matcher actually carries must match -- not just
    `description`. A matcher naming both `description` and `manufacturer`
    (real example: enocean) is honest only if both hold, the same as HA's
    own matching code requires."""
    fields = (
        (matcher.description, product),
        (matcher.manufacturer, manufacturer),
        (matcher.serial_number, serial),
    )
    return all(pattern is None or _glob_match(value, pattern) for pattern, value in fields)


def _match(
    vendor_id: str,
    product_id: str,
    product: str | None,
    manufacturer: str | None,
    serial: str | None,
    matchers: tuple[UsbMatcher, ...],
) -> _Match:
    """Match against Home Assistant's own table.

    A vid:pid match whose glob fields also match is recognised and named. A
    vid:pid match where they do not is ambiguous: several products share the
    CP210x id, so naming one would be a guess. No vid:pid match is no claim.
    """
    candidates = [m for m in matchers if _hard_fields_match(m, vendor_id, product_id)]
    if not candidates:
        return _NO_MATCH
    for matcher in candidates:
        if _glob_fields_match(matcher, product, manufacturer, serial):
            return _Match(
                recognised=True,
                ambiguous=False,
                category=_CATEGORY_BY_DOMAIN.get(matcher.domain),
                domain=matcher.domain,
            )
    return _Match(recognised=False, ambiguous=True, category=None, domain=None)


@dataclass(frozen=True, slots=True)
class CuratedUsbId:
    """One entry in our own curated table. `pid=None` means vendor-only --
    the same "hard field this matcher doesn't carry matches anything" shape
    `_hard_fields_match` already gives HA's real vid-only `insteon` entry --
    used here for the UPS vendors, where the point is "any UPS this vendor
    makes", not one specific model. `key` is the honest, prose-free
    identifier app.web.routes_wizard looks its own note text up by (mirrors
    `name_hint`'s family word); `category` is the short badge label, shown
    verbatim, the same way `_CATEGORY_BY_DOMAIN`'s values are."""

    vid: str
    pid: str | None
    key: str
    category: str


CURATED_USB_IDS: tuple[CuratedUsbId, ...] = (
    CuratedUsbId(vid="0BDA", pid="2838", key="rtl_sdr", category="Software-defined radio"),
    CuratedUsbId(vid="0BDA", pid="2832", key="rtl_sdr", category="Software-defined radio"),
    CuratedUsbId(vid="1A6E", pid="089A", key="coral", category="Coral USB accelerator"),
    CuratedUsbId(vid="18D1", pid="9302", key="coral", category="Coral USB accelerator"),
    CuratedUsbId(vid="051D", pid=None, key="ups", category="UPS"),  # APC
    CuratedUsbId(vid="0764", pid=None, key="ups", category="UPS"),  # CyberPower
    CuratedUsbId(vid="0463", pid=None, key="ups", category="UPS"),  # Eaton / MGE
)


def _curated_match(
    vendor_id: str, product_id: str, curated_ids: tuple[CuratedUsbId, ...]
) -> CuratedUsbId | None:
    """The same vid-hard/pid-optional shape `_hard_fields_match` uses for
    HA's own table, applied to our tiny curated one -- the first matching
    entry wins, deterministic, mirroring `_match`'s own tie-break stance."""
    for candidate in curated_ids:
        if candidate.vid.lower() != vendor_id:
            continue
        if candidate.pid is not None and candidate.pid.lower() != product_id:
            continue
        return candidate
    return None


_BT_CLASS = "e0"
_BT_SUBCLASS = "01"
_BT_PROTOCOL = "01"

_HID_INTERFACE_CLASS = "03"

_HID_PROTOCOL_KEYBOARD = "01"
_HID_PROTOCOL_MOUSE = "02"

_STORAGE_CLASS = "08"


@dataclass(frozen=True, slots=True)
class _ClassSignals:
    bluetooth: bool
    hid: bool
    hid_boot_protocol: bool
    storage: bool


def _is_bt_triplet(device_class: str | None, subclass: str | None, protocol: str | None) -> bool:
    """All three fields present and matching -- a triplet with any field
    missing (an interface that, say, carries a class but no protocol file)
    is never treated as a match; absence is not evidence."""
    return (
        device_class is not None
        and device_class.strip().lower() == _BT_CLASS
        and subclass is not None
        and subclass.strip().lower() == _BT_SUBCLASS
        and protocol is not None
        and protocol.strip().lower() == _BT_PROTOCOL
    )


def _interface_entries(devices_dir: Path, bus_port: str, entry_names: list[str]) -> list[Path]:
    """This device's own interface entries -- sysfs names this device's
    `bus_port` with a `:` suffix (e.g. device "1-8" owns interfaces
    "1-8:1.0", "1-8:1.1", ...). The trailing `:` in the prefix is load
    bearing: it is what stops device "1-8" from also matching interfaces
    that actually belong to a *different* device sharing its prefix as a
    string, like "1-8.1" (a child device on the same hub port, not an
    interface of "1-8" at all) -- "1-8.1:1.0" does not start with "1-8:".
    These entries are read here for classification only, exactly like
    `enumerate_devices`' own docstring promises: never added to the returned
    device list."""
    prefix = f"{bus_port}:"
    return [devices_dir / name for name in entry_names if name.startswith(prefix)]


def _classify_by_class_codes(
    entry: Path, devices_dir: Path, bus_port: str, entry_names: list[str]
) -> _ClassSignals:
    """Bluetooth/HID/mass-storage, straight off the device's own and its
    interfaces' class-code attributes -- see the constants above for exactly
    which codes and which levels. Every interface is read regardless of
    what an earlier one already found, so a garbled or missing attribute on
    one interface can never hide a real signal on another.
    """
    device_class = _read_attr(entry, "bDeviceClass")
    device_subclass = _read_attr(entry, "bDeviceSubClass")
    device_protocol = _read_attr(entry, "bDeviceProtocol")
    bluetooth = _is_bt_triplet(device_class, device_subclass, device_protocol)
    storage = device_class is not None and device_class.strip().lower() == _STORAGE_CLASS
    hid = False
    hid_boot_protocol = False

    for iface in _interface_entries(devices_dir, bus_port, entry_names):
        iface_class = _read_attr(iface, "bInterfaceClass")
        iface_subclass = _read_attr(iface, "bInterfaceSubClass")
        iface_protocol = _read_attr(iface, "bInterfaceProtocol")
        if _is_bt_triplet(iface_class, iface_subclass, iface_protocol):
            bluetooth = True
        normalised = iface_class.strip().lower() if iface_class is not None else None
        if normalised == _HID_INTERFACE_CLASS:
            hid = True
            protocol = iface_protocol.strip().lower() if iface_protocol is not None else None
            if protocol in (_HID_PROTOCOL_KEYBOARD, _HID_PROTOCOL_MOUSE):
                hid_boot_protocol = True
        elif normalised == _STORAGE_CLASS:
            storage = True

    return _ClassSignals(
        bluetooth=bluetooth, hid=hid, hid_boot_protocol=hid_boot_protocol, storage=storage
    )


def _not_typically_useful_key(signals: _ClassSignals) -> str | None:
    """HID before storage -- both are the same weak "soft note" rung, so
    where a single device somehow trips both (a composite device with a HID
    interface and a storage interface at once), the choice is arbitrary but
    has to be deterministic; HID is checked first only because the spec's
    own wording lists it first, not because it is any more or less useful
    than the storage case.
    """
    if signals.hid:
        return "hid_keyboard_mouse" if signals.hid_boot_protocol else "hid_other"
    if signals.storage:
        return "storage"
    return None


_NAME_HINT_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("zigbee", "zigbee"),
    ("conbee", "zigbee"),
    ("zwave", "zwave"),
    ("z-wave", "zwave"),
    ("coordinator", "coordinator"),
)


def _name_hint(product: str | None, manufacturer: str | None) -> str | None:
    """The bottom rung of the honesty ladder, below "no claim" -- only ever
    consulted by `enumerate_devices` once `_match` has already found neither
    a recognised nor an ambiguous match (a vid:pid match, even a failed one,
    always outranks a name keyword; see `UsbDevice.name_hint`'s docstring).
    """
    haystack = " ".join(text for text in (product, manufacturer) if text is not None).lower()
    for keyword, family in _NAME_HINT_KEYWORDS:
        if keyword in haystack:
            return family
    return None


def _display_name(
    vendor_id: str, product_id: str, product: str | None, manufacturer: str | None
) -> str:
    """The device's own sysfs string, observed -- never a catalogue guess,
    even for a recognised device (see this module's own docstring and
    _match's). Falls back to manufacturer, then to the bare ids, only when
    sysfs itself offered nothing better."""
    if product:
        return product
    if manufacturer:
        return manufacturer
    return f"USB device {vendor_id}:{product_id}"


def enumerate_devices(
    sysfs_root: str = SYSFS_ROOT,
    var_ini_path: str = VAR_INI_PATH,
    matchers: tuple[UsbMatcher, ...] = USB_MATCHERS,
    curated_ids: tuple[CuratedUsbId, ...] = CURATED_USB_IDS,
) -> list[UsbDevice]:
    """Every USB device on this server that is safe to offer for
    passthrough, annotated with whatever app.usbmatchers' table (Tier 0,
    HA's own), `curated_ids` (Tier 2, ours), and raw USB class codes
    (Tier 1) honestly support -- never raises.
    """
    devices_dir = Path(sysfs_root) / _DEVICES_SUBPATH
    try:
        entry_names = sorted(p.name for p in devices_dir.iterdir())
    except OSError:
        entry_names = []

    flash_guid = _read_flash_guid(var_ini_path)

    found: list[UsbDevice] = []
    for name in entry_names:
        entry = devices_dir / name
        vendor_id = _read_attr(entry, "idVendor")
        product_id = _read_attr(entry, "idProduct")
        if vendor_id is None or product_id is None:
            continue  # not a device node (an interface entry, or garbled) -- skip silently

        vendor_id = vendor_id.lower()
        product_id = product_id.lower()

        if _is_hub(_read_attr(entry, "bDeviceClass")):
            continue
        if _is_boot_flash(vendor_id, product_id, flash_guid):
            continue

        product = _read_attr(entry, "product")
        manufacturer = _read_attr(entry, "manufacturer")
        serial = _read_attr(entry, "serial")

        match = _match(vendor_id, product_id, product, manufacturer, serial, matchers)

        curated = None
        if not match.recognised:
            curated = _curated_match(vendor_id, product_id, curated_ids)

        # A curated match outranks what would otherwise be an ambiguous HA
        # hard-match-glob-fail -- see this module's own docstring for why.
        ambiguous = match.ambiguous and curated is None

        stronger_claim = match.recognised or curated is not None or ambiguous
        class_signals = (
            _classify_by_class_codes(entry, devices_dir, name, entry_names)
            if not stronger_claim
            else None
        )
        bluetooth = class_signals is not None and class_signals.bluetooth

        name_hint = None
        if not stronger_claim and not bluetooth:
            name_hint = _name_hint(product, manufacturer)

        # The weakest rung of all: a soft "not typically useful" note, only
        # ever reached once every other rung above has come back negative.
        not_typically_useful = None
        if not stronger_claim and not bluetooth and name_hint is None:
            not_typically_useful = _not_typically_useful_key(class_signals)

        found.append(
            UsbDevice(
                bus_port=name,
                vendor_id=vendor_id,
                product_id=product_id,
                serial=serial,
                display_name=_display_name(vendor_id, product_id, product, manufacturer),
                category=match.category,
                recognised=match.recognised,
                ambiguous=ambiguous,
                domain=match.domain,
                name_hint=name_hint,
                bluetooth=bluetooth,
                curated_key=curated.key if curated is not None else None,
                curated_category=curated.category if curated is not None else None,
                not_typically_useful=not_typically_useful,
            )
        )

    return found
