"""Objective video quality measurement (VMAF / SSIM / PSNR).

The one thing that matters here
-------------------------------

A full-reference metric compares two videos **frame by frame, matched by
timestamp**.  ``vidcrush`` changes timestamps on purpose (``setpts``), so
measuring the output against the untouched source is meaningless — ffmpeg's
framesync pairs ``out[i]`` with ``src[i // speed]`` and the score collapses.

Measured on a real 8s screen recording, CRF 28 at 2x:

    reference left alone      -> VMAF 28.85
    reference re-timed to match -> VMAF 93.03

Same encoded file, 64 points apart.  So the reference is always built by
applying the *same filter chain* to the source, in the same ffmpeg invocation:

    [0:v]<video chain>[ref];  [1:v]null[main];  [main][ref]libvmaf=...

That is exactly the alignment compensation ITU-T J.340 requires, done without
materialising a reference file.

So the reference is built by pushing the source through the *same pipeline*
(identical filter chain, same encoder frame sync at ``-crf 0``) and only then
comparing the two files.  Doing the obvious thing instead — applying the chain
in the measurement filtergraph — does **not** work: ffmpeg's CFR frame
selection under ``setpts`` is not reproducible filter-side, and the pairing
drifts, scoring the same file 66.59 instead of 93.03.

``--drop-static`` cannot be measured at all: ``mpdecimate`` throws frames away
non-deterministically, so there is no longer a frame-to-frame mapping.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .core import CrushOptions, Plan, build_plan, execute
from .errors import UsageError, VidcrushError
from .ffmpeg_tools import find_ffmpeg, run
from .probe import probe

#: ffmpeg's libvmaf filter defaults to this model when ``model=`` is omitted.
DEFAULT_VMAF_MODEL = "version=vmaf_v0.6.1"

SUPPORTED_METRICS = ("vmaf", "ssim", "psnr")

#: Substring ffmpeg prints in ``-filters`` when libvmaf is compiled in.
_VMAF_FILTER_MARKER = "libvmaf"


@dataclass(frozen=True)
class QualityReport:
    """The outcome of one measurement."""

    metric: str
    score: float
    frames: int
    model: str | None = None
    worst: float | None = None
    harmonic: float | None = None

    def as_dict(self) -> dict:
        data = {
            "metric": self.metric,
            "score": round(self.score, 4),
            "frames": self.frames,
        }
        if self.model:
            data["model"] = self.model
        if self.worst is not None:
            data["worst_frame"] = round(self.worst, 4)
        if self.harmonic is not None:
            data["harmonic_mean"] = round(self.harmonic, 4)
        return data

    def format_score(self) -> str:
        if self.metric == "psnr":
            return f"{self.score:.2f} dB"
        return f"{self.score:.2f}"


def parse_metrics_listing(text: str) -> set[str]:
    """Find which quality filters this ffmpeg build actually has."""
    found: set[str] = set()
    for line in text.splitlines():
        for token in line.split():
            if token == _VMAF_FILTER_MARKER:
                found.add("vmaf")
            elif token in ("ssim", "psnr"):
                found.add(token)
    return found


def available_metrics(ffmpeg: str | None = None) -> set[str]:
    """Return the subset of :data:`SUPPORTED_METRICS` this ffmpeg can compute."""
    exe = find_ffmpeg(ffmpeg)
    proc = run([exe, "-hide_banner", "-filters"], capture=True, check=False)
    return parse_metrics_listing((proc.stdout or "") + (proc.stderr or ""))


def require_metric(metric: str, ffmpeg: str | None = None) -> None:
    """Raise a helpful error when the requested metric is unavailable."""
    metric = metric.lower()
    if metric not in SUPPORTED_METRICS:
        raise UsageError(f"unknown metric {metric!r}; choose one of {', '.join(SUPPORTED_METRICS)}")
    if metric not in available_metrics(ffmpeg):
        hint = (
            " This ffmpeg build lacks --enable-libvmaf; use --metric ssim instead."
            if metric == "vmaf"
            else ""
        )
        raise UsageError(f"ffmpeg does not provide the {metric} filter.{hint}")


# --------------------------------------------------------------------------
# filter graph
# --------------------------------------------------------------------------


def build_compare_graph(
    *,
    metric: str,
    log_name: str,
    model: str | None = None,
    subsample: int = 1,
) -> str:
    """Graph that compares ``[0:v]`` (reference) with ``[1:v]`` (encoded output).

    Both inputs already share a timeline because the reference is produced by
    the same pipeline as the output.
    """
    parts = ["[0:v]null[ref]", "[1:v]null[main]"]

    if metric == "vmaf":
        opts = [f"log_path={log_name}", "log_fmt=json"]
        if model:
            # Single quotes keep the ':' separators inside the model string from
            # being read as filtergraph option separators.
            opts.append(f"model='{model}'")
        if subsample > 1:
            opts.append(f"n_subsample={int(subsample)}")
        parts.append(f"[main][ref]libvmaf={':'.join(opts)}[out]")
    elif metric == "ssim":
        parts.append(f"[main][ref]ssim=stats_file={log_name}[out]")
    elif metric == "psnr":
        parts.append(f"[main][ref]psnr=stats_file={log_name}[out]")
    else:  # pragma: no cover - guarded by require_metric
        raise UsageError(f"unknown metric {metric!r}")

    return ";".join(parts)


def build_reference_command(
    src: str | Path,
    dst: str | Path,
    video_chain: Sequence[str],
    *,
    ffmpeg: str,
    video_codec: str = "libx264",
    lossless_crf: int = 0,
    preset: str = "veryfast",
) -> list[str]:
    """Encode *src* through *video_chain* as a near-lossless reference.

    The point is that this is the same command shape as the real encode, so
    ffmpeg's frame sync makes the same frame choices and the two files line up.
    ``-crf 0`` is lossless for x264/x265.
    """
    chain = ",".join(video_chain) if video_chain else "null"
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-filter_complex",
        f"[0:v]{chain}[v]",
        "-map",
        "[v]",
    ]
    if video_codec in {"libx264", "libx265"}:
        cmd += ["-c:v", video_codec, "-preset", preset, "-crf", str(lossless_crf)]
    elif video_codec in {"libvpx-vp9", "libaom-av1", "libsvtav1"}:
        cmd += ["-c:v", video_codec, "-lossless", "1", "-b:v", "0"]
    else:
        cmd += ["-c:v", video_codec, "-qp", "0"]
    cmd += ["-pix_fmt", "yuv420p", "-an", "-y", str(dst)]
    return cmd


# --------------------------------------------------------------------------
# log parsing
# --------------------------------------------------------------------------

_SSIM_ALL = re.compile(r"All:\s*([0-9.]+)")
_PSNR_AVG = re.compile(r"psnr_avg:\s*([0-9.]+)")


def parse_vmaf_log(text: str, *, model: str | None = None) -> QualityReport:
    """Read libvmaf's JSON log."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VidcrushError(f"could not parse the VMAF log: {exc}") from exc

    pooled = payload.get("pooled_metrics", {}).get("vmaf")
    if not pooled:
        raise VidcrushError("the VMAF log contained no pooled vmaf metrics")
    return QualityReport(
        metric="vmaf",
        score=float(pooled["mean"]),
        frames=len(payload.get("frames") or []),
        model=model or DEFAULT_VMAF_MODEL,
        worst=float(pooled["min"]) if "min" in pooled else None,
        harmonic=float(pooled["harmonic_mean"]) if "harmonic_mean" in pooled else None,
    )


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def parse_ssim_log(text: str) -> QualityReport:
    """Average the ``All:`` column of an ffmpeg ``ssim`` stats file."""
    scores = [float(m.group(1)) for m in _SSIM_ALL.finditer(text)]
    if not scores:
        raise VidcrushError("the SSIM log contained no frames")
    return QualityReport(
        metric="ssim",
        score=_mean(scores) or 0.0,
        frames=len(scores),
        worst=min(scores),
    )


def parse_psnr_log(text: str) -> QualityReport:
    """Average the ``psnr_avg`` column of an ffmpeg ``psnr`` stats file."""
    scores = [float(m.group(1)) for m in _PSNR_AVG.finditer(text)]
    if not scores:
        raise VidcrushError("the PSNR log contained no frames")
    finite = [s for s in scores if math.isfinite(s)]
    return QualityReport(
        metric="psnr",
        score=_mean(finite) or 0.0,
        frames=len(scores),
        worst=min(finite) if finite else None,
    )


_PARSERS = {
    "vmaf": parse_vmaf_log,
    "ssim": parse_ssim_log,
    "psnr": parse_psnr_log,
}


def parse_log(metric: str, text: str, *, model: str | None = None) -> QualityReport:
    if metric == "vmaf":
        return parse_vmaf_log(text, model=model)
    return _PARSERS[metric](text)


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------


def build_measure_command(
    reference: str | Path,
    dist: str | Path,
    *,
    graph: str,
    ffmpeg: str,
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(reference),
        "-i",
        str(dist),
        "-filter_complex",
        graph,
        "-map",
        "[out]",
        "-f",
        "null",
        "-",
    ]


def measure(
    src: str | Path,
    dist: str | Path,
    video_chain: Sequence[str],
    *,
    metric: str = "vmaf",
    model: str | None = None,
    subsample: int = 1,
    video_codec: str = "libx264",
    reference: str | Path | None = None,
    ffmpeg: str | None = None,
    workdir: str | Path | None = None,
) -> QualityReport:
    """Measure *dist* against a reference rebuilt from *src*.

    *reference* lets a caller reuse a reference it already built (the CRF
    search makes one per probe clip).  Otherwise it is generated here.

    Runs ffmpeg from *workdir* with a bare log filename, because ``log_path``
    is parsed by the filtergraph parser and a Windows absolute path would be
    mangled by its backslash handling.
    """
    metric = metric.lower()
    require_metric(metric, ffmpeg)
    effective_model = model or (DEFAULT_VMAF_MODEL if metric == "vmaf" else None)

    exe = find_ffmpeg(ffmpeg)
    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        tmpdir = Path(tmp)
        ref_path = Path(reference) if reference else tmpdir / "reference.mp4"
        if reference is None:
            run(
                build_reference_command(
                    src,
                    ref_path,
                    video_chain,
                    ffmpeg=exe,
                    video_codec=video_codec,
                ),
                capture=True,
            )

        log_name = "quality.log"
        graph = build_compare_graph(
            metric=metric,
            log_name=log_name,
            model=effective_model,
            subsample=subsample,
        )
        cmd = build_measure_command(ref_path, dist, graph=graph, ffmpeg=exe)
        run(cmd, capture=True, cwd=tmpdir)

        log_path = tmpdir / log_name
        if not log_path.exists():
            raise VidcrushError("ffmpeg produced no quality log; measurement failed")
        text = log_path.read_text(encoding="utf-8", errors="replace")

    return parse_log(metric, text, model=effective_model)


# --------------------------------------------------------------------------
# sampling + CRF search
# --------------------------------------------------------------------------


def sample_windows(
    duration: float, *, count: int = 3, window: float = 10.0
) -> list[tuple[float, float]]:
    """Evenly spaced ``(start, length)`` probe windows covering the whole clip.

    Sampling only the head would be a trap for screen recordings, which are
    typically static at the start — exactly the content that compresses best.
    """
    if duration <= 0:
        return []
    if count < 1:
        raise UsageError(f"sample count must be >= 1, got {count}")
    window = max(0.5, min(float(window), duration))
    if duration <= window * count:
        return [(0.0, round(duration, 3))]

    step = duration / count
    windows: list[tuple[float, float]] = []
    for i in range(count):
        start = i * step + (step - window) / 2.0
        start = max(0.0, min(start, duration - window))
        windows.append((round(start, 3), round(window, 3)))
    return windows


def build_sample_source(
    src: str | Path,
    dst: str | Path,
    windows: Sequence[tuple[float, float]],
    *,
    ffmpeg: str | None = None,
) -> Path:
    """Concatenate *windows* of *src* into a lossless probe clip."""
    exe = find_ffmpeg(ffmpeg)
    dst = Path(dst)
    parts = [
        f"[0:v]trim=start={start}:end={start + length},setpts=PTS-STARTPTS[s{i}]"
        for i, (start, length) in enumerate(windows)
    ]
    joined = "".join(f"[s{i}]" for i in range(len(windows)))
    graph = ";".join(parts) + f";{joined}concat=n={len(windows)}:v=1:a=0[out]"

    cmd = [
        exe,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-filter_complex",
        graph,
        "-map",
        "[out]",
        "-an",
        "-c:v",
        "libx264",
        "-qp",
        "0",
        "-preset",
        "veryfast",
        "-y",
        str(dst),
    ]
    run(cmd, capture=True)
    return dst


@dataclass(frozen=True)
class Candidate:
    """One rung of the CRF ladder, measured."""

    crf: int
    score: float
    size: int

    def as_dict(self) -> dict:
        return {"crf": self.crf, "score": round(self.score, 4), "size": self.size}


def pick_candidate(candidates: Sequence[Candidate], target: float) -> Candidate:
    """Smallest encode that still meets *target*.

    Falls back to the best-scoring rung when the target is unreachable — better
    to report "this is as good as it gets" than to silently return junk.
    """
    if not candidates:
        raise UsageError("no candidates to choose from")
    passing = [c for c in candidates if c.score >= target]
    if passing:
        return min(passing, key=lambda c: c.size)
    return max(candidates, key=lambda c: c.score)


@dataclass
class SearchOutcome:
    """Everything the caller needs to report a search."""

    chosen: Candidate
    candidates: list[Candidate]
    target: float
    metric: str
    model: str | None
    hit_target: bool
    windows: list[tuple[float, float]]

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "model": self.model,
            "target": self.target,
            "chosen_crf": self.chosen.crf,
            "hit_target": self.hit_target,
            "windows": [{"start": s, "duration": d} for s, d in self.windows],
            "candidates": [c.as_dict() for c in self.candidates],
        }


DEFAULT_CRF_LADDER = (18, 22, 26, 30, 34, 38)


def search_crf(
    src: str | Path,
    options: CrushOptions,
    *,
    target: float,
    metric: str = "vmaf",
    model: str | None = None,
    ladder: Sequence[int] = DEFAULT_CRF_LADDER,
    window: float = 10.0,
    window_count: int = 3,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    progress=None,
) -> SearchOutcome:
    """Trial-encode a sampled probe clip across a CRF ladder and pick a rung.

    This is the cheap half of Netflix's *per-title encoding*: trial encodes over
    a finite candidate set, measure, then choose.  The full method also searches
    resolutions and builds a convex hull; the sampling step is what keeps this
    affordable, since one VMAF pass over a 2-minute 1080p clip costs minutes.
    """
    metric = metric.lower()
    if options.drop_static:
        raise UsageError(
            "--drop-static cannot be measured: mpdecimate discards frames "
            "non-deterministically, so there is no frame-to-frame reference"
        )
    require_metric(metric, ffmpeg)

    src_path = Path(src)
    info = probe(src_path, ffprobe=ffprobe)
    windows = sample_windows(info.duration, count=window_count, window=window)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        sample = build_sample_source(src_path, tmpdir / "probe.mkv", windows, ffmpeg=ffmpeg)
        sample_info = probe(sample, ffprobe=ffprobe)

        # One reference for every rung: it only depends on the filter chain,
        # which is identical across the ladder (only the CRF moves).
        reference = tmpdir / "reference.mp4"
        probe_plan = build_plan(
            sample,
            replace(options, force=True),
            info=sample_info,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
        run(
            build_reference_command(
                sample,
                reference,
                probe_plan.video_chain,
                ffmpeg=find_ffmpeg(ffmpeg),
                video_codec=options.video_codec,
            ),
            capture=True,
        )

        candidates: list[Candidate] = []
        for crf in sorted({int(c) for c in ladder}):
            candidate_path = tmpdir / f"cand_{crf}.mp4"
            plan = build_plan(
                sample,
                replace(options, crf=crf, output=candidate_path, force=True),
                info=sample_info,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            )
            execute(plan, quiet=True, ffprobe=ffprobe)
            report = measure(
                sample,
                candidate_path,
                plan.video_chain,
                metric=metric,
                model=model,
                video_codec=options.video_codec,
                reference=reference,
                ffmpeg=ffmpeg,
                workdir=tmpdir,
            )
            candidates.append(
                Candidate(crf=crf, score=report.score, size=candidate_path.stat().st_size)
            )
            if progress is not None:
                progress(candidates[-1], report)

    chosen = pick_candidate(candidates, target)
    return SearchOutcome(
        chosen=chosen,
        candidates=candidates,
        target=target,
        metric=metric,
        model=model or (DEFAULT_VMAF_MODEL if metric == "vmaf" else None),
        hit_target=chosen.score >= target,
        windows=list(windows),
    )


def measure_plan(
    plan: Plan,
    *,
    metric: str = "vmaf",
    model: str | None = None,
    subsample: int = 1,
    video_codec: str = "libx264",
    ffmpeg: str | None = None,
) -> QualityReport:
    """Measure an already-executed plan's output against its source."""
    return measure(
        plan.src,
        plan.dst,
        plan.video_chain,
        metric=metric,
        model=model,
        subsample=subsample,
        video_codec=video_codec,
        ffmpeg=ffmpeg,
    )


__all__ = [
    "Candidate",
    "DEFAULT_CRF_LADDER",
    "DEFAULT_VMAF_MODEL",
    "QualityReport",
    "SearchOutcome",
    "SUPPORTED_METRICS",
    "available_metrics",
    "build_compare_graph",
    "build_measure_command",
    "build_reference_command",
    "build_sample_source",
    "measure",
    "measure_plan",
    "parse_log",
    "parse_metrics_listing",
    "parse_psnr_log",
    "parse_ssim_log",
    "parse_vmaf_log",
    "pick_candidate",
    "require_metric",
    "sample_windows",
    "search_crf",
]
