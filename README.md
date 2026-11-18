# 쇼츠 자동 생성기 (Preset-driven Shorts Maker)

> **화면으로 쓰고 싶다면 → [롱폼 스튜디오](#0-롱폼-스튜디오-화면으로-편집썸네일업로드)**
> 편집 · 썸네일 · 업로드를 명령어 없이 클릭으로 합니다. 윈도우는 `스튜디오 실행.bat` 더블클릭.

영상 파일이나 URL 하나를 넣으면 **세로 쇼츠(1080x1920) mp4** 를 자동으로 뽑아주는 도구입니다.
편집 스타일은 전부 **프리셋 JSON 한 장**에 들어 있어서, 한 번 정해두면 그 다음부터는 명령 한 줄이면 끝납니다.

```bash
python src/make.py --preset presets/default.json --input 내영상.mp4
```

자동으로 처리되는 것:

| 단계 | 내용 |
|---|---|
| 소스 확보 | 로컬 파일 또는 URL(yt-dlp 다운로드) |
| 화면 변환 | 가로/정사각 영상을 9:16 세로로 (잘라내기 / 단색 여백 / 블러 여백) |
| 자막 | 음성인식으로 자동 생성하거나, 준비한 SRT 를 스타일 입혀 화면에 굽기 (한국어·일본어·영어) |
| 훅 문구 | 첫 몇 초간 화면 상단에 큰 글씨 |
| 배경음악 | 루프 + 페이드 + 말할 때 자동으로 볼륨 낮추기(더킹) |
| 워터마크 | 로고 PNG 를 원하는 모서리에 |
| 마무리 | 길이 자르기, 배속, 인코딩, 날짜 기반 파일명 |

---

## 0. 롱폼 스튜디오 (화면으로 편집·썸네일·업로드)

명령어 대신 **브라우저 화면**에서 클릭으로 작업하는 프로그램입니다.
내 컴퓨터 안에서만 돌아가고(127.0.0.1), 인터넷에 공개되지 않습니다.

### 실행

- **윈도우**: 저장소 폴더의 **`스튜디오 실행.bat`** 를 더블클릭 → 브라우저가 자동으로 열립니다.
- 그 외: `python src/studio.py`

처음 한 번은 아래 1번 설치(Python, ffmpeg)가 되어 있어야 합니다.
자동 자막까지 쓰려면 `pip install faster-whisper` 도 설치하세요.

### 화면 구성

| 단계 | 하는 일 |
|---|---|
| **1. 영상 편집** | 영상을 끌어다 놓고 → 프리셋(롱폼 1920×1080 / 쇼츠) · 구간 · 배속 · 자막(자동/SRT/없음) · 폰트 · 배경음악 · 워터마크 · 첫 화면 문구 → **렌더링 시작** |
| **2. 썸네일** | 완성된 영상에서 장면을 골라 큰 글씨를 얹은 1280×720 JPG 생성. 줄 앞에 `!` 를 붙이면 강조색 |
| **3. 업로드** | 제목(100자) · 설명(5000자) · 태그 · 공개 범위 · 예약 시각 입력 후 **업로드** |
| **업로드 설정** | 업로드에 쓸 명령을 한 번만 적어 둠 (아래 참고) |

끌어다 놓은 파일은 확장자에 따라 자동으로 정리됩니다:
영상·자막·이미지 → `samples/`, 음악 → `assets/bgm/`, 폰트 → `assets/fonts/`. 결과물은 `output/`.

### 업로드 연결 (Volcano Kit 등)

이 프로그램은 유튜브에 직접 로그인하지 않습니다. **지금 쓰고 있는 업로드 도구의 명령을
대신 실행**합니다. ‘업로드 설정’ 화면에 명령을 **한 줄에 한 칸씩** 적으세요.

```
volcano
upload
{video}
--title
{title}
--description-file
{description_file}
--thumbnail
{thumbnail}
```

(위는 예시입니다. 실제 명령 이름과 옵션은 쓰고 있는 도구에 맞게 바꾸세요.)

| 자리표시자 | 바뀌는 값 |
|---|---|
| `{video}` / `{thumbnail}` | 영상 / 썸네일 파일 경로 |
| `{title}` / `{description}` | 제목 / 설명 |
| `{description_file}` | 설명이 담긴 .txt 경로 (설명이 길 때 편함) |
| `{tags}` | 쉼표로 이은 태그 |
| `{privacy}` / `{publish_at}` | `private·unlisted·public` / 예약 시각 |
| `{meta}` | 위 정보를 모두 담은 JSON 경로 |

- 값이 비어 있으면(예: 썸네일 없음) 바로 앞의 `--옵션` 과 함께 빠집니다.
- 셸을 거치지 않고 실행하므로 제목에 따옴표·특수문자가 있어도 깨지지 않습니다.
- 설정은 `studio.config.json` 에 저장되며 git 에는 올라가지 않습니다.
- 업로드할 때마다 영상 옆에 `영상이름.upload.json`, `영상이름.description.txt` 가 저장됩니다.

### 썸네일만 명령어로

```bash
python src/thumbnail.py --input output/내영상.mp4 --time 12.5 --text "이거 모르면 손해\n!3분 정리"
```

---

## 1. 설치

**필수** — ffmpeg (ffprobe 포함)

```bash
# macOS
brew install ffmpeg
# Ubuntu / Debian
sudo apt install ffmpeg
# Windows (winget)
winget install Gyan.FFmpeg
```

**선택** — 쓰는 기능에 따라

```bash
pip install faster-whisper   # 자동 자막 (subtitle.mode = "auto")
pip install yt-dlp           # --input 에 URL 을 넣을 때
```

파이썬은 3.10 이상이면 됩니다. 그 외 의존성은 없습니다.

```bash
python tests/test_basics.py   # 설치 확인용 단위 테스트
```

---

## 2. 자동화를 위해 준비할 것

### 2-1. 매번 주는 것 (실행할 때마다)

| | 예시 |
|---|---|
| 소스 영상 | `내영상.mp4` 또는 `https://...` |
| (선택) 훅 문구 | `--hook "이거 모르면 손해"` |
| (선택) 직접 만든 자막 | `--subs 자막.srt` |

### 2-2. 한 번만 정해두는 것 (프리셋에 저장)

아래 항목을 채워 `presets/내스타일.json` 으로 저장하면 그 뒤로는 안 건드려도 됩니다.

- **폰트** — `assets/fonts/` 에 `.ttf`/`.otf` 를 넣고 `subtitle.font_file` 에 경로를 적으세요.
  한글 영상이라면 한글 폰트가 꼭 필요합니다 (Pretendard, 나눔스퀘어, 프리텐다드 등).
  적지 않으면 시스템 폰트(`subtitle.font_name`)를 씁니다.
- **자막 스타일** — 크기, 색, 외곽선, 화면상 위치, 한 줄 글자 수
- **화면 변환 방식** — `source.fit` (`crop` / `pad` / `blur_pad`)
- **배경음악** — `assets/bgm/` 에 mp3 를 넣고 `bgm.file` 에 경로
- **워터마크** — `assets/watermark/` 에 투명 PNG 를 넣고 `watermark.file` 에 경로
- **출력 규격** — 길이 상한, 화질(crf), 파일명 규칙

> `assets/` 와 `output/` 은 `.gitignore` 에 걸려 있습니다. 폰트·음원·로고는 저작권이 있으니
> 저장소에 커밋하지 말고 로컬에만 두세요.

---

## 3. 바로 써먹는 예시

```bash
# 기본: 중앙 크롭 + 자동 자막
python src/make.py --preset presets/default.json --input 내영상.mp4

# 가로 영상을 쇼츠로 (위아래를 같은 영상 블러로 채움)
python src/make.py --preset presets/landscape-to-shorts.json --input 강의.mp4

# URL 에서 받아서 15초~45초 구간만, 훅 문구 얹어서
python src/make.py --preset presets/bold-caption.json \
  --input "https://..." --hook "3초 안에 이해되는 설명" \
  --set source.trim.start=15 --set source.trim.end=45

# 직접 쓴 자막 사용 (음성인식 없이)
python src/make.py --preset presets/default.json --input 내영상.mp4 \
  --subs 자막.srt --set subtitle.mode=file

# 자막 없이, 배경음악만 깔기
python src/make.py --preset presets/default.json --input 내영상.mp4 \
  --no-subs --set bgm.enabled=true --set bgm.file=assets/bgm/lofi.mp3

# 실행하지 않고 ffmpeg 명령만 확인
python src/make.py --preset presets/default.json --input 내영상.mp4 --dry-run
```

### 폴더 통째로 일괄 처리

```bash
for f in ~/영상/*.mp4; do
  python src/make.py --preset presets/default.json --input "$f" --out output
done
```

---

## 4. CLI 옵션

| 옵션 | 설명 |
|---|---|
| `--preset <경로>` | (필수) 프리셋 JSON |
| `--input <파일\|URL>` | (필수) 소스 영상 |
| `--out <폴더>` | 출력 폴더 (기본 `output`) |
| `--subs <파일.srt>` | `subtitle.mode=file` 일 때 쓸 자막 |
| `--hook "<문구>"` | 훅 문구를 켜고 내용 지정 |
| `--slug <이름>` | 출력 파일명에 들어갈 이름 |
| `--set 경로=값` | 프리셋 항목 즉석 변경 (여러 번 사용 가능) |
| `--no-subs` | 이번 실행만 자막 끄기 |
| `--dry-run` | ffmpeg 명령만 출력 |

`--set` 은 프리셋을 새로 만들지 않고 한 항목만 바꿔볼 때 씁니다.
없는 항목을 적으면 바로 오류로 알려주므로 오타 걱정은 없습니다.

```bash
--set subtitle.font_size=90 --set source.speed=1.2 --set output.crf=18
```

---

## 5. 프리셋 항목 레퍼런스

전체 목록·기본값·제약은 [`preset.schema.json`](preset.schema.json) 에 있습니다.
VS Code 등에서 프리셋을 열면 `$schema` 덕분에 자동완성이 됩니다.
프리셋에는 **바꾸고 싶은 항목만** 적으면 되고, 나머지는 기본값이 채워집니다.

### `output` — 출력 규격
| 항목 | 기본값 | 설명 |
|---|---|---|
| `width` / `height` | 1080 / 1920 | 짝수여야 합니다 (H.264 제약) |
| `fps` | 30 | |
| `max_duration` | 60 | 최종 길이 상한(초). `null` 이면 소스 길이 그대로 |
| `crf` | 20 | 낮을수록 고화질·큰 용량. 18~23 권장 |
| `x264_preset` | `medium` | `veryfast` 면 빠르고 용량이 큼 |
| `filename_template` | `{date}_{preset}_{slug}.mp4` | `{date} {time} {preset} {slug}` 치환 |

### `source` — 소스 다루기
| 항목 | 기본값 | 설명 |
|---|---|---|
| `fit` | `crop` | `crop`=꽉 채우고 잘라냄 / `pad`=단색 여백 / `blur_pad`=같은 영상 블러 여백 |
| `crop_anchor` | `center` | `crop` 일 때 어느 쪽을 남길지 (`center/top/bottom/left/right`) |
| `pad_color` | `#000000` | `pad` 일 때 여백 색 |
| `blur_strength` | 40 | `blur_pad` 의 흐림 정도 (1~128) |
| `trim.start` / `trim.end` | `null` | 사용할 구간(초) |
| `speed` | 1.0 | 배속. 영상·음성 같이 조정됩니다 (0.25~4.0) |
| `mute_source` | false | 원본 소리 끄기 (BGM 만 깔 때) |

### `subtitle` — 자막
| 항목 | 기본값 | 설명 |
|---|---|---|
| `mode` | `auto` | `auto`=음성인식 / `file`=`--subs` SRT / `none`=없음 |
| `language` | `ko` | 음성인식 언어 |
| `whisper_model` | `small` | `tiny`~`large-v3`. 클수록 정확하고 느림 |
| `font_file` | `null` | `.ttf`/`.otf` 경로. 패밀리 이름은 파일에서 자동으로 읽습니다 |
| `font_name` | `Noto Sans CJK KR` | `font_file` 이 없을 때 쓸 시스템 폰트 |
| `font_size` | 74 | 1080 폭 기준 |
| `border_style` | 1 | `1`=외곽선, `3`=박스 배경(`back_color`) |
| `alignment` | 2 | 숫자패드 배열. `2`=하단중앙, `5`=정중앙, `8`=상단중앙 |
| `margin_v` | 420 | 아래 여백(px). 클수록 자막이 위로 올라옵니다 |
| `max_chars_per_line` | 18 | 한 줄 글자 수. 일본어는 12~14 권장 |
| `max_lines` | 2 | 한 번에 띄울 줄 수. **글자가 잘리는 일은 없습니다** |

`language` 는 음성인식 언어이자 줄바꿈 방식을 정합니다. 아래 "여러 언어" 항목을 보세요.

### `bgm` — 배경음악
| 항목 | 기본값 | 설명 |
|---|---|---|
| `enabled` / `file` | false / `null` | mp3·m4a·wav |
| `volume_db` | -20 | 말소리 대비 배경음 크기 |
| `loop` | true | 영상보다 짧으면 반복 |
| `fade_in` / `fade_out` | 1.0 / 1.5 | 초 |
| `duck.enabled` | true | 말할 때 배경음을 자동으로 낮춤 |
| `duck.amount_db` | -10 | 얼마나 낮출지 |

### `watermark` — 로고
| 항목 | 기본값 | 설명 |
|---|---|---|
| `file` | `null` | 투명 배경 PNG 권장 |
| `position` | `top-right` | 네 모서리 중 하나 |
| `scale` | 0.12 | 영상 가로폭 대비 로고 너비 비율 |
| `opacity` | 0.85 | |

### `hook` — 첫 화면 문구
| 항목 | 기본값 | 설명 |
|---|---|---|
| `text` | `""` | `--hook` 으로 덮어쓸 수 있음 |
| `start` / `duration` | 0.0 / 3.0 | 언제부터 몇 초간 |
| `font_size` | 96 | |
| `box` / `box_opacity` | false / 0.6 | 글씨 뒤 반투명 박스 |
| `position_y` | 0.18 | 화면 높이 대비 세로 위치 |

### 여러 언어 (한국어 / 일본어 / 영어)

줄바꿈은 글자 종류를 보고 알아서 갈립니다. 따로 켤 설정은 없습니다.

- **한국어·영어** — 단어(공백) 경계에서 끊습니다.
- **일본어·중국어** — 단어 사이에 공백이 없으므로 글자 단위로 끊되,
  **금칙처리(禁則処理)** 를 적용합니다. `、。？！」` 가 줄 맨 앞에 오지 않고,
  `「（` 가 줄 맨 끝에 오지 않으며, 문장부호만 홀로 남는 자막이 생기지 않습니다.
- **섞인 문장** — `日本語とEnglish` 처럼 섞여 있어도 영단어는 쪼개지 않습니다.

```bash
# 일본어 영상
python src/make.py --preset presets/ja-shorts.json --input 動画.mp4

# 다른 프리셋을 일본어로 돌릴 때
python src/make.py --preset presets/default.json --input 動画.mp4 \
  --set subtitle.language=ja --set subtitle.max_chars_per_line=12
```

> 일본어 자막에는 일본어 글자가 들어 있는 폰트가 필요합니다.
> `subtitle.font_file` 에 경로를 주는 편이 가장 확실합니다
> (예: Noto Sans JP, M PLUS, 源ノ角ゴシック).

---

## 6. 기본 제공 프리셋

| 파일 | 쓰임새 |
|---|---|
| `presets/default.json` | 세로 소스 그대로. 중앙 크롭 + 외곽선 자막 |
| `presets/landscape-to-shorts.json` | 가로 영상 → 쇼츠. 위아래 블러 여백 |
| `presets/bold-caption.json` | 정보성 클립. 큰 박스 자막 + 1.05배속 |
| `presets/ja-shorts.json` | 일본어 영상용. `language: ja`, 글자 단위 줄바꿈에 맞춘 값 |
| `presets/longform.json` | **롱폼(가로 1920×1080)**. 길이 제한 없음, 하단 자막, 단색 여백 |

내 스타일을 만들려면 하나를 복사해서 고치면 됩니다.

```bash
cp presets/default.json presets/내스타일.json
```

---

## 7. 동작 구조

```
make.py
 ├─ preset.py     프리셋 읽기 → 기본값 병합 → 검증 (오타·잘못된 값을 여기서 잡음)
 ├─ source.py     로컬 파일 확인 또는 yt-dlp 다운로드
 ├─ probe.py      ffprobe 로 해상도·길이·오디오 유무 확인
 ├─ subtitles.py  Whisper 인식 또는 SRT 파싱 → 읽기 좋은 길이로 묶기 → ASS 작성
 ├─ fonts.py      폰트 파일에서 패밀리 이름 읽기, 언어별 폰트 찾기
 └─ filters.py    필터그래프 + ffmpeg 명령 조립 → 한 번에 렌더링

studio.py         롱폼 스튜디오 (로컬 웹서버 + studio/index.html 화면)
 ├─ make.py       렌더링은 위 파이프라인을 그대로 실행
 ├─ thumbnail.py  장면 추출 + 글자 얹기 → 1280x720 JPG
 └─ uploader.py   업로드 정보 파일 작성 + 설정된 업로드 명령 실행
```

전 과정이 **ffmpeg 한 번 호출**로 끝나서 중간 파일이 쌓이지 않습니다.
임시 파일(자막 ASS, 다운로드 영상)은 작업 후 자동으로 지워집니다.

---

## 8. 문제 해결

**자막이 네모(□)로 나와요**
해당 언어 글자가 들어 있는 폰트가 없습니다. `assets/fonts/` 에 `.ttf` 를 넣고
`--set subtitle.font_file=assets/fonts/폰트.ttf` 로 지정하세요.
한글이면 Pretendard·나눔스퀘어, 일본어면 Noto Sans JP·M PLUS 등이 무난합니다.

**자동 자막이 틀려요**
`--set subtitle.whisper_model=medium` 으로 모델을 키우거나,
결과 SRT 를 손본 뒤 `--subs` + `--set subtitle.mode=file` 로 다시 돌리세요.

**자막이 화면 UI 에 가려요**
`--set subtitle.margin_v=560` 처럼 여백을 키우면 자막이 위로 올라옵니다.
쇼츠는 하단 200~400px 이 UI 에 가려지므로 `margin_v` 를 넉넉히 주세요.

**렌더링이 느려요**
`--set output.x264_preset=veryfast --set output.crf=23` 으로 속도를 올릴 수 있습니다.
자동 자막을 쓴다면 첫 실행 때 Whisper 모델 다운로드 시간이 따로 듭니다.

**`ffmpeg 를 찾을 수 없습니다`**
1번 설치 항목을 확인하세요. `ffmpeg -version` 이 동작해야 합니다.

---

## 9. 참고

- URL 입력은 본인이 권리를 가졌거나 이용이 허용된 영상에만 쓰세요.
- 폰트·배경음악도 상업적 이용 가능 여부를 확인하고 쓰세요.
- 업로드는 롱폼 스튜디오의 ‘업로드 설정’에 적은 외부 도구가 처리합니다. 이 프로그램이 직접 유튜브에 로그인하지는 않습니다.
