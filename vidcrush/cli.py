"""Command line interface: ``vidcrush``."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__, filters as F
from .core import CrushOptions, build_plan, execute
from .errors import VidcrushError
from .probe import MediaInfo, probe

PRESETS = [
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
]

# Characters that survive a copy-paste into PowerShell / cmd / POSIX shells untouched.
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_\-./:=+@\\]+$")

SHELLS = ("auto", "powershell", "cmd", "posix")

EXAMPLES = """\
examples:
  # 2x faster, CRF 28, downscaled to 1920px wide  (the usual screen-recording job)
  vidcrush "recording.mp4" -s 2 -w 1920 --crf 28

  # squeeze a 2m21s clip down to exactly 70s
  vidcrush "recording.mp4" -d 70 -w 1920

  # drop duplicated frames from a mostly-static screen recording (mutes audio)
  vidcrush "recording.mp4" --drop-static

  # just show what the input is
  vidcrush "recording.mp4" --info

  # print the ffmpeg command without running it
  vidcrush "recording.mp4" -s 2 --dry-run
"""


def detect_shell() -> str:
    """Best guess at the shell the dry-run line will be pasted into.

    cmd.exe sets ``PROMPT``; PowerShell does not.  It is only a heuristic —
    ``--shell`` exists precisely so the guess can be overridden.
    """
    if os.name != "nt":
        return "posix"
    return "cmd" if os.environ.get("PROMPT") else "powershell"


def shell_quote(part: str, shell: str = "posix") -> str:
    """Quote one argv element so the printed command can be pasted into a shell.

    Without this the filter graph (``[0:v]setpts=...;[0:a]atempo=...``) looks
    fine on screen but explodes the moment a user copies it, because ``;``,
    ``[`` and ``,`` are all shell metacharacters.
    """
    if part and _SAFE_ARG.match(part):
        return part
    if shell == "powershell":
        # Single quotes are fully literal in PowerShell; embed one by doubling it.
        return "'" + part.replace("'", "''") + "'"
    if shell == "cmd":
        return '"' + part.replace('"', '""') + '"'
    return "'" + part.replace("'", "'\\''") + "'"


def format_command(cmd: Sequence[str], shell: str = "posix") -> str:
    """Render a command list as a line the user can actually run again."""
    parts = [shell_quote(part, shell) for part in cmd]
    if shell == "powershell" and parts and parts[0].startswith("'"):
        # PowerShell needs the call operator before a quoted path.
        parts[0] = "& " + parts[0]
    return " ".join(parts)


def format_bytes(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} TB"  # pragma: no cover - unreachable


def format_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:04.1f}"
    return f"{int(minutes)}:{sec:04.1f}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vidcrush",
        description="Speed up and shrink videos with ffmpeg.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", help="input video file")

    out = parser.add_argument_group("output")
    out.add_argument("-o", "--output", help="output path (default: <name>_<speed>x_crf<crf>.mp4)")
    out.add_argument("-y", "--force", action="store_true", help="overwrite an existing output file")

    timing = parser.add_mutually_exclusive_group()
    timing.add_argument("-s", "--speed", type=float, help="playback speed multiplier, e.g. 2 (default: 1)")
    timing.add_argument("-d", "--duration", type=float, help="target duration in seconds")

    quality = parser.add_argument_group("quality")
    quality.add_argument("--crf", type=int, default=28, help="x264/x265 CRF, lower is better (default: 28)")
    quality.add_argument("--preset", default="medium", choices=PRESETS, help="x264/x265 preset (default: medium)")
    quality.add_argument("-w", "--width", type=int, help="scale down to this width, keeping aspect ratio")
    quality.add_argument("--max-height", type=int, help="shrink to at most this height")
    quality.add_argument("--upscale", action="store_true", help="allow scaling above the source size")
    quality.add_argument("--fps", type=float, help="force an output frame rate")
    quality.add_argument("--video-codec", default="libx264", help="video encoder (default: libx264)")

    audio = parser.add_argument_group("audio")
    audio.add_argument("--audio-bitrate", default="128k", help="audio bitrate (default: 128k)")
    audio.add_argument("--audio-codec", default="aac", help="audio encoder (default: aac)")
    audio.add_argument("--no-audio", action="store_true", help="drop the audio track entirely")

    trim = parser.add_argument_group("static-frame removal")
    trim.add_argument("--drop-static", action="store_true", help="remove near-duplicate frames (mutes audio)")
    trim.add_argument("--decimate-hi", type=int, default=768, help="mpdecimate hi threshold (default: 768)")
    trim.add_argument("--decimate-lo", type=int, default=320, help="mpdecimate lo threshold (default: 320)")
    trim.add_argument("--decimate-frac", type=float, default=0.33, help="mpdecimate frac (default: 0.33)")

    misc = parser.add_argument_group("misc")
    misc.add_argument("--info", action="store_true", help="print media info and exit")
    misc.add_argument("--dry-run", action="store_true", help="print the ffmpeg command and exit")
    misc.add_argument(
        "--shell",
        choices=SHELLS,
        default="auto",
        help="shell syntax for --dry-run output (default: auto)",
    )
    misc.add_argument("--json", action="store_true", help="emit a JSON report")
    misc.add_argument("-q", "--quiet", action="store_true", help="hide ffmpeg output and progress")
    misc.add_argument("--ffmpeg", help="path to the ffmpeg executable")
    misc.add_argument("--ffprobe", help="path to the ffprobe executable")
    misc.add_argument("-V", "--version", action="version", version=f"vidcrush {__version__}")
    return parser


def _info_lines(info: MediaInfo) -> list[str]:
    rows = [
        ("path", str(info.path)),
        ("duration", format_duration(info.duration)),
        ("size", f"{format_bytes(info.size)} ({info.size_mb:.1f} MB)"),
        ("resolution", info.resolution),
        ("fps", f"{info.fps:g}"),
        ("bitrate", f"{info.bit_rate / 1000:.0f} kbps" if info.bit_rate else "?"),
        ("video", info.video_codec or "?"),
        ("audio", info.audio_codec or "none"),
    ]
    width = max(len(key) for key, _ in rows)
    return [f"{key.ljust(width)} : {value}" for key, value in rows]


def _print_plan(plan, *, quiet: bool) -> None:
    if quiet:
        return
    print(f"input     : {plan.src}")
    print(f"            {plan.info.resolution} @ {plan.info.fps:g}fps, "
          f"{format_duration(plan.info.duration)}, {format_bytes(plan.info.size)}")
    print(f"output    : {plan.dst}")
    print(f"speed     : {plan.speed:g}x  -> about {format_duration(plan.estimated_duration)}")
    print(f"filters   : v[{F.describe(plan.video_chain)}]"
          + (f" a[{F.describe(plan.audio_chain)}]" if plan.audio_chain else " a[-]"))
    for warning in plan.warnings:
        print(f"warning   : {warning}", file=sys.stderr)
    print()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Never crash on a console that cannot represent a path.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover - exotic streams
            pass

    try:
        info = probe(args.input, ffprobe=args.ffprobe)

        if args.info:
            if args.json:
                print(json.dumps(info.as_dict(), ensure_ascii=False, indent=2))
            else:
                print("\n".join(_info_lines(info)))
            return 0

        options = CrushOptions(
            speed=args.speed,
            duration=args.duration,
            crf=args.crf,
            preset=args.preset,
            width=args.width,
            max_height=args.max_height,
            upscale=args.upscale,
            fps=args.fps,
            audio_bitrate=args.audio_bitrate,
            no_audio=args.no_audio,
            drop_static=args.drop_static,
            decimate_hi=args.decimate_hi,
            decimate_lo=args.decimate_lo,
            decimate_frac=args.decimate_frac,
            video_codec=args.video_codec,
            audio_codec=args.audio_codec,
            output=Path(args.output) if args.output else None,
            force=args.force,
        )

        plan = build_plan(args.input, options, info=info, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
        _print_plan(plan, quiet=args.quiet or args.json)

        if args.dry_run:
            shell = detect_shell() if args.shell == "auto" else args.shell
            print(format_command(plan.cmd, shell))
            return 0

        if plan.dst.exists() and not args.force:
            print(f"error: {plan.dst} already exists (use -y to overwrite)", file=sys.stderr)
            return 2

        result = execute(plan, quiet=args.quiet or args.json, ffprobe=args.ffprobe)

        if args.json:
            print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
            return 0

        print(
            f"done      : {format_duration(result.output.duration)} "
            f"({result.duration_ratio * 100:.0f}% of original), "
            f"{format_bytes(result.output.size)} ({result.size_ratio * 100:.1f}% of original)"
        )
        print(f"            {plan.dst}")
        print(f"            took {result.elapsed:.1f}s")
        return 0

    except VidcrushError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
