"""Оркестратор: сгенерировать → отрендерить медиа → сохранить → опубликовать / собрать пакет для ручной выкладки."""
import asyncio
import html
import json
import logging
from dataclasses import dataclass

from .generator import ContentGenerator, magnet_text
from .images import MediaAI
from .media import MediaStore
from .store import ContentStore, body_json, media_json

log = logging.getLogger(__name__)

# Куда реально умеем публиковать через API; остальное (TikTok, Дзен…) — пакетом для ручной выкладки
API_PLATFORMS = {"telegram": "Telegram", "vk": "VK", "threads": "Threads", "instagram": "Instagram", "youtube": "YouTube"}
MANUAL_PLATFORMS = {"tiktok": "TikTok", "dzen": "Дзен", "pinterest": "Pinterest"}
PLATFORM_TIMEOUT = 300  # с на одну площадку


@dataclass
class Outcome:
    results: list  # PublishResult
    errors: list[str]
    status: str = ""  # busy — уже идёт/вышло; published | partial | draft — итог этого прохода


def brand_from_setting(raw: str | None):
    from .render import Brand
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        data = {}
    return Brand(**{k: v for k, v in data.items() if k in Brand.__dataclass_fields__})


def compose_text(row) -> str:
    """Итоговый текст публикации (HTML для Telegram; публикаторы других площадок снимают теги сами)."""
    e = html.escape
    tags = f"\n\n{e(row['hashtags'])}" if row["hashtags"] else ""
    if row["kind"] == "post":
        return e(row["body"] or "") + tags
    if row["kind"] == "magnet":
        data = body_json(row)
        return e(data.get("post") or row["caption"] or row["title"] or "") + tags
    return e(row["caption"] or row["title"] or "") + tags


class ContentFactory:
    def __init__(self, store: ContentStore, gen: ContentGenerator, media: MediaStore, ai, db, video_ai=None):
        self.store = store
        self.gen = gen
        self.media = media
        self.ai = ai  # картинки и озвучка: Gemini или OpenAI (общий интерфейс image/speech/*_enabled)
        self.video_ai = video_ai  # ИИ-видео через Gemini Omni
        self.db = db
        self.active: set[int] = set()  # материалы, которые этот процесс публикует прямо сейчас

    async def brand(self):
        return brand_from_setting(await self.db.get_setting("brand"))

    # --- создание ---
    async def create(self, kind: str, topic: str, *, created_by: str = "", lead_id: int | None = None,
                     parent_id: int | None = None, data: dict | None = None):
        data = data or await self.gen.generate(kind, topic)
        item_id = await self.save(kind, topic, data, created_by=created_by, lead_id=lead_id, parent_id=parent_id)
        await self.render(item_id)
        return await self.store.get(item_id)

    async def save(self, kind: str, topic: str, data: dict, **kw) -> int:
        if kind == "post":
            body = data.get("body", "")
        else:
            body = json.dumps(data, ensure_ascii=False)
        return await self.store.add(kind, topic, title=data.get("title", "")[:200], body=body,
                                    caption=data.get("caption", ""), hashtags=data.get("hashtags") or [],
                                    image_prompt=data.get("image_prompt", ""), **kw)

    async def replace_data(self, item_id: int, data: dict) -> None:
        row = await self.store.get(item_id)
        body = data.get("body", "") if row["kind"] == "post" else json.dumps(data, ensure_ascii=False)
        await self.store.update(item_id, title=data.get("title", row["title"] or "")[:200], body=body,
                                caption=data.get("caption", row["caption"] or ""),
                                hashtags=data.get("hashtags") if data.get("hashtags") is not None else row["hashtags"],
                                image_prompt=data.get("image_prompt", row["image_prompt"] or ""))
        await self.render(item_id)

    def data_of(self, row) -> dict:
        if row["kind"] == "post":
            return {"title": row["title"] or "", "body": row["body"] or "", "hashtags": (row["hashtags"] or "").split(),
                    "image_prompt": row["image_prompt"] or ""}
        data = body_json(row)
        return data if isinstance(data, dict) else {}

    # --- медиа ---
    async def render(self, item_id: int, *, ai_image: bool = True) -> dict:
        from .render import render_carousel, render_cover

        row = await self.store.get(item_id)
        brand = await self.brand()
        data = self.data_of(row)
        media: dict = {}
        if row["kind"] == "carousel":
            slides = data.get("slides") or []
            media["images"] = [self.media.save(png, ".png") for png in render_carousel(slides, brand)]
        elif row["kind"] == "reel":
            try:
                media["video"] = await self._video(data, brand)
            except Exception as e:
                log.warning("видео не собралось: %s", e)
                media["error"] = str(e)[:300]
        elif row["kind"] == "toon":
            try:
                media["video"] = await self._toon(data, brand)
            except Exception as e:
                log.warning("мультик не собрался: %s", e)
                media["error"] = str(e)[:300]
        elif row["kind"] == "aivideo":
            try:
                media["video"] = await self._aivideo(data)
            except Exception as e:
                log.warning("ИИ-видео не собралось: %s", e)
                media["error"] = str(e)[:300]
        else:
            cover = None
            if ai_image and self.ai.images_enabled and row["image_prompt"]:
                try:
                    cover = await self.ai.image(row["image_prompt"])
                except Exception as e:
                    log.warning("картинка ИИ не сгенерировалась: %s", e)
            if cover:
                media["images"] = [self.media.save(cover, ".jpg" if cover[:3] == b"\xff\xd8\xff" else ".png")]
                media["ai_cover"] = True
            else:
                subtitle = data.get("subtitle") or (data.get("body") or "").split("\n")[0][:120]
                media["images"] = [self.media.save(render_cover(row["title"] or row["topic"], subtitle, brand), ".png")]
        await self.store.update(item_id, media=media)
        return media

    async def _speech(self, text: str) -> bytes | None:
        if not text or not self.ai.tts_enabled:
            return None
        try:
            return await self.ai.speech(text)
        except Exception as e:
            log.warning("озвучка не удалась: %s", e)
            return None

    async def _built(self, name: str, build) -> None:
        """Ждёт сборку ролика; при сбое/отмене убирает недописанный файл."""
        try:
            await build
        except BaseException:
            self.media.discard(name)
            raise

    async def _video(self, data: dict, brand) -> str:
        from .video import build_video

        scenes = []
        for s in data.get("scenes") or []:
            scenes.append({"text": s.get("text", ""), "duration": float(s.get("duration") or 3),
                           "audio": await self._speech(s.get("voice", ""))})
        if not scenes:
            raise RuntimeError("в сценарии нет сцен")
        name = self.media.reserve(".mp4")
        await self._built(name, build_video(scenes, brand, self.media.path(name)))
        return name

    async def _toon(self, data: dict, brand) -> str:
        from .toon import build_toon, normalize_script

        script = normalize_script(data)
        for scene in script["scenes"]:
            line = scene.get("line") or {}
            scene["audio"] = await self._speech(line.get("text", ""))
        name = self.media.reserve(".mp4")
        await self._built(name, build_toon(script, brand, self.media.path(name)))
        return name

    async def _aivideo(self, data: dict) -> str:
        from .aivideo import assemble

        if not self.video_ai or not self.video_ai.video_enabled:
            raise RuntimeError("Генерация видео нейросетью требует GEMINI_API_KEY (Gemini Omni)")
        clips = []
        for clip in data.get("clips") or []:
            video = await self.video_ai.video(clip["prompt"], aspect="9:16")
            if not video:
                raise RuntimeError("Gemini не вернул видео")
            clips.append((video, await self._speech(clip.get("voice", ""))))
        if not clips:
            raise RuntimeError("нет клипов")
        name = self.media.reserve(".mp4")
        await self._built(name, assemble(clips, self.media.path(name)))
        return name

    def files(self, row) -> tuple[list[bytes], str | None]:
        """Картинки (bytes) и путь к видео для публикации."""
        m = media_json(row)
        images = []
        for name in m.get("images") or []:
            try:
                images.append(self.media.read(name))
            except OSError:
                log.warning("файл %s потерян", name)
        video = self.media.path(m["video"]) if m.get("video") else None
        return images, video

    # --- публикация ---
    async def publish(self, row, publishers: list, platforms: list[str] | None = None) -> Outcome:
        """Идемпотентно: материал захватывается атомарно (publishing), результат каждой площадки пишется сразу,
        повтор (в т.ч. после рестарта) публикует только туда, где ещё не вышло."""
        item_id = row["id"]
        if not await self.store.claim_publish(item_id):
            cur = await self.store.get(item_id)
            st = cur["status"] if cur else ""
            msg = {"publishing": "уже публикуется", "published": "уже опубликовано"}.get(
                st, f"публикация недоступна (статус: {st or 'материала нет'})")
            return Outcome([], [msg], status="busy")
        self.active.add(item_id)
        try:
            return await self._publish_claimed(item_id, publishers, platforms)
        finally:
            self.active.discard(item_id)

    async def _publish_claimed(self, item_id: int, publishers: list, platforms: list[str] | None) -> Outcome:
        row = await self.store.get(item_id)  # свежая строка: вызывающий мог держать устаревшую
        done = json.loads(row["published"] or "[]")
        old_errors = {e["platform"]: e for e in json.loads(row["publish_errors"] or "[]") if e.get("platform")}
        wanted = platforms or [p.strip() for p in (row["platforms"] or "").split(",") if p.strip()] or None
        targets = [p for p in publishers if not wanted or p.platform in wanted]
        have = {d["platform"] for d in done}
        results, errors = [], []
        status = "draft"
        try:
            text = compose_text(row)
            images, video = self.files(row)
            for pub in targets:
                if pub.platform in have:
                    continue
                try:
                    res = await asyncio.wait_for(pub.publish(text, images, video), timeout=PLATFORM_TIMEOUT)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    reason = str(e)[:200] or type(e).__name__
                    if isinstance(e, asyncio.TimeoutError):
                        reason = f"таймаут {PLATFORM_TIMEOUT} с, пост мог выйти — проверьте площадку"
                    errors.append({"platform": pub.platform, "label": pub.label, "error": reason})
                    log.warning("публикация %s в %s не удалась: %s", item_id, pub.label, e)
                    continue
                results.append(res)
                have.add(pub.platform)
                done.append({"platform": res.platform, "url": res.url})
                await self.store.touch_publishing(item_id, done)
        finally:
            # и при отмене/сбое не оставляем материал в publishing; отменённая площадка в done не попадает
            retried = {e["platform"] for e in errors}
            kept = [e for p, e in old_errors.items() if p not in have and p not in retried]
            all_done = bool(targets) and all(p.platform in have for p in targets)
            status = await asyncio.shield(self.store.finish_publish(
                item_id, done, [] if all_done else kept + errors, all_done=all_done))
        if not targets:
            return Outcome([], ["нет подходящих площадок"], status=status)
        return Outcome(results, [f"{e['label'] or e['platform']}: {e['error']}" for e in errors], status=status)

    def magnet_text(self, row) -> str:
        return magnet_text(body_json(row)) if row["kind"] == "magnet" else (row["body"] or "")
