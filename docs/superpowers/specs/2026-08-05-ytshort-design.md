# ytshort — 유튜브 롱폼을 한국어 숏폼으로 만드는 멀티 에이전트 웹 데모

작성일: 2026-08-05

## 1. 목적

유튜브 외국어 영상에서 가장 많이 시청된 구간을 잘라내, 한국어 자막과 나레이션을 얹은 세로형 숏츠를 만든다. 화자의 원본 음성은 그대로 두고 나레이션만 합성 음성으로 삽입한다.

최종 산출물은 **Colab에서 실행되는 Gradio 웹 데모**다. 사용자는 브라우저에서 URL을 넣고, 자동 생성된 대본을 검토·수정한 뒤, 완성된 숏츠를 내려받는다.

## 2. 스코프

### 1차 구현 범위

- Gradio 2단계 웹 UI (입력 → 대본 편집 → 결과), Colab에서 `share=True`로 공개
- 임의의 유튜브 URL 입력, 다운로드 차단 시 파일 업로드 폴백
- `yt-dlp` heatmap 기반 하이라이트 구간 자동 선정
- WhisperX 전사 + pyannote 화자분리
- 지정한 말투로 한국어 번역
- 대사 사이 빈 구간에만 나레이션 삽입, 드립은 자막 전용
- 대본 단계 사람 검토 — 표에서 직접 편집하거나 자연어 피드백으로 되감기
- Typecast TTS 나레이션 합성
- 9:16 크롭 + 자막 번인 렌더링

### 1차에서 제외

| 항목 | 이유 | 처리 |
|---|---|---|
| heatmap 없는 영상 | 대체 하이라이트 판정을 2차로 미룸 | `UnsupportedVideo`로 명시적 중단 |
| 효과음 태깅 | STT의 영역이 아니라 별도 오디오 이벤트 분류 모델이 필요 | 미구현 |
| YouTube Analytics API | 채널 소유자만 조회 가능하므로 임의 URL 시나리오에 원천적으로 사용 불가 | 설계에서 배제 |
| 얼굴 추적 크롭 | 별도 모델 필요 | 블러 배경 레터박스로 대체 |
| 대본 비평 루프 | 실패 패턴을 관찰한 뒤 설계해야 유의미 | 2차 |
| 프리즈 프레임 연출 | 타임코드 재계산(EDL) 구조 필요 | 2차 |
| 사용자 인증, 작업 이력 | 데모 범위 밖 | 미구현 |

## 3. 설계 결정과 근거

| 결정 | 근거 |
|---|---|
| supervisor 라우팅이 아닌 선형 그래프 | 노드 순서가 고정되어 있어 매 스텝 LLM 라우팅은 비용만 늘리고 실패 지점을 만든다 |
| supervisor는 되감기 판단에만 사용 | 사람의 자연어 피드백은 미리 알 수 없고 해석이 필요하다. 라우터가 필요한 유일한 조건 |
| 나레이션을 빈 구간에만 삽입 | 대사와 겹치지 않고 영상 길이가 변하지 않아 타임코드가 불변. 덕킹 필터도 불필요 |
| 모든 시간은 클립 기준 상대 초 | 절대 시각이 섞이면 되감기 시 오프셋 버그가 난다 |
| `cues`를 단일 진실 원천으로 | 자막, TTS, 사람 검토, 웹 UI 표가 모두 같은 자료구조를 본다 |
| 웹에서도 `interrupt()`를 그대로 사용 | Gradio 콜백을 제너레이터로 만들면 스트리밍과 중단·재개가 자연스럽게 맞물린다 |
| Colab에서 Gradio 실행 | GPU가 Colab에만 있으므로 웹 서버도 같은 프로세스에 있어야 한다 |
| 로직을 노트북 밖 `.py`로 분리 | 셀에 로직이 들어가면 자동 테스트가 불가능해진다 |

## 4. 아키텍처

```
START
  → ingest       [fn]     영상과 메타데이터 확보
  → highlight    [fn]     heatmap으로 구간 선정, 클립과 오디오 추출
  → transcribe   [fn]     WhisperX 전사 + 정렬 + pyannote 화자분리
  → translate    [LLM]    말투 프롬프트를 적용한 한국어 번역
  → script       [AGENT]  빈 슬롯에 나레이션 배치, 드립 자막 작성
  → interrupt()           대본을 웹 UI로 내보내고 대기
  → tts          [AGENT]  Typecast 합성 후 길이 검증 재시도
  → subtitle     [fn]     .ass 자막 파일 생성
  → render       [fn]     9:16 크롭 + 오디오 믹싱 + 자막 번인
END
```

`interrupt()` 재개 시 `feedback_supervisor`가 동작한다. 사용자가 표를 직접 고쳤을 뿐이면 `tts`로 직진하고, 자연어 피드백이 있으면 `highlight` / `translate` / `script` / `tts` 중 하나로 되감는다.

`[fn]`은 LLM을 호출하지 않는 결정적 함수 노드다.

## 5. 상태 스키마

```python
from typing import Literal, TypedDict

class Segment(TypedDict):
    start: float          # 클립 시작점 기준 상대 초
    end: float
    speaker: str          # "SPEAKER_00", 화자분리 실패 시 "UNKNOWN"
    text: str

class Slot(TypedDict):
    id: str
    start: float
    end: float
    duration: float       # 나레이션이 쓸 수 있는 최대 길이
    max_chars: int        # duration에서 역산한 한국어 글자 수

class Cue(TypedDict):
    id: str
    kind: Literal["dialogue", "narration", "joke"]
    start: float
    end: float
    speaker: str | None   # dialogue만 사용
    text: str
    slot_id: str | None   # narration만 사용

class ClipState(TypedDict):
    # 입력
    url: str
    local_video: str | None
    tone: str                       # 번역과 나레이션에 적용할 말투 지시

    # ingest
    video_id: str
    title: str
    source_duration: float
    source_path: str
    heatmap: list[dict]             # [{start, end, value}]

    # highlight
    clip_start: float               # 원본 좌표를 아는 유일한 필드
    clip_end: float
    clip_path: str
    audio_path: str                 # 16kHz mono wav

    # transcribe / translate
    segments: list[Segment]         # 원어
    segments_ko: list[Segment]      # 번역. 타임코드 동일, text만 교체

    # script
    slots: list[Slot]
    cues: list[Cue]

    # HITL
    feedback: str | None
    directive: str | None           # supervisor가 대상 노드에 넘기는 구체 지시
    revision: int

    # tts
    narration_audio: list[dict]     # [{cue_id, path, duration}]

    # 산출물
    ass_path: str
    output_path: str
```

## 6. 노드 명세

### 6.1 ingest

`yt-dlp`로 영상과 메타데이터를 받는다. `--dump-json`의 `heatmap` 필드를 확인한다.

- heatmap이 없으면 `UnsupportedVideo`로 중단한다.
- 봇 차단(`Sign in to confirm you're not a bot`)이면 `DownloadBlocked`로 중단한다. UI는 파일 업로드를 안내한다.
- `local_video`가 주어지면 다운로드를 건너뛴다. 이 경우 heatmap이 없으므로 UI에서 받은 `clip_start` / `clip_end`를 사용하며, 지정이 없으면 `UnsupportedVideo`로 중단한다.

### 6.2 highlight

heatmap의 `value` 최고 지점을 중심으로 고정 25초 윈도우를 잡는다. 영상 경계를 벗어나면 안쪽으로 민다.

25초는 숏츠 흡입력과 처리 시간을 함께 노린 값이다. 클립이 짧을수록 전사가 빨라져 데모의 체감 대기 시간이 줄어든다. 대신 빈 슬롯 수도 줄어 나레이션은 1~2개가 현실적이다.

ffmpeg로 두 개를 추출한다.

- `clip_path` — 잘라낸 mp4
- `audio_path` — 16kHz mono wav

문장 중간에서 잘리는 문제는 1차에서 감수한다. 세그먼트 경계 스냅은 전사 이후에야 가능한데, 그러려면 클립을 두 번 자르는 구조가 되어 복잡도가 는다. 2차 개선 대상이다.

### 6.3 transcribe

WhisperX로 전사하고 강제 정렬한 뒤 pyannote로 화자를 붙인다. 언어는 자동 감지한다. 결과를 `Segment` 배열로 정규화한다. 모든 타임코드에서 `clip_start`를 빼 클립 상대 좌표로 바꾼다.

화자분리가 실패하면 전 세그먼트를 `speaker="UNKNOWN"`으로 두고 진행한다.

모델은 설정값으로 뺀다. Colab T4에서는 `large-v3`를 기본으로 하고, 로컬 등 VRAM이 부족한 환경에서는 `medium`이나 `small`로 낮춘다.

### 6.4 translate

`segments`를 한국어로 옮겨 `segments_ko`에 넣는다. 타임코드는 건드리지 않고 `text`만 교체한다. 세그먼트를 개별 호출하지 않고 전문을 한 번에 넘겨 문맥을 유지한다. 시스템 프롬프트에 `tone`을, `directive`가 있으면 그것도 이어 붙인다.

`tone`의 기본값은 자연스러운 대화 말투다.

> 친구에게 말하듯 자연스러운 구어체로 옮길 것. 반말을 쓴다. `~한다`, `~이다` 같은 문어체 종결어미와 번역 티가 나는 직역투를 피하고, 실제 대화에서 쓰는 어미를 쓴다. 과장된 유튜브 자막 말투나 인터넷 밈 남용은 하지 않는다.

같은 `tone`이 `script` 에이전트의 나레이션과 드립 작성에도 그대로 적용된다. 번역과 나레이션의 말투가 어긋나면 한 사람이 만든 영상처럼 들리지 않는다.

### 6.5 script (에이전트)

먼저 `compute_slots(segments_ko)`로 슬롯을 계산한다. 인접 세그먼트 간격이 `MIN_GAP = 1.2`초 이상이면 슬롯이다. 클립 시작 전과 마지막 세그먼트 이후 구간도 슬롯이 된다.

```
max_chars = int(duration * 5.5 * 0.85)
```

한국어 발화 속도를 초당 5.5음절로 두고 안전계수 0.85를 곱한다.

도구는 세 개다.

| 도구 | 반환 |
|---|---|
| `get_slots()` | 슬롯 목록. 각 슬롯의 시각, 길이, 최대 글자 수 |
| `get_transcript()` | 화자와 시각이 붙은 번역 대사 전문 |
| `search_context(query)` | 영상 속 인물, 밈, 사건의 배경 (Tavily) |

`search_context`는 외국 영상의 웃음 지점이 현지 맥락에 붙어 있는 경우를 위한 것이다. 이것이 없으면 나레이션이 자막을 되풀이하는 설명에 그친다.

출력은 `response_format`으로 `list[Cue]`를 강제한다. `dialogue` 큐는 `segments_ko`를 그대로 옮긴 것이고, 에이전트는 `narration`과 `joke`만 새로 만든다.

- `narration` — 반드시 슬롯 안에 들어가야 하며 `slot_id`를 갖는다. `max_chars`를 넘지 않는다.
- `joke` — 자막 전용이므로 슬롯 제약이 없다. 임의 시점에 배치할 수 있다. 표시 시간은 에이전트가 정하지 않고 시작 시각에서 `JOKE_DURATION = 2.5`초를 더해 계산한다. 클립 끝을 넘으면 클립 끝에서 잘린다.

### 6.6 interrupt와 feedback_supervisor

`interrupt(state["cues"])`로 대본을 UI에 내보내고 대기한다. 재개 값은 다음 형태다.

```python
{"cues": list[Cue], "feedback": str}
```

`cues`는 사용자가 표에서 직접 고친 결과이고, `feedback`은 자연어 요청이다. 둘 다 선택적이다.

`feedback`이 비어 있으면 supervisor를 호출하지 않고 곧바로 `tts`로 간다. 표만 고친 경우 되감을 이유가 없고, LLM 호출을 아낀다.

`feedback`이 있으면 supervisor가 판단한다.

```python
class Rewind(BaseModel):
    goto: Literal["highlight", "translate", "script", "tts", "FINISH"]
    reason: str
    directive: str
```

`directive`가 핵심이다. 되감기만 하면 같은 입력에 같은 프롬프트라 비슷한 결과가 나온다. 사용자의 자연어 불만을 대상 노드가 실행할 수 있는 지시로 번역해서 넘긴다.

> "말투가 너무 딱딱해" → `goto="translate"`, `directive="종결어미를 구어체로 바꾸고 문어체 표현을 피할 것. 반말 톤 유지."`

대상 노드는 자기 시스템 프롬프트 뒤에 `directive`를 이어 붙인다.

`revision`은 되감을 때마다 1 증가하며, 3에 도달하면 supervisor는 무조건 `FINISH`를 반환한다.

### 6.7 tts (에이전트)

`narration` 큐만 처리한다. `joke`와 `dialogue`는 건드리지 않는다.

| 도구 | 동작 |
|---|---|
| `synthesize(text, cue_id)` | Typecast 합성 요청 후 결과 URL을 폴링해 파일로 저장 |
| `measure(cue_id)` | 합성된 음성의 실제 길이와 슬롯 허용 길이를 함께 반환 |

루프는 `synthesize` → `measure` → 초과 시 에이전트가 직접 문장을 줄여 재합성이다. 글자 수를 기계적으로 자르면 문장이 깨지므로 축약은 LLM이 한다. 재시도는 2회까지, 그래도 안 맞으면 해당 나레이션을 폐기하고 로그를 남긴다.

`synthesize` 도구 안에 폴링과 타임아웃이 포함된다. Typecast는 비동기 API다.

보이스 액터 ID는 설정값으로 뺀다.

### 6.8 subtitle

`cues`에서 `.ass` 파일을 만든다. `kind`에 따라 스타일이 갈린다.

| kind | 위치 | 스타일 |
|---|---|---|
| `dialogue` | 하단 | 화자별 색상 구분 |
| `narration` | 하단 | 대사와 다른 색, 이탤릭 |
| `joke` | 상단 | 강조색, 굵게 |

폰트는 나눔고딕으로 명시한다.

### 6.9 render

세 가지를 한 번의 ffmpeg 호출로 처리한다.

1. 9:16 크롭 — 원본을 세로 캔버스 상단에 배치하고 배경은 원본을 확대·블러 처리해 채운다
2. 오디오 믹싱 — 원본 오디오 위에 나레이션을 각자의 슬롯 시각에 `adelay`로 배치해 `amix`
3. 자막 번인 — `.ass` 파일을 `subtitles` 필터로 굽는다

나레이션은 빈 구간에만 놓이므로 볼륨 덕킹이 필요 없다.

## 7. 웹 UI

### 7.1 화면 구성

**1단계 — 입력**

| 요소 | 설명 |
|---|---|
| URL 입력 | 유튜브 주소 |
| 파일 업로드 | URL과 나란히 배치. 다운로드 차단 시의 우회 경로이자 대안 입력 |
| 말투 | 드롭다운 프리셋과 자유 입력. `tone`으로 전달 |
| 시작 버튼 | |
| 진행 로그 | 노드 단위 진행 상황을 스트리밍 |

업로드 경로를 선택하면 클립 구간(시작·끝 초)을 직접 입력받는다. heatmap이 없기 때문이다.

**2단계 — 대본 편집**

| 요소 | 설명 |
|---|---|
| 대본 표 | `cues`를 편집 가능한 표로. 열은 시각, 종류, 화자, 텍스트 |
| 피드백 입력 | 자연어. 비워두면 supervisor를 건너뛴다 |
| 진행 버튼 | 편집 결과와 피드백을 `Command(resume=...)`로 반환 |
| 클립 미리보기 | 자막 없는 원본 클립. 대본을 판단할 근거 |

**3단계 — 결과**

완성된 mp4 재생과 다운로드. 폐기된 나레이션이 있으면 그 사실과 이유를 함께 보여준다.

### 7.2 그래프 구동 방식

Gradio 콜백을 제너레이터로 만들어 `graph.stream()`을 소비한다.

```python
def run(url, upload, tone, thread_id):
    config = {"configurable": {"thread_id": thread_id}}
    for chunk in graph.stream(payload, config=config, stream_mode="updates"):
        if "__interrupt__" in chunk:
            yield to_table(chunk["__interrupt__"][0].value), gr.update(visible=True)
            return
        yield progress_text(chunk), gr.update()
```

재개도 같은 `thread_id`로 `Command(resume=...)`를 넣어 다시 스트리밍한다. 되감기가 일어나면 그래프가 앞 노드로 돌아가 다시 `interrupt`에 도달하므로, 2단계 화면이 갱신된 대본으로 다시 뜬다. UI 쪽에는 되감기를 위한 별도 분기가 필요 없다.

`thread_id`는 세션마다 UUID로 만들어 `gr.State`에 담는다. 체크포인터가 두 호출을 잇는다.

### 7.3 동시성

T4 GPU는 하나뿐이라 두 사람이 동시에 전사를 돌리면 OOM이 난다.

```python
demo.queue(default_concurrency_limit=1)
```

대기열을 켜고 동시 실행을 1로 제한한다. 대기 중인 사용자에게는 Gradio가 자동으로 대기 순번을 보여준다.

## 8. 되감기 무효화 규칙

LangGraph 상태는 누적되므로, 되감을 때 하위 필드를 비우지 않으면 옛 데이터가 남아 수정이 반영되지 않은 것처럼 보인다.

| goto | 비우는 필드 |
|---|---|
| `highlight` | clip_start, clip_end, clip_path, audio_path, segments, segments_ko, slots, cues, narration_audio |
| `translate` | segments_ko, slots, cues, narration_audio |
| `script` | slots, cues, narration_audio |
| `tts` | narration_audio |

`invalidate_downstream(state, goto)`라는 순수 함수로 구현하고 테스트한다.

## 9. 에러 처리

### 치명적 — 즉시 중단

| 실패 | 예외 | UI 표시 |
|---|---|---|
| heatmap 없음 | `UnsupportedVideo` | 지원하지 않는 영상임을 알리고 파일 업로드를 권한다 |
| 봇 차단 | `DownloadBlocked` | 업로드 입력란을 강조한다 |
| 전사 실패 / OOM | `TranscriptionFailed` | 모델 크기를 낮춰 재시도하도록 안내한다 |

세 경우 모두 예외를 UI 로그 영역에 사람이 읽을 수 있는 한국어로 표시한다. 스택 트레이스를 그대로 노출하지 않는다.

### 부분적 — 저하시켜 진행

| 실패 | 처리 |
|---|---|
| pyannote 화자분리 실패 | 전 세그먼트 `speaker="UNKNOWN"` |
| Typecast 합성 실패 또는 타임아웃 | 해당 나레이션만 폐기 |
| 재시도 2회 후에도 슬롯 초과 | 해당 나레이션만 폐기 |
| 슬롯이 하나도 없음 | 나레이션 0개로 진행. 드립 자막은 그대로 출력 |

폐기된 나레이션은 결과 화면에 목록으로 보여준다. 조용히 사라지면 사용자는 버그로 인식한다.

마지막 항목이 "빈 구간에만 삽입" 방식의 안전장치다. 말이 빽빽한 영상에서는 나레이션이 들어가지 않지만, 드립 자막은 슬롯과 무관하므로 결과물은 나온다.

### 체크포인트

Drive에 `SqliteSaver`를 둔다. `interrupt` 재개에 필요하고, Colab 세션이 유휴 90분으로 끊겨도 STT부터 다시 돌리지 않아도 된다. T4에서 STT가 가장 오래 걸리는 구간이다.

### Colab 한글 폰트

기본 이미지에 한글 폰트가 없어 ffmpeg가 자막을 번인하면 전부 두부(□□□)로 나온다. 에러는 발생하지 않는다. 노트북 첫 셀에서 나눔고딕을 설치하고 `.ass` 스타일에 폰트명을 명시한다.

## 10. 프로젝트 구조

```
ytshort/
  ytshort/
    state.py            ClipState, Segment, Slot, Cue, invalidate_downstream
    slots.py            compute_slots, estimate_max_chars
    ass.py              cues_to_ass
    supervisor.py       Rewind, feedback_supervisor
    graph.py            그래프 조립
    config.py           모델명, 보이스 ID, MIN_GAP 등 설정값
    nodes/
      ingest.py
      highlight.py
      transcribe.py
      translate.py
      script.py
      tts.py
      subtitle.py
      render.py
  app.py                Gradio UI
  tests/
  notebooks/
    run.ipynb           Colab 실행 노트북
```

`run.ipynb`는 설치, Drive 마운트, 폰트 설치, `app.py` 실행만 담당한다. 로직이 셀에 들어가면 자동 테스트가 불가능해진다.

`app.py`는 UI 정의와 그래프 호출만 담당한다. 파이프라인 로직을 여기 넣지 않는다.

### config.py 설정값

| 이름 | 기본값 | 설명 |
|---|---|---|
| `CLIP_DURATION` | 25.0 | 하이라이트 클립 길이(초) |
| `MIN_GAP` | 1.2 | 슬롯으로 인정할 최소 공백(초) |
| `SYLLABLES_PER_SEC` | 5.5 | 한국어 발화 속도 |
| `CHAR_SAFETY` | 0.85 | 글자 수 안전계수 |
| `JOKE_DURATION` | 2.5 | 드립 자막 표시 시간(초) |
| `MAX_REVISION` | 3 | 되감기 허용 횟수 |
| `TTS_RETRY` | 2 | 나레이션 재합성 횟수 |
| `WHISPER_MODEL` | `large-v3` | Colab T4 기준. VRAM이 부족하면 낮춘다 |
| `TYPECAST_VOICE` | 미정 | 보이스 액터 ID. 실제 음성을 들어보고 정한다 |
| `DEFAULT_TONE` | 위 6.4의 문구 | 번역과 나레이션에 공통 적용 |

## 11. 테스트 전략

GPU와 유료 API에 걸려 있어 전 구간 자동 테스트는 불가능하다. 버그가 실제로 나는 곳에 집중한다.

### 1계층 — 순수 함수 pytest

외부 의존성이 0인 `slots.py`, `ass.py`, `state.py`에 테스트를 집중한다.

- `compute_slots` — 세그먼트 0개, 1개, 겹치는 세그먼트, 클립 앞뒤 여백, 간격이 `MIN_GAP` 경계에 정확히 걸리는 경우
- `estimate_max_chars` — 0초, 소수점 길이
- `cues_to_ass` — `H:MM:SS.cc` 타임코드 포맷, 특수문자 이스케이프, kind별 스타일 지정
- `invalidate_downstream` — 네 개 goto 각각에 대해 비워야 할 필드와 남아야 할 필드

### 2계층 — 스텁 통합 테스트

저장해둔 STT 결과 JSON을 픽스처로 두고 STT, LLM, TTS를 스텁으로 갈아끼운다. 나레이션은 무음 wav로 대체한다. GPU도 API 키도 없이 그래프 전체가 mp4까지 도는지 확인한다.

`interrupt` 중단과 `Command(resume=...)` 재개도 여기서 검증한다. 가짜 피드백을 주입하고 supervisor가 올바른 노드로 가는지, 무효화가 정확히 일어나는지, 되감기 후 다시 `interrupt`에 도달하는지 확인한다.

### 3계층 — 수동

실제 영상 1편을 웹 UI로 끝까지 돌려 확인한다. 자동화하지 않는다.

## 12. 환경과 의존성

- 실행 환경: Google Colab (T4 GPU)
- Python 3.12
- 외부 도구: `yt-dlp`, `ffmpeg`, 나눔고딕 폰트
- 파이썬 패키지: `gradio`, `langgraph`, `langchain`, `langchain-openai`, `langchain-tavily`, `whisperx`, `pyannote.audio`
- API 키: `OPENAI_API_KEY`, `TAVILY_API_KEY`, `TYPECAST_API_KEY`, HuggingFace 토큰(pyannote)
- pyannote 모델은 HuggingFace에서 사용 조건에 동의해야 다운로드된다

## 13. 2차 확장 계획

우선순위 순이다.

1. heatmap이 없는 영상 지원 — STT 텍스트와 오디오 에너지를 근거로 LLM이 하이라이트 구간 판정
2. 클립 경계를 세그먼트 경계에 스냅
3. 대본 비평 루프 — 1차에서 관찰한 실패 패턴을 근거로 감독 에이전트 설계
4. 프리즈 프레임 연출 — EDL 레이어를 얹어 타임코드 재계산
5. 효과음 태깅 — 오디오 이벤트 분류 모델
6. 얼굴 추적 크롭

## 14. 리스크

| 리스크 | 영향 | 대응 |
|---|---|---|
| Colab 세션이 끊기면 데모 링크가 즉시 죽음 | 발표 도중 중단 | 발표 직전에 실행하고 탭을 방치하지 않는다. 체크포인터로 재시작 시 복구 |
| Colab에서 유튜브 다운로드가 상시 차단 | ingest가 항상 실패 | 업로드가 사실상 기본 경로가 된다. UI에 처음부터 나란히 배치 |
| 전체 처리 시간이 수 분 | 데모 흐름이 늘어짐 | 미리 처리해둔 결과를 예비로 준비한다. 진행 로그를 스트리밍해 멈춘 것처럼 보이지 않게 한다 |
| 말이 빽빽한 영상에서 슬롯이 거의 없음 | 나레이션이 시스템의 핵심인데 거의 안 들어감. 클립이 25초로 짧아 더 첨예하다 | 드립 자막으로 대체. 데모용 영상은 슬롯이 확보되는 것으로 미리 검증. 슬롯이 0개면 결과 화면에 그 사실을 표시 |
| tts 에이전트 턴 수 과다 | 나레이션 10개 기준 LLM 턴 20~30회, 느림 | 합성·측정을 결정적 루프로 내리고 축약할 때만 LLM 호출 |
| T4 동시 사용 시 OOM | 여러 명이 접속하면 실패 | `queue(default_concurrency_limit=1)` |
| Typecast 음성이 한국어 구어체 드립에 안 어울림 | 나레이션 품질 저하 | 보이스 액터를 설정값으로 빼 교체 가능하게 |
| 저작권 | 배포 시 문제 | 학습과 포트폴리오 목적으로 한정 |
