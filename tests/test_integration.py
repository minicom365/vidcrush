"""End-to-end tests that really shell out to ffmpeg.

These are the tests with teeth: they generate a synthetic clip, crush it, then
probe the result to prove the length and the byte count actually went down.
"""

from __future__ import annotations

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
