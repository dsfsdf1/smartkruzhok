import asyncio
import logging
import math
import shutil
import subprocess
import time
from dataclasses import asdict

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramServerError
from aiogram.types import FSInputFile

from . import media
from .ui import CANCEL_KEYS, MODES, editor_back, keys, length_keys, result_keys, subtitle_panel

log = logging.getLogger(__name__)


class UnconfirmedDelivery(Exception):
    pass


class CancelledJob(Exception):
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
            except CancelledJob:
                text = "Отменено."
                if job["sent"]:
                    text += f' В чате осталось кружков: {job["sent"]}.'
                markup = None
                if "video" in clip["state"]:
                    markup = keys(clip, [("‹ Назад", editor_back(job["kind"], job["args"]))])
                await self.finish_notice(job, text, markup)
                self.store.mark(job, status="cancelled", args={})
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

    def check_cancel(self, job):
        if not job["sending"] and self.store.cancelling(job["id"]):
            raise CancelledJob

    async def edit_notice(self, job, text, markup=None):
        message = job["args"].get("progress")
        if message is None:
            return False
        try:
            await self.bot.edit_message_text(text, chat_id=job["chat"], message_id=message,
                                             reply_markup=markup, request_timeout=5)
            return True
        except TelegramBadRequest as error:
            return "message is not modified" in str(error).lower()
        except TelegramAPIError:
            return False

    async def step(self, job, text):
        self.check_cancel(job)
        await self.edit_notice(job, text, CANCEL_KEYS)
        self.check_cancel(job)

    async def finish_notice(self, job, text, markup=None):
        if not await self.edit_notice(job, text, markup):
            try:
                await self.bot.send_message(job["chat"], text, reply_markup=markup, request_timeout=5)
            except TelegramAPIError:
                log.warning("job_notice_failed kind=%s", job["kind"])

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
        self.store.mark(job, status="failed" if retry else "rejected")
        if not retry and job["kind"] == "ingest":
            clip["expires"] = 0
            self.store.save(clip)
        row = [("Повторить", f'retry-{job["id"]}')] if retry else []
        if "video" in clip["state"] and not (job["kind"] == "ingest" and not retry):
            row.append(("‹ Назад", editor_back(job["kind"], job["args"])))
        markup = keys(clip, row) if row else None
        if retry:
            self.store.save(clip, refresh=True)
        await self.finish_notice(job, message, markup)
        if not retry:
            self.store.mark(job, args={})

    async def process(self, job, clip, work, scratch):
        state = clip["state"]
        chat = clip["chat"]
        source = work / "source"
        self.check_cancel(job)
        if job["kind"] == "ingest" and "video" not in state:
            await self.step(job, "Проверяю видео.")
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
            self.check_cancel(job)
        video = media.Video(**state["video"])
        if job["kind"] == "ingest" and video.frames > media.PART_FRAMES:
            await asyncio.to_thread(media.photo, source, video, 0, {"mode": "center"}, [], scratch / "check.jpg")
            self.check_cancel(job)
            self.store.save(clip, refresh=True)
            await self.finish_notice(job, "Видео длиннее минуты. Как его обработать?", length_keys(clip))
            return

        settings = dict(state["settings"])
        if state["series"] and "series_parts" not in state:
            pauses = []
            if video.audio and not (job["announced"] or job["sending"] or job["sent"]):
                await self.step(job, "Ищу паузы для серии.")
                try:
                    pauses = await asyncio.to_thread(media.speech_pauses, source)
                except subprocess.SubprocessError:
                    log.warning("pause_detection_failed")
            self.check_cancel(job)
            state["series_parts"] = video.parts(True, pauses=pauses)
            self.store.save(clip)
        parts = state["series_parts"] if state["series"] else video.parts(False, state.get("fragment"))
        first, end = parts[0][0], parts[-1][1]
        settings["zoom_start"] = first
        if job["kind"] in ("render", "decor_preview"):
            log.info("settings kind=%s crop=%s subtitles=%s title=%s effects=%s",
                     job["kind"], settings["mode"], settings.get("subtitles", "off"),
                     bool(settings.get("title")), media.active_effects(settings))
        quick = job["args"].get("quick_subtitles")
        wants_subtitles = quick or settings.get("subtitles") in ("plain", "words")
        if "lines" not in state and (job["kind"] == "transcribe" or wants_subtitles):
            lines = []
            if video.audio:
                await self.step(job, "Распознаю речь.")
                lines = await asyncio.to_thread(media.transcribe, source, min(video.seconds, end / media.FPS),
                                                scratch / "speech.wav", start=first / media.FPS)
                self.check_cancel(job)
            state["lines"] = lines
            self.store.save(clip)
        lines = state.get("lines", [])
        has_speech = any(line["text"] for line in lines)
        if job["kind"] == "transcribe":
            if has_speech:
                state["settings"]["subtitles"] = "plain"
            state["revision"] += 1
            self.store.save(clip, refresh=True)
            body, markup = subtitle_panel(clip)
            await self.edit_notice(job, "Распознавание завершено.")
            await self.bot.send_message(chat, body, reply_markup=markup)
            return
        if quick and not has_speech:
            await self.finish_notice(job, "В выбранном видео не удалось найти речь.", result_keys(clip))
            return
        if quick and settings.get("subtitles") not in ("plain", "words"):
            settings["subtitles"] = "plain"
            state["settings"]["subtitles"] = "plain"
            self.store.save(clip)

        if job["kind"] == "face_preview":
            await self.face_preview(job, clip, source, video, first, end, work, scratch)
            return

        if job["kind"] == "crop_preview":
            settings["mode"] = job["args"]["mode"]
        needs_face = (settings["mode"] == "smart" or "zoom" in media.active_effects(settings)
                      or settings.get("subtitles") in ("plain", "words"))
        trajectory = []
        if needs_face:
            await self.step(job, "Подбираю кадр.")
            selected = settings.get("face")
            cache = f"track-{selected[0]}-{selected[1]}.json" if selected else "track.json"
            trajectory = await asyncio.to_thread(media.analyze, source, video, work / cache, selected=selected)
            self.check_cancel(job)
        if job["kind"] == "crop_preview":
            await self.crop_preview(job, clip, source, video, settings, trajectory, parts[0], scratch)
        elif job["kind"] == "decor_preview":
            await self.decor_preview(job, clip, source, video, settings, trajectory, lines, parts, scratch)
        else:
            await self.deliver(job, clip, source, video, settings, trajectory, lines, parts, scratch)

    async def face_preview(self, job, clip, source, video, first, end, work, scratch):
        await self.step(job, "Ищу лица в выбранном фрагменте.")
        faces = await asyncio.to_thread(media.face_samples, source, video, work / "faces.json")
        self.check_cancel(job)
        low = math.ceil(first * media.DETECT_FPS / media.FPS)
        high = math.ceil(end * media.DETECT_FPS / media.FPS)
        midpoint = (low + high) / 2
        sample = max(range(low, high), key=lambda index: (len(faces[index]), -abs(index - midpoint)), default=None)
        if sample is None or not faces[sample]:
            await self.finish_notice(job, "На этом фрагменте не удалось найти лицо.",
                                     keys(clip, [("‹ К кадрированию", "crop")]))
            return
        photo = scratch / "faces.jpg"
        await asyncio.to_thread(media.face_photo, source, video, sample, faces[sample], photo)
        self.check_cancel(job)
        clip["state"]["face_preview"] = [sample, len(faces[sample])]
        self.store.save(clip)
        options = [(str(index + 1), f"face-{index}") for index in range(len(faces[sample]))]
        rows = [options[index:index + 4] for index in range(0, len(options), 4)]
        rows.append([("Автовыбор лица", "face-auto")])
        rows.append([("‹ К кадрированию", "crop")])
        await self.edit_notice(job, "Кадр с лицами готов.")
        await self.bot.send_photo(clip["chat"], FSInputFile(photo), caption="Выбери, за кем следить.",
                                  reply_markup=keys(clip, *rows))

    async def crop_preview(self, job, clip, source, video, settings, trajectory, part, scratch):
        image = scratch / "crop.jpg"
        midpoint = sum(part) // 2
        await asyncio.to_thread(media.photo, source, video, midpoint, settings, trajectory, image)
        self.check_cancel(job)
        await self.edit_notice(job, "Кадр готов.")
        await self.bot.send_photo(
            clip["chat"], FSInputFile(image), caption=MODES[settings["mode"]],
            reply_markup=keys(clip, [("Сделать", f'render-{settings["mode"]}')],
                              [("‹ К кадрированию", "crop"), ("К оформлению", "decor")]),
        )

    async def decor_preview(self, job, clip, source, video, settings, trajectory, lines, parts, scratch):
        start, end = parts[0]
        first = start
        span = 4 * media.FPS
        if settings.get("subtitles") in ("plain", "words") and not job["args"].get("zoom"):
            line = next((line for line in lines if line["text"]), None)
            if line:
                line_start = int(line["start"] * media.FPS)
                start, end = next((part for part in parts if part[0] <= line_start < part[1]), parts[0])
                first = min(max(start, line_start - media.FPS // 2), max(start, end - span))
        last = min(end, first + span)
        path = scratch / "preview.mp4"
        await self.step(job, "Готовлю предпросмотр.")
        await asyncio.to_thread(media.render, source, video, first, last, settings,
                                trajectory, lines, path, mask=True)
        self.check_cancel(job)
        await self.edit_notice(job, "Предпросмотр готов.")
        back = editor_back(job["kind"], job["args"])
        rows = [[("Сделать кружок", "decor-render"), ("‹ Назад", back)]]
        if "zoom" in media.active_effects(settings) and first > parts[0][0]:
            action = "effects-zoom-preview" if back == "effects" else "zoom-preview"
            rows.append([("Посмотреть начало", action)])
        await self.bot.send_video(
            clip["chat"], FSInputFile(path), caption="Предпросмотр оформления",
            width=media.SIZE, height=media.SIZE, supports_streaming=True, reply_markup=keys(clip, *rows),
        )

    async def deliver(self, job, clip, source, video, settings, trajectory, lines, parts, scratch):
        if job["sending"]:
            raise UnconfirmedDelivery
        chat = clip["chat"]
        if len(parts) > 1 and not job["announced"] and not job["args"].get("progress"):
            await self.bot.send_message(chat, f"Отправляю кружки по порядку. Всего частей — {len(parts)}.")
            self.store.mark(job, announced=1)
        for index, (first, last) in enumerate(parts[job["sent"]:], job["sent"] + 1):
            label = f"кружок {index} из {len(parts)}" if len(parts) > 1 else "кружок"
            await self.step(job, f"Готовлю {label}.")
            output = scratch / f"{index}.mp4"
            await asyncio.to_thread(media.render, source, video, first, last,
                                    settings, trajectory, lines, output)
            await self.step(job, f"Отправляю {label}.")
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
        await self.edit_notice(job, "Обработка завершена.")
        try:
            await self.bot.send_message(chat, "Готово!", reply_markup=result_keys(clip), request_timeout=5)
        except TelegramAPIError:
            await self.finish_notice(job, "Готово!", result_keys(clip))
