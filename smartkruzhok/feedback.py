from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message,
)

from .ui import FEEDBACK, MENU

MAX_LENGTH = 600
PAGE_SIZE = 3


def inbox_page(store, before=None):
    rows = store.recent_feedback(PAGE_SIZE + 1, before)
    if not rows:
        return "На этой странице отзывов нет." if before else "Отзывов пока нет.", None

    page = rows[:PAGE_SIZE]
    entries = []
    for row in page:
        date = datetime.fromtimestamp(row["created"]).astimezone().strftime("%d.%m.%Y %H:%M %Z")
        entries.append(f'#{row["id"]} · {date}\n{row["body"]}')

    buttons = []
    if before is not None:
        buttons.append(InlineKeyboardButton(text="К последним", callback_data="feedback-inbox:latest"))
    if len(rows) > PAGE_SIZE:
        buttons.append(InlineKeyboardButton(text="Более ранние", callback_data=f'feedback-inbox:{page[-1]["id"]}'))
    markup = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None
    return "Обратная связь\n\n" + "\n\n".join(entries), markup


def feedback_routes(store, owner_id):
    router = Router()
    router.message.filter(F.chat.type == "private")
    no_preview = LinkPreviewOptions(is_disabled=True)

    @router.message(Command("feedback"))
    @router.message(F.text == FEEDBACK)
    async def ask(message: Message):
        prompt = await message.answer(
            f"Что улучшить в SmartKruzhok? Ответь на это сообщение одним текстом до {MAX_LENGTH} символов. "
            "Отзыв увидит владелец бота.",
            reply_markup=ForceReply(input_field_placeholder="Идея, отзыв или описание проблемы"),
        )
        store.open_feedback(message.chat.id, prompt.message_id)

    @router.message(Command("feedback_inbox"))
    async def inbox(message: Message):
        if not message.from_user or message.from_user.id != owner_id or message.chat.id != owner_id:
            await message.answer("Отзывы доступны владельцу бота.", reply_markup=MENU)
            return
        text, markup = inbox_page(store)
        await message.answer(text, reply_markup=markup, link_preview_options=no_preview)

    @router.callback_query(F.data.regexp(r"^feedback-inbox:(latest|[0-9]{1,18})$"))
    async def inbox_navigation(query):
        if (not isinstance(query.message, Message) or query.message.chat.type != "private"
                or query.from_user.id != owner_id or query.message.chat.id != owner_id):
            await query.answer("Отзывы доступны владельцу бота.")
            return
        _, cursor = query.data.split(":", 1)
        text, markup = inbox_page(store, None if cursor == "latest" else int(cursor))
        await query.answer()
        await query.message.edit_text(text, reply_markup=markup, link_preview_options=no_preview)

    async def replying_to_feedback(message: Message):
        reply = message.reply_to_message
        return reply is not None and store.feedback_prompt(message.chat.id, reply.message_id)

    @router.message(replying_to_feedback)
    async def submit(message: Message):
        body = (message.text or "").strip()
        if not 1 <= len(body) <= MAX_LENGTH:
            await message.answer(f"Ответь на исходный запрос текстом до {MAX_LENGTH} символов.")
            return
        if store.save_feedback(message.chat.id, message.reply_to_message.message_id, body):
            await message.answer("Спасибо! Отзыв сохранён.", reply_markup=MENU)

    return router
