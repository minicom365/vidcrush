"""Turn a set of options into a single ffmpeg invocation, and run it."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import filters as F
from .errors import UsageError
from .ffmpeg_tools import find_ffmpeg, run
from .probe import MediaInfo, probe

FASTSTART_SUFFIXES = {".mp4", ".m4v", ".mov"}


@dataclass
class CrushOptions:
    """Everything the caller can influence."""

    speed: float | None = None
    duration: float | None = None
    crf: int = 28
    preset: str = "medium"
    width: int | None = None
    max_height: int | None = None
    upscale: bool = False
    fps: float | None = None
    audio_bitrate: str = "128k"
    no_audio: bool = False
    drop_static: bool = False
    decimate_hi: int = 768
    decimate_lo: int = 320
    decimate_frac: float = 0.33
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    output: Path | None = None
    force: bool = False
    loglevel: str = "warning"


@dataclass
class Plan:
    """A fully resolved, ready-to-run job."""

    src: Path
    dst: Path
    info: MediaInfo
    speed: float
    video_chain: list[str]
    audio_chain: list[str]
    cmd: list[str]
    audio_dropped: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def estimated_duration(self) -> float:
        """Expected output length (ignores what ``--drop-static`` removes)."""
        if self.speed <= 0:
            return 0.0
        return self.info.duration / self.speed


@dataclass
class Result:
    """What actually happened."""

    plan: Plan
    elapsed: float
    output: MediaInfo

    @property
    def size_ratio(self) -> float:
        before = self.plan.info.size
        return self.output.size / before if before else 0.0

    @property
    def duration_ratio(self) -> float:
        before = self.plan.info.duration
        return self.output.duration / before if before else 0.0

    def as_dict(self) -> dict:
        return {
            "input": self.plan.info.as_dict(),
            "output": self.output.as_dict(),
            "speed": self.plan.speed,
            "elapsed": round(self.elapsed, 3),
            "size_ratio": round(self.size_ratio, 4),
            "duration_ratio": round(self.duration_ratio, 4),
            "filter_complex": self.plan.cmd[self.plan.cmd.index("-filter_complex") + 1],
        }


def default_output(src: Path, *, speed: float, crf: int, drop_static: bool) -> Path:
    """``clip.mp4`` -> ``clip_2x_crf28.mp4``."""
    tags: list[str] = []
    if abs(speed - 1.0) > 1e-9:
        tags.append(f"{speed:g}x")
    if drop_static:
        tags.append("nodup")
    tags.append(f"crf{crf}")
    suffix = src.suffix or ".mp4"
    return src.with_name(f"{src.stem}_{'_'.join(tags)}{suffix}")


def resolve_speed(options: CrushOptions, info: MediaInfo) -> float:
    """Validate ``--speed`` / ``--duration`` and settle on one multiplier."""
    if options.speed is not None and options.duration is not None:
        raise UsageError("--speed and --duration are mutually exclusive")
    if options.duration is not None:
        return F.speed_for_duration(info.duration, options.duration)
    if options.speed is not None:
        return F.normalize_speed(options.speed)
    return 1.0


def build_plan(
    src: str | Path,
    options: CrushOptions | None = None,
    *,
    info: MediaInfo | None = None,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> Plan:
    """Resolve options into an executable plan (no side effects)."""
    options = options or CrushOptions()
    src_path = Path(src)
    info = info or probe(src_path, ffprobe=ffprobe)

    speed = resolve_speed(options, info)

    warnings: list[str] = []
    audio_dropped = False
    keep_audio = info.has_audio and not options.no_audio

    if options.drop_static and keep_audio:
        # Re-timing the video stream would slide the audio out of sync, and
        # there is no reliable way to re-time it after mpdecimate.
        keep_audio = False
        audio_dropped = True
        warnings.append("--drop-static re-times the video, so audio was removed")

    if options.no_audio and info.has_audio:
        warnings.append("audio removed by request")

    video_chain = F.video_filters(
        speed=speed,
        width=options.width,
        max_height=options.max_height,
        fps=options.fps,
        drop_static=options.drop_static,
        decimate_hi=options.decimate_hi,
        decimate_lo=options.decimate_lo,
        decimate_frac=options.decimate_frac,
        allow_upscale=options.upscale,
    )
    audio_chain = F.audio_filters(speed) if keep_audio else []
    if keep_audio and not audio_chain:
        # No tempo change means there is nothing to re-time, but the audio still
        # has to be mapped explicitly — `-map [v]` turns off ffmpeg's automatic
        # stream selection, so leaving this empty silently drops the track.
        audio_chain = ["anull"]

    dst_path = (
        Path(options.output)
        if options.output
        else default_output(src_path, speed=speed, crf=options.crf, drop_static=options.drop_static)
    )
    if dst_path.resolve() == src_path.resolve():
        raise UsageError("output path would overwrite the input; use -o to pick another name")

    graph = F.filter_complex(video_chain, audio_chain)

    cmd = [
        find_ffmpeg(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        options.loglevel,
        "-i",
        str(src_path),
        "-filter_complex",
        graph,
        "-map",
        "[v]",
    ]
    if audio_chain:
        cmd += ["-map", "[a]"]

    cmd += ["-c:v", options.video_codec]
    if options.video_codec in {"libx264", "libx265"}:
        cmd += ["-preset", options.preset, "-crf", str(options.crf)]
    elif options.video_codec in {"libvpx-vp9", "libaom-av1", "libsvtav1"}:
        cmd += ["-crf", str(options.crf), "-b:v", "0"]
    cmd += ["-pix_fmt", "yuv420p"]

    if audio_chain:
        cmd += ["-c:a", options.audio_codec, "-b:a", options.audio_bitrate]
    else:
        cmd += ["-an"]

    if dst_path.suffix.lower() in FASTSTART_SUFFIXES:
        cmd += ["-movflags", "+faststart"]

    cmd += ["-y" if options.force else "-n", str(dst_path)]

    return Plan(
        src=src_path,
        dst=dst_path,
        info=info,
        speed=speed,
        video_chain=video_chain,
        audio_chain=audio_chain,
        cmd=cmd,
        audio_dropped=audio_dropped,
        warnings=warnings,
    )


def execute(plan: Plan, *, quiet: bool = False, ffprobe: str | None = None) -> Result:
    """Run the plan and measure the outcome."""
    if not plan.dst.parent.exists():
        plan.dst.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    run(plan.cmd, capture=quiet)
    elapsed = time.perf_counter() - started

    output = probe(plan.dst, ffprobe=ffprobe)
    return Result(plan=plan, elapsed=elapsed, output=output)


def crush(
    src: str | Path,
    options: CrushOptions | None = None,
    *,
    quiet: bool = False,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> Result:
    """Plan + execute in one call (the convenience entry point)."""
    plan = build_plan(src, options, ffmpeg=ffmpeg, ffprobe=ffprobe)
    return execute(plan, quiet=quiet, ffprobe=ffprobe)
