"""Fetch and prepare the HAOS disk image."""

import hashlib
import lzma
import urllib.request
from collections.abc import Callable
from pathlib import Path

from app.net import NetworkTimeout, is_timeout, request_for
from app.proc import run

CHUNK = 1024 * 256

DOWNLOAD_TIMEOUT = 120.0

# The half-written image is parked here and renamed into place only once it is
# whole. See decompress().
PART_SUFFIX = ".part"

ProgressFn = Callable[[int, int], None]
"""Progress callback, called as progress(done, total) in bytes.

`total` is 0 when the total is genuinely unknown -- a response with no
Content-Length, say -- and a renderer must then show a byte count rather than a
percentage. Every producer that *can* know its total reports one.
"""


class ChecksumMismatch(Exception):
    """The downloaded bytes do not match the digest GitHub published."""


def download(
    url: str,
    dest: Path,
    expected_sha256: str,
    progress: ProgressFn | None = None,
    opener=urllib.request.urlopen,
) -> Path:
    """Stream `url` to `dest`, verifying its SHA256 as it goes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    done = 0

    request = request_for(url)
    try:
        with opener(request, timeout=DOWNLOAD_TIMEOUT) as response, dest.open("wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            while chunk := response.read(CHUNK):
                handle.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except BaseException as err:
        dest.unlink(missing_ok=True)
        if is_timeout(err):
            raise NetworkTimeout(
                f"no data from {url} for {DOWNLOAD_TIMEOUT:.0f}s, so the download was "
                "abandoned rather than left hanging. Check the server's internet "
                "connection and try again."
            ) from err
        raise

    actual = digest.hexdigest()
    if actual != expected_sha256.lower():
        dest.unlink(missing_ok=True)
        raise ChecksumMismatch(
            f"expected {expected_sha256.lower()} but got {actual}; download discarded"
        )
    return dest


def file_sha256(path: Path) -> str:
    """Digest a file already on disk."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class ResizeError(Exception):
    """qemu-img refused to resize the image."""


def decompress(src: Path, dest: Path, progress: ProgressFn | None = None) -> Path:
    """Expand a .xz image, and only then give it its final name."""
    part = dest.with_name(dest.name + PART_SUFFIX)
    total = src.stat().st_size
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with src.open("rb") as raw, lzma.open(raw, "rb") as compressed, part.open("wb") as handle:
            while chunk := compressed.read(CHUNK):
                handle.write(chunk)
                if progress:
                    progress(min(raw.tell(), total), total)
        if progress:
            progress(total, total)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.rename(dest)
    return dest


def resize(image: Path, size_gib: int, runner=run) -> None:
    """Grow the image to `size_gib`. HAOS expands its data partition on first boot."""
    code, _out, err = runner(["qemu-img", "resize", str(image), f"{size_gib}G"])
    if code != 0:
        raise ResizeError(f"qemu-img resize failed: {err.strip() or f'exit {code}'}")


# The container's own view of the OVMF variables template. libvirt reads the
# host path (app.vmxml.OVMF_VARS_TEMPLATE); this is only used to check the file
# is really there before a VM is defined that depends on it.
OVMF_VARS = "/host/usr/share/qemu/ovmf-x64/OVMF_VARS-pure-efi.fd"


def check_ovmf_vars(template: str = OVMF_VARS) -> str:
    """Confirm the OVMF variables template exists, and return its path.

    libvirt creates each VM's NVRAM from this file when the domain first
    starts, so a missing template means a VM that defines cleanly and then
    fails to boot. Better caught before anything is defined.
    """
    source = Path(template)
    if not source.is_file():
        raise FileNotFoundError(
            f"OVMF variables template not found at {template}; is /host/usr/share/qemu mapped?"
        )
    return str(source)
