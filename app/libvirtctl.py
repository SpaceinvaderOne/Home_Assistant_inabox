"""A narrow wrapper over the `virsh` CLI."""

import json
import shlex
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr

from app.proc import run
from app.vmxml import metadata_attributes, read_metadata_namespace

MISSING = "missing"

GUEST_INTERNAL_INTERFACE_PREFIXES = ("docker", "hassio", "veth", "br-")


class VirshError(Exception):
    def __init__(self, argv: list[str], code: int, stderr: str):
        self.argv = argv
        self.code = code
        self.stderr = stderr
        super().__init__(f"{shlex.join(argv)} failed ({code}): {stderr.strip()}")


class MetadataConflictError(Exception):
    """More than one <vmtemplate> element survived a metadata write."""


class UnsupportedAttributeError(Exception):
    """An existing <vmtemplate> attribute can't be safely rewritten."""


def _vmtemplate_attributes(domain_xml: str) -> dict[str, str]:
    """The first <vmtemplate> element's own attributes, whichever of the three
    serialisations produced it (see read_metadata_namespace's docstring) --
    local-name matching, not a fixed tag string, so a namespace prefix doesn't
    hide the element. Empty if the domain carries none."""
    for element in ElementTree.fromstring(domain_xml).iter():
        if element.tag.rpartition("}")[-1] == "vmtemplate":
            return dict(element.attrib)
    return {}


def _count_vmtemplates(domain_xml: str) -> int:
    return sum(
        1
        for element in ElementTree.fromstring(domain_xml).iter()
        if element.tag.rpartition("}")[-1] == "vmtemplate"
    )


class Virsh:
    def __init__(self, runner: Callable = run, log: Callable[[str], None] | None = None):
        self._run = runner
        self._log = log

    def _call(self, *args: str, check: bool = True) -> tuple[int, str, str]:
        argv = ["virsh", *args]
        if self._log:
            self._log(shlex.join(argv))
        code, out, err = self._run(argv)
        if check and code != 0:
            raise VirshError(argv, code, err)
        return code, out, err

    def capabilities(self) -> str:
        return self._call("capabilities")[1]

    def domain_exists(self, name: str) -> bool:
        return self._call("dominfo", name, check=False)[0] == 0

    def state(self, name: str) -> str:
        code, out, _err = self._call("domstate", name, check=False)
        return MISSING if code != 0 else out.strip()

    def define(self, xml_path: Path | str) -> None:
        self._call("define", str(xml_path))

    def start(self, name: str) -> None:
        self._call("start", name)

    def resume(self, name: str) -> None:
        """Unpause a *paused* domain."""
        self._call("resume", name)

    def agent_ping(self, name: str) -> bool:
        code, _out, _err = self._call(
            "qemu-agent-command", name, '{"execute":"guest-ping"}', check=False
        )
        return code == 0

    def interface_addresses(self, name: str, mac: str | None = None) -> list[str]:
        """IPv4 addresses of the VM's real NIC, as seen by the guest agent."""
        code, out, _err = self._call("domifaddr", name, "--source", "agent", check=False)
        if code != 0:
            return []
        found = []
        current_iface = current_mac = ""
        for line in out.splitlines():
            fields = line.split()
            if len(fields) != 4 or fields[2] not in ("ipv4", "ipv6"):
                continue
            iface_field, mac_field, protocol, address_field = fields
            if iface_field != "-":
                current_iface, current_mac = iface_field, mac_field
            if protocol != "ipv4":
                continue
            address = address_field.split("/")[0]
            if address.startswith("127."):
                continue
            if mac is not None:
                if current_mac.lower() != mac.lower():
                    continue
            elif current_iface.startswith(GUEST_INTERNAL_INTERFACE_PREFIXES):
                continue
            found.append(address)
        return found

    def guest_hostname(self, name: str) -> str | None:
        code, out, _err = self._call(
            "qemu-agent-command", name, '{"execute":"guest-get-host-name"}', check=False
        )
        if code != 0:
            return None
        try:
            return json.loads(out)["return"]["host-name"]
        except (ValueError, KeyError, TypeError):
            return None

    def list_domains(self) -> list[str]:
        """One domain name per line — `.split()` would tear a spaced name like
        "Windows 10" (what Unraid's own VM wizard suggests) into two entries."""
        lines = self._call("list", "--all", "--name")[1].splitlines()
        return [stripped for line in lines if (stripped := line.strip())]

    def dumpxml(self, name: str) -> str:
        return self._call("dumpxml", name)[1]

    def set_webui(self, name: str, url: str, default: str | tuple[str, ...] = "") -> bool | None:
        """Correct the WebUI link Unraid's VM manager offers for `name`."""
        domain_xml = self.dumpxml(name)
        namespace = read_metadata_namespace(domain_xml)
        if namespace is None:
            return False

        defaults = (default,) if isinstance(default, str) else tuple(default)
        existing = _vmtemplate_attributes(domain_xml)
        current_webui = existing.get("webui", "")
        if current_webui not in ("", *defaults):
            return None
        if current_webui == url:
            # Already correct -- an identical --set on a live domain would be
            # a write for no gain.
            return True

        extra = {key: value for key, value in existing.items() if key != "webui"}
        for key in extra:
            if key.startswith("{"):
                raise UnsupportedAttributeError(
                    f"{name}: <vmtemplate> has a namespace-prefixed attribute "
                    f"{key!r} that cannot be safely rewritten; leaving the "
                    "webui attribute as it was"
                )
        attributes = metadata_attributes(
            icon=existing.get("icon", ""), webui=url, namespace=namespace, extra=extra
        )
        never_invent = {"name", "iconold", "icon", "os", "storage"}
        attributes = {
            key: value
            for key, value in attributes.items()
            if key not in never_invent or key in existing
        }
        rendered = " ".join(f"{key}={quoteattr(value)}" for key, value in attributes.items())
        element = f"<vmtemplate {rendered}/>"

        self._call(
            "metadata",
            name,
            "--uri",
            namespace,
            "--key",
            "vmtemplate",
            "--set",
            element,
            "--live",
            "--config",
        )

        after = self.dumpxml(name)
        count = _count_vmtemplates(after)
        if count != 1:
            raise MetadataConflictError(
                f"{name}: {count} <vmtemplate> elements remain after writing webui "
                f"(expected 1) -- likely wrote the wrong namespace URI"
            )
        return True
