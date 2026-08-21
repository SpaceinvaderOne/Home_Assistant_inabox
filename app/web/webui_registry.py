"""The webui URLs this project has written, per VM."""

from app.web import state_store

_LAST_KNOWN: dict[str, str] = {}


def remember(vm_name: str, url: str) -> None:
    """Record `url` as a value this project wrote for `vm_name`, in memory and,
    when persistence is enabled, on disk."""
    _LAST_KNOWN[vm_name] = url
    state_store.record_webui(vm_name, url)


def known_for(vm_name: str) -> str | None:
    """The last value recorded for `vm_name`, or None if there is none."""
    return _LAST_KNOWN.get(vm_name)


def reset() -> None:
    """Test isolation only."""
    _LAST_KNOWN.clear()
