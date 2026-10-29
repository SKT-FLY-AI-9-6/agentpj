# Reaction Factory — 번역·나레이션 클립 자동 생산 라인

긴 해외 영상 URL 하나를 넣으면 시청자가 **가장 많이 다시 본 구간**을 찾아 잘라내고,
화자별 받아쓰기 → 말투 지정 번역 → 나레이션·드립 작성 → TTS → 3종 자막 → 세로 쇼츠 mp4 까지
자동으로 만드는 LangGraph 파이프라인과 웹 GUI.

- **과정**: SKT FLY AI 9기 프로젝트
- **담당**: [@hoonseung2](https://github.com/hoonseung2) — 기획, LangGraph 파이프라인, 백엔드(FastAPI·SSE), 프론트엔드(React) 전체
- **스택**: Python · LangGraph · LangChain · FastAPI · yt-dlp · WhisperX / faster-whisper · Typecast / edge-tts · ffmpeg(libass) · React + Vite

```
fetcher ─▶ highlighter ─▶ clip_gate ─▶ transcriber ─▶ translator
영상확보    하이라이트      구간확정      STT·화자분리     말투번역
                          (사람 개입)
                             │
                             ▼
        scripter ─▶ voicer ─▶ subtitler ─▶ renderer ─▶ 완성
     나레이션·드립   TTS·배치     3종 자막    믹싱·합성
```

---

## 1. 설계에서 중요하게 본 것

**① 9단계 노드 + 사람 개입 한 곳**
노드 9개를 순서대로 잇는 선형 `StateGraph`로 구성했다. 각 노드는 공유 상태(`ClipState`)를 읽고 자기 결과만 덧붙인다.
결과물 품질을 가장 크게 좌우하는 건 "어느 구간을 자르느냐"라서, 그 지점(`clip_gate`)에만 LangGraph `interrupt()`로 사람이 확인하는 단계를 넣었다.
나머지는 자동으로 흘러가고, `REVIEW_CLIP_SELECTION=false`로 두면 이 게이트도 건너뛴다.

**② 외부 의존성마다 폴백**
API 키나 데이터가 없어도 파이프라인이 멈추지 않도록 단계마다 대체 경로를 뒀다.

| 단계 | 기본 | 없을 때 |
|---|---|---|
| 하이라이트 | 유튜브 heatmap | 오디오 RMS 에너지 → 도입부 |
| STT | WhisperX(화자분리) | faster-whisper → 유튜브 자막 재활용 |
| 나레이션 TTS | Typecast API | edge-tts(무료) |
| 나레이션 배치 | 원본 볼륨 덕킹 | 1.25배속 압축 → 프레임 정지 삽입 |

**③ 진행 상황을 실시간으로**
영상 처리는 수십 초~수 분이 걸려서, 각 노드가 이벤트를 내보내고 FastAPI가 **SSE**로 프론트에 전달한다.
사용자는 히트맵, 번역, 연출 타임라인이 채워지는 과정을 단계별로 볼 수 있다.

---

## 2. 각 노드가 하는 일

### ① fetcher — 영상 확보
`yt-dlp`로 메타데이터·heatmap·자막 트랙만 먼저 가져온다. 영상 본체는 구간이 정해진 뒤에 받는다.
heatmap이 없는 영상일 때만 오디오를 먼저 받아 분석에 쓴다.

### ② highlighter — 하이라이트 추출
처음에는 YouTube Analytics API의 시청 지속률(`audienceWatchRatio`)을 쓰려 했지만,
이 API는 **본인 소유 채널의 영상만** 조회할 수 있어서 남의 영상에는 쓸 수 없었다. 그래서 신호를 세 단계로 나눴다.

| 순위 | 신호 | 설명 |
|----|------|------|
| 1 | **yt-dlp `heatmap`** | 유튜브 '가장 많이 다시 본 장면' 그래프. `{start_time, end_time, value}` 약 100구간. 조회수가 어느 정도 있는 영상이면 대부분 존재 |
| 2 | **오디오 RMS 에너지** | heatmap이 없을 때. 웃음·환호·큰 소리 구간이 높게 나온다 |
| 3 | 도입부 폴백 | 둘 다 없을 때 |

길이 L 슬라이딩 윈도우의 점수 합이 최대인 구간을 겹치지 않게 N개 뽑고, 자막이 있으면 LLM이 후보별 대사를 읽고 최종 1개를 고른다.
본인 채널 영상이라면 `tools/highlight.py`에 Analytics API 기반 함수를 추가해 1순위로 넣을 수 있다. 반환 형식이 heatmap과 같은 `(t, score)` 곡선이라 그대로 붙는다.

### ③ clip_gate — 구간 확정 (사람 개입)
`interrupt()`로 그래프를 멈추고, 웹 화면의 **히트맵 그래프**에서 후보를 클릭하거나 드래그로 구간을 직접 지정한다.
확정하면 체크포인트에서 그래프를 재개하고 그 구간 영상을 받는다.

### ④ transcriber — STT + 화자분리
WhisperX(전사 → wav2vec 강제정렬 → pyannote 화자분리)로 화자별 대사를 뽑고,
LLM이 `SPEAKER_00` 같은 익명 라벨에 '진행자', '친구' 같은 호칭을 붙인다.
동시에 **대사가 없는 빈 구간(gap)** 목록을 계산한다. 나레이션이 들어갈 자리다.

| STT_PROVIDER | 화자분리 | 설치 | 용도 |
|---|---|---|---|
| `whisperx` | ✅ | torch + whisperx + pyannote | 실사용 |
| `faster-whisper` | ❌ | faster-whisper | GPU가 없을 때 |
| `subtitles` | ❌ | 없음 | 유튜브 자막 재활용 (기본값, 테스트·오프라인) |

### ⑤ translator — 번역
40줄씩 묶어 병렬로 번역한다. 화자별 말투를 일관되게 유지하고, 자막 한 줄이 20자 안팎이 되도록 압축한다.
말투는 실행할 때마다 웹에서 지정한다 (예: "반말 위주, 인터넷 방송 자막 톤, 욕설은 순화").

### ⑥ scripter — 나레이션·드립 작성
전체 대사 타임라인과 **실제 빈 구간 목록**을 LLM에 주고 두 가지를 만든다.

- **나레이션**: TTS 음성으로 들어간다. 빈 구간 안에서만 시작하도록 지시하고, 그래도 구간 밖으로 나가면 코드가 가장 가까운 빈 구간으로 옮긴다. LLM 출력을 그대로 믿지 않고 코드로 한 번 더 보정하는 구조다.
- **드립**: 화면에만 뜨는 자막. 음성은 없고 대사 위에 겹쳐도 된다. `gag_level` 0~3으로 개수를 조절하고, 0이면 만들지 않는다.

### ⑦ voicer — 나레이션 음성
**Typecast API**로 나레이션만 합성한다. 감정 프리셋(normal/happy/whisper 등)과 배속을 넘기고, 키가 없으면 edge-tts로 자동 전환한다.
드립은 음성을 만들지 않고, 화자 대사는 원본 오디오를 그대로 쓴다.

합성한 음성이 빈 구간보다 조금 길면 1.25배속까지 압축하고, 그래도 안 되면 프레임 정지 삽입으로 바꾼다.

| NARRATION_MODE | 동작 |
|---|---|
| `duck` | 나레이션 구간에서 원본 볼륨을 `DUCK_LEVEL`로 낮춤. 영상 길이 유지 |
| `freeze` | 나레이션마다 화면을 정지시키고 끼워 넣음. 영상이 길어짐 |
| `auto` | 빈 구간이 충분하면 duck, 모자라면 freeze (기본값) |

### ⑧ subtitler — 자막
`libass`용 **ASS** 파일을 만든다. 레이아웃은 한국 리액션 쇼츠에서 흔한 '검은 레터박스 + 상단 고정 타이틀' 형태를 따랐다.

```
┌──────────────────┐
│   무모한 실험이      │ ← 상단 고정 타이틀 (영상 내내)
│   성공한다면        │
├──────────────────┤
│      (친구)        │ ← 라벨·드립
│                  │
│     원본 영상       │
│                  │
│  자, 이거 봐봐.      │ ← 화자 대사 (화자별 색)
├──────────────────┤
│  이 실험, 세 번째…   │ ← 나레이션
│   © 채널명         │ ← 출처 표기
└──────────────────┘
```

- **화자 대사**: 이름 대신 화자별 색으로 구분한다 (노랑 → 흰색 → 하늘 → 연두 순 자동 배정).
- **긴 대사 분할**: 2~3어절씩 끊어 넘긴다. 단어 타임스탬프가 없으면 글자 수에 비례해 시간을 나누되, `SUBTITLE_MIN_DURATION`보다 짧아지지 않게 한다.
- **위치**: 최종 프레임에서 영상이 차지하는 세로 범위를 계산해 자막을 영상 안쪽 가장자리에 붙인다.
- **시간축 보정**: 프레임 정지가 들어가면 시간이 밀리므로, 모든 시각을 `원본 클립 시간 → 최종 영상 시간`으로 변환한 뒤 찍는다.

### ⑨ renderer — 최종 합성
1. 정지 삽입 구간을 만든 뒤 concat
2. 나레이션 트랙을 `adelay`로 배치하고, 원본은 `volume` 필터의 timeline `enable`로 덕킹
3. 세로 리프레임 (끄면 원본 비율 유지)
4. `ass` 필터로 자막을 입혀 `final.mp4`와 썸네일 생성

---

## 3. 트러블슈팅 — 구간 다운로드가 느렸던 문제

처음에는 "필요한 구간만 받으면 빠르겠다"고 생각해 yt-dlp의 `download_ranges`를 썼는데, 30초 클립 하나에 수백 초가 걸렸다.
yt-dlp 소스를 따라가 보니 원인은 다운로더 선택에 있었다.

1. 구간(`section_start`/`section_end`)이 지정되면 yt-dlp는 다른 판단보다 먼저 **`FFmpegFD`** 를 다운로더로 고른다 (`yt_dlp/downloader/__init__.py`).
2. 유튜브 추출기는 모든 포맷에 **10MiB 단위로 끊어 받으라**는 힌트(`http_chunk_size`)를 붙인다. 기본 다운로더(`HttpFD`)는 이 힌트대로 Range 요청을 나눠 보내고, 이게 유튜브 속도 제한을 피하는 방법이다.
3. 그런데 `FFmpegFD`는 이 힌트를 읽지 않는다. 결국 ffmpeg가 한 번에 스트리밍하면서 속도 제한에 그대로 걸렸고, `force_keyframes_at_cuts` 재인코딩까지 겹쳐 더 느려졌다.

**해결**
- 기본값을 `DOWNLOAD_SECTIONS_ONLY=false`로 바꿨다. 전체를 기본 다운로더로 빠르게 받고 로컬에서 정확히 자른다.
- `SECTION_DOWNLOAD_OVER_MINUTES`(기본 45분)를 넘는 긴 영상만 구간 다운로드로 자동 전환한다.
- 포맷은 **h264(avc1) 우선**으로 골랐다. AV1/VP9는 디코딩이 몇 배 느려 자르기·합성도 느려진다.
- 구간 다운로드를 쓸 때는 재인코딩 프리셋을 `veryfast`로 넘긴다 (yt-dlp 기본은 `medium`).
- heatmap이 없어 오디오 분석이 필요할 때는 오디오만 받는다.

**그래도 느리면**
1. `MAX_HEIGHT=720`: 받을 용량이 절반 이하로 준다.
2. `YTDLP_FAST_CUT=true`: 재인코딩 없이 스트림 복사로 자른다. 시작점이 키프레임까지 몇 초 앞당겨져 `STT_PROVIDER=subtitles`에서는 자막이 밀리므로 WhisperX를 쓸 때만 켠다.
3. `aria2c`를 설치하고 `tools/source.py`의 `_base_opts`에 `external_downloader='aria2c'`를 넣으면 병렬 다운로드로 더 빨라진다.

---

## 4. 실행

### 준비물

| 항목 | 필수 | 비고 |
|---|---|---|
| Python 3.10+ / Node 18+ | ✅ | |
| ffmpeg (libass 포함) | ✅ | `brew install ffmpeg` / `sudo apt install ffmpeg` |
| LLM API 키 | ✅ | OpenAI 또는 Anthropic |
| Typecast API 키 | ⬜ | 없으면 edge-tts로 대체 |
| HuggingFace 토큰 | ⬜ | 화자분리(pyannote)를 쓸 때만 |
| 한글 폰트 | ⬜ | 없으면 시스템에서 자동 탐색 |

### 설치

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

# 화자분리까지 쓰려면 (용량 큼, GPU 권장)
pip install -r backend/requirements-stt.txt

cd frontend && npm install && cd ..
```

저장소 루트(또는 `backend/`)에 `.env`를 만들고 최소한 LLM 키를 넣는다.

```bash
OPENAI_API_KEY=sk-...
```

### 실행

```bash
./run.sh
# 또는 따로:
#   cd backend && uvicorn app.main:app --reload --port 8000
#   cd frontend && npm run dev
```

[http://localhost:5173](http://localhost:5173) 접속. 프론트를 `npm run build` 해 두면 `http://localhost:8000` 하나로도 된다.

### 화면 사용 흐름
1. 왼쪽에 **영상 URL**, 번역 말투, 나레이터 캐릭터, 드립 강도, 클립 길이를 넣고 시작
2. **구간 탭**: '가장 많이 다시 본' 곡선과 후보 구간 확인 → 클릭하거나 드래그해서 **이 구간으로 확정**
3. **대사·번역 탭**: 화자별 원문과 번역 비교
4. **연출 탭**: 대사 / 나레이션 / 드립 3트랙 타임라인. 나레이션마다 `덕킹` / `정지 삽입` 표시
5. **결과 탭**: 완성 영상 미리보기, mp4 / SRT / ASS / 썸네일 다운로드

산출물은 `output/<job_id>/`에 남는다.

```
output/<job_id>/
├── final.mp4        최종 영상
├── clip.mp4         자른 원본 구간
├── subtitles.ass    3종 스타일 자막 (편집기에서 수정 가능)
├── subtitles.srt    대사 + 나레이션 통합 자막
├── thumbnail.jpg
└── narration/       나레이션 음성 wav
```

ASS를 Aegisub 등에서 고친 뒤 다시 입히려면 `ffmpeg -i clip.mp4 -vf "ass=subtitles.ass" out.mp4`로 재렌더하면 된다.

---

## 5. 주요 환경변수

전체 목록과 기본값은 `backend/app/config.py`에 있다.

```bash
LLM_MODEL=openai:gpt-4o-mini      # anthropic:... 도 가능
LLM_WRITER_MODEL=                 # 번역·드립만 큰 모델로 쓰고 싶을 때
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

STT_PROVIDER=subtitles            # whisperx | faster-whisper | subtitles
WHISPER_MODEL=large-v3
HF_TOKEN=                         # 화자분리용

TTS_PROVIDER=edge                 # typecast | edge | none
TYPECAST_API_KEY=
TYPECAST_VOICE_ID=                # /api/voices 로 조회

NARRATION_MODE=auto               # duck | freeze | auto
DUCK_LEVEL=0.25
MAX_NARRATIONS=6
MAX_GAGS=8

VERTICAL_REFRAME=true             # 1080x1920 세로 변환
SUBTITLE_SIZE=106
REVIEW_CLIP_SELECTION=true        # 구간 선택 게이트

DOWNLOAD_SECTIONS_ONLY=false      # 3장 트러블슈팅 참고
SECTION_DOWNLOAD_OVER_MINUTES=45
MAX_HEIGHT=1080
YTDLP_FAST_CUT=false
YTDLP_COOKIES_FROM_BROWSER=       # 로그인이 필요한 영상: chrome / firefox / edge
```

---

## 6. 테스트

LLM·STT 없이 전체 흐름을 검증하는 스모크 테스트. 하이라이트 탐색·TTS·자막·ffmpeg 합성은 실제로 돌려서 `final.mp4`까지 만든다.

```bash
cd backend && python -m tests.test_pipeline
```

```
✓ 구간 선택 게이트 진입 — 후보 2개 (전략=audio)
✓ resume 후 끝까지 완주
✓ 번역 8줄 · 화자 2명
✓ 나레이션 3개 (모드: duck) +0초
✓ 자막 ASS: 대사 8 / 나레이션 3 / 드립 3 (1080x1920)
✓ 최종 영상: output/xxxx/final.mp4 (50.0초 / 15.8MB / 1080x1920)
모든 스모크 테스트 통과
```

URL 자리에 로컬 파일 경로를 넣으면 yt-dlp 없이도 파이프라인 전체가 돈다 (오프라인 개발용).

---

## 7. API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/health` | 키·설정 상태 |
| GET | `/api/graph` | 노드 목록 + mermaid |
| GET | `/api/voices` | Typecast/edge 보이스 목록 |
| POST | `/api/jobs` | 작업 생성·실행 |
| GET | `/api/jobs` | 작업 목록 |
| GET | `/api/jobs/{id}` | 작업 상태 |
| GET | `/api/jobs/{id}/stream` | **SSE** 실시간 이벤트 |
| POST | `/api/jobs/{id}/clip` | 구간 확정 후 그래프 재개 |
| POST | `/api/jobs/{id}/cancel` | 중단 |
| GET | `/api/artifacts/{id}/{file}` | mp4 / ass / srt / 썸네일 |

SSE `kind`: `status` `node` `log` `source` `candidates` `clip_selection_required` `clip` `transcript` `translated` `script` `narration` `subtitle` `render` `result`

---

## 8. 구조

```
.
├── backend/app/
│   ├── main.py          FastAPI + SSE
│   ├── runner.py        작업 실행 / interrupt 재개
│   ├── events.py        contextvar 기반 이벤트 버스
│   ├── config.py        환경변수 설정
│   ├── schemas.py       구조화 출력 스키마
│   ├── graph/
│   │   ├── builder.py   StateGraph 조립
│   │   ├── state.py     공유 상태
│   │   └── nodes/       9개 노드
│   └── tools/
│       ├── source.py    yt-dlp (메타·heatmap·자막·다운로드)
│       ├── highlight.py heatmap / 오디오 에너지 → 후보 구간
│       ├── stt.py       WhisperX / faster-whisper / 자막
│       ├── tts.py       Typecast / edge-tts
│       ├── llm.py       LLM 팩토리 + 구조화 출력
│       ├── subtitle.py  ASS 3종 스타일
│       ├── render.py    정지 삽입 · 덕킹 · 리프레임 · 자막 입히기
│       ├── media.py     ffmpeg/ffprobe 공통
│       └── fonts.py     한글 폰트 탐색
├── frontend/            React + Vite (히트맵 차트 · 타임라인)
└── output/
```

---

## 9. 한계와 다음 단계

- **작업 상태가 메모리에만 있다.** 체크포인터가 `InMemorySaver`라서 서버를 재시작하면 진행 중 작업과 이력이 사라진다. `SqliteSaver` 등으로 영속화가 필요하다.
- **작업을 하나의 서버 프로세스가 직접 실행한다.** STT·렌더링처럼 무거운 단계는 작업 큐와 별도 워커(GPU/CPU)로 분리하는 게 맞다.
- **여러 클립 동시 생산**: `clip_gate`를 끄고 후보 개수만큼 작업을 만들면 한 영상에서 N개를 병렬로 뽑을 수 있다.
- **효과음 자막**: `tools/stt.py`에 오디오 이벤트 태깅 백엔드를 붙이면 `(웃음)` `(박수)`를 자동으로 넣을 수 있다.

---

## 10. 주의

- **저작권**: 남의 영상을 잘라 재가공하는 파이프라인이다. 원저작자 허락, 출처 표기, 변형 정도(해설·번역·비평의 비중)에 따라 적법성이 달라진다. 자동 업로드 기능을 넣지 않은 것도 이 때문이고, 올리기 전에는 사람이 판단해야 한다.
- yt-dlp로 다운로드하는 것 자체가 유튜브 서비스 약관에 저촉될 수 있다.
- **드립 자막**은 LLM이 쓴다. 인신공격·비하는 프롬프트로 막았지만 완전하지 않으므로, 업로드 전에 연출 탭에서 직접 확인해야 한다.
- 화자분리(pyannote)는 HuggingFace에서 모델 약관 동의가 필요하다.
