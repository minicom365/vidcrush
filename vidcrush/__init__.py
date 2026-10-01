"""vidcrush — speed up and shrink videos with ffmpeg.

The package is intentionally tiny and dependency-free: everything it does is
driven by ``ffmpeg`` / ``ffprobe``, and all the interesting decisions live in
pure functions so they can be unit-tested without touching a real video file.
"""

from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["__version__"]
