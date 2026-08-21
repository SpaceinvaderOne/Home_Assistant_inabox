"""Facts about the Unraid host the container is running on."""

import http.client
import json
import os.path
import re
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from app.libvirtctl import VirshError
from app.paths import DOMAIN_CFG_PATH, USER_SHARE_ROOT

DOCKER_SOCKET = "/var/run/docker.sock"
DOCKER_TIMEOUT = 10.0
BRNAME = re.compile(r'^BRNAME="([^"]+)"', re.MULTILINE)
VHOST = re.compile(r"^vhost\d+$")

# What a correct `docker run -v` looks like for each mapping we ask about, so the
# error can name the exact argument that is missing rather than a bare KeyError.
MOUNT_HINTS = {
    "/domains": "/mnt/user/domains",
    "/config": "/mnt/user/appdata/homeassistant-inabox",
}

# A path shaped like /mnt/<something>/<share>/<rest>, for the pool-path fallback
# in `container_path_for` below.
_POOL_PATH = re.compile(r"^/mnt/(?P<root>[^/]+)/(?P<share>[^/]+)/(?P<rest>.+)$")

_NOT_A_USER_SHARE_ALIAS = frozenset({"disks", "remotes", "user", "user0", "addons"})


class BridgeNotFoundError(Exception):
    """Neither domain.cfg nor any existing VM offered a usable network source."""


class DockerApiError(Exception):
    """The Docker API could not be reached, or refused to describe this container."""


class MountNotFoundError(Exception):
    """A bind mount this container needs was not passed to `docker run`."""


@dataclass(frozen=True, slots=True)
class Bridge:
    name: str
    kind: Literal["bridge", "direct"]
    is_default: bool
    in_use_by: tuple[str, ...]


def _connect(path: str = DOCKER_SOCKET, timeout: float = DOCKER_TIMEOUT) -> socket.socket:
    """Open the Docker unix socket, naming the mapping to add when it is not there."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
    except OSError as err:
        sock.close()
        raise DockerApiError(
            f"cannot reach the Docker API at {path} ({err}); "
            f"add -v {path}:{path}:ro to your docker run"
        ) from err
    return sock


def _docker_inspect(connect=_connect) -> dict:
    """Ask the Docker API about our own container over its unix socket."""
    container_id = socket.gethostname()
    with connect() as sock:
        sock.sendall(
            f"GET /containers/{container_id}/json HTTP/1.1\r\n"
            "Host: localhost\r\nConnection: close\r\n\r\n".encode()
        )
        response = http.client.HTTPResponse(sock)
        try:
            response.begin()
            status = response.status
            body = response.read()
        except (http.client.HTTPException, OSError) as err:
            raise DockerApiError(
                f"malformed reply from the Docker API at {DOCKER_SOCKET}: {err}"
            ) from err

    if status != 200:
        detail = body[:200].decode(errors="replace").strip()
        raise DockerApiError(f"Docker API returned {status} for container {container_id}: {detail}")
    try:
        return json.loads(body)
    except ValueError as err:
        raise DockerApiError(f"Docker API returned a body we cannot parse: {err}") from err


def host_path_for(container_path: str, inspect=_docker_inspect) -> str:
    """Map a path inside the container to its path on the Unraid host."""
    for mount in inspect().get("Mounts", []):
        if mount.get("Destination") == container_path:
            return mount["Source"]
    hint = MOUNT_HINTS.get(container_path, "<host path>")
    raise MountNotFoundError(
        f"no bind mount found for container path {container_path}; "
        f"add -v {hint}:{container_path} to your docker run"
    )


def container_path_for(host_path: str, inspect=_docker_inspect) -> str | None:
    """Map a path on the Unraid host to wherever this container can actually
    read it -- the inverse of host_path_for.
    """
    mounts = inspect().get("Mounts", [])
    normalized = os.path.normpath(host_path)
    direct = _resolve_against_mounts(normalized, mounts)
    if direct is not None:
        return direct
    rewritten = _user_share_equivalent(normalized)
    if rewritten is None:
        return None
    return _resolve_against_mounts(rewritten, mounts)


def _resolve_against_mounts(normalized_host_path: str, mounts: list[dict]) -> str | None:
    """The longest-mount-wins lookup `container_path_for` runs, unchanged,
    against whichever path it is handed -- `host_path` as virsh wrote it on
    the first attempt, its `/mnt/user/...` rewrite on the second."""
    best_specificity = -1
    best_container_path: str | None = None
    for mount in mounts:
        source, destination = mount.get("Source"), mount.get("Destination")
        if not source or not destination:
            continue
        try:
            relative = Path(normalized_host_path).relative_to(source)
        except ValueError:
            continue
        specificity = len(Path(source).parts)
        if specificity > best_specificity:
            best_specificity = specificity
            best_container_path = str(Path(destination) / relative)
    return best_container_path


def _user_share_equivalent(normalized_host_path: str) -> str | None:
    """Rewrite `/mnt/<pool-or-disk>/<share>/<rest>` to `/mnt/user/<share>/<rest>`,
    Unraid's own FUSE user-share view of the same file -- see
    `container_path_for`'s docstring for why that is always safe to try.
    """
    match = _POOL_PATH.match(normalized_host_path)
    if not match or match["root"] in _NOT_A_USER_SHARE_ALIAS:
        return None
    return f"{USER_SHARE_ROOT}/{match['share']}/{match['rest']}"


def free_bytes(path: str) -> int:
    return shutil.disk_usage(path).free


def bridges(virsh, domain_cfg: str = DOMAIN_CFG_PATH) -> list[Bridge]:
    """Discover usable network sources from the two sources we have."""
    default = ""
    try:
        with open(domain_cfg) as handle:
            match = BRNAME.search(handle.read())
            if match:
                default = match.group(1)
    except OSError:
        pass

    users: dict[str, list[str]] = {}
    kinds: dict[str, str] = {}
    for name in virsh.list_domains():
        try:
            root = ElementTree.fromstring(virsh.dumpxml(name))
        except (ElementTree.ParseError, VirshError):
            continue
        for interface in root.iterfind("devices/interface"):
            source = interface.find("source")
            if source is None:
                continue
            found = source.get("bridge") or source.get("dev")
            if not found:
                continue
            users.setdefault(found, []).append(name)
            kinds[found] = "direct" if interface.get("type") == "direct" else "bridge"

    names = list(dict.fromkeys([default, *users]))
    names = [n for n in names if n]
    if not names:
        raise BridgeNotFoundError(
            f"no network source found: {domain_cfg} unreadable and no other VM has one"
        )

    return [
        Bridge(
            name=name,
            kind=kinds.get(name, "direct" if VHOST.match(name) else "bridge"),
            is_default=(name == default),
            in_use_by=tuple(users.get(name, ())),
        )
        for name in names
    ]
