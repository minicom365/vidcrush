"""End-to-end tests that really shell out to ffmpeg.

These are the tests with teeth: they generate a synthetic clip, crush it, then
probe the result to prove the length and the byte count actually went down.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vidcrush.core import CrushOptions, crush
from vidcrush.errors import FFmpegError
from vidcrush.probe import probe

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="ffmpeg/ffprobe are not installed"
)


def make_video(dst: Path, *, source: str, seconds: float = 4.0, with_audio: bool = True) -> Path:
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        source,
    ]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(dst)]
    subprocess.run(cmd, check=True, capture_output=True)
    return dst


@pytest.fixture(scope="module")
def moving_clip(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("media") / "moving.mp4"
    return make_video(path, source="testsrc2=size=320x240:rate=30:duration=4", seconds=4.0)


@pytest.fixture(scope="module")
def static_clip(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("media") / "static.mp4"
    return make_video(
        path, source="color=c=black:size=320x240:rate=30:duration=4", with_audio=False
    )


class TestSpeed:
    def test_halves_the_duration(self, moving_clip, tmp_path):
        result = crush(
            moving_clip, CrushOptions(speed=2.0, output=tmp_path / "out.mp4"), quiet=True
        )
        assert result.output.duration == pytest.approx(2.0, abs=0.25)
        assert result.plan.info.duration == pytest.approx(4.0, abs=0.25)
        assert result.duration_ratio == pytest.approx(0.5, abs=0.1)

    def test_audio_survives_a_speed_change(self, moving_clip, tmp_path):
        result = crush(
            moving_clip, CrushOptions(speed=2.0, output=tmp_path / "out.mp4"), quiet=True
        )
        assert result.output.has_audio is True

    def test_audio_survives_a_pure_reencode(self, moving_clip, tmp_path):
        """No speed change, just a smaller file — the audio must still be there."""
        result = crush(moving_clip, CrushOptions(crf=30, output=tmp_path / "out.mp4"), quiet=True)
        assert result.output.has_audio is True
        assert result.plan.audio_dropped is False

    def test_audio_survives_a_speed_change_and_downscale(self, moving_clip, tmp_path):
        result = crush(
            moving_clip,
            CrushOptions(speed=2.0, width=160, crf=30, output=tmp_path / "out.mp4"),
            quiet=True,
        )
        assert result.output.has_audio is True

    def test_duration_option_is_equivalent_to_speed(self, moving_clip, tmp_path):
        info = probe(moving_clip)
        result = crush(
            moving_clip,
            CrushOptions(duration=info.duration / 2, output=tmp_path / "out.mp4"),
            quiet=True,
        )
        assert result.plan.speed == pytest.approx(2.0, rel=1e-3)
        assert result.output.duration == pytest.approx(2.0, abs=0.25)


class TestShrinking:
    def test_output_is_smaller_than_input(self, moving_clip, tmp_path):
        result = crush(
            moving_clip,
            CrushOptions(speed=2.0, crf=28, width=1920, output=tmp_path / "out.mp4"),
            quiet=True,
        )
        assert result.output.size < result.plan.info.size
        assert result.size_ratio < 1.0

    def test_width_is_applied(self, moving_clip, tmp_path):
        result = crush(
            moving_clip, CrushOptions(width=160, output=tmp_path / "out.mp4"), quiet=True
        )
        assert result.output.width == 160
        assert result.output.height == 120

    def test_a_bigger_target_does_not_upscale(self, moving_clip, tmp_path):
        result = crush(
            moving_clip, CrushOptions(width=1920, output=tmp_path / "out.mp4"), quiet=True
        )
        assert (result.output.width, result.output.height) == (320, 240)

    def test_upscale_flag_enables_enlarging(self, moving_clip, tmp_path):
        result = crush(
            moving_clip,
            CrushOptions(width=640, upscale=True, output=tmp_path / "out.mp4"),
            quiet=True,
        )
        assert result.output.width == 640

    def test_odd_width_becomes_even(self, moving_clip, tmp_path):
        result = crush(
            moving_clip, CrushOptions(width=161, output=tmp_path / "out.mp4"), quiet=True
        )
        assert result.output.width == 160

    def test_no_audio_removes_the_track(self, moving_clip, tmp_path):
        result = crush(
            moving_clip, CrushOptions(no_audio=True, output=tmp_path / "out.mp4"), quiet=True
        )
        assert result.output.has_audio is False


class TestDropStatic:
    def test_freezes_are_removed(self, static_clip, tmp_path):
        before = probe(static_clip)
        result = crush(
            static_clip, CrushOptions(drop_static=True, output=tmp_path / "out.mp4"), quiet=True
        )
        assert result.output.duration < before.duration / 2

    def test_audio_is_dropped_and_flagged(self, moving_clip, tmp_path):
        result = crush(
            moving_clip, CrushOptions(drop_static=True, output=tmp_path / "out.mp4"), quiet=True
        )
        assert result.output.has_audio is False
        assert result.plan.audio_dropped is True


class TestFailures:
    def test_existing_output_requires_force(self, moving_clip, tmp_path):
        target = tmp_path / "out.mp4"
        target.write_bytes(b"not a video")
        with pytest.raises(FFmpegError):
            crush(moving_clip, CrushOptions(output=target), quiet=True)

    def test_force_overwrites(self, moving_clip, tmp_path):
        target = tmp_path / "out.mp4"
        target.write_bytes(b"not a video")
        result = crush(moving_clip, CrushOptions(output=target, force=True), quiet=True)
        assert result.output.duration > 0


class TestQualityCli:
    def test_measure_reports_a_score_after_encoding(self, moving_clip, tmp_path, capsys):
        from vidcrush import cli

        code = cli.main(
            [
                str(moving_clip),
                "-s",
                "2",
                "-o",
                str(tmp_path / "o.mp4"),
                "-y",
                "-q",
                "--measure",
                "ssim",
            ]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "quality" in out and "ssim" not in out  # the score line is printed
        assert "frames" in out

    def test_measure_against_skips_encoding(self, moving_clip, tmp_path, capsys):
        from vidcrush import cli

        existing = tmp_path / "existing.mp4"
        crush(moving_clip, CrushOptions(speed=2.0, output=existing), quiet=True)
        capsys.readouterr()

        code = cli.main([str(moving_clip), "-s", "2", "--measure-against", str(existing)])
        assert code == 0
        out = capsys.readouterr().out
        assert "against" in out
        assert "quality" in out

    def test_quality_target_picks_a_ladder_rung(self, moving_clip, tmp_path, capsys):
        from vidcrush import cli

        code = cli.main(
            [
                str(moving_clip),
                "-o",
                str(tmp_path / "o.mp4"),
                "-y",
                "--quality-target",
                "0.80",
                "--quality-metric",
                "ssim",
                "--crf-ladder",
                "26,34",
                "--sample-window",
                "2",
                "--sample-count",
                "1",
            ]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "crf  26" in out or "crf  34" in out
        assert "chosen" in out

    def test_quality_target_and_drop_static_is_rejected(self, moving_clip, tmp_path, capsys):
        from vidcrush import cli

        code = cli.main(
            [
                str(moving_clip),
                "--drop-static",
                "--quality-target",
                "90",
                "-o",
                str(tmp_path / "o.mp4"),
            ]
        )
        assert code == 2
        assert "drop-static" in capsys.readouterr().err

    def test_measure_and_drop_static_is_rejected(self, moving_clip, tmp_path, capsys):
        from vidcrush import cli

        code = cli.main(
            [str(moving_clip), "--drop-static", "--measure", "-o", str(tmp_path / "o.mp4")]
        )
        assert code == 2
        assert "drop-static" in capsys.readouterr().err

    def test_json_includes_the_quality_block(self, moving_clip, tmp_path, capsys):
        import json as _json

        from vidcrush import cli

        code = cli.main(
            [
                str(moving_clip),
                "-s",
                "2",
                "-o",
                str(tmp_path / "o.mp4"),
                "-y",
                "-q",
                "--json",
                "--measure",
                "psnr",
            ]
        )
        assert code == 0
        payload = _json.loads(capsys.readouterr().out)
        assert payload["quality"]["metric"] == "psnr"
        assert payload["quality"]["score"] > 0


class TestQualityOnlyMode:
    """`--crf N` on its own must shrink the file without touching the timeline.

    This is the "just make it smaller" workflow: no speed change, no scaling, no
    frame dropping.  Everything a viewer or a downstream tool can observe about
    the timeline and the container has to survive.
    """

    @pytest.fixture(scope="class")
    def rich_clip(self, tmp_path_factory) -> Path:
        """A clip with metadata, a chapter, 23.976 fps and low-bitrate audio."""
        directory = tmp_path_factory.mktemp("qualityonly")
        base = directory / "base.mp4"
        meta = directory / "meta.txt"
        meta.write_text(
            ";FFMETADATA1\n"
            "title=My Title\n"
            "artist=Some Artist\n"
            "comment=hello world\n"
            "[CHAPTER]\n"
            "TIMEBASE=1/1000\n"
            "START=0\n"
            "END=2200\n"
            "title=Chapter One\n",
            encoding="utf-8",
        )
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
                "testsrc2=size=320x240:rate=24000/1001:duration=4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100:duration=4",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-shortest",
                str(base),
            ],
            check=True,
            capture_output=True,
        )
        rich = directory / "rich.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(base),
                "-i",
                str(meta),
                "-map",
                "0",
                "-map_metadata",
                "1",
                "-map_chapters",
                "1",
                "-c",
                "copy",
                str(rich),
            ],
            check=True,
            capture_output=True,
        )
        return rich

    @staticmethod
    def _json(path: Path, *entries: str) -> dict:
        proc = subprocess.run(
            [FFPROBE, "-v", "error", "-of", "json", *entries, str(path)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return json.loads(proc.stdout)

    def test_frames_and_timing_are_untouched(self, rich_clip, tmp_path):
        result = crush(rich_clip, CrushOptions(crf=30, output=tmp_path / "out.mp4"), quiet=True)
        before, after = result.plan.info, result.output
        assert after.nb_frames == before.nb_frames
        assert after.fps == pytest.approx(before.fps)
        assert (after.width, after.height) == (before.width, before.height)
        assert after.duration == pytest.approx(before.duration, abs=0.05)

    def test_no_video_filter_is_applied(self, rich_clip, tmp_path):
        result = crush(rich_clip, CrushOptions(crf=30, output=tmp_path / "out.mp4"), quiet=True)
        assert result.plan.video_chain == []
        assert result.plan.speed == 1.0

    def test_the_file_gets_smaller(self, rich_clip, tmp_path):
        result = crush(rich_clip, CrushOptions(crf=30, output=tmp_path / "out.mp4"), quiet=True)
        assert result.output.size < result.plan.info.size

    def test_global_metadata_survives(self, rich_clip, tmp_path):
        out = tmp_path / "out.mp4"
        crush(rich_clip, CrushOptions(crf=30, output=out), quiet=True)
        tags = self._json(out, "-show_entries", "format_tags")["format"]["tags"]
        assert tags["title"] == "My Title"
        assert tags["artist"] == "Some Artist"
        assert tags["comment"] == "hello world"

    def test_chapters_survive(self, rich_clip, tmp_path):
        out = tmp_path / "out.mp4"
        crush(rich_clip, CrushOptions(crf=30, output=out), quiet=True)
        before = self._json(rich_clip, "-show_chapters")["chapters"]
        after = self._json(out, "-show_chapters")["chapters"]
        assert len(after) == len(before) == 1
        assert after[0]["tags"]["title"] == "Chapter One"

    def test_copy_audio_keeps_the_track_bit_for_bit(self, rich_clip, tmp_path):
        out = tmp_path / "copy.mp4"
        result = crush(rich_clip, CrushOptions(crf=30, copy_audio=True, output=out), quiet=True)
        assert result.plan.audio_copied is True
        # A stream copy cannot pad or resample, so the duration is exact.
        assert result.output.duration == pytest.approx(result.plan.info.duration, abs=0.001)

        before = self._json(rich_clip, "-select_streams", "a", "-show_streams")["streams"][0]
        after = self._json(out, "-select_streams", "a", "-show_streams")["streams"][0]
        assert after["codec_name"] == before["codec_name"]
        assert after["bit_rate"] == before["bit_rate"]
        assert after["nb_frames"] == before["nb_frames"]

    def test_copy_audio_still_preserves_metadata(self, rich_clip, tmp_path):
        out = tmp_path / "copy.mp4"
        crush(rich_clip, CrushOptions(crf=30, copy_audio=True, output=out), quiet=True)
        tags = self._json(out, "-show_entries", "format_tags")["format"]["tags"]
        assert tags["title"] == "My Title"


class TestCliRoundTrip:
    def test_cli_json_report(self, moving_clip, tmp_path, capsys):
        from vidcrush import cli

        code = cli.main(
            [str(moving_clip), "-s", "2", "-o", str(tmp_path / "out.mp4"), "--json", "-q", "-y"]
        )
        assert code == 0
        import json

        payload = json.loads(capsys.readouterr().out)
        assert payload["speed"] == 2.0
        assert payload["duration_ratio"] == pytest.approx(0.5, abs=0.1)
        assert "setpts" in payload["filter_complex"]
