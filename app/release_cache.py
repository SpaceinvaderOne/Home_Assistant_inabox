"""The Home Assistant OS release list, kept so GitHub is asked as rarely as possible.

A published release never changes, checksum included, so a list saved earlier is
always safe to install from. It is used when GitHub refuses or does not answer.
"""

import contextlib
import dataclasses
import json
import logging
import os
import tempfile
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.installer import CONFIG_PATH
from app.net import NetworkTimeout
from app.releases import RateLimitedError, Release, fetch_releases

_LOG = logging.getLogger(__name__)

CACHE_PATH = Path(CONFIG_PATH) / "releases.json"

# How long a list just fetched is reused before GitHub is asked again.
MEMORY_SECONDS = 600

# Failures that mean "GitHub could not be asked", as opposed to a bad answer.
UNREACHABLE = (RateLimitedError, NetworkTimeout, urllib.error.URLError)


@dataclass(frozen=True, slots=True)
class ReleaseList:
    releases: tuple[Release, ...]
    fetched_at: float
    # Why this came from the saved list rather than GitHub, or None if fresh.
    stale_reason: str | None = None


def _describe(err: BaseException) -> str:
    """Why GitHub could not be asked, in words rather than Python's own text."""
    if isinstance(err, urllib.error.URLError) and not isinstance(err, urllib.error.HTTPError):
        reason = err.reason
        detail = reason.strerror if isinstance(reason, OSError) and reason.strerror else reason
        return f"GitHub couldn't be reached ({detail})."
    return str(err)


class ReleaseSource:
    def __init__(
        self,
        fetch: Callable[[], list[Release]] = fetch_releases,
        path: Path = CACHE_PATH,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._fetch = fetch
        self._path = Path(path)
        self._clock = clock
        self._memory: ReleaseList | None = None

    def current(self) -> ReleaseList:
        """The release list, from GitHub unless it was fetched moments ago, or
        from the saved list when GitHub cannot be asked."""
        memory = self._memory
        if memory is not None and self._clock() - memory.fetched_at < MEMORY_SECONDS:
            return memory
        try:
            fetched = ReleaseList(tuple(self._fetch()), self._clock())
        except UNREACHABLE as err:
            saved = memory or self._load()
            if saved is None:
                raise
            return dataclasses.replace(saved, stale_reason=_describe(err))
        self._memory = fetched
        self._save(fetched)
        return fetched

    def find(self, version: str) -> Release | None:
        """The listed release for `version`, from what was last fetched or saved."""
        listed = self._memory or self._load()
        if listed is None:
            return None
        return next((r for r in listed.releases if r.version == version), None)

    def _load(self) -> ReleaseList | None:
        try:
            data = json.loads(self._path.read_text())
            return ReleaseList(
                releases=tuple(Release(**entry) for entry in data["releases"]),
                fetched_at=float(data["fetched_at"]),
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError) as err:
            _LOG.warning("ignoring the saved release list at %s: %s", self._path, err)
            return None

    def _save(self, listed: ReleaseList) -> None:
        payload = {
            "fetched_at": listed.fetched_at,
            "releases": [dataclasses.asdict(r) for r in listed.releases],
        }
        tmp_name = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp_name, self._path)
            tmp_name = None
        except OSError as err:
            _LOG.warning("could not save the release list to %s: %s", self._path, err)
        finally:
            if tmp_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
