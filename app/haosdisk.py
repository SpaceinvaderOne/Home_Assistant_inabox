"""Identify Home Assistant OS from what is actually written on a VM's disk."""

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.proc import run

HAOS_PARTITION_PREFIX = "hassos-"

# GPT layout within the first MiB, as verified on hardware (see module
# docstring). Field offsets are absolute, from the start of the disk.
_HEADER_OFFSET = 512
_GPT_MAGIC = b"EFI PART"
_ARRAY_LBA_OFFSET = _HEADER_OFFSET + 72  # little-endian u64
_ENTRY_COUNT_OFFSET = _HEADER_OFFSET + 80  # little-endian u32
_ENTRY_SIZE_OFFSET = _HEADER_OFFSET + 84  # little-endian u32
_NAME_OFFSET_IN_ENTRY = 56
_NAME_LENGTH = 72  # bytes; UTF-16LE, NUL-padded
_SECTOR_SIZE = 512

_MAX_ENTRY_SIZE = 4096
_MAX_ENTRY_COUNT = 16384


@dataclass(frozen=True)
class DiskIdentity:
    is_haos: bool
    partitions: tuple[str, ...]
    reason: str


def identify(path: str, *, runner: Callable = run) -> DiskIdentity:
    """Read the first MiB of the disk at `path` and look for HAOS's GPT labels."""
    if not Path(path).is_file():
        return DiskIdentity(False, (), "the disk file was not found")

    fd, temp_name = tempfile.mkstemp(prefix="haosdisk-")
    os.close(fd)  # only qemu-img writes to this path; nothing here needs the fd open
    try:
        try:
            code, _out, err = runner(
                ["qemu-img", "dd", "-U", f"if={path}", f"of={temp_name}", "bs=1M", "count=1"]
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # one bad disk must not abort a scan of many
            return DiskIdentity(False, (), f"qemu-img could not open it: {exc}")

        if code != 0:
            detail = err.strip() or f"exit code {code}"
            return DiskIdentity(False, (), f"qemu-img could not open it: {detail}")

        try:
            data = Path(temp_name).read_bytes()
        except OSError as exc:
            return DiskIdentity(False, (), f"qemu-img could not open it: {exc}")
    finally:
        Path(temp_name).unlink(missing_ok=True)

    return _identify_from_first_mib(data)


def _identify_from_first_mib(data: bytes) -> DiskIdentity:
    try:
        scan = _scan_gpt(data)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # garbage bytes degrade to "cannot tell", never a crash
        return DiskIdentity(False, (), "no GPT partition table")

    if scan is None:
        return DiskIdentity(False, (), "no GPT partition table")

    if not scan.names:
        reason = f"read {scan.used_count} GPT partitions, none of them named"
        return DiskIdentity(False, (), reason)

    haos_count = sum(1 for name in scan.names if name.startswith(HAOS_PARTITION_PREFIX))
    reason = f"read {scan.used_count} GPT partitions, {haos_count} named {HAOS_PARTITION_PREFIX}*"
    return DiskIdentity(haos_count > 0, tuple(scan.names), reason)


@dataclass(frozen=True)
class _GPTScan:
    """`used_count` counts every entry whose type GUID is non-zero (i.e.
    actually allocated to a partition); `names` holds only the ones that
    also carry a non-empty label. The two are not interchangeable: many
    real-world partitioning tools leave the name field blank, and a disk
    with five used-but-unnamed entries is not a disk with zero partitions."""

    used_count: int
    names: list[str]


def _scan_gpt(data: bytes) -> _GPTScan | None:
    """`None` means "no usable GPT here" -- absent magic, a header too short
    to trust, or entry_count/entry_size that fail the sanity bounds above.
    Every slice below is bounds-safe by construction (Python slicing never
    raises on an out-of-range index), so a short or truncated read falls
    through to `None` rather than an exception."""
    if data[_HEADER_OFFSET : _HEADER_OFFSET + len(_GPT_MAGIC)] != _GPT_MAGIC:
        return None
    if len(data) < _ENTRY_SIZE_OFFSET + 4:
        return None

    array_lba = int.from_bytes(data[_ARRAY_LBA_OFFSET : _ARRAY_LBA_OFFSET + 8], "little")
    entry_count = int.from_bytes(data[_ENTRY_COUNT_OFFSET : _ENTRY_COUNT_OFFSET + 4], "little")
    entry_size = int.from_bytes(data[_ENTRY_SIZE_OFFSET : _ENTRY_SIZE_OFFSET + 4], "little")
    if not (128 <= entry_size <= _MAX_ENTRY_SIZE):
        return None
    if not (0 < entry_count <= _MAX_ENTRY_COUNT):
        return None

    array_offset = array_lba * _SECTOR_SIZE
    used_count = 0
    names = []
    for index in range(entry_count):
        start = array_offset + index * entry_size
        end = start + entry_size
        if end > len(data):
            break  # ran off the end of what was actually read -- stop, don't guess
        entry = data[start:end]
        if entry[:16] == b"\x00" * 16:
            continue  # unused entry
        used_count += 1
        name_bytes = entry[_NAME_OFFSET_IN_ENTRY : _NAME_OFFSET_IN_ENTRY + _NAME_LENGTH]
        name = name_bytes.decode("utf-16-le", errors="replace").rstrip("\x00")
        if name:
            names.append(name)
    return _GPTScan(used_count, names)
