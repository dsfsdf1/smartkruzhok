import asyncio
import logging
import math
import shutil
import time
from dataclasses import asdict

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramServerError
from aiogram.types import FSInputFile

from . import media
from .ui import MODES, keys, length_keys, result_keys, subtitle_panel

log = logging.getLogger(__name__)


class UnconfirmedDelivery(Exception):
    pass


class Worker:
    def __init__(self, store, bot, root):
        self.store = store
        self.bot = bot
        self.root = root

    async def run(self, stop):
        next_cleanup = 0
        while not stop.is_set():
            if time.monotonic() >= next_cleanup:
                self.cleanup()
                next_cleanup = time.monotonic() + 60
            job = self.store.take()
            if job is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.25)
                except TimeoutError:
                    pass
                continue
            clip = self.store.clip(job["clip"], active=False)
            work = self.root / clip["id"]
            work.mkdir(mode=0o700, exist_ok=True)
            scratch = work / "job"
            if scratch.exists():
                shutil.rmtree(scratch)
            scratch.mkdir(mode=0o700)
            started = time.monotonic()
            log.info("started kind=%s queue_seconds=%.2f", job["kind"], time.time() - job["created"])
            try:
                await self.process(job, clip, work, scratch)
            except Exception as error:
                await self.failure(job, clip, error)
            else:
                self.store.mark(job, status="done", args={})
                log.info("done kind=%s seconds=%.2f", job["kind"], time.monotonic() - started)
            finally:
                media.text_overlay.cache_clear()
                shutil.rmtree(scratch, ignore_errors=True)

    def cleanup(self):
        for clip_id in self.store.expired():
            work = self.root / clip_id
            try:
                if work.exists():
                    shutil.rmtree(work)
            except OSError:
                log.warning("cleanup_failed")
                continue
            self.store.forget_media(clip_id)

    async def failure(self, job, clip, error):
        log.warning("failed kind=%s code=%s", job["kind"], type(error).__name__)
        message = "Не удалось закончить обработку. Попробуй ещё раз."
        retry = True
        if isinstance(error, media.MediaError):
            message = str(error)
            retry = job["sent"] > 0
        elif isinstance(error, UnconfirmedDelivery):
            message = "Telegram не подтвердил отправку. Последний кружок мог прийти. При повторе он может продублироваться."
        if job["sent"]:
            message += f' Подтверждено частей: {job["sent"]}.'
        self.store.mark(job, status="failed" if retry else "rejected", args=None if retry else {})
        if not retry and job["kind"] == "ingest":
            clip["expires"] = 0
            self.store.save(clip)
        markup = keys(clip, [("Повторить", f'retry-{job["id"]}')]) if retry else None
        if job["kind"] in ("decor_preview", "transcribe") and "settings" in clip["state"]:
            back = ("К оформлению", "decor")
            row = [("Повторить", f'retry-{job["id"]}'), back] if retry else [back]
            markup = keys(clip, row)
        if job["kind"] == "crop_preview":
            row = [("Другой кадр", "crop"), ("К оформлению", "decor")]
            markup = keys(clip, [("Повторить", f'retry-{job["id"]}')], row) if retry else keys(clip, row)
        if retry:
            self.store.save(clip, refresh=True)
        try:
            await self.bot.send_message(clip["chat"], message, reply_markup=markup)
        except Exception as notification_error:
            log.warning("failure_notice code=%s", type(notification_error).__name__)

    async def process(self, job, clip, work, scratch):
        state = clip["state"]
        chat = clip["chat"]
        source = work / "source"
        if job["kind"] == "ingest" and "video" not in state:
            if not source.exists():
                pending = scratch / "download"
                try:
                    info = await self.bot.get_file(job["args"]["file_id"])
                    if info.file_size and info.file_size > media.MAX_BYTES:
                        raise media.MediaError("Файл больше 20 МБ. Сожми видео или отправь фрагмент.")
                    await self.bot.download_file(info.file_path, pending, timeout=45)
                except TelegramBadRequest as error:
                    if "file is too big" in str(error).lower():
                        raise media.MediaError("Файл больше 20 МБ. Сожми видео или отправь фрагмент.") from error
                    raise
                if pending.stat().st_size > media.MAX_BYTES:
                    raise media.MediaError("Файл больше 20 МБ. Сожми видео или отправь фрагмент.")
                pending.replace(source)
            video = await asyncio.to_thread(media.probe, source)
            state.update(video=asdict(video), settings={"mode": "smart"}, series=False)
            self.store.save(clip, refresh=True)
        video = media.Video(**state["video"])
        if job["kind"] == "ingest" and video.frames > media.PART_FRAMES:
            await asyncio.to_thread(media.photo, source, video, 0, {"mode": "center"}, [], scratch / "check.jpg")
            await self.bot.send_message(chat, "Видео длиннее минуты. Как его обработать?",
                                        reply_markup=length_keys(clip))
            self.store.save(clip, refresh=True)
            return

        settings = dict(state["settings"])
        if job["kind"] in ("render", "decor_preview"):
            log.info("settings kind=%s crop=%s subtitles=%s title=%s effect=%s",
                     job["kind"], settings["mode"], settings.get("subtitles", "off"),
                     bool(settings.get("title")), settings.get("effect", "off"))
        end = video.parts(state["series"])[-1][1]
        if job["kind"] == "transcribe":
            if not video.audio:
                state["lines"] = []
            elif "lines" not in state:
                state["lines"] = await asyncio.to_thread(
                    media.transcribe, source, min(video.seconds, end / media.FPS), scratch / "speech.wav",
                )
            if any(line["text"] for line in state["lines"]):
                state["settings"]["subtitles"] = "plain"
            state["revision"] += 1
            self.store.save(clip)
            body, markup = subtitle_panel(clip)
            await self.bot.send_message(chat, body, reply_markup=markup)
            return

        if job["kind"] == "crop_preview":
            settings["mode"] = job["args"]["mode"]
        needs_face = (settings["mode"] == "smart" or settings.get("effect") == "zoom"
                      or settings.get("title") or settings.get("subtitles") in ("plain", "words"))
        trajectory = await asyncio.to_thread(media.analyze, source, video, work / "track.json") if needs_face else []
        lines = state.get("lines", [])
        if job["kind"] == "crop_preview":
            await self.crop_preview(clip, source, video, settings, trajectory, scratch)
        elif job["kind"] == "decor_preview":
            await self.decor_preview(job, clip, source, video, settings, trajectory, lines, end, scratch)
        else:
            await self.deliver(job, clip, source, video, settings, trajectory, lines, scratch)

    async def crop_preview(self, clip, source, video, settings, trajectory, scratch):
        image = scratch / "crop.jpg"
        midpoint = min(video.frames, media.PART_FRAMES) // 2
        await asyncio.to_thread(media.photo, source, video, midpoint, settings, trajectory, image)
        await self.bot.send_photo(
            clip["chat"], FSInputFile(image), caption=MODES[settings["mode"]],
            reply_markup=keys(clip, [("Сделать", f'render-{settings["mode"]}')],
                              [("Другой кадр", "crop"), ("К оформлению", "decor")]),
        )

    async def decor_preview(self, job, clip, source, video, settings, trajectory, lines, end, scratch):
        first = 0
        span = 4 * media.FPS
        if settings.get("subtitles") in ("plain", "words") and not job["args"]["zoom"]:
            line = next((line for line in lines if line["text"]), None)
            if line:
                first = min(max(0, int(line["start"] * media.FPS) - media.FPS // 2), max(0, end - span))
        last = min(end, first + span)
        path = scratch / "preview.mp4"
        await asyncio.to_thread(media.render, source, video, first, last, settings,
                                trajectory, lines, path, mask=True)
        rows = [[("Сделать", "decor-render"), ("Изменить", "decor")]]
        if settings.get("effect") == "zoom" and first > 0:
            rows.append([("Посмотреть начало", "zoom-preview")])
        await self.bot.send_video(
            clip["chat"], FSInputFile(path), caption="Предпросмотр оформления",
            width=media.SIZE, height=media.SIZE, supports_streaming=True, reply_markup=keys(clip, *rows),
        )

    async def deliver(self, job, clip, source, video, settings, trajectory, lines, scratch):
        if job["sending"]:
            raise UnconfirmedDelivery
        parts = video.parts(clip["state"]["series"])
        chat = clip["chat"]
        if len(parts) > 1 and not job["announced"]:
            await self.bot.send_message(chat, f"Отправляю кружки по порядку. Всего частей — {len(parts)}.")
            self.store.mark(job, announced=1)
        for index, (first, last) in enumerate(parts[job["sent"]:], job["sent"] + 1):
            output = scratch / f"{index}.mp4"
            await asyncio.to_thread(media.render, source, video, first, last,
                                    settings, trajectory, lines, output)
            self.store.mark(job, sending=1)
            try:
                await self.bot.send_video_note(chat, FSInputFile(output), length=media.SIZE,
                                               duration=math.ceil((last - first) / media.FPS),
                                               request_timeout=60)
            except (TelegramNetworkError, TelegramServerError) as error:
                raise UnconfirmedDelivery from error
            except Exception:
                self.store.mark(job, sending=0)
                raise
            self.store.mark(job, sent=index, sending=0)
            output.unlink()
        clip["state"]["revision"] += 1
        clip["state"].pop("prompt", None)
        self.store.save(clip, refresh=True)
        await self.bot.send_message(chat, "Готово!", reply_markup=result_keys(clip))
