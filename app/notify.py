"""Get a Home Assistant restart, a stuck-unreachable VM, or a
giving-up escalation in front of the person running this server, without
depending on this container's own web UI being open.
"""

import logging
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal

Importance = Literal["normal", "warning", "alert"]

NOTIFY_DIR = "/tmp/notifications/unread"

EVENT_NAME = "Home Assistant in a box"

NOTIFY_SCRIPT = "/usr/local/emhttp/webGui/scripts/notify"

_PHP_SHORT_OPEN_TAG_FLAG = ("-d", "short_open_tag=On")

NOTIFY_SCRIPT_TIMEOUT = 10.0

_LOG = logging.getLogger(__name__)


def _escaped(value: str) -> str:
    """Keep `value` on the one line this format allows it."""
    return value.replace('"', "'").replace("\n", " ").replace("\r", " ")


def _script_ready(script: str | Path) -> bool:
    """Whether Unraid's own notify script can actually be run right now."""
    try:
        path = Path(script)
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _run_script(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """The default `runner`: plain `subprocess.run`, argument list only --
    see the module docstring's own hazard example (a VM name the user chose
    can contain anything, including `foo; rm -rf /`) for why this is never
    built as a shell string and never runs with `shell=True`. Text output,
    not bytes, so PHP's own stderr can be logged on a non-zero exit.
    """
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    return completed.returncode, completed.stdout, completed.stderr


def _via_script(
    *,
    script: str | Path,
    event: str,
    subject: str,
    description: str,
    importance: Importance,
    runner: Callable[[list[str], float], tuple[int, str, str]],
) -> bool:
    """Attempt Unraid's own notify script for one notification."""
    argv = [
        "php",
        *_PHP_SHORT_OPEN_TAG_FLAG,
        str(script),
        "-e",
        event,
        "-s",
        subject,
        "-d",
        description,
        "-i",
        importance,
    ]
    try:
        code, _out, err = runner(argv, NOTIFY_SCRIPT_TIMEOUT)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        _LOG.warning("notify: Unraid's notify script could not be run (%r): %s", subject, exc)
        return False
    if code != 0:
        detail = err.strip() or f"exit code {code}"
        _LOG.warning("notify: Unraid's notify script exited non-zero (%r): %s", subject, detail)
        return False
    return True


def notify(
    subject: str,
    description: str,
    importance: Importance,
    *,
    link: str = "",
    event: str = EVENT_NAME,
    directory: str | Path = NOTIFY_DIR,
    clock=time.time,
    script: str | Path = NOTIFY_SCRIPT,
    runner: Callable[[list[str], float], tuple[int, str, str]] = _run_script,
) -> bool:
    """Deliver one notification -- Unraid's own notify script if it is
    reachable and accepts it, otherwise a `.notify` file dropped into
    Unraid's unread-notifications folder for the watcher to pick up.
    """
    if _script_ready(script) and _via_script(
        script=script,
        event=event,
        subject=subject,
        description=description,
        importance=importance,
        runner=runner,
    ):
        return True

    try:
        ts = int(clock())
        body = (
            f"timestamp={ts}\n"
            f'event="{_escaped(event)}"\n'
            f'subject="{_escaped(subject)}"\n'
            f'description="{_escaped(description)}"\n'
            f'importance="{importance}"\n'
            f'link="{_escaped(link)}"\n'
        )
        target_dir = Path(directory)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"home-assistant-inabox-{ts}-{uuid.uuid4().hex}.notify"
        path.write_text(body)
        return True
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        _LOG.warning("notify: could not write a notification (%r): %s", subject, exc)
        return False
