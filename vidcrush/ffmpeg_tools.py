"""Locating and running the ffmpeg / ffprobe binaries.

Two details matter on Windows and are easy to get wrong:

* Command lines are always passed as a ``list`` (never ``shell=True``), so
  paths containing spaces or CJK characters survive untouched.
* ``-nostdin`` is always injected.  Without it ffmpeg inherits the console
  stdin and will happily block forever waiting for a ``q`` keypress when the
  parent process is not an interactive terminal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence

from .errors import FFmpegError, ToolNotFoundError

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def _resolve(explicit: str | None, name: str) -> str:
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        found = shutil.which(explicit)
        if found:
            return found
        raise ToolNotFoundError(f"{name} not found at {explicit!r}")
    found = shutil.which(name)
    if not found:
        raise ToolNotFoundError(
            f"{name} is not on PATH. Install ffmpeg and make sure {name} is runnable."
        )
    return found


def find_ffmpeg(explicit: str | None = None) -> str:
    """Return the ffmpeg executable to use."""
    return _resolve(explicit, FFMPEG)


def find_ffprobe(explicit: str | None = None) -> str:
    """Return the ffprobe executable to use."""
    return _resolve(explicit, FFPROBE)


def run(
    cmd: Sequence[str],
    *,
    capture: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a command, raising :class:`FFmpegError` on a non-zero exit."""
    proc = subprocess.run(
        list(cmd),
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and proc.returncode != 0:
        raise FFmpegError(proc.returncode, proc.stderr or "")
    return proc
