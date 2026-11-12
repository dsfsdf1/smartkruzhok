from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .media import MAX_BYTES, wrap

START = "Пришли видео или файл MP4, MOV или WebM до 20 МБ и 3 минут. Я сделаю кружок."
EXPIRED = "Срок редактирования истёк. Отправь видео снова."
BUSY = "Я ещё обрабатываю предыдущее задание."
MODES = {"smart": "Умный фокус", "center": "По центру", "fit": "Вместить всё"}
EFFECTS = {"off": "Без эффекта", "retro": "Ретро", "zoom": "Драматический зум"}
SUBTITLES = {"off": "Выключены", "plain": "Обычные", "words": "Подсветка слов"}


def keys(clip, *rows):
    prefix = f'{clip["id"]}:{clip["state"]["revision"]}'
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"{prefix}:{action}") for label, action in row]
        for row in rows
    ])


def result_keys(clip):
    return keys(clip, [("Переделать кадр", "crop"), ("Оформить кружок", "decor")])


def length_keys(clip):
    return keys(clip, [("Первые 60 секунд", "first"), ("Разбить на кружки", "series")])


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
        if store.busy(message.chat.id):
            await message.answer(BUSY)
            return
        if store.upload(message.chat.id, message.message_id, attachment.file_id):
            await message.answer("Видео принято. Проверяю файл и готовлю кружок.")

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

        def enqueue(kind, *, revise=False, **arguments):
            return store.enqueue(clip, kind, f'{chat}:{query.message.message_id}:{query.data}',
                                 arguments, revise=revise)

        async def show_decor():
            body, markup = decor_panel(clip)
            await answer(body, reply_markup=markup)

        if action.startswith("retry-") and action[6:].isdigit():
            job = store.job(int(action[6:]), clip_id)
            if job and job["status"] == "failed":
                store.mark(job, sending=0)
                store.retry(job)
                await answer("Повторяю обработку.")
        elif action in ("first", "series"):
            state["series"] = action == "series"
            enqueue("render", revise=True)
            await answer("Готовлю серию." if state["series"] else "Готовлю первую минуту.")
        elif action == "crop":
            await answer("Как обрезать кадр?", reply_markup=keys(
                clip, *([(name, f"mode-{mode}")] for mode, name in MODES.items()),
            ))
        elif action.startswith("mode-") and action[5:] in MODES:
            mode = action[5:]
            enqueue("crop_preview", revise=True, mode=mode)
        elif action.startswith("render-") and action[7:] in MODES:
            settings["mode"] = action[7:]
            enqueue("render", revise=True)
        elif action == "decor":
            await show_decor()
        elif action == "subs":
            if "lines" not in state:
                if enqueue("transcribe"):
                    await answer("Распознаю речь. Это может занять немного времени.")
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
            if enqueue("decor_preview", zoom=action == "zoom-preview"):
                await answer("Готовлю предпросмотр.")
        elif action == "decor-render":
            enqueue("render", revise=True)

    @router.message()
    async def other(message: Message):
        await message.answer(START)

    return router
