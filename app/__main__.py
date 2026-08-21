"""Command line entry point."""

import argparse
import os
import sys
from pathlib import Path
from uuid import uuid4

from app import download as dl
from app import paths
from app.capabilities import highest_q35
from app.events import Event
from app.hostinfo import free_bytes, host_path_for
from app.installer import InstallError, InstallPlan, install
from app.libvirtctl import Virsh
from app.models import NetworkSource, VMSettings
from app.releases import RateLimitedError, fetch_latest, fetch_releases
from app.vmxml import generate_mac


def format_event(event: Event) -> str:
    if event.status == "progress":
        if event.total:
            return f"  {event.step}: {event.done * 100 // event.total}%"
        return f"  {event.step}: {event.done} bytes"
    marker = {"start": "->", "ok": "OK", "fail": "FAIL", "info": "  "}[event.status]
    return f"{marker} {event.step}{': ' + event.detail if event.detail else ''}"


class RealDeps:
    """Binds the installer's Protocol to the real modules."""

    def __init__(self, virsh: Virsh):
        self._virsh = virsh

    def free_bytes(self, path):
        return free_bytes(path)

    def exists(self, path):
        return Path(path).exists()

    def sha256(self, path):
        return dl.file_sha256(Path(path))

    def download(self, url, dest, sha256, progress=None):
        return dl.download(url, Path(dest), sha256, progress=progress)

    def decompress(self, src, dest, progress=None):
        return dl.decompress(Path(src), Path(dest), progress=progress)

    def remove(self, path):
        Path(path).unlink(missing_ok=True)

    def resize(self, image, size_gib):
        dl.resize(Path(image), size_gib)

    def prepare_nvram(self, uuid):
        return dl.prepare_nvram(uuid)

    def write_xml(self, xml, path):
        Path(path).write_text(xml)
        return path

    def define(self, path):
        self._virsh.define(path)

    def start(self, name):
        self._virsh.start(name)


def serve() -> None:
    """Run the wizard's web server -- the product's front door -- unless the
    Docker template that started this container does not match this
    image's own major version.
    """
    from app.notice import classify_whatversion, run_notice_server

    raw_value = os.environ.get("WHATVERSION")
    kind = classify_whatversion(raw_value)
    if kind == "match":
        _serve_app()
    else:
        run_notice_server(kind, raw_value)


def _serve_app() -> None:
    """Start the real wizard on 0.0.0.0:9123. The one real entrypoint for
    everything mount-dependent -- state_store's /config/state.json, the
    watcher, libvirt -- so serve()'s gate above can be proven correct by
    proving this function alone is never reached except when WHATVERSION
    already matches this image.
    """
    import uvicorn

    from app.web.main import create_app
    from app.web.state_store import DEFAULT_STATE_PATH

    uvicorn.run(
        create_app(start_watcher=True, state_path=DEFAULT_STATE_PATH), host="0.0.0.0", port=9123
    )


def main(argv: list[str], out=print) -> int:
    parser = argparse.ArgumentParser(prog="app", add_help=True)
    parser.add_argument("command", nargs="?", choices=["releases", "install", "serve"])
    parser.add_argument("--name", default="Home Assistant")
    parser.add_argument("--vcpus", type=int, default=2)
    parser.add_argument("--memory", type=int, default=4096)
    parser.add_argument("--disk", type=int, default=64)
    parser.add_argument("--version", default="")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if not args.command:
        out("usage: python -m app [releases|install|serve]")
        return 2

    try:
        if args.command == "releases":
            for release in fetch_releases():
                out(f"{release.version}\t{release.published[:10]}\t{release.size // 1024**2} MB")
            return 0
        if args.command == "serve":
            serve()
            return 0
    except RateLimitedError as err:
        out(f"FAIL {err}")
        return 1

    virsh = Virsh(log=lambda line: out(f"   $ {line}"))
    try:
        release = fetch_latest()
        if args.version:
            release = next((r for r in fetch_releases(30) if r.version == args.version), release)
    except RateLimitedError as err:
        out(f"FAIL {err}")
        return 1

    domains = host_path_for("/domains")
    out(f"host path for /domains is {domains}")

    settings = VMSettings(
        name=args.name,
        uuid=str(uuid4()),
        machine=highest_q35(virsh.capabilities()),
        memory_mib=args.memory,
        vcpus=args.vcpus,
        disk_path=paths.disk_path(domains, args.name),
        mac=generate_mac(),
        network=NetworkSource(name="br0", kind="bridge"),
    )
    plan = InstallPlan(
        settings=settings,
        release=release,
        size_gib=args.disk,
        domains_host_path=domains,
        domains_container_path="/domains",
        webui="http://homeassistant.local",
    )
    try:
        install(plan, RealDeps(virsh), lambda event: out(format_event(event)))
    except InstallError as err:
        out(f"FAIL {err}")
        return 1
    out(f"defined and started {args.name}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
