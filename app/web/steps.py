"""Step order and navigation."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Step:
    key: str
    label: str
    template: str


STEPS = (
    Step("welcome", "Welcome", "step_welcome.html"),
    Step("version", "Version", "step_version.html"),
    Step("placement", "Storage & network", "step_placement.html"),
    Step("resources", "CPU & memory", "step_resources.html"),
    Step("usb", "USB devices", "step_usb.html"),
    Step("review", "Review", "step_review.html"),
    Step("installing", "Install", "step_installing.html"),
    Step("firstboot", "First boot", "step_firstboot.html"),
    Step("ready", "Ready", "step_ready.html"),
)

STATUS_STEP = Step("status", "Status", "status.html")

_ORDER = [s.key for s in STEPS]


def step(key: str) -> Step:
    return next(s for s in STEPS if s.key == key)


def next_step(key: str) -> str | None:
    i = _ORDER.index(key)
    return _ORDER[i + 1] if i + 1 < len(_ORDER) else None


def previous_step(key: str) -> str | None:
    i = _ORDER.index(key)
    return _ORDER[i - 1] if i > 0 else None
