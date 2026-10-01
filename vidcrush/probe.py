"""Read media metadata through ffprobe."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .errors import ProbeError
from .ffmpeg_tools import find_ffprobe, run


def parse_fraction(value: str | None) -> float:
    """``"30000/1001"`` -> ``29.970...``; ``"0/0"`` -> ``0.0``."""
    if not value:
        return 0.0
    try:
        frac = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return 0.0
    if frac.denominator == 0:
        return 0.0
    return float(frac)


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MediaInfo:
    """A flattened, JSON-friendly view of one input file."""

    path: Path
    duration: float
    size: int
    bit_rate: int
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str | None
    nb_frames: int | None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    @property
    def has_video(self) -> bool:
        return bool(self.video_codec)

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width and self.height else "?"

    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024)

    def as_dict(self) -> dict:
        data = {
            "path": str(self.path),
            "duration": round(self.duration, 3),
            "size": self.size,
            "size_mb": round(self.size_mb, 3),
            "bit_rate": self.bit_rate,
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
        }
        return data

    @classmethod
    def from_ffprobe(cls, payload: dict, path: str | os.PathLike) -> "MediaInfo":
        streams = payload.get("streams") or []
        fmt = payload.get("format") or {}

        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if video is None:
            raise ProbeError(f"no video stream found in {path}")

        duration = _to_float(fmt.get("duration"))
        if duration <= 0:
            duration = _to_float(video.get("duration"))

        return cls(
            path=Path(path),
            duration=duration,
            size=_to_int(fmt.get("size")),
            bit_rate=_to_int(fmt.get("bit_rate")),
            width=_to_int(video.get("width")),
            height=_to_int(video.get("height")),
            fps=parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate")),
            video_codec=str(video.get("codec_name") or ""),
            audio_codec=str(audio.get("codec_name")) if audio else None,
            nb_frames=_to_int(video.get("nb_frames")) or None,
        )


def probe(path: str | os.PathLike, *, ffprobe: str | None = None, capture: bool = True) -> MediaInfo:
    """Inspect *path* and return a :class:`MediaInfo`."""
    src = Path(path)
    if not src.exists():
        raise ProbeError(f"input file does not exist: {src}")

    exe = find_ffprobe(ffprobe)
    cmd = [
        exe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(src),
    ]
    proc = run(cmd, capture=capture)
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise ProbeError(f"ffprobe returned invalid JSON for {src}: {exc}") from exc
    return MediaInfo.from_ffprobe(payload, src)
