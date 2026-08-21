"""Run an external command and capture what it said."""

import subprocess

DEFAULT_TIMEOUT = 60.0

# Conventional "timed out" exit status, as GNU coreutils' `timeout` uses.
TIMEOUT_EXIT_CODE = 124


def run(argv: list[str], timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Return (exit code, stdout, stderr). Never raises on a non-zero exit —
    callers decide which failures matter.
    """
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return (
            TIMEOUT_EXIT_CODE,
            "",
            f"{argv[0] if argv else 'command'} timed out after {timeout:g}s and was "
            "killed. The server may be busy, or libvirt or a disk may not be "
            "responding.",
        )
    return completed.returncode, completed.stdout, completed.stderr
