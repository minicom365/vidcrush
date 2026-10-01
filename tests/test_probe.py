"""Unit tests for ffprobe payload parsing (no real media involved)."""

from __future__ import annotations

import pytest

from vidcrush.errors import ProbeError
from vidcrush.probe import MediaInfo, parse_fraction

PAYLOAD = {
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 2560,
            "height": 1032,
            "r_frame_rate": "30/1",
            "avg_frame_rate": "30/1",
            "nb_frames": "4226",
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
        },
    ],
    "format": {
        "duration": "140.927979",
        "size": "198800000",
        "bit_rate": "11287694",
    },
}


class TestParseFraction:
    def test_plain(self):
        assert parse_fraction("30/1") == 30.0

    def test_ntsc(self):
        assert parse_fraction("30000/1001") == pytest.approx(29.97002997)

    def test_garbage_falls_back_to_zero(self):
        assert parse_fraction("0/0") == 0.0
        assert parse_fraction("") == 0.0
        assert parse_fraction(None) == 0.0
        assert parse_fraction("n/a") == 0.0


class TestFromFfprobe:
    def test_reads_everything(self):
        info = MediaInfo.from_ffprobe(PAYLOAD, "clip.mp4")
        assert info.duration == pytest.approx(140.927979)
        assert info.width == 2560
        assert info.height == 1032
        assert info.resolution == "2560x1032"
        assert info.fps == 30.0
        assert info.video_codec == "h264"
        assert info.audio_codec == "aac"
        assert info.has_audio is True
        assert info.nb_frames == 4226

    def test_video_without_audio(self):
        payload = {"streams": PAYLOAD["streams"][:1], "format": PAYLOAD["format"]}
        info = MediaInfo.from_ffprobe(payload, "silent.mp4")
        assert info.audio_codec is None
        assert info.has_audio is False

    def test_audio_only_is_rejected(self):
        payload = {"streams": [PAYLOAD["streams"][1]], "format": PAYLOAD["format"]}
        with pytest.raises(ProbeError):
            MediaInfo.from_ffprobe(payload, "song.m4a")

    def test_duration_falls_back_to_stream(self):
        payload = {
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 2, "height": 2, "duration": "12.5"}],
            "format": {},
        }
        assert MediaInfo.from_ffprobe(payload, "x.mp4").duration == 12.5

    def test_as_dict_is_json_safe(self):
        data = MediaInfo.from_ffprobe(PAYLOAD, "clip.mp4").as_dict()
        assert data["size_mb"] == pytest.approx(189.6, abs=0.5)
        assert data["audio_codec"] == "aac"
        assert isinstance(data["path"], str)
