"""Put the Home Assistant logo where Unraid's VM manager will find it."""

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.paths import ICONS_DIR, VM_ICON_NAME

# Ships via the Dockerfile's `COPY app/`, so it needs no separate COPY line.
ASSETS_DIR = str(Path(__file__).parent / "assets")


@dataclass(frozen=True, slots=True)
class IconOutcome:
    """`placed` is True only when this call wrote the file; `reason` explains
    the outcome either way."""

    placed: bool
    reason: str


def place_vm_icon(
    name: str = VM_ICON_NAME,
    *,
    assets_dir: str = ASSETS_DIR,
    icons_dir: str = ICONS_DIR,
) -> IconOutcome:
    """Copy `name` from the shipped assets into Unraid's VM icons folder."""
    source = Path(assets_dir) / name
    destination = Path(icons_dir) / name

    try:
        if destination.exists():
            return IconOutcome(False, f"{destination} is already there")
        if not source.is_file():
            return IconOutcome(False, f"no icon asset at {source}")
        if not destination.parent.is_dir():
            return IconOutcome(False, f"{destination.parent} is not mounted")
        shutil.copyfile(source, destination)
        return IconOutcome(True, str(destination))
    except OSError as err:
        return IconOutcome(False, f"could not place {destination}: {err}")
