"""Unit tests for plan building (no ffmpeg process is ever started)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vidcrush.core import CrushOptions, build_plan, default_output
from vidcrush.errors import UsageError
from vidcrush.probe import MediaInfo


def make_info(**overrides) -> MediaInfo:
    base = {
        "path": Path("in.mp4"),
        "duration": 140.927979,
        "size": 198_800_000,
        "bit_rate": 11_287_694,
        "width": 2560,
        "height": 1032,
        "fps": 30.0,
        "video_codec": "h264",
        "audio_codec": "aac",
        "nb_frames": 4226,
    }
    base.update(overrides)
    return MediaInfo(**base)


@pytest.fixture(autouse=True)
def fake_ffmpeg(monkeypatch):
    """Keep plan building hermetic — no dependency on a real ffmpeg install."""
    monkeypatch.setattr("vidcrush.core.find_ffmpeg", lambda explicit=None: explicit or "ffmpeg")


class TestDefaultOutput:
    def test_speed_and_crf(self):
        out = default_output(Path("clip.mp4"), speed=2.0, crf=28, drop_static=False)
        assert out.name == "clip_2x_crf28.mp4"

    def test_no_speed_tag_at_normal_speed(self):
        out = default_output(Path("clip.mp4"), speed=1.0, crf=28, drop_static=False)
        assert out.name == "clip_crf28.mp4"

    def test_fractional_speed(self):
        out = default_output(Path("clip.mp4"), speed=1.5, crf=23, drop_static=False)
        assert out.name == "clip_1.5x_crf23.mp4"

    def test_drop_static_tag(self):
        out = default_output(Path("clip.mp4"), speed=2.0, crf=28, drop_static=True)
        assert out.name == "clip_2x_nodup_crf28.mp4"

    def test_keeps_extension(self):
        out = default_output(Path("clip.mkv"), speed=2.0, crf=28, drop_static=False)
        assert out.suffix == ".mkv"

    def test_stays_in_source_directory(self):
        out = default_output(Path("a/b/clip.mp4"), speed=2.0, crf=28, drop_static=False)
        assert out.parent == Path("a/b")


class TestSpeedResolution:
    def test_default_is_passthrough(self):
        plan = build_plan("in.mp4", CrushOptions(), info=make_info())
        assert plan.speed == 1.0

    def test_duration_becomes_a_multiplier(self):
        plan = build_plan("in.mp4", CrushOptions(duration=70.4639895), info=make_info())
        assert plan.speed == pytest.approx(2.0)

    def test_speed_and_duration_conflict(self):
        options = CrushOptions(speed=2.0, duration=70.0)
        with pytest.raises(UsageError):
            build_plan("in.mp4", options, info=make_info())

    def test_estimated_duration(self):
        plan = build_plan("in.mp4", CrushOptions(speed=2.0), info=make_info())
        assert plan.estimated_duration == pytest.approx(70.46, abs=0.01)


class TestCommandBuilding:
    def test_filter_graph_labels(self):
        plan = build_plan("in.mp4", CrushOptions(speed=2.0, width=1920), info=make_info())
        graph = plan.cmd[plan.cmd.index("-filter_complex") + 1]
        assert graph.startswith(
            "[0:v]setpts=0.500000*PTS,scale='trunc(min(iw,1920)/2)*2':-2:flags=lanczos,setsar=1[v]"
        )
        assert graph.endswith("[0:a]atempo=2[a]")

    def test_maps_both_streams(self):
        plan = build_plan("in.mp4", CrushOptions(speed=2.0), info=make_info())
        assert plan.cmd.count("-map") == 2
        assert "[v]" in plan.cmd and "[a]" in plan.cmd

    def test_audio_survives_a_pure_reencode(self):
        """Regression: at speed 1.0 there is no atempo filter, so audio_chain
        used to come back empty and build_plan emitted -an, silently dropping
        the audio track of a plain `--crf 30` re-encode."""
        plan = build_plan("in.mp4", CrushOptions(crf=30), info=make_info())
        assert plan.audio_chain == ["anull"]
        assert "-an" not in plan.cmd
        assert plan.cmd.count("-map") == 2
        assert plan.audio_dropped is False

    def test_pure_reencode_graph_maps_audio(self):
        plan = build_plan("in.mp4", CrushOptions(), info=make_info())
        graph = plan.cmd[plan.cmd.index("-filter_complex") + 1]
        assert graph == "[0:v]null[v];[0:a]anull[a]"

    def test_silent_input_still_gets_an(self):
        plan = build_plan("in.mp4", CrushOptions(crf=30), info=make_info(audio_codec=None))
        assert plan.audio_chain == []
        assert "-an" in plan.cmd
        assert plan.cmd.count("-map") == 1

    def test_silent_input_maps_video_only(self):
        plan = build_plan("in.mp4", CrushOptions(speed=2.0), info=make_info(audio_codec=None))
        assert plan.cmd.count("-map") == 1
        assert "-an" in plan.cmd

    def test_no_audio_flag(self):
        plan = build_plan("in.mp4", CrushOptions(speed=2.0, no_audio=True), info=make_info())
        assert "-an" in plan.cmd
        assert any("audio removed" in w for w in plan.warnings)

    def test_drop_static_silently_disables_audio(self):
        plan = build_plan("in.mp4", CrushOptions(drop_static=True), info=make_info())
        assert plan.audio_chain == []
        assert plan.audio_dropped is True
        assert any("drop-static" in w for w in plan.warnings)

    def test_encoder_flags(self):
        plan = build_plan("in.mp4", CrushOptions(crf=23, preset="fast"), info=make_info())
        assert plan.cmd[plan.cmd.index("-crf") + 1] == "23"
        assert plan.cmd[plan.cmd.index("-preset") + 1] == "fast"
        assert plan.cmd[plan.cmd.index("-pix_fmt") + 1] == "yuv420p"

    def test_non_x264_codec_skips_preset(self):
        plan = build_plan("in.mp4", CrushOptions(video_codec="libvpx-vp9"), info=make_info())
        assert "-preset" not in plan.cmd
        assert "-crf" in plan.cmd

    def test_faststart_only_for_mp4_family(self):
        mp4 = build_plan("in.mp4", CrushOptions(), info=make_info())
        assert "+faststart" in mp4.cmd
        mkv = build_plan("in.mp4", CrushOptions(output=Path("out.mkv")), info=make_info())
        assert "+faststart" not in mkv.cmd

    def test_refuses_to_run_without_force(self):
        plan = build_plan("in.mp4", CrushOptions(), info=make_info())
        assert plan.cmd[-2] == "-n"

    def test_force_switches_to_overwrite(self):
        plan = build_plan("in.mp4", CrushOptions(force=True), info=make_info())
        assert plan.cmd[-2] == "-y"

    def test_always_runs_ffmpeg_non_interactively(self):
        plan = build_plan("in.mp4", CrushOptions(), info=make_info())
        assert "-nostdin" in plan.cmd

    def test_output_path_is_last(self):
        plan = build_plan("in.mp4", CrushOptions(), info=make_info())
        assert plan.cmd[-1] == str(plan.dst)

    def test_explicit_output_wins(self):
        plan = build_plan("in.mp4", CrushOptions(output=Path("custom.mp4")), info=make_info())
        assert plan.dst == Path("custom.mp4")

    def test_rejects_overwriting_the_input(self):
        with pytest.raises(UsageError):
            build_plan("in.mp4", CrushOptions(output=Path("in.mp4")), info=make_info())
