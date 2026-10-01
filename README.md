# vidcrush

[![CI](https://github.com/minicom365/vidcrush/actions/workflows/ci.yml/badge.svg)](https://github.com/minicom365/vidcrush/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

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

## Measuring quality (VMAF / SSIM / PSNR)

Guessing a CRF is guesswork. vidcrush can measure the result instead — and,
for the impatient, search for the CRF that hits a quality target.

```bash
# how good is this encode, really?
vidcrush "recording.mp4" -s 2 -w 1920 --crf 28 --measure
#   quality   : 93.64 (2115 frames, model version=vmaf_v0.6.1)

# check a file you already made (the -s/-w flags describe how it was produced)
vidcrush "recording.mp4" -s 2 -w 1920 --measure-against "recording_2x_crf28.mp4"

# let it pick the CRF: smallest encode that still scores 93
vidcrush "recording.mp4" -s 2 -w 1920 --quality-target 93
```

The search trial-encodes a **probe clip** built from evenly spaced windows of the
source, measures each rung, and re-encodes the full file at the winner:

```
probe     : 31.232s+8s, 101.696s+8s
            crf  26   94.927     1.0 MB  <-- chosen
            crf  30   92.224   757.4 KB
            crf  34   87.153   539.5 KB
```

That probe predicted 94.93 for CRF 26; the full encode measured **94.91**.

| Option | Purpose |
| --- | --- |
| `--measure [METRIC]` | Measure the output after encoding (`vmaf`, `ssim`, `psnr`; default `vmaf`) |
| `--measure-against FILE` | Measure an existing FILE against the input, no encoding |
| `--quality-target SCORE` | Search the CRF ladder for the smallest encode meeting SCORE |
| `--quality-metric` | Metric for the search (default `vmaf`) |
| `--crf-ladder` | CRF candidates (default `18,22,26,30,34,38`) |
| `--sample-window`, `--sample-count` | Probe geometry (default 10s × 3, spread across the file) |
| `--vmaf-model` | VMAF model string, recorded in the JSON report |

VMAF needs an ffmpeg built with `--enable-libvmaf`; `ssim` and `psnr` are always
available. The output's `--json` report carries the metric, the model and the
frame count, so a score is always traceable to how it was obtained.

### Why the reference is not just "the original file"

Full-reference metrics compare frames **matched by timestamp**. vidcrush moves
timestamps on purpose, so measuring an output against the untouched source is
meaningless. On a real 8-second screen recording at CRF 28 / 2x:

| reference | VMAF |
| --- | --- |
| left alone | **28.85** |
| re-timed to match | **93.03** |

Same encoded file, 64 points apart. So the reference is produced by pushing the
source through the *same pipeline* (identical filter chain, same encoder frame
sync, `-crf 0`). Doing the cheaper thing — rebuilding the chain inside the
measurement graph — does **not** work either: ffmpeg's CFR frame selection under
`setpts` is not reproducible filter-side, and the same lossless file scores 66.59
instead of >99. There is a regression test pinning this down.

Two consequences worth knowing:

* **`--drop-static` cannot be measured.** `mpdecimate` discards frames
  non-deterministically, so no frame-to-frame reference exists. Combining the two
  is rejected with an error.
* **VMAF is not cheap.** One pass over a 2m21s 1080p clip takes ~2 minutes on a
  desktop CPU. `ssim` is ~30x faster, which is why the search defaults to a probe
  clip rather than the whole file.

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
pytest                    # 178 tests
ruff check .              # lint
ruff format --check .     # formatting
```

- `tests/test_filters.py`, `test_probe.py`, `test_core.py`, `test_cli.py` are
  hermetic: no ffmpeg needed.
- `tests/test_integration.py` generates synthetic clips with ffmpeg, crushes
  them, and proves the duration, the byte count and the audio track survived.

---

## License

MIT — see [LICENSE](LICENSE).

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

### 품질 측정과 자동 CRF 선택

```powershell
# 결과 품질 측정
python -m vidcrush "recording.mp4" -s 2 -w 1920 --crf 28 --measure

# 이미 만든 파일 검사 (인코딩 없음)
python -m vidcrush "recording.mp4" -s 2 -w 1920 --measure-against "결과.mp4"

# VMAF 93을 만족하는 가장 작은 CRF 자동 선택
python -m vidcrush "recording.mp4" -s 2 -w 1920 --quality-target 93
```

★ **기준(reference)을 원본 그대로 쓰면 안 됩니다.** 전역 메트릭은 **타임스탬프로
프레임을 짝짓기** 때문에, 배속으로 타임스탬프를 바꾼 결과물과 원본을 비교하면
점수가 무의미해집니다. 실측(8초 화면녹화, CRF 28 / 2배속):

| 기준 준비 | VMAF |
|---|---|
| 원본 그대로 | **28.85** |
| 동일 파이프라인으로 재정렬 | **93.03** |

같은 파일인데 64점 차이입니다. 그래서 vidcrush는 **소스를 동일 필터체인 + 동일
인코더 프레임 동기 + `-crf 0`으로 다시 인코딩해** 기준을 만듭니다.

* `--drop-static`은 **측정할 수 없습니다** (`mpdecimate`가 프레임을 비결정적으로
  버려 대응 관계가 사라집니다). 두 옵션을 함께 쓰면 오류로 거부합니다.
* VMAF는 저렴하지 않습니다. 2분 21초 1080p 한 번 측정에 **약 2분** 걸립니다.
  SSIM은 약 30배 빠르며, 그래서 탐색은 전량이 아니라 **샘플 구간**에서 수행합니다.
