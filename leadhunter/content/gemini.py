"""Google Gemini: картинки (Nano Banana), видео (Omni, преемник Veo), озвучка (TTS) через Interactions API.
Модели и формат запросов сверены с ai.google.dev на 09.11.2026. Без ключа все методы возвращают None."""
import asyncio
import base64
import logging
import re
import struct

import httpx

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_BASE = f"{API}/openai/"

TTS_FALLBACK = "gemini-3.8-flash-lite-tts"  # тоже бесплатный, отдельная квота

VIDEO_MIN_SECONDS, VIDEO_DEFAULT_SECONDS, VIDEO_MAX_SECONDS = 3, 8, 15  # Omni 1.2 Flash: клипы до 15 с

DEFAULTS = {
    "image_model": "gemini-nano-banana-2.1",  # платный тариф; бюджетный вариант — gemini-3.1-flash-lite-image
    "video_model": "gemini-omni-1.2-flash",   # Veo 3.1 preview в Gemini API закрыт 22.10.2026; 3–15 с, 9:16, звук
    "tts_model": "gemini-3.8-flash-tts",      # есть бесплатный тариф
    "llm_model": "gemini-3.5-flash-lite",     # для LLMClassifier: бесплатный лимит щедрее, чем у 3.8-flash, и без перегрузок
}


class GeminiError(RuntimeError):
    pass


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def gemini_openai_base_url() -> str:
    """База OpenAI-совместимого API: с ней LLMClassifier работает на том же ключе Gemini."""
    return OPENAI_BASE


def is_gemini_key(key: str) -> bool:
    return (key or "").strip().startswith("AIza")


def pcm_to_wav(pcm: bytes, rate: int = 24000, channels: int = 1, bits: int = 16) -> bytes:
    block = channels * bits // 8
    header = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " +
              struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block, block, bits) +
              b"data" + struct.pack("<I", len(pcm)))
    return header + pcm


def _error_message(r: httpx.Response) -> str:
    try:
        msg = r.json()["error"]["message"]
        if msg:
            return str(msg)
    except Exception:
        pass
    return r.text[:300] or r.reason_phrase


def _parts(data: dict, kind: str) -> list[dict]:
    """Блоки нужного типа из model_output-шагов ответа Interactions API."""
    out = []
    for step in data.get("steps") or []:
        if step.get("type") == "model_output":
            out += [c for c in step.get("content") or [] if c.get("type") == kind]
    return out


def _texts(data: dict) -> str:
    return " ".join(c.get("text", "") for c in _parts(data, "text")).strip()


class GeminiMedia:
    DEFAULTS = DEFAULTS

    def __init__(self, api_key: str | None, *, image_model: str = DEFAULTS["image_model"],
                 video_model: str = DEFAULTS["video_model"], tts_model: str = DEFAULTS["tts_model"],
                 tts_voice: str = "Kore", client: httpx.AsyncClient | None = None):
        self.api_key = (api_key or "").strip()
        self.image_model = image_model
        self.video_model = video_model
        self.tts_model = tts_model
        self.tts_voice = tts_voice
        self.client = client or httpx.AsyncClient(timeout=180)

    @property
    def images_enabled(self) -> bool:
        return bool(self.api_key and self.image_model)

    @property
    def video_enabled(self) -> bool:
        return bool(self.api_key and self.video_model)

    @property
    def tts_enabled(self) -> bool:
        return bool(self.api_key and self.tts_model)

    async def _request(self, method: str, url: str, *, timeout: float | None = None, **kw) -> httpx.Response:
        headers = {"x-goog-api-key": self.api_key}
        if timeout:
            kw["timeout"] = timeout
        r = await self.client.request(method, url, headers=headers, **kw)
        if r.status_code >= 400:
            raise GeminiError(f"Gemini {r.status_code}: {_error_message(r)}")
        return r

    async def _interact(self, model: str, input_, response_format: dict, *, timeout: float | None = None,
                        extra: dict | None = None) -> dict:
        body = {"model": model, "input": input_, "response_format": response_format, **(extra or {})}
        r = await self._request("POST", f"{API}/interactions", json=body, timeout=timeout)
        data = r.json()
        if data.get("status") == "failed":
            raise GeminiError(f"Gemini: {(data.get('error') or {}).get('message') or 'генерация не удалась'}")
        return data

    async def image(self, prompt: str, aspect: str = "1:1") -> bytes | None:
        if not self.images_enabled or not prompt.strip():
            return None
        data = await self._interact(
            self.image_model, [{"type": "text", "text": prompt[:3000]}],
            {"type": "image", "mime_type": "image/jpeg", "aspect_ratio": aspect, "image_size": "1K"})
        imgs = _parts(data, "image")
        if not imgs or not imgs[-1].get("data"):
            raise GeminiError(f"Gemini не вернул картинку: {_texts(data) or 'пустой ответ'}")
        return base64.b64decode(imgs[-1]["data"])

    async def speech(self, text: str) -> bytes | None:
        """Бесплатный тариф TTS: 3 запроса в минуту и ~10 в день — ждём минутный лимит, на дневном уходим на lite-модель."""
        if not self.tts_enabled or not text.strip():
            return None
        model = self.tts_model
        for attempt in range(3):
            try:
                return await self._speech(model, text)
            except GeminiError as e:
                msg = str(e)
                if "429" not in msg or attempt == 2:
                    raise
                if "per day" in msg and model != TTS_FALLBACK:
                    model = TTS_FALLBACK
                    continue
                wait = 22.0
                if "retry in" in msg.lower():
                    m = re.search(r"retry in (\d+)s", msg, re.I)
                    if m:
                        wait = min(90.0, float(m.group(1)) + 2)
                    elif re.search(r"retry in \d+h", msg, re.I) and model != TTS_FALLBACK:
                        model = TTS_FALLBACK
                        continue
                await _sleep(wait)
        return None

    async def _speech(self, model: str, text: str) -> bytes | None:
        data = await self._interact(
            model,
            [{"type": "user_input", "content": [{"type": "text", "text": text[:4000]}]}],
            {"type": "audio"}, extra={"generation_config": {"speech_config": [{"voice": self.tts_voice}]}})
        audio = _parts(data, "audio")
        if not audio or not audio[-1].get("data"):
            raise GeminiError("Gemini не вернул аудио")
        raw = base64.b64decode(audio[-1]["data"])
        if raw[:4] == b"RIFF":  # одиночный запрос отдаёт готовый WAV, стриминг — сырой PCM
            return raw
        mime = audio[-1].get("mime_type") or ""
        rate = 24000
        if "rate=" in mime:
            try:
                rate = int(mime.split("rate=")[1].split(";")[0])
            except ValueError:
                pass
        return pcm_to_wav(raw, rate)

    async def video(self, prompt: str, *, aspect: str = "9:16", duration: int = VIDEO_DEFAULT_SECONDS, image: bytes | None = None,
                    negative_prompt: str = "", poll_interval: float = 10.0, timeout: float = 600.0) -> bytes | None:
        if not self.video_enabled or not prompt.strip():
            return None
        text = prompt[:3000]
        if negative_prompt:
            text += f"\n\nНе показывать: {negative_prompt}"
        parts: list[dict] = [{"type": "text", "text": text}]
        if image:
            parts.insert(0, {"type": "image", "data": base64.b64encode(image).decode(),
                             "mime_type": "image/png" if image[:4] == b"\x89PNG" else "image/jpeg"})
        fmt = {"type": "video", "aspect_ratio": aspect if aspect in ("9:16", "16:9") else "9:16",
               "duration": f"{max(VIDEO_MIN_SECONDS, min(VIDEO_MAX_SECONDS, int(duration)))}s", "resolution": "720p"}
        data = await self._interact(self.video_model, parts, fmt, timeout=timeout)

        waited = 0.0
        # Обычно ответ синхронный; если сервер вернул незавершённое взаимодействие — опрашиваем его по id
        while data.get("status") in ("in_progress", "queued", "running") and data.get("id"):
            if waited >= timeout:
                raise GeminiError(f"Генерация видео не завершилась за {int(timeout)} с")
            await _sleep(poll_interval)
            waited += poll_interval
            r = await self._request("GET", f"{API}/interactions/{data['id']}")
            data = r.json()
            if data.get("status") == "failed":
                raise GeminiError(f"Gemini: {(data.get('error') or {}).get('message') or 'генерация не удалась'}")

        videos = _parts(data, "video")
        if not videos:
            reason = _texts(data) or data.get("status") or "видео не получено"
            raise GeminiError(f"Omni отклонил запрос: {reason}")
        v = videos[-1]
        if v.get("data"):
            return base64.b64decode(v["data"])
        if v.get("uri"):  # delivery=uri: файл в Files API, скачиваем с тем же ключом
            r = await self._request("GET", v["uri"], timeout=timeout, follow_redirects=True)
            return r.content
        raise GeminiError("Omni отклонил запрос: пустое видео в ответе")
