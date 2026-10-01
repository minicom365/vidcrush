"""Exception types used across vidcrush."""

from __future__ import annotations


class VidcrushError(Exception):
    """Base class for every error vidcrush raises on purpose."""


class ToolNotFoundError(VidcrushError):
    """``ffmpeg`` or ``ffprobe`` could not be located."""


class ProbeError(VidcrushError):
    """ffprobe failed, or returned something we cannot interpret."""


class FFmpegError(VidcrushError):
    """ffmpeg exited with a non-zero status."""

    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        summary = (stderr or "").strip().splitlines()
        tail = "\n".join(summary[-8:]) if summary else "(no stderr captured)"
        super().__init__(f"ffmpeg failed with exit code {returncode}:\n{tail}")


class UsageError(VidcrushError):
    """Bad combination of options supplied by the caller."""
