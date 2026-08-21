"""Read Home Assistant's own onboarding progress, over its stdlib-reachable API."""

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

HA_PORT = 8123

CANDIDATE_PORTS: tuple[int, ...] = (80, HA_PORT)

ONBOARDING_TIMEOUT = 5.0
PORT_PROBE_TIMEOUT = 2.0

_DEFAULT_PORT_FOR_SCHEME = {"http": 80, "https": 443}

STEP_LABELS = {
    "user": "Create your account",
    "core_config": "Set your location and units",
    "analytics": "Analytics preferences",
    "integration": "Discovered devices",
}


@dataclass(frozen=True, slots=True)
class OnboardingStep:
    key: str
    done: bool

    @property
    def label(self) -> str:
        """The wizard's wording for this step."""
        return STEP_LABELS.get(self.key, self.key.replace("_", " ").capitalize())


@dataclass(frozen=True, slots=True)
class OnboardingState:
    reachable: bool
    complete: bool
    steps: tuple[OnboardingStep, ...]


_UNKNOWN = OnboardingState(reachable=True, complete=False, steps=())


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Where Home Assistant actually answered -- not where we guessed it would."""

    scheme: str
    host: str
    port: int | None

    @property
    def base_url(self) -> str:
        """The URL to show or store, with the port omitted when it adds nothing."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port is None or self.port == _DEFAULT_PORT_FOR_SCHEME.get(self.scheme):
            return f"{self.scheme}://{host}"
        return f"{self.scheme}://{host}:{self.port}"

    def with_host(self, host: str) -> "Endpoint":
        """The same resolved port, attached to a different host."""
        return Endpoint(self.scheme, host, self.port)


def _endpoint_from_url(url: str | None) -> Endpoint | None:
    """Turn the URL a response landed on into an Endpoint's scheme and port,
    or None if there is nothing here worth trusting.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return Endpoint(scheme=parsed.scheme, host=parsed.hostname, port=parsed.port)


def resolve_endpoint(
    ip: str,
    ports: tuple[int, ...] = CANDIDATE_PORTS,
    timeout: float = PORT_PROBE_TIMEOUT,
    opener=urllib.request.urlopen,
) -> Endpoint | None:
    """Ask Home Assistant where it lives, instead of assuming a port."""
    for port in ports:
        url = f"http://{ip}:{port}/api/onboarding"
        try:
            with opener(url, timeout=timeout) as response:
                landed = response.geturl()
        except urllib.error.HTTPError as err:
            landed = err.url
        except (TimeoutError, urllib.error.URLError):
            continue
        resolved = _endpoint_from_url(landed)
        if resolved is None:
            continue
        return resolved.with_host(ip)
    return None


def _parse_steps(payload: object) -> tuple[OnboardingStep, ...] | None:
    """Turn HA's onboarding payload into steps, or None if the shape is wrong."""
    if not isinstance(payload, list):
        return None
    steps = []
    for entry in payload:
        if not isinstance(entry, dict) or "step" not in entry or "done" not in entry:
            return None
        steps.append(OnboardingStep(key=entry["step"], done=bool(entry["done"])))
    return tuple(steps)


def onboarding_status(
    endpoint: Endpoint,
    timeout: float = ONBOARDING_TIMEOUT,
    opener=urllib.request.urlopen,
) -> OnboardingState:
    """Ask HA where it is in onboarding, translated into the wizard's states."""
    url = f"{endpoint.base_url}/api/onboarding"
    try:
        with opener(url, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return OnboardingState(reachable=True, complete=True, steps=())
        return _UNKNOWN
    except TimeoutError:
        return _UNKNOWN
    except urllib.error.URLError:
        return OnboardingState(reachable=False, complete=False, steps=())

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return _UNKNOWN

    steps = _parse_steps(payload)
    if not steps:
        return _UNKNOWN
    return OnboardingState(reachable=True, complete=all(s.done for s in steps), steps=steps)


def manifest_name(
    endpoint: Endpoint,
    timeout: float = ONBOARDING_TIMEOUT,
    opener=urllib.request.urlopen,
) -> str | None:
    """Home Assistant's own frontend manifest -- `{"name": "Home Assistant",
    ...}`, unauthenticated, one request, unmistakable.
    """
    url = f"{endpoint.base_url}/manifest.json"
    try:
        with opener(url, timeout=timeout) as response:
            body = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    return name if isinstance(name, str) else None


def port_open(
    ip: str,
    port: int = HA_PORT,
    timeout: float = PORT_PROBE_TIMEOUT,
    connector=socket.create_connection,
) -> bool:
    """Is anything listening on ip:port at all?"""
    try:
        connection = connector((ip, port), timeout=timeout)
    except OSError:
        return False
    if connection is not None:
        connection.close()
    return True
