"""나레이션 TTS — Typecast 우선, edge-tts 폴백.

Typecast API
  POST https://api.typecast.ai/v1/text-to-speech   (헤더 X-API-KEY, 응답=오디오 바이너리)
  GET  https://api.typecast.ai/v2/voices           (보이스 목록)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from ..config import settings
from ..events import log

TYPECAST_TTS = "https://api.typecast.ai/v1/text-to-speech"
TYPECAST_VOICES = "https://api.typecast.ai/v2/voices"

# ISO 639-1 → Typecast 가 쓰는 ISO 639-3
LANG3 = {"ko": "kor", "en": "eng", "ja": "jpn", "zh": "cmn", "es": "spa", "de": "deu", "fr": "fra"}


async def synthesize(
    text: str,
    out_path: Path,
    *,
    emotion: str = "",
    tempo: float = 1.0,
    voice_id: str = "",
) -> float:
    """나레이션 한 줄을 wav 로 만들고 길이(초)를 반환."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = (text or "").strip()
    if not text:
        return 0.0

    provider = (settings.tts_provider or "edge").lower()

    if provider == "typecast" and settings.typecast_api_key:
        if await _typecast(text, out_path, emotion, tempo, voice_id):
            return await duration(out_path)
        log("Typecast 실패 → edge-tts 로 폴백", level="warn")
        provider = "edge"

    if provider == "edge":
        if await _edge(text, out_path, tempo):
            return await duration(out_path)
        log("edge-tts 실패 → 무음 트랙으로 대체", level="warn")

    return await _silence(text, out_path)


async def _typecast(
    text: str, out_path: Path, emotion: str, tempo: float, voice_id: str
) -> bool:
    voice = voice_id or settings.typecast_voice_id
    if not voice:
        log("TYPECAST_VOICE_ID 가 비어 있습니다. /api/voices 로 확인하세요.", level="warn")
        return False

    body = {
        "voice_id": voice,
        "text": text[:2000],
        "model": settings.typecast_model,
        "language": LANG3.get(settings.target_language, "kor"),
        "prompt": {
            "emotion_type": "preset",
            "emotion_preset": emotion or settings.typecast_emotion or "normal",
            "emotion_intensity": settings.typecast_emotion_intensity,
        },
        "output": {
            "audio_format": "wav",
            "volume": 100,
            "audio_pitch": 0,
            "audio_tempo": round(max(0.5, min(2.0, tempo)), 2),
        },
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                TYPECAST_TTS,
                headers={"X-API-KEY": settings.typecast_api_key,
                         "Content-Type": "application/json"},
                json=body,
            )
            r.raise_for_status()
            if r.headers.get("content-type", "").startswith("application/json"):
                log(f"Typecast 응답이 오디오가 아닙니다: {r.text[:200]}", level="warn")
                return False
            out_path.write_bytes(r.content)
        return out_path.stat().st_size > 1000
    except Exception as exc:  # noqa: BLE001
        log(f"Typecast 오류: {exc}", level="warn")
        return False


async def _edge(text: str, out_path: Path, tempo: float) -> bool:
    try:
        import edge_tts
    except ImportError:
        return False
    # tempo 1.15 → "+15%"
    rate = f"{int(round((tempo - 1) * 100)):+d}%"
    try:
        comm = edge_tts.Communicate(text, settings.edge_voice, rate=rate)
        tmp = out_path.with_suffix(".mp3")
        await comm.save(str(tmp))
        if out_path.suffix != ".mp3":
            from . import media

            await media.run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(tmp),
                "-ar", "48000", "-ac", "2", str(out_path),
            ], label="TTS 변환")
            tmp.unlink(missing_ok=True)
        return out_path.exists() and out_path.stat().st_size > 1000
    except Exception as exc:  # noqa: BLE001
        log(f"edge-tts 오류: {exc}", level="warn")
        return False


async def _silence(text: str, out_path: Path) -> float:
    """TTS 불가 시 텍스트 길이에 비례한 무음. 파이프라인 검증용."""
    dur = max(1.5, round(len(text) / 6.2, 1))
    from . import media

    await media.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-t", f"{dur:.2f}", str(out_path),
    ], label="무음 생성")
    return dur


async def duration(path: Path) -> float:
    from . import media

    info = await media.probe(path)
    return round(info.get("duration", 0.0), 3)


async def list_voices() -> list[dict]:
    if (settings.tts_provider or "").lower() == "typecast" and settings.typecast_api_key:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    TYPECAST_VOICES, headers={"X-API-KEY": settings.typecast_api_key}
                )
                r.raise_for_status()
                data = r.json()
            items = data if isinstance(data, list) else data.get("voices", data.get("result", []))
            return [
                {"id": v.get("voice_id") or v.get("id"),
                 "name": v.get("voice_name") or v.get("name", ""),
                 "provider": "typecast"}
                for v in items
            ]
        except Exception as exc:  # noqa: BLE001
            log(f"Typecast 보이스 목록 실패: {exc}", level="warn")

    try:
        import edge_tts

        voices = await edge_tts.list_voices()
        return [
            {"id": v["ShortName"], "name": v["ShortName"], "provider": "edge"}
            for v in voices if v["ShortName"].startswith("ko-KR")
        ]
    except Exception:  # noqa: BLE001
        return [{"id": settings.edge_voice, "name": settings.edge_voice, "provider": "edge"}]


async def sleep_backoff(i: int) -> None:
    await asyncio.sleep(min(2 ** i * 0.4, 4))
