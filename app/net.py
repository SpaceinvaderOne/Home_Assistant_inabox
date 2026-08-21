"""HTTP conventions shared by the release API client and the image downloader."""

import urllib.request

USER_AGENT = "HomeAssistant_inabox/3.0"


class NetworkTimeout(Exception):
    """A socket stalled for longer than the caller was willing to wait."""


def request_for(url: str) -> urllib.request.Request:
    """A GET carrying our User-Agent; GitHub throttles unidentified callers harder."""
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def is_timeout(err: BaseException) -> bool:
    """Did this failure come from a socket timeout?"""
    return isinstance(err, TimeoutError) or isinstance(getattr(err, "reason", None), TimeoutError)
