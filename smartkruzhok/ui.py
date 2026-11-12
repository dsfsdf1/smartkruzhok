import json
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    Message, ReplyKeyboardMarkup,
)

from .effects import BORDER_COLORS, STICKERS
from .media import FPS, MAX_BYTES, PART_FRAMES, Video, active_effects, wrap

NEW_VIDEO = "🎬 Новое видео"
STATUS = "📋 Статус"
RESET = "🔄 Начать заново"
UPLOAD = "Пришли видео или прикрепи файл MP4, MOV или WebM. До 20 МБ и 3 минут."
EXPIRED = "Срок редактирования истёк. Отправь видео снова."
BUSY = "Предыдущее задание ещё в работе. Дождись результата или нажми «Начать заново»."
MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=NEW_VIDEO)], [KeyboardButton(text=STATUS), KeyboardButton(text=RESET)]],
    resize_keyboard=True,
    is_persistent=True,
)
COMMANDS = [
    BotCommand(command="new", description="Отправить новое видео"),
    BotCommand(command="status", description="Проверить статус"),
    BotCommand(command="reset", description="Начать заново"),
    BotCommand(command="menu", description="Открыть главное меню"),
]
MODES = {"smart": "Умный фокус", "center": "По центру", "fit": "Вместить всё"}
EFFECTS = {"off": "Без эффекта", "retro": "Ретро", "zoom": "Драматический зум", "film": "Плёнка"}
BORDER_NAMES = {"white": "Белый", "neon": "Неон", "pink": "Розовый", "gold": "Золотой"}
EFFECT_SECTIONS = {"border": "Рамка", "stickers": "Стикеры", "monochrome": "Чёрно-белое"}
STICKER_PANELS = {f"sticker-{name}" for name in STICKERS}
SUBTITLES = {"off": "Выключены", "plain": "Обычные", "words": "Подсветка слов"}
CANCEL_KEYS = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="Отменить", callback_data="cancel-job"),
]])
EDITOR_PANELS = {"result", "length", "decor", "crop", "subs", "title", "title-position", "effects",
                 *EFFECT_SECTIONS, *STICKER_PANELS}


def keys(clip, *rows):
    prefix = f'{clip["id"]}:{clip["state"]["revision"]}'
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"{prefix}:{action}") for label, action in row]
        for row in rows
    ])


def result_keys(clip):
    markup = keys(clip, [("Переделать кадр", "crop"), ("Оформить кружок", "decor")],
                  [("Выбрать фрагмент", "trim"), ("С субтитрами", "quick-subs")])
    markup.inline_keyboard.append([InlineKeyboardButton(text=NEW_VIDEO, callback_data="new-video")])
    return markup


def length_keys(clip):
    return keys(clip, [("Первые 60 секунд", "first"), ("Разбить на кружки", "series")],
                [("Выбрать фрагмент", "trim-long")])


def editor_back(kind, args):
    return args.get("back") or {
        "decor_preview": "decor", "transcribe": "decor",
        "crop_preview": "crop", "face_preview": "crop",
    }.get(kind, "result")


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


def time_label(second):
    minutes, seconds = divmod(second, 60)
    return f"{minutes}:{seconds:02}"


def selected_frames(state):
    parts = Video(**state["video"]).parts(state["series"], state.get("fragment"))
    return parts[-1][1] - parts[0][0]


def effect_second(value, state):
    if not re.fullmatch(r"[0-9]{1,3}(?::[0-5][0-9])?", value):
        return None
    minutes, _, seconds = value.rpartition(":")
    second = 60 * int(minutes or 0) + int(seconds)
    return second if second * FPS < selected_frames(state) else None


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
    store.save(clip, refresh=True)


def decor_panel(clip):
    settings = clip["state"]["settings"]
    labels = [EFFECTS[effect] for effect in active_effects(settings)]
    labels.extend(label for key, label in EFFECT_SECTIONS.items() if key in settings)
    effects = ", ".join(labels)
    return (
        f'Субтитры · {SUBTITLES[settings.get("subtitles", "off")]}\n'
        f'Надпись · {settings.get("title") or "выключена"}\n'
        f'Эффекты · {effects or EFFECTS["off"]}',
        keys(clip, [("Субтитры", "subs"), ("Надпись", "title"), ("Эффекты", "effects")],
             [("Предпросмотр", "decor-preview"), ("Сделать кружок", "decor-render")],
             [("‹ Назад", "result")]),
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
    rows.append([("‹ К оформлению", "decor")])
    return "\n".join(content), keys(clip, *rows)


def title_position_keys(clip):
    return keys(clip, [("Сверху", "title-top"), ("Снизу", "title-bottom")], [("‹ Назад", "title")])


async def show_effects(answer, clip):
    settings = clip["state"]["settings"]
    enabled = active_effects(settings)
    buttons = [(f"✓ {label}" if value in enabled else label, f"effect-{value}")
               for value, label in EFFECTS.items() if value != "off"]
    sections = [(f"✓ {label}" if key in settings else label, key) for key, label in EFFECT_SECTIONS.items()]
    actions = [("Предпросмотр", "effects-preview")]
    if enabled or EFFECT_SECTIONS.keys() & settings.keys():
        actions.append(("Отключить все", "effect-off"))
    await answer("Выбери эффекты для кружка.", reply_markup=keys(
        clip, buttons, sections, actions, [("‹ К оформлению", "decor")],
    ))


async def show_effect_settings(answer, clip, section):
    state = clip["state"]
    settings = state["settings"]
    second = None
    back = "effects"
    if section == "border":
        color = settings.get("border")
        body = f"Цвет рамки · {color}" if color else "Выбери цвет рамки."
        presets = [(f"✓ {label}" if BORDER_COLORS[key] == color else label, f"border-{key}")
                   for key, label in BORDER_NAMES.items()]
        rows = [presets[:2], presets[2:], [("Свой цвет", "border-custom")]]
        if color:
            rows[-1].append(("Убрать рамку", "border-off"))
    elif section == "monochrome":
        second = settings.get("monochrome")
        body = "Когда видео должно стать чёрно-белым?"
        if second is not None:
            body += f"\nСейчас · с {time_label(second)}"
        rows = [[("С начала", "mono-start"), ("Выбрать секунду", "time-mono")]]
        if second is not None:
            rows.append([("Выключить", "mono-off")])
    elif section == "stickers":
        stickers = settings.get("stickers", {})
        body = "Выбери стикер и укажи, когда его показать."
        rows = [[(f"{label} · {time_label(stickers[name])}" if name in stickers else label, f"sticker-{name}")]
                for name, label in STICKERS.items()]
    else:
        name = section.removeprefix("sticker-")
        second = settings.get("stickers", {}).get(name)
        body = STICKERS[name] if second is None else f"{STICKERS[name]} · {time_label(second)}"
        rows = [[("Изменить секунду" if second is not None else "Выбрать секунду", f"time-{name}")]]
        if second is not None:
            rows[0].append(("Убрать стикер", f"remove-{name}"))
        back = "stickers"
    if second is not None and second * FPS >= selected_frames(state):
        body += "\nЭто время за пределами выбранного фрагмента."
    await answer(body, reply_markup=keys(clip, *rows, [("‹ Назад", back)]))


async def ask_for_input(answer, store, clip, text, prompt):
    kind = prompt["kind"]
    rows = []
    if kind == "line":
        index = prompt["index"]
        rows.append([("Удалить строку", f"delete-{index}")])
        back = f"page-{index // 6}"
    elif kind == "title":
        back = "title"
    else:
        back = prompt.get("back", "result")
    message = await answer(text, reply_markup=keys(clip, *rows, [("‹ Назад", back)]))
    clip["state"]["prompt"] = prompt | {"message": message.message_id}
    store.save(clip, new_prompt=True, refresh=True)


def routes(store):
    router = Router()
    router.message.filter(F.chat.type == "private")

    @router.message(CommandStart())
    @router.message(Command("menu", "help"))
    async def start(message: Message):
        await message.answer("SmartKruzhok делает кружки из видео.\n\n" + UPLOAD, reply_markup=MENU)

    @router.message(Command("new"))
    @router.message(F.text == NEW_VIDEO)
    async def new_video(message: Message):
        await message.answer(BUSY if store.busy(message.chat.id) else UPLOAD, reply_markup=MENU)

    @router.message(Command("status"))
    @router.message(F.text == STATUS)
    async def status(message: Message):
        job = store.active_job(message.chat.id)
        if job is None:
            body = "Готов принять видео. Можно отправить его прямо в чат."
        elif job["cancel_requested"]:
            body = "Останавливаю задание. После остановки можно прислать новое видео."
        elif job["status"] == "queued":
            body = "Видео в очереди. Начну обработку, когда освобожусь."
        else:
            body = "Обрабатываю видео. Пришлю результат в этот чат."
        await message.answer(body, reply_markup=MENU)

    @router.message(Command("reset"))
    @router.message(F.text == RESET)
    async def reset(message: Message):
        job = store.reset_chat(message.chat.id)
        if job is None:
            body = "Начинаем заново. " + UPLOAD
        elif job["status"] == "queued" and not job["sending"]:
            body = "Задание отменено. " + UPLOAD
            progress = json.loads(job["args"]).get("progress")
            if progress:
                try:
                    await message.bot.edit_message_text("Задание отменено.", chat_id=message.chat.id,
                                                        message_id=progress, request_timeout=5)
                except TelegramAPIError:
                    pass
        else:
            body = "Останавливаю задание после текущего этапа. Когда оно остановится, пришли новое видео."
        await message.answer(body, reply_markup=MENU)

    @router.message(F.video | F.document)
    async def upload(message: Message):
        attachment = message.video or message.document
        if attachment.file_size and attachment.file_size > MAX_BYTES:
            await message.answer("Файл больше 20 МБ. Сожми видео или отправь фрагмент.")
            return
        if store.uploaded(message.chat.id, message.message_id):
            return
        if store.busy(message.chat.id):
            await message.answer(BUSY, reply_markup=MENU)
            return
        progress = await queue_notice(message.answer)
        store.upload(message.chat.id, message.message_id, attachment.file_id, progress)

    @router.message(F.text)
    async def input_text(message: Message):
        reply = message.reply_to_message
        clip = store.prompt(message.chat.id, reply.message_id if reply else None)
        if clip is None:
            body = EXPIRED if reply and reply.from_user and reply.from_user.is_bot else UPLOAD
            await message.answer(body, reply_markup=MENU)
            return
        if store.busy(message.chat.id):
            await message.answer(BUSY)
            return
        state = clip["state"]
        prompt = state["prompt"]
        value = " ".join(message.text.split())
        if prompt["kind"] == "border-color":
            if not re.fullmatch(r"#?[0-9a-fA-F]{6}", value):
                await ask_for_input(message.answer, store, clip,
                                    "Введи цвет из шести символов, например #38E8C6.", prompt)
                return
            state["settings"]["border"] = "#" + value.lstrip("#").upper()
            advance(store, clip)
            await show_effect_settings(message.answer, clip, "border")
            return
        if prompt["kind"] == "effect-time":
            second = effect_second(value, state)
            if second is None:
                await ask_for_input(message.answer, store, clip,
                                    "Укажи время внутри выбранного видео, например 0:03 или 3.", prompt)
                return
            name = prompt["effect"]
            if name == "mono":
                state["settings"]["monochrome"] = second
            else:
                state["settings"].setdefault("stickers", {})[name] = second
            advance(store, clip)
            await show_effect_settings(message.answer, clip, prompt["back"])
            return
        if prompt["kind"] == "trim":
            fragment = fragment_frames(value, state["video"])
            if fragment is None:
                await ask_for_input(
                    message.answer, store, clip,
                    "Нужны начало и конец внутри видео, например 0:25–0:53. Отрезок должен быть не длиннее минуты.",
                    prompt,
                )
                return
            if state.get("fragment") != fragment:
                state.pop("lines", None)
            state.update(fragment=fragment, series=False)
            progress = await queue_notice(message.answer)
            store.enqueue(clip, "render", f'trim:{message.chat.id}:{message.message_id}',
                          {"progress": progress, "back": prompt.get("back", "result")}, revise=True)
            return
        is_title = prompt["kind"] == "title"
        if not value or (is_title and len(value) > 60) or not wrap(value, 29 if is_title else 23):
            await ask_for_input(message.answer, store, clip,
                                "Сократи текст до двух строк и отправь его сообщением.", prompt)
            return
        if is_title:
            state["settings"]["title"] = value
            state["settings"].setdefault("position", "top")
            advance(store, clip)
            await message.answer("Где показать надпись?", reply_markup=title_position_keys(clip))
        else:
            index = prompt["index"]
            state["lines"][index].update(text=value, words=[])
            advance(store, clip)
            body, markup = subtitle_panel(clip, index // 6)
            await message.answer(body, reply_markup=markup)

    @router.callback_query(F.data == "new-video")
    async def next_video(query):
        await query.answer()
        if isinstance(query.message, Message) and query.message.chat.type == "private":
            await new_video(query.message)

    @router.callback_query(F.data == "cancel-job")
    async def cancel_job(query):
        if not isinstance(query.message, Message) or query.message.chat.type != "private":
            await query.answer()
            return
        job = store.request_cancel(query.message.chat.id, query.message.message_id)
        await query.answer("Запрос принят." if job else "Это задание уже завершено.")
        if job:
            summary = "Останавливаю после текущего этапа."
            markup = None
            if job["status"] == "queued" and not job["sending"]:
                summary = "Отменено."
                if job["sent"]:
                    summary += f' В чате осталось кружков: {job["sent"]}.'
                clip = store.clip(job["clip"], job["chat"])
                if clip and "video" in clip["state"]:
                    back = editor_back(job["kind"], json.loads(job["args"]))
                    markup = keys(clip, [("‹ Назад", back)])
            try:
                await query.message.edit_text(summary, reply_markup=markup)
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
        settings = state.get("settings")
        page = action[5:] if action.startswith("page-") else ""
        preview_at = re.fullmatch(r"(decor|effects)-at-([0-9]{1,3})", action)
        navigation = action in EDITOR_PANELS or page.isdigit()
        if not navigation and state["revision"] != int(revision):
            await query.answer("Настройки уже изменились.")
            if settings is not None:
                body, markup = decor_panel(clip)
                await query.message.answer(body, reply_markup=markup)
            return
        await query.answer()
        answer = query.message.answer
        if navigation and settings is not None:
            state.pop("prompt", None)
            store.save(clip, refresh=True)

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
        elif settings is None:
            return
        elif action == "result":
            await answer("Что сделать с этим видео?", reply_markup=result_keys(clip))
        elif action == "length":
            await answer("Как обработать длинное видео?", reply_markup=length_keys(clip))
        elif action in ("first", "series"):
            if state.pop("fragment", None) or state["series"] != (action == "series"):
                state.pop("lines", None)
            state["series"] = action == "series"
            await enqueue("render", revise=True, back="length")
        elif action in ("trim", "trim-long"):
            await ask_for_input(answer, store, clip,
                                "Отправь начало и конец фрагмента, например 0:25–0:53. Максимум 60 секунд.",
                                {"kind": "trim", "back": "length" if action == "trim-long" else "result"})
        elif action == "quick-subs":
            await enqueue("render", revise=True, quick_subtitles=True)
        elif action == "crop":
            await answer("Как обрезать кадр?", reply_markup=keys(
                clip, *([(name, f"mode-{mode}")] for mode, name in MODES.items()),
                [("Выбрать лицо", "faces"), ("Автовыбор лица", "face-auto")],
                [("‹ Назад", "result")],
            ))
        elif action == "faces":
            await enqueue("face_preview", revise=True)
        elif action == "face-auto":
            settings.pop("face", None)
            settings["mode"] = "smart"
            await enqueue("render", revise=True, back="crop")
        elif action.startswith("face-") and action[5:].isdigit():
            index = int(action[5:])
            sample = state.get("face_preview")
            if sample and 0 <= index < sample[1]:
                settings["face"] = [sample[0], index]
                settings["mode"] = "smart"
                await enqueue("render", revise=True, back="crop")
        elif action.startswith("mode-") and action[5:] in MODES:
            mode = action[5:]
            await enqueue("crop_preview", revise=True, mode=mode)
        elif action.startswith("render-") and action[7:] in MODES:
            settings["mode"] = action[7:]
            await enqueue("render", revise=True, back="crop")
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
        elif page.isdigit():
            body, markup = subtitle_panel(clip, int(page))
            await answer(body, reply_markup=markup)
        elif action.startswith("line-") and action[5:].isdigit():
            index = int(action[5:])
            if index < len(state.get("lines", [])):
                await ask_for_input(answer, store, clip, f"Отправь новый текст строки {index + 1}.",
                                    {"kind": "line", "index": index})
        elif action.startswith("delete-") and action[7:].isdigit():
            index = int(action[7:])
            if index < len(state.get("lines", [])):
                state["lines"][index].update(text="", words=[])
                advance(store, clip)
                body, markup = subtitle_panel(clip, index // 6)
                await answer(body, reply_markup=markup)
        elif action == "title":
            title = settings.get("title")
            rows = [[("Изменить текст" if title else "Ввести текст", "title-input")]]
            if title:
                rows.append([("Расположение", "title-position"), ("Убрать надпись", "title-off")])
            await answer(f"Надпись · {title or 'выключена'}", reply_markup=keys(
                clip, *rows, [("‹ К оформлению", "decor")]))
        elif action == "title-input":
            await ask_for_input(answer, store, clip, "Отправь надпись до 60 символов.", {"kind": "title"})
        elif action == "title-position":
            await answer("Где показать надпись?", reply_markup=title_position_keys(clip))
        elif action in ("title-top", "title-bottom", "title-off"):
            if action == "title-off":
                settings.pop("title", None)
            else:
                settings["position"] = action[6:]
            advance(store, clip)
            await show_decor()
        elif action == "effects":
            await show_effects(answer, clip)
        elif action in EFFECT_SECTIONS or action in STICKER_PANELS:
            await show_effect_settings(answer, clip, action)
        elif action == "border-custom":
            await ask_for_input(answer, store, clip, "Введи код цвета, например #38E8C6.",
                                {"kind": "border-color", "back": "border"})
        elif action == "border-off" or (action.startswith("border-") and action[7:] in BORDER_COLORS):
            if action == "border-off":
                settings.pop("border", None)
            else:
                settings["border"] = BORDER_COLORS[action[7:]]
            advance(store, clip)
            await show_effect_settings(answer, clip, "border")
        elif action in ("mono-start", "mono-off"):
            if action == "mono-off":
                settings.pop("monochrome", None)
            else:
                settings["monochrome"] = 0
            advance(store, clip)
            await show_effect_settings(answer, clip, "monochrome")
        elif action.startswith("time-") and (action[5:] == "mono" or action[5:] in STICKERS):
            name = action[5:]
            back = "monochrome" if name == "mono" else f"sticker-{name}"
            await ask_for_input(answer, store, clip,
                                "Укажи время от начала выбранного видео, например 0:03 или 3.",
                                {"kind": "effect-time", "effect": name, "back": back})
        elif action.startswith("remove-") and action[7:] in STICKERS:
            name = action[7:]
            stickers = settings.get("stickers", {})
            stickers.pop(name, None)
            if not stickers:
                settings.pop("stickers", None)
            advance(store, clip)
            await show_effect_settings(answer, clip, f"sticker-{name}")
        elif action.startswith("effect-") and action[7:] in EFFECTS:
            effect = action[7:]
            enabled = set(active_effects(settings))
            if effect == "off":
                enabled.clear()
                for key in EFFECT_SECTIONS:
                    settings.pop(key, None)
            else:
                enabled.symmetric_difference_update((effect,))
            settings["effects"] = [value for value in EFFECTS if value in enabled]
            settings.pop("effect", None)
            advance(store, clip)
            await show_effects(answer, clip)
        elif preview_at:
            back, second = preview_at.groups()
            await enqueue("decor_preview", revise=True, at=int(second), back=back)
        elif action in ("decor-preview", "zoom-preview", "effects-preview", "effects-zoom-preview"):
            back = "effects" if action.startswith("effects-") else "decor"
            at = 0 if action.endswith("zoom-preview") else None
            await enqueue("decor_preview", revise=True, at=at, back=back)
        elif action == "decor-render":
            await enqueue("render", revise=True, back="decor")

    @router.message()
    async def other(message: Message):
        await message.answer(UPLOAD, reply_markup=MENU)

    return router
