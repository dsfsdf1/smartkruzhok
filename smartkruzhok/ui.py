import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .media import FPS, MAX_BYTES, PART_FRAMES, wrap

START = "SmartKruzhok 2.0. Пришли видео или файл MP4, MOV или WebM до 20 МБ и 3 минут. Я сделаю кружок."
EXPIRED = "Срок редактирования истёк. Отправь видео снова."
BUSY = "Я ещё обрабатываю предыдущее задание."
MODES = {"smart": "Умный фокус", "center": "По центру", "fit": "Вместить всё"}
EFFECTS = {"off": "Без эффекта", "retro": "Ретро", "zoom": "Драматический зум"}
SUBTITLES = {"off": "Выключены", "plain": "Обычные", "words": "Подсветка слов"}
CANCEL_KEYS = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="Отменить", callback_data="cancel-job"),
]])


def keys(clip, *rows):
    prefix = f'{clip["id"]}:{clip["state"]["revision"]}'
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"{prefix}:{action}") for label, action in row]
        for row in rows
    ])


def result_keys(clip):
    return keys(clip, [("Переделать кадр", "crop"), ("Оформить кружок", "decor")],
                [("Выбрать фрагмент", "trim"), ("С субтитрами", "quick-subs")])


def length_keys(clip):
    return keys(clip, [("Первые 60 секунд", "first"), ("Разбить на кружки", "series")],
                [("Выбрать фрагмент", "trim")])


def fragment_frames(text, video):
    match = re.fullmatch(r"([0-9]{1,3}(?::[0-5][0-9])?)\s*[-–—]\s*([0-9]{1,3}(?::[0-5][0-9])?)", text.strip())
    if match is None:
        return None
    times = []
    for value in match.groups():
        minutes, _, seconds = value.rpartition(":")
        times.append(int(minutes or 0) * 60 + int(seconds))
    first, last = (time * FPS for time in times)
    if 0 < last - first <= PART_FRAMES and last <= video["frames"]:
        return [first, last]
    return None


async def queue_notice(answer):
    try:
        message = await answer("В очереди. Начну обработку, как только освобожусь.",
                               reply_markup=CANCEL_KEYS)
        return message.message_id
    except TelegramAPIError:
        return None


def advance(store, clip):
    clip["state"]["revision"] += 1
    clip["state"].pop("prompt", None)
    store.save(clip)


def decor_panel(clip):
    state = clip["state"]
    settings = state["settings"]
    return (
        f'Субтитры · {SUBTITLES[settings.get("subtitles", "off")]}\n'
        f'Надпись · {settings.get("title") or "выключена"}\n'
        f'Эффект · {EFFECTS[settings.get("effect", "off")]}',
        keys(clip, [("Субтитры", "subs"), ("Надпись", "title"), ("Эффекты", "effects")],
             [("Предпросмотр", "decor-preview")]),
    )


def subtitle_panel(clip, page=0):
    lines = clip["state"].get("lines", [])
    page = max(0, min(page, max(0, (len(lines) - 1) // 6)))
    first = page * 6
    rows = [
        [("Обычные", "sub-plain"), ("Подсветка слов", "sub-words"), ("Выключить", "sub-off")],
    ]
    content = ["Выбери стиль или строку для правки."]
    if lines:
        numbers = []
        for index, line in enumerate(lines[first:first + 6], first):
            content.append(f'{index + 1}. {line["text"] or "Удалена"}')
            numbers.append((str(index + 1), f"line-{index}"))
        rows.append(numbers)
        navigation = []
        if page:
            navigation.append(("‹ Назад", f"page-{page - 1}"))
        if first + 6 < len(lines):
            navigation.append(("Дальше ›", f"page-{page + 1}"))
        if navigation:
            rows.append(navigation)
    elif "lines" in clip["state"]:
        content = ["В видео не удалось найти речь."]
        rows = []
    rows.append([("К оформлению", "decor")])
    return "\n".join(content), keys(clip, *rows)


def routes(store):
    router = Router()
    router.message.filter(F.chat.type == "private")

    @router.message(CommandStart())
    async def start(message: Message):
        await message.answer(START)

    @router.message(F.video | F.document)
    async def upload(message: Message):
        attachment = message.video or message.document
        if attachment.file_size and attachment.file_size > MAX_BYTES:
            await message.answer("Файл больше 20 МБ. Сожми видео или отправь фрагмент.")
            return
        if store.uploaded(message.chat.id, message.message_id):
            return
        if store.busy(message.chat.id):
            await message.answer(BUSY)
            return
        progress = await queue_notice(message.answer)
        store.upload(message.chat.id, message.message_id, attachment.file_id, progress)

    @router.message(F.text)
    async def input_text(message: Message):
        reply = message.reply_to_message
        clip = store.prompt(message.chat.id, reply.message_id) if reply else None
        if clip is None:
            await message.answer(EXPIRED if reply and reply.from_user and reply.from_user.is_bot else START)
            return
        if store.busy(message.chat.id):
            await message.answer(BUSY)
            return
        state = clip["state"]
        prompt = state["prompt"]
        value = " ".join(message.text.split())
        if prompt["kind"] == "trim":
            fragment = fragment_frames(value, state["video"])
            if fragment is None:
                retry = await message.answer(
                    "Нужны начало и конец внутри видео, например 0:25–0:53. Отрезок должен быть не длиннее минуты.",
                    reply_markup=ForceReply(),
                )
                prompt["message"] = retry.message_id
                store.save(clip)
                return
            if state.get("fragment") != fragment:
                state.pop("lines", None)
            state.update(fragment=fragment, series=False)
            progress = await queue_notice(message.answer)
            store.enqueue(clip, "render", f'trim:{message.chat.id}:{message.message_id}',
                          {"progress": progress}, revise=True)
            return
        is_title = prompt["kind"] == "title"
        if not value or (is_title and len(value) > 60) or not wrap(value, 29 if is_title else 23):
            retry = await message.answer(
                "Сократи текст до двух строк и отправь его в ответ на это сообщение.",
                reply_markup=ForceReply(),
            )
            prompt["message"] = retry.message_id
            store.save(clip)
            return
        if is_title:
            state["settings"]["title"] = value
            state["settings"].setdefault("position", "top")
            advance(store, clip)
            await message.answer("Где показать надпись?", reply_markup=keys(
                clip, [("Сверху", "title-top"), ("Снизу", "title-bottom")],
            ))
        else:
            index = prompt["index"]
            state["lines"][index].update(text=value, words=[])
            advance(store, clip)
            body, markup = subtitle_panel(clip, index // 6)
            await message.answer(body, reply_markup=markup)

    @router.callback_query(F.data == "cancel-job")
    async def cancel_job(query):
        if not isinstance(query.message, Message) or query.message.chat.type != "private":
            await query.answer()
            return
        job = store.request_cancel(query.message.chat.id, query.message.message_id)
        await query.answer("Запрос принят." if job else "Это задание уже завершено.")
        if job:
            summary = "Останавливаю после текущего этапа."
            if job["status"] == "queued" and not job["sending"]:
                summary = "Отменено."
                if job["sent"]:
                    summary += f' В чате осталось кружков: {job["sent"]}.'
            try:
                await query.message.edit_text(summary)
            except TelegramAPIError:
                pass

    @router.callback_query(F.data.regexp(r"^[0-9a-f]{16}:\d+:"))
    async def controls(query):
        if not isinstance(query.message, Message) or query.message.chat.type != "private":
            await query.answer()
            return
        chat = query.message.chat.id
        clip_id, revision, action = query.data.split(":", 2)
        clip = store.clip(clip_id, chat)
        if clip is None:
            await query.answer(EXPIRED, show_alert=True)
            return
        if store.busy(chat):
            await query.answer(BUSY)
            return
        state = clip["state"]
        if state["revision"] != int(revision):
            await query.answer("Эта кнопка устарела. Используй последнее сообщение.")
            return
        await query.answer()
        settings = state.get("settings", {})
        answer = query.message.answer

        async def enqueue(kind, *, revise=False, **arguments):
            key = f'{chat}:{query.message.message_id}:{query.data}'
            if store.enqueued(key):
                return False
            arguments["progress"] = await queue_notice(answer)
            return store.enqueue(clip, kind, key, arguments, revise=revise)

        async def show_decor():
            body, markup = decor_panel(clip)
            await answer(body, reply_markup=markup)

        if action.startswith("retry-") and action[6:].isdigit():
            job = store.job(int(action[6:]), clip_id)
            if job and job["status"] == "failed":
                args = job["args"] | {"progress": await queue_notice(answer)}
                store.mark(job, sending=0, args=args)
                store.retry(job)
        elif action in ("first", "series"):
            if state.pop("fragment", None) or state["series"] != (action == "series"):
                state.pop("lines", None)
            state["series"] = action == "series"
            await enqueue("render", revise=True)
        elif action == "trim":
            prompt = await answer("Ответь диапазоном начала и конца, например 0:25–0:53. Максимум 60 секунд.",
                                  reply_markup=ForceReply())
            state["prompt"] = {"kind": "trim", "message": prompt.message_id}
            store.save(clip)
        elif action == "quick-subs":
            await enqueue("render", revise=True, quick_subtitles=True)
        elif action == "crop":
            await answer("Как обрезать кадр?", reply_markup=keys(
                clip, *([(name, f"mode-{mode}")] for mode, name in MODES.items()),
                [("Выбрать лицо", "faces"), ("Автовыбор лица", "face-auto")],
            ))
        elif action == "faces":
            await enqueue("face_preview", revise=True)
        elif action == "face-auto":
            settings.pop("face", None)
            settings["mode"] = "smart"
            await enqueue("render", revise=True)
        elif action.startswith("face-") and action[5:].isdigit():
            index = int(action[5:])
            sample = state.get("face_preview")
            if sample and 0 <= index < sample[1]:
                settings["face"] = [sample[0], index]
                settings["mode"] = "smart"
                await enqueue("render", revise=True)
        elif action.startswith("mode-") and action[5:] in MODES:
            mode = action[5:]
            await enqueue("crop_preview", revise=True, mode=mode)
        elif action.startswith("render-") and action[7:] in MODES:
            settings["mode"] = action[7:]
            await enqueue("render", revise=True)
        elif action == "decor":
            await show_decor()
        elif action == "subs":
            if "lines" not in state:
                await enqueue("transcribe")
            else:
                body, markup = subtitle_panel(clip)
                await answer(body, reply_markup=markup)
        elif action.startswith("sub-") and action[4:] in SUBTITLES:
            settings["subtitles"] = action[4:]
            advance(store, clip)
            await show_decor()
        elif action.startswith("page-") and action[5:].isdigit():
            body, markup = subtitle_panel(clip, int(action[5:]))
            await answer(body, reply_markup=markup)
        elif action.startswith("line-") and action[5:].isdigit():
            index = int(action[5:])
            if index < len(state.get("lines", [])):
                prompt = await answer(
                    f'Отправь новый текст строки {index + 1} в ответ на это сообщение.',
                    reply_markup=ForceReply(),
                )
                state["prompt"] = {"kind": "line", "index": index, "message": prompt.message_id}
                store.save(clip)
                await answer(f"Строка {index + 1}", reply_markup=keys(
                    clip, [("Удалить строку", f"delete-{index}")],
                ))
        elif action.startswith("delete-") and action[7:].isdigit():
            index = int(action[7:])
            if index < len(state.get("lines", [])):
                state["lines"][index].update(text="", words=[])
                advance(store, clip)
                body, markup = subtitle_panel(clip, index // 6)
                await answer(body, reply_markup=markup)
        elif action == "title":
            await answer("Настрой надпись.", reply_markup=keys(
                clip, [("Ввести текст", "title-input"), ("Убрать надпись", "title-off")],
            ))
        elif action == "title-input":
            prompt = await answer(
                "Отправь надпись до 60 символов в ответ на это сообщение.",
                reply_markup=ForceReply(),
            )
            state["prompt"] = {"kind": "title", "message": prompt.message_id}
            store.save(clip)
        elif action in ("title-top", "title-bottom", "title-off"):
            if action == "title-off":
                settings.pop("title", None)
            else:
                settings["position"] = action[6:]
            advance(store, clip)
            await show_decor()
        elif action == "effects":
            await answer("Выбери эффект.", reply_markup=keys(
                clip, *([(label, f"effect-{value}")] for value, label in EFFECTS.items()),
            ))
        elif action.startswith("effect-") and action[7:] in EFFECTS:
            settings["effect"] = action[7:]
            advance(store, clip)
            await show_decor()
        elif action in ("decor-preview", "zoom-preview"):
            await enqueue("decor_preview", zoom=action == "zoom-preview")
        elif action == "decor-render":
            await enqueue("render", revise=True)

    @router.message()
    async def other(message: Message):
        await message.answer(START)

    return router
