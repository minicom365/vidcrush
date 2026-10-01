"""Pure functions that translate user intent into ffmpeg filter strings.

Nothing in here touches the filesystem or starts a process, which makes the
whole module cheap to unit-test.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .errors import UsageError

#: ffmpeg's ``atempo`` accepts one factor per instance within this range.
ATEMPO_MIN = 0.5
ATEMPO_MAX = 2.0

_EPS = 1e-9


def even(value: int) -> int:
    """Round *down* to the nearest even integer.

    ``yuv420p`` needs even width and height; ffmpeg's ``-2`` in a scale
    expression does this for us, but we still need it when a user asks for an
    explicit height.
    """
    value = int(value)
    if value < 2:
        raise UsageError(f"dimension must be >= 2, got {value}")
    return value - (value % 2)


def atempo_factors(speed: float) -> tuple[float, ...]:
    """Split a speed factor into a chain of ``atempo`` values.

    A single ``atempo`` instance only handles ``0.5 .. 2.0``; anything more
    extreme has to be expressed as ``atempo=2.0,atempo=2.0`` (and so on).
    """
    if speed <= 0:
        raise UsageError(f"speed must be > 0, got {speed}")

    remaining = float(speed)
    factors: list[float] = []
    while remaining > ATEMPO_MAX + _EPS:
        factors.append(ATEMPO_MAX)
        remaining /= ATEMPO_MAX
    while remaining < ATEMPO_MIN - _EPS:
        factors.append(ATEMPO_MIN)
        remaining /= ATEMPO_MIN

    last = min(ATEMPO_MAX, max(ATEMPO_MIN, remaining))
    factors.append(round(last, 6))
    return tuple(factors)


def normalize_speed(speed: float) -> float:
    """Validate a speed multiplier and snap chained-atempo noise to 1.0."""
    if speed <= 0:
        raise UsageError(f"speed must be > 0, got {speed}")
    if speed > 100:
        raise UsageError(f"speed {speed} is unreasonable (max 100)")
    return 1.0 if abs(speed - 1.0) < 1e-6 else float(speed)


def speed_for_duration(source_duration: float, target_duration: float) -> float:
    """Speed multiplier that turns *source_duration* into *target_duration*."""
    if source_duration <= 0:
        raise UsageError(f"source duration must be > 0, got {source_duration}")
    if target_duration <= 0:
        raise UsageError(f"target duration must be > 0, got {target_duration}")
    return normalize_speed(source_duration / target_duration)


def _even_expr(expr: str) -> str:
    """Wrap an ffmpeg expression so its result is even (yuv420p needs that)."""
    return f"trunc({expr}/2)*2"


def scale_filter(
    width: int | None, max_height: int | None, allow_upscale: bool = False
) -> str | None:
    """Build the ``scale`` filter, or ``None`` when no resize was requested.

    Sizes are treated as *upper bounds*: a 320x240 clip asked to become 1920px
    wide stays 320x240.  Upscaling a video to make it "smaller" is always a
    mistake, so ``--upscale`` has to be asked for explicitly.

    ``-2`` keeps the aspect ratio and guarantees an even result.
    """
    if width is not None:
        target = even(width)
        if allow_upscale:
            return f"scale={target}:-2:flags=lanczos"
        return f"scale='{_even_expr(f'min(iw,{target})')}':-2:flags=lanczos"
    if max_height is not None:
        target = even(max_height)
        if allow_upscale:
            return f"scale=-2:{target}:flags=lanczos"
        return f"scale=-2:'{_even_expr(f'min(ih,{target})')}':flags=lanczos"
    return None


def video_filters(
    *,
    speed: float = 1.0,
    width: int | None = None,
    max_height: int | None = None,
    fps: float | None = None,
    drop_static: bool = False,
    decimate_hi: int = 768,
    decimate_lo: int = 320,
    decimate_frac: float = 0.33,
    allow_upscale: bool = False,
) -> list[str]:
    """Assemble the video filter chain in the order that actually works.

    Order matters:

    1. ``mpdecimate`` must see the *original* timeline, so it goes first.
    2. ``setpts=N/FRAME_RATE/TB`` re-times the surviving frames back-to-back.
       This is what actually shortens a static screen recording.
    3. The playback-speed ``setpts`` then scales whatever is left.
    4. ``fps`` / ``scale`` normalise the result for encoding.
    """
    chain: list[str] = []

    if drop_static:
        if not 0.0 < decimate_frac <= 1.0:
            raise UsageError(f"decimate frac must be in (0, 1], got {decimate_frac}")
        if decimate_lo > decimate_hi:
            raise UsageError("decimate_lo must be <= decimate_hi")
        chain.append(
            f"mpdecimate=hi={int(decimate_hi)}:lo={int(decimate_lo)}:frac={float(decimate_frac):g}"
        )
        chain.append("setpts=N/FRAME_RATE/TB")

    speed = normalize_speed(speed)
    if speed != 1.0:
        chain.append(f"setpts={1.0 / speed:.6f}*PTS")

    if fps is not None:
        if fps <= 0:
            raise UsageError(f"fps must be > 0, got {fps}")
        chain.append(f"fps={fps:g}")

    scale = scale_filter(width, max_height, allow_upscale=allow_upscale)
    if scale is not None:
        chain.append(scale)
        chain.append("setsar=1")

    return chain


def audio_filters(speed: float) -> list[str]:
    """``atempo`` chain that keeps the pitch intact while changing tempo."""
    speed = normalize_speed(speed)
    if speed == 1.0:
        return []
    return [f"atempo={factor:g}" for factor in atempo_factors(speed)]


def filter_complex(video_chain: Sequence[str], audio_chain: Sequence[str]) -> str:
    """Wrap the chains into a labelled ``-filter_complex`` graph.

    An empty chain becomes ``null`` so the labels are always defined, which
    keeps the ``-map "[v]"`` arguments valid no matter what the caller asked
    for.
    """
    v = ",".join(video_chain) if video_chain else "null"
    parts = [f"[0:v]{v}[v]"]
    if audio_chain:
        parts.append(f"[0:a]{','.join(audio_chain)}[a]")
    return ";".join(parts)


def describe(chain: Iterable[str]) -> str:
    """Short human-readable summary of a filter chain (used in logs)."""
    names = []
    for item in chain:
        names.append(item.split("=", 1)[0])
    return ",".join(names) if names else "-"
