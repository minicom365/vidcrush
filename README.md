# vidcrush

Speed up and shrink videos with ffmpeg — one command, no configuration.

Built for the case that ffmpeg's documentation makes surprisingly annoying:
**a long screen recording you want to be 2× faster, smaller and still readable.**
It also handles the two things people get wrong by hand — keeping the audio in
sync at speed, and never accidentally upscaling a small clip into a bigger file.

```console
$ vidcrush "recording.mp4" -s 2 -w 1920 --crf 28
input     : recording.mp4
            2560x1032 @ 30fps, 2:20.9, 189.6 MB
output    : recording_2x_crf28.mp4
speed     : 2x  -> about 1:10.5
filters   : v[setpts,scale,setsar] a[atempo]

done      : 1:10.5 (50% of original), 9.3 MB (4.9% of original)
            recording_2x_crf28.mp4
            took 41.7s
```

That is a real run: **189.6 MB → 9.3 MB (−95%)** and **2:20.9 → 1:10.5**.

---

## Requirements

* Python 3.9+
* `ffmpeg` and `ffprobe` on `PATH`

Nothing else. vidcrush has **zero Python dependencies**.

## Install

```bash
pip install .
# or, without installing, from a checkout:
python -m vidcrush --help
```

## Usage

```
vidcrush INPUT [options]
```

### The usual job

```bash
# 2x faster, at most 1920px wide, CRF 28
vidcrush "recording.mp4" -s 2 -w 1920 --crf 28

# hit an exact target length instead of a speed multiplier
vidcrush "lecture.mp4" -d 70 -w 1920

# just a re-encode to shrink the file, same length
vidcrush "clip.mp4" --crf 30

# mostly-idle screen recording? drop the duplicate frames
vidcrush "recording.mp4" --drop-static -s 2
```

### Options

| Option | Default | Description |
| --- | --- | --- |
| `-o, --output` | `<name>_<speed>x_crf<crf>.mp4` | Output path |
| `-y, --force` | off | Overwrite an existing output file |
| `-s, --speed` | `1` | Playback speed multiplier |
| `-d, --duration` | – | Target length in seconds (mutually exclusive with `-s`) |
| `--crf` | `28` | x264/x265 quality — lower is better and bigger |
| `--preset` | `medium` | x264/x265 speed/size trade-off |
| `-w, --width` | source | Shrink to this width, aspect ratio preserved |
| `--max-height` | source | Shrink to at most this height |
| `--upscale` | off | Allow scaling *above* the source size |
| `--fps` | source | Force an output frame rate |
| `--audio-bitrate` | `128k` | Audio bitrate |
| `--no-audio` | off | Drop the audio track |
| `--drop-static` | off | Remove near-duplicate frames (**mutes audio**) |
| `--info` | – | Print media info and exit |
| `--dry-run` | – | Print the ffmpeg command and exit |
| `--shell` | `auto` | Quoting style for `--dry-run`: `auto`, `powershell`, `cmd`, `posix` |
| `--json` | off | Emit a machine-readable report |
| `-q, --quiet` | off | Hide ffmpeg output |

Run `vidcrush --help` for the full list, including the `--decimate-*` knobs.

## How it works

Two filters do most of the work, and both are easy to get subtly wrong:

| Goal | Filter | Why this exact form |
| --- | --- | --- |
| Speed | `setpts=0.5*PTS` | Re-times every frame; the stream keeps its fps, the file gets shorter. |
| Speed (audio) | `atempo=2` | Changes tempo **without** shifting pitch. Values outside `0.5–2.0` are chained: `atempo=2,atempo=2` for 4×. |
| Resize | `scale='trunc(min(iw,1920)/2)*2':-2` | Clamps to the source width so a small clip is never blown up, and forces an even width because `yuv420p` demands it. |
| Static footage | `mpdecimate,setpts=N/FRAME_RATE/TB` | Drops near-duplicate frames, then closes the gaps. This is what actually shortens a recording where nothing moves. |

A few deliberate behaviours worth knowing:

* **`-w 1920` never upscales.** A 320×240 clip asked for `-w 1920` stays
  320×240. Pass `--upscale` if you really want the bigger file.
* **`--drop-static` mutes the video.** Re-timing the video stream slides it out
  of sync with the audio, and there is no reliable way to fix that afterwards,
  so the audio is removed and a warning is printed.
* **Existing outputs are never clobbered** unless you pass `-y`.
* **`--dry-run` prints a command you can actually paste.** `;`, `[` and `,` in a
  filter graph are shell metacharacters, so every argument is quoted for your
  shell — and on PowerShell the executable gets the required `&` call operator.
  Pass `--shell cmd` (or `posix`) if the guess is wrong.

## Development

```bash
pip install -e ".[dev]"
pytest
```

- `tests/test_filters.py`, `test_probe.py`, `test_core.py`, `test_cli.py` are
  hermetic: no ffmpeg needed.
- `tests/test_integration.py` generates synthetic clips with ffmpeg, crushes
  them, and proves the duration and the byte count really went down.

---

## 한국어

긴 화면 녹화를 **빠르게 + 작게** 만드는 ffmpeg 래퍼입니다. 의존성은 0개,
`ffmpeg`/`ffprobe`만 있으면 됩니다.

```powershell
# 2배속, 최대 1920px, CRF 28  (가장 흔한 조합)
python -m vidcrush "recording.mp4" -s 2 -w 1920 --crf 28

# 길이를 정확히 70초로
python -m vidcrush "lecture.mp4" -d 70 -w 1920

# 정보만 보기 / 명령만 보기
python -m vidcrush "recording.mp4" --info
python -m vidcrush "recording.mp4" -s 2 --dry-run
```

실측 예시: 2560×1032 · 2분 21초 · 189.6 MB → **1920×774 · 1분 10초 · 9.3 MB** (−95%)

주의할 점:

* `-w 1920`은 **원본보다 키우지 않습니다.** 키우려면 `--upscale`을 쓰세요.
  (작은 영상을 "압축"하려다 오히려 커지는 사고를 막습니다.)
* `--drop-static`은 정지 구간의 중복 프레임을 제거하는 대신 **오디오가 사라집니다.**
  영상 타임라인을 다시 짜기 때문에 오디오를 동기화할 방법이 없습니다.
* 결과 파일이 이미 있으면 기본적으로 덮어쓰지 않습니다 (`-y` 필요).
* `--dry-run`이 출력하는 명령은 **그대로 복사해서 실행 가능**합니다.
  PowerShell이면 `&` 호출 연산자까지 붙습니다 (`--shell`로 변경 가능).
