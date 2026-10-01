"""Unit tests for the pure filter-building logic."""

from __future__ import annotations

import math

import pytest

from vidcrush import filters as F
from vidcrush.errors import UsageError


class TestEven:
    def test_rounds_down(self):
        assert F.even(1921) == 1920
        assert F.even(1920) == 1920
        assert F.even(775) == 774

    def test_rejects_tiny(self):
        with pytest.raises(UsageError):
            F.even(1)


class TestAtempoFactors:
    @pytest.mark.parametrize("speed", [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 7.5, 8.0, 10.0, 60.0])
    def test_product_matches_speed(self, speed):
        factors = F.atempo_factors(speed)
        assert math.prod(factors) == pytest.approx(speed, rel=1e-4)

    @pytest.mark.parametrize("speed", [0.25, 0.5, 1.0, 2.0, 4.0, 8.0])
    def test_every_factor_inside_ffmpeg_range(self, speed):
        assert all(F.ATEMPO_MIN <= f <= F.ATEMPO_MAX for f in F.atempo_factors(speed))

    def test_single_factor_when_possible(self):
        assert F.atempo_factors(1.0) == (1.0,)
        assert F.atempo_factors(2.0) == (2.0,)

    def test_chains_beyond_two(self):
        assert F.atempo_factors(4.0) == (2.0, 2.0)
        assert F.atempo_factors(8.0) == (2.0, 2.0, 2.0)

    def test_slowing_down_chains_backwards(self):
        assert F.atempo_factors(0.25) == (0.5, 0.5)

    def test_rejects_zero_and_negative(self):
        for bad in (0, -1):
            with pytest.raises(UsageError):
                F.atempo_factors(bad)


class TestNormalizeSpeed:
    def test_snaps_float_noise_to_one(self):
        assert F.normalize_speed(1.0 + 1e-9) == 1.0

    def test_keeps_real_change(self):
        assert F.normalize_speed(1.5) == 1.5

    def test_rejects_absurd(self):
        with pytest.raises(UsageError):
            F.normalize_speed(1000)


class TestSpeedForDuration:
    def test_basic(self):
        assert F.speed_for_duration(140.928, 70.464) == pytest.approx(2.0)

    def test_rejects_bad_input(self):
        with pytest.raises(UsageError):
            F.speed_for_duration(0, 10)
        with pytest.raises(UsageError):
            F.speed_for_duration(10, 0)


class TestVideoFilters:
    def test_empty_when_nothing_requested(self):
        assert F.video_filters() == []

    def test_speed_only(self):
        assert F.video_filters(speed=2.0) == ["setpts=0.500000*PTS"]

    def test_no_setpts_at_normal_speed(self):
        assert F.video_filters(speed=1.0) == []

    def test_drop_static_comes_first(self):
        chain = F.video_filters(drop_static=True, speed=2.0)
        assert chain[0].startswith("mpdecimate=")
        assert chain[1] == "setpts=N/FRAME_RATE/TB"
        assert chain[2] == "setpts=0.500000*PTS"

    def test_width_wins_over_max_height(self):
        chain = F.video_filters(width=1920, max_height=720)
        assert "scale='trunc(min(iw,1920)/2)*2':-2:flags=lanczos" in chain
        assert "setsar=1" in chain

    def test_max_height_only_shrinks(self):
        chain = F.video_filters(max_height=1080)
        assert "scale=-2:'trunc(min(ih,1080)/2)*2':flags=lanczos" in chain

    def test_odd_width_is_made_even(self):
        assert "min(iw,1920)" in "".join(F.video_filters(width=1921))

    def test_fps_is_applied(self):
        assert "fps=30" in F.video_filters(fps=30.0)

    def test_rejects_bad_decimate_frac(self):
        with pytest.raises(UsageError):
            F.video_filters(drop_static=True, decimate_frac=0)
        with pytest.raises(UsageError):
            F.video_filters(drop_static=True, decimate_frac=1.5)

    def test_rejects_inverted_decimate_thresholds(self):
        with pytest.raises(UsageError):
            F.video_filters(drop_static=True, decimate_hi=100, decimate_lo=200)


class TestNoUpscaleByDefault:
    """A small clip must never be blown up just because a target was given."""

    def test_width_is_clamped_to_the_source(self):
        assert "min(iw,1920)" in F.scale_filter(1920, None)

    def test_max_height_is_clamped_to_the_source(self):
        assert "min(ih,1080)" in F.scale_filter(None, 1080)

    def test_results_are_forced_even(self):
        assert F.scale_filter(1920, None).startswith("scale='trunc(")
        assert F.scale_filter(None, 1080).count("trunc(") == 1

    def test_upscale_opt_in_gives_an_exact_width(self):
        assert F.scale_filter(1920, None, allow_upscale=True) == "scale=1920:-2:flags=lanczos"

    def test_upscale_opt_in_gives_an_exact_height(self):
        assert F.scale_filter(None, 1080, allow_upscale=True) == "scale=-2:1080:flags=lanczos"

    def test_no_scale_when_nothing_requested(self):
        assert F.scale_filter(None, None) is None

    def test_video_filters_forwards_the_flag(self):
        assert "scale=1920:-2:flags=lanczos" in F.video_filters(width=1920, allow_upscale=True)


class TestAudioFilters:
    def test_no_filter_at_normal_speed(self):
        assert F.audio_filters(1.0) == []

    def test_single_atempo(self):
        assert F.audio_filters(2.0) == ["atempo=2"]

    def test_chained_atempo(self):
        assert F.audio_filters(4.0) == ["atempo=2", "atempo=2"]


class TestFilterComplex:
    def test_video_only(self):
        assert F.filter_complex(["setpts=0.5*PTS"], []) == "[0:v]setpts=0.5*PTS[v]"

    def test_video_and_audio(self):
        graph = F.filter_complex(["scale=1920:-2"], ["atempo=2"])
        assert graph == "[0:v]scale=1920:-2[v];[0:a]atempo=2[a]"

    def test_empty_video_chain_becomes_null(self):
        assert F.filter_complex([], []) == "[0:v]null[v]"


class TestDescribe:
    def test_names_only(self):
        chain = ["setpts=0.5*PTS", "scale=1920:-2:flags=lanczos"]
        assert F.describe(chain) == "setpts,scale"

    def test_empty(self):
        assert F.describe([]) == "-"
