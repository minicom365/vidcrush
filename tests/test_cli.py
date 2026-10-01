"""CLI-level tests. ffprobe / ffmpeg are stubbed out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vidcrush import cli
from vidcrush.probe import MediaInfo

INFO = MediaInfo(
    path=Path("clip.mp4"),
    duration=140.927979,
    size=198_800_000,
    bit_rate=11_287_694,
    width=2560,
    height=1032,
    fps=30.0,
    video_codec="h264",
    audio_codec="aac",
    nb_frames=4226,
)


@pytest.fixture(autouse=True)
def stub_tools(monkeypatch):
    monkeypatch.setattr("vidcrush.cli.probe", lambda path, ffprobe=None: INFO)
    # A realistic Windows install path — the space is the whole point.
    monkeypatch.setattr(
        "vidcrush.core.find_ffmpeg", lambda explicit=None: r"C:\Program Files\ffmpeg\bin\ffmpeg.EXE"
    )


class TestInfo:
    def test_human_readable(self, capsys):
        assert cli.main(["clip.mp4", "--info"]) == 0
        out = capsys.readouterr().out
        assert "2560x1032" in out
        assert "2:20.9" in out
        assert "189.6 MB" in out

    def test_json(self, capsys):
        assert cli.main(["clip.mp4", "--info", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["width"] == 2560
        assert data["audio_codec"] == "aac"


class TestShellQuote:
    """The dry-run line has to survive a copy-paste into a real shell."""

    def test_plain_words_are_left_alone(self):
        for shell in cli.SHELLS:
            assert cli.shell_quote("ffmpeg", shell) == "ffmpeg"
            assert cli.shell_quote("-crf", shell) == "-crf"
            assert cli.shell_quote("28", shell) == "28"

    def test_backslash_path_needs_no_quoting_anywhere(self):
        for shell in cli.SHELLS:
            assert cli.shell_quote(r"C:\tmp\a.mp4", shell) == r"C:\tmp\a.mp4"

    def test_posix_uses_single_quotes(self):
        assert cli.shell_quote("my clip.mp4", "posix") == "'my clip.mp4'"

    def test_cmd_uses_double_quotes(self):
        assert cli.shell_quote("my clip.mp4", "cmd") == '"my clip.mp4"'

    def test_powershell_doubles_embedded_quotes(self):
        assert cli.shell_quote("it's here", "powershell") == "'it''s here'"

    def test_filter_graph_is_quoted(self):
        graph = "[0:v]setpts=0.5*PTS[v];[0:a]atempo=2[a]"
        for shell in cli.SHELLS:
            quoted = cli.shell_quote(graph, shell)
            assert quoted[0] in "'\"" and quoted[-1] == quoted[0]

    def test_empty_argument_is_quoted(self):
        assert cli.shell_quote("", "cmd") == '""'

    def test_format_command_quotes_only_what_needs_it(self):
        assert cli.format_command(["ffmpeg", "-i", "my clip.mp4", "-crf", "28"], "cmd") == (
            'ffmpeg -i "my clip.mp4" -crf 28'
        )

    def test_powershell_gets_the_call_operator(self):
        line = cli.format_command([r"C:\Program Files\ffmpeg.EXE", "-i", "a b.mp4"], "powershell")
        assert line == r"& 'C:\Program Files\ffmpeg.EXE' -i 'a b.mp4'"

    def test_powershell_skips_the_call_operator_for_bare_names(self):
        assert cli.format_command(["ffmpeg", "-i", "a b.mp4"], "powershell") == "ffmpeg -i 'a b.mp4'"

    def test_cmd_never_uses_the_call_operator(self):
        line = cli.format_command([r"C:\Program Files\ffmpeg.EXE"], "cmd")
        assert not line.startswith("&")
        assert line == r'"C:\Program Files\ffmpeg.EXE"'

    def test_detect_shell_returns_a_supported_value(self):
        assert cli.detect_shell() in cli.SHELLS


class TestDryRun:
    def test_prints_the_command_and_does_not_crash(self, capsys):
        assert cli.main(["clip.mp4", "-s", "2", "-w", "1920", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "setpts=0.500000*PTS" in out
        assert "min(iw,1920)" in out
        assert "atempo=2" in out

    def test_upscale_flag_produces_an_exact_width(self, capsys):
        cli.main(["clip.mp4", "-w", "1920", "--upscale", "--dry-run"])
        assert "scale=1920:-2:flags=lanczos" in capsys.readouterr().out

    def test_shell_option_selects_the_quoting_style(self, capsys):
        cli.main(["clip.mp4", "-s", "2", "--dry-run", "--shell", "cmd"])
        last = capsys.readouterr().out.strip().splitlines()[-1]
        assert last.startswith('"C:\\Program Files')

    def test_shell_powershell_adds_the_call_operator(self, capsys):
        cli.main(["clip.mp4", "-s", "2", "--dry-run", "--shell", "powershell"])
        last = capsys.readouterr().out.strip().splitlines()[-1]
        assert last.startswith("& '")

    def test_shell_posix_quotes_everything_unsafe(self, capsys):
        cli.main(["clip.mp4", "-s", "2", "--dry-run", "--shell", "posix"])
        last = capsys.readouterr().out.strip().splitlines()[-1]
        assert last.startswith("'C:\\Program Files")
        # The whole filter graph must sit inside one quoted argument, otherwise
        # the ';' between the video and audio chains would split the command.
        assert "'[0:v]" in last and "[a]'" in last

    def test_quiet_suppresses_the_summary_but_keeps_the_command(self, capsys):
        assert cli.main(["clip.mp4", "-s", "2", "--dry-run", "-q"]) == 0
        out = capsys.readouterr().out
        assert "input     :" not in out
        assert "-filter_complex" in out

    def test_default_output_name_is_reported(self, capsys):
        cli.main(["clip.mp4", "-s", "2", "--dry-run"])
        assert "clip_2x_crf28.mp4" in capsys.readouterr().out


class TestValidation:
    def test_speed_and_duration_are_mutually_exclusive(self):
        with pytest.raises(SystemExit) as exc:
            cli.main(["clip.mp4", "-s", "2", "-d", "70"])
        assert exc.value.code == 2

    def test_bad_preset_is_rejected(self):
        with pytest.raises(SystemExit) as exc:
            cli.main(["clip.mp4", "--preset", "nope"])
        assert exc.value.code == 2

    def test_duration_drives_the_speed(self, capsys):
        cli.main(["clip.mp4", "-d", "70.4639895", "--dry-run"])
        assert "setpts=0.500000*PTS" in capsys.readouterr().out


class TestVersion:
    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--version"])
        assert exc.value.code == 0
        assert "vidcrush" in capsys.readouterr().out
