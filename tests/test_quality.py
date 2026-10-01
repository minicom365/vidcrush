"""Tests for objective quality measurement.

The pure parts (parsing, graph building, ladder choice) run everywhere.  The
integration part is the important one: it pins down the alignment rule that
makes the difference between a meaningful score and garbage.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vidcrush import quality as Q

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

VMAF_JSON = json.dumps(
    {
        "version": "2.3.1",
        "frames": [
            {"frameNum": 0, "metrics": {"vmaf": 90.0}},
            {"frameNum": 1, "metrics": {"vmaf": 80.0}},
        ],
        "pooled_metrics": {"vmaf": {"min": 80.0, "max": 90.0, "mean": 85.0, "harmonic_mean": 84.7}},
    }
)


class TestParseMetricsListing:
    def test_finds_all_three(self):
        listing = " ... libvmaf           VV->V      Calculate the VMAF ...\n"
        listing += " TS. psnr             VV->V      Calculate the PSNR ...\n"
        listing += " TS. ssim             VV->V      Calculate the SSIM ...\n"
        assert Q.parse_metrics_listing(listing) == {"vmaf", "psnr", "ssim"}

    def test_missing_vmaf(self):
        assert Q.parse_metrics_listing(" TS. psnr VV->V\n") == {"psnr"}

    def test_empty(self):
        assert Q.parse_metrics_listing("") == set()


class TestParseVmafLog:
    def test_reads_pooled_metrics(self):
        report = Q.parse_vmaf_log(VMAF_JSON)
        assert report.metric == "vmaf"
        assert report.score == 85.0
        assert report.frames == 2
        assert report.worst == 80.0
        assert report.harmonic == 84.7
        assert report.model == Q.DEFAULT_VMAF_MODEL

    def test_records_an_explicit_model(self):
        report = Q.parse_vmaf_log(VMAF_JSON, model="version=vmaf_v1.0.16")
        assert report.model == "version=vmaf_v1.0.16"

    def test_rejects_bad_json(self):
        with pytest.raises(Q.VidcrushError):
            Q.parse_vmaf_log("not json")

    def test_rejects_a_log_without_metrics(self):
        with pytest.raises(Q.VidcrushError):
            Q.parse_vmaf_log(json.dumps({"frames": []}))


class TestParseSsimLog:
    def test_averages_the_all_column(self):
        log = (
            "n:1 Y:0.99 U:0.99 V:0.99 All:1.000000 (inf)\n"
            "n:2 Y:0.97 U:0.99 V:0.99 All:0.980000 (17.5)\n"
        )
        report = Q.parse_ssim_log(log)
        assert report.score == pytest.approx(0.99)
        assert report.frames == 2
        assert report.worst == pytest.approx(0.98)
        assert report.model is None

    def test_rejects_an_empty_log(self):
        with pytest.raises(Q.VidcrushError):
            Q.parse_ssim_log("")


class TestParsePsnrLog:
    def test_averages_psnr_avg(self):
        log = "n:1 mse_avg:1.0 psnr_avg:48.13 psnr_y:48.0\nn:2 mse_avg:2.0 psnr_avg:45.13 psnr_y:45.0\n"
        report = Q.parse_psnr_log(log)
        assert report.score == pytest.approx(46.63)
        assert report.frames == 2

    def test_skips_infinite_psnr_frames(self):
        log = "n:1 psnr_avg:inf\nn:2 psnr_avg:50.0\n"
        report = Q.parse_psnr_log(log)
        assert report.score == pytest.approx(50.0)
        assert report.worst == pytest.approx(50.0)


class TestReportFormatting:
    def test_vmaf_and_ssim_are_bare_numbers(self):
        assert Q.QualityReport("vmaf", 93.03, 10).format_score() == "93.03"
        assert Q.QualityReport("ssim", 0.9773, 10).format_score() == "0.98"

    def test_psnr_carries_units(self):
        assert Q.QualityReport("psnr", 41.5, 10).format_score() == "41.50 dB"

    def test_as_dict_only_includes_what_is_known(self):
        assert Q.QualityReport("ssim", 0.9, 3).as_dict() == {
            "metric": "ssim",
            "score": 0.9,
            "frames": 3,
        }


# ---------------------------------------------------------------------------
# command / graph building
# ---------------------------------------------------------------------------


class TestBuildCompareGraph:
    def test_vmaf_defaults_to_a_json_log(self):
        graph = Q.build_compare_graph(metric="vmaf", log_name="q.json")
        assert (
            graph
            == "[0:v]null[ref];[1:v]null[main];[main][ref]libvmaf=log_path=q.json:log_fmt=json[out]"
        )

    def test_vmaf_main_input_is_the_distorted_one(self):
        """libvmaf takes #0 = main (distorted) and #1 = reference."""
        graph = Q.build_compare_graph(metric="vmaf", log_name="q.json")
        assert "[main][ref]libvmaf" in graph

    def test_model_is_single_quoted(self):
        graph = Q.build_compare_graph(metric="vmaf", log_name="q.json", model="version=vmaf_v0.6.1")
        assert "model='version=vmaf_v0.6.1'" in graph

    def test_subsample_only_when_asked(self):
        assert "n_subsample" not in Q.build_compare_graph(metric="vmaf", log_name="q.json")
        assert "n_subsample=3" in Q.build_compare_graph(
            metric="vmaf", log_name="q.json", subsample=3
        )

    def test_ssim_and_psnr_use_stats_files(self):
        assert "ssim=stats_file=q.log" in Q.build_compare_graph(metric="ssim", log_name="q.log")
        assert "psnr=stats_file=q.log" in Q.build_compare_graph(metric="psnr", log_name="q.log")

    def test_the_reference_chain_is_not_applied_here(self):
        """Regression guard: rebuilding the chain inside the graph misaligns.

        Applying ``setpts`` filter-side does not reproduce ffmpeg's encoder
        frame selection, so the pairing drifts and a lossless encode scores
        ~66 instead of ~100.  The reference is materialised instead.
        """
        graph = Q.build_compare_graph(metric="vmaf", log_name="q.json")
        assert "setpts" not in graph
        assert "mpdecimate" not in graph


class TestBuildReferenceCommand:
    def test_applies_the_chain_and_encodes_losslessly(self):
        cmd = Q.build_reference_command(
            "in.mp4", "ref.mp4", ["setpts=0.500000*PTS"], ffmpeg="ffmpeg"
        )
        assert cmd[cmd.index("-filter_complex") + 1] == "[0:v]setpts=0.500000*PTS[v]"
        assert cmd[cmd.index("-crf") + 1] == "0"
        assert cmd[cmd.index("-c:v") + 1] == "libx264"

    def test_empty_chain_becomes_null(self):
        cmd = Q.build_reference_command("in.mp4", "ref.mp4", [], ffmpeg="ffmpeg")
        assert cmd[cmd.index("-filter_complex") + 1] == "[0:v]null[v]"

    def test_audio_is_dropped(self):
        cmd = Q.build_reference_command("in.mp4", "ref.mp4", [], ffmpeg="ffmpeg")
        assert "-an" in cmd


class TestBuildMeasureCommand:
    def test_reference_is_input_zero_and_distorted_is_input_one(self):
        cmd = Q.build_measure_command("ref.mp4", "out.mp4", graph="G", ffmpeg="ffmpeg")
        assert cmd[cmd.index("-i") + 1] == "ref.mp4"
        assert cmd[cmd.index("-i", cmd.index("-i") + 1) + 1] == "out.mp4"

    def test_output_goes_nowhere(self):
        cmd = Q.build_measure_command("ref.mp4", "out.mp4", graph="G", ffmpeg="ffmpeg")
        assert cmd[-3:] == ["-f", "null", "-"]


# ---------------------------------------------------------------------------
# sampling + ladder choice
# ---------------------------------------------------------------------------


class TestSampleWindows:
    def test_short_clip_is_used_whole(self):
        assert Q.sample_windows(8.0, count=3, window=10.0) == [(0.0, 8.0)]

    def test_long_clip_gets_evenly_spread_windows(self):
        # Centred inside each third: step 100, window 10 -> offsets 45/145/245.
        windows = Q.sample_windows(300.0, count=3, window=10.0)
        assert len(windows) == 3
        assert windows[0][0] == pytest.approx(45.0)
        assert windows[1][0] == pytest.approx(145.0)
        assert windows[2][0] == pytest.approx(245.0)
        assert all(length == 10.0 for _, length in windows)

    def test_windows_never_run_off_the_end(self):
        for start, length in Q.sample_windows(100.0, count=4, window=30.0):
            assert start + length <= 100.0 + 1e-9

    def test_rejects_a_zero_count(self):
        with pytest.raises(Q.UsageError):
            Q.sample_windows(100.0, count=0)

    def test_zero_duration_gives_nothing(self):
        assert Q.sample_windows(0.0) == []


class TestPickCandidate:
    LADDER = [
        Q.Candidate(crf=18, score=99.0, size=900),
        Q.Candidate(crf=26, score=95.0, size=400),
        Q.Candidate(crf=34, score=88.0, size=150),
    ]

    def test_picks_the_smallest_rung_that_meets_the_target(self):
        assert Q.pick_candidate(self.LADDER, 93.0).crf == 26

    def test_picks_the_best_when_nothing_meets_the_target(self):
        assert Q.pick_candidate(self.LADDER, 99.9).crf == 18

    def test_picks_the_cheapest_when_everything_passes(self):
        assert Q.pick_candidate(self.LADDER, 10.0).crf == 34

    def test_empty_ladder_is_an_error(self):
        with pytest.raises(Q.UsageError):
            Q.pick_candidate([], 90.0)


# ---------------------------------------------------------------------------
# integration - the alignment rule
# ---------------------------------------------------------------------------

pytestmark_int = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="ffmpeg/ffprobe are not installed"
)


@pytest.fixture(scope="module")
def patterned_clip(tmp_path_factory) -> Path:
    """High inter-frame difference, so a one-frame slip is obvious."""
    path = tmp_path_factory.mktemp("quality") / "pattern.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "12",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _encode(path: Path, chain: list[str], qp: int, tmp: Path) -> Path:
    out = tmp / f"enc_{qp}.mp4"
    subprocess.run(
        Q.build_reference_command(path, out, chain, ffmpeg=FFMPEG, lossless_crf=qp),
        check=True,
        capture_output=True,
    )
    return out


@pytestmark_int
class TestAlignmentIntegration:
    CHAIN = ["setpts=0.500000*PTS"]

    def test_lossless_through_the_same_chain_scores_near_perfect(self, patterned_clip, tmp_path):
        dist = _encode(patterned_clip, self.CHAIN, 0, tmp_path)
        report = Q.measure(patterned_clip, dist, self.CHAIN, metric="vmaf", ffmpeg=FFMPEG)
        assert report.score > 99.0, report
        assert report.frames > 0

    def test_a_real_crf_encode_lands_in_a_sane_range(self, patterned_clip, tmp_path):
        dist = _encode(patterned_clip, self.CHAIN, 28, tmp_path)
        report = Q.measure(patterned_clip, dist, self.CHAIN, metric="vmaf", ffmpeg=FFMPEG)
        assert 70.0 < report.score < 99.0, report

    def test_the_wrong_chain_scores_much_worse(self, patterned_clip, tmp_path):
        """Teeth: if the reference loses its re-timing the score collapses.

        This is what a naïve single-pass implementation reports, and it is why
        the reference is materialised through the same pipeline.
        """
        dist = _encode(patterned_clip, self.CHAIN, 0, tmp_path)
        aligned = Q.measure(patterned_clip, dist, self.CHAIN, metric="vmaf", ffmpeg=FFMPEG)
        misaligned = Q.measure(patterned_clip, dist, [], metric="vmaf", ffmpeg=FFMPEG)
        assert misaligned.score < aligned.score - 20.0, (misaligned.score, aligned.score)

    def test_ssim_agrees_that_the_lossless_case_is_perfect(self, patterned_clip, tmp_path):
        dist = _encode(patterned_clip, self.CHAIN, 0, tmp_path)
        report = Q.measure(patterned_clip, dist, self.CHAIN, metric="ssim", ffmpeg=FFMPEG)
        assert report.score > 0.999

    def test_psnr_reports_decibels(self, patterned_clip, tmp_path):
        dist = _encode(patterned_clip, self.CHAIN, 28, tmp_path)
        report = Q.measure(patterned_clip, dist, self.CHAIN, metric="psnr", ffmpeg=FFMPEG)
        assert 25.0 < report.score < 60.0, report

    def test_reference_can_be_reused(self, patterned_clip, tmp_path):
        dist = _encode(patterned_clip, self.CHAIN, 28, tmp_path)
        reference = tmp_path / "shared_ref.mp4"
        subprocess.run(
            Q.build_reference_command(
                patterned_clip, reference, self.CHAIN, ffmpeg=FFMPEG, lossless_crf=0
            ),
            check=True,
            capture_output=True,
        )
        shared = Q.measure(patterned_clip, dist, self.CHAIN, reference=reference, ffmpeg=FFMPEG)
        fresh = Q.measure(patterned_clip, dist, self.CHAIN, ffmpeg=FFMPEG)
        assert shared.score == pytest.approx(fresh.score, abs=0.01)


@pytestmark_int
class TestDropStaticIsUnmeasurable:
    def test_search_refuses_drop_static(self, patterned_clip):
        from vidcrush.core import CrushOptions

        with pytest.raises(Q.UsageError, match="drop-static"):
            Q.search_crf(patterned_clip, CrushOptions(drop_static=True), target=90.0, ffmpeg=FFMPEG)
