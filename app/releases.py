"""Resolve Home Assistant OS releases from the GitHub releases API."""

import json
import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.net import NetworkTimeout, is_timeout, request_for

API = "https://api.github.com/repos/home-assistant/operating-system/releases"

API_TIMEOUT = 15.0

OVER_FETCH = 3
MAX_PER_PAGE = 100

# The x86_64 KVM image. Deliberately excludes generic-aarch64, .ova, .vdi,
# .vmdk and .raucb, which are all published under the same release.
ASSET = re.compile(r"^haos_ova-.+\.qcow2\.xz$")


class NoAssetError(Exception):
    """The release has no verifiable x86_64 qcow2.xz asset."""


class RateLimitedError(Exception):
    """GitHub rejected the request; unauthenticated calls are capped at 60/hour."""


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    published: str
    notes: str
    asset_url: str
    sha256: str
    size: int


def parse_release(payload: dict) -> Release:
    """Build a Release from one GitHub release object."""
    for asset in payload.get("assets", []):
        if not ASSET.match(asset.get("name", "")):
            continue
        digest = asset.get("digest") or ""
        if not digest.startswith("sha256:"):
            raise NoAssetError(
                f"asset {asset['name']} has no sha256 digest; refusing an unverifiable download"
            )
        return Release(
            version=payload["tag_name"],
            published=payload.get("published_at", ""),
            notes=payload.get("body", ""),
            asset_url=asset["browser_download_url"],
            sha256=digest.removeprefix("sha256:").lower(),
            size=int(asset["size"]),
        )
    raise NoAssetError(f"release {payload.get('tag_name')} has no haos_ova-*.qcow2.xz asset")


def parse_release_list(payload: list) -> list[Release]:
    """Stable releases that have a usable asset, newest first as GitHub returns them."""
    releases = []
    for entry in payload:
        if entry.get("prerelease"):
            continue
        try:
            releases.append(parse_release(entry))
        except NoAssetError:
            continue
    return releases


def _rate_limit_message(headers, clock) -> str:
    """Say when GitHub will take requests again, if it told us."""
    lead = (
        "GitHub's rate limit for your network's public address has been reached "
        "(60 requests an hour for anyone not signed in). "
    )
    try:
        reset = int((headers or {}).get("X-RateLimit-Reset") or "")
    except ValueError:
        return lead + "Try again later."
    minutes = math.ceil(max(reset - clock(), 0) / 60)
    if minutes <= 1:
        return lead + "It will accept them again in about a minute."
    return lead + f"It will accept them again in about {minutes} minutes."


def _get_json(url: str, opener=urllib.request.urlopen, clock=time.time):
    try:
        with opener(request_for(url), timeout=API_TIMEOUT) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as err:
        if err.code in (403, 429):
            raise RateLimitedError(_rate_limit_message(err.headers, clock)) from err
        raise
    except (TimeoutError, urllib.error.URLError) as err:
        if is_timeout(err):
            raise NetworkTimeout(
                f"api.github.com did not respond within {API_TIMEOUT:.0f}s. "
                "Check the server's internet connection and try again."
            ) from err
        raise


def fetch_latest(opener=urllib.request.urlopen, clock=time.time) -> Release:
    return parse_release(_get_json(f"{API}/latest", opener, clock))


def fetch_releases(
    limit: int = 10, opener=urllib.request.urlopen, clock=time.time
) -> list[Release]:
    """The newest `limit` stable releases that have a usable asset."""
    per_page = min(limit * OVER_FETCH, MAX_PER_PAGE)
    return parse_release_list(_get_json(f"{API}?per_page={per_page}", opener, clock))[:limit]
