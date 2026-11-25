import asyncio
import logging
import os
from datetime import timezone
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
)
import database as db

logging.basicConfig(level=logging.INFO)

# Поставь False, когда чат раскрутится и захочешь сделать поиск по полу платным (VIP за Stars)
FREE_MODE = True

BOT_TOKEN = "".join(os.getenv("BOT_TOKEN", "").split())
DATABASE_URL = "".join(os.getenv("DATABASE_URL", "").split())
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

waiting_queue: dict[int, dict] = {}
active_chats: dict[int, int] = {}
last_target: dict[int, str] = {}


def gender_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👨 Я парень", callback_data="set_gender:M"),
            InlineKeyboardButton(text="👩 Я девушка", callback_data="set_gender:F"),
        ]
    ])


def main_menu_kb():
    if FREE_MODE:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🎲 Случайный собеседник")],
                [KeyboardButton(text="👩 Искать девушку"), KeyboardButton(text="👨 Искать парня")],
                [KeyboardButton(text="👤 Профиль и пригласить друзей")],
            ],
            resize_keyboard=True,
        )
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎲 Случайный собеседник")],
            [KeyboardButton(text="👩 Искать девушку 👑"), KeyboardButton(text="👨 Искать парня 👑")],
            [KeyboardButton(text="👑 VIP-доступ"), KeyboardButton(text="👤 Профиль и пригласить друзей")],
        ],
        resize_keyboard=True,
    )


def searching_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎲 Искать любого (быстрее)")],
            [KeyboardButton(text="❌ Отменить поиск")],
        ],
        resize_keyboard=True,
    )


def in_chat_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
                        [KeyboardButton(text="✂️ Камень, ножницы, бумага")],
            [KeyboardButton(text="➡️ Следующий"), KeyboardButton(text="🛑 Завершить чат")],
            [KeyboardButton(text="⚠️ Пожаловаться")],
        ],
        resize_keyboard=True,
    )


def after_chat_kb(partner_id: int = 0):
    buttons = []
    if partner_id:
        buttons.append([InlineKeyboardButton(text="❤️ Оценить собеседника (Лайк)", callback_data=f"like:{partner_id}")])
    if FREE_MODE:
        buttons.append([InlineKeyboardButton(text="🔎 Найти нового собеседника", callback_data="search:ANY")])
    else:
        buttons.append([InlineKeyboardButton(text="🔄 Вернуть прошлого собеседника (25 ⭐️)", callback_data="buy:reconnect")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def vip_shop_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👑 VIP на 24 часа — 35 ⭐️", callback_data="buy:vip_1d")],
        [InlineKeyboardButton(text="👑 VIP на 7 дней — 120 ⭐️", callback_data="buy:vip_7d")],
        [InlineKeyboardButton(text="👑 VIP на 30 дней — 300 ⭐️", callback_data="buy:vip_30d")],
    ])


def is_compatible(u1_id: int, u1_data: dict, u2_id: int, u2_data: dict) -> bool:
    if u1_id == u2_id:
        return False
    u1_ok = (u1_data["target"] == "ANY") or (u1_data["target"] == u2_data["gender"])
    u2_ok = (u2_data["target"] == "ANY") or (u2_data["target"] == u1_data["gender"])
    return u1_ok and u2_ok


async def start_search(user_id: int, target_gender: str):
    if user_id in active_chats or user_id in ai_chats:
        await bot.send_message(user_id, "⚠️ Ты уже в диалоге! Сначала заверши текущий чат.", reply_markup=in_chat_kb())
        return

    user = await db.get_user(user_id)
    if not user or user["is_banned"]:
        await bot.send_message(user_id, "🚫 Ваш аккаунт заблокирован за нарушение правил.")
        return

    if not user["gender"]:
        await bot.send_message(user_id, "Сначала укажи свой пол:", reply_markup=gender_kb())
        return

    if not FREE_MODE and target_gender != "ANY" and not await db.is_vip(user_id):
        await bot.send_message(
            user_id,
            "👑 **Поиск по полу доступен только с VIP-статусом!**\n\n"
            "Ты можешь открыть VIP за звёзды Telegram или бесплатно пригласить 2 друзей по своей ссылке.",
            parse_mode="Markdown",
            reply_markup=vip_shop_kb(),
        )
        return

    last_target[user_id] = target_gender
    my_data = {"gender": user["gender"], "target": target_gender}

    matched_id = None
    for candidate_id, cand_data in list(waiting_queue.items()):
        if is_compatible(user_id, my_data, candidate_id, cand_data):
            matched_id = candidate_id
            break

    if matched_id:
        waiting_queue.pop(matched_id, None)
        waiting_queue.pop(user_id, None)
        cancel_ai_timer(matched_id)
        cancel_ai_timer(user_id)
        active_chats[user_id] = matched_id
        active_chats[matched_id] = user_id

        await db.save_last_partner(user_id, matched_id)
        await db.save_last_partner(matched_id, user_id)

        msg = (
            "✅ **Собеседник найден! Общайтесь.**\n"
            "Используй кнопки ниже, чтобы переключить диалог или завершить его."
        )
        await bot.send_message(user_id, msg, parse_mode="Markdown", reply_markup=in_chat_kb())
        await bot.send_message(matched_id, msg, parse_mode="Markdown", reply_markup=in_chat_kb())
    else:
        waiting_queue[user_id] = my_data
        cancel_ai_timer(user_id)
        ai_wait_tasks[user_id] = asyncio.create_task(connect_ai_after_timeout(user_id, target_gender))
        mode_text = {
            "ANY": "случайного собеседника",
            "F": "девушку",
            "M": "парня",
        }.get(target_gender, "собеседника")
        await bot.send_message(
            user_id,
            f"🔎 **Ищем {mode_text}...**\n"
            f"Сейчас в поиске: {len(waiting_queue)} чел.\n"
            "Как только кто-то подключится, чат начнётся автоматически.",
            parse_mode="Markdown",
            reply_markup=searching_kb(),
        )


async def stop_chat(user_id: int, notify_partner: bool = True):
        cancel_ai_timer(user_id)
        waiting_queue.pop(user_id, None)
        if ai_chats.pop(user_id, None) is not None:
        await bot.send_message(user_id, "🛑 **Диалог завершён.**", parse_mode="Markdown", reply_markup=main_menu_kb())
        return
    partner_id = active_chats.pop(user_id, None)
    if partner_id:
        active_chats.pop(partner_id, None)
        await bot.send_message(
            user_id,
            "🛑 **Диалог завершён.**",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        ) 
        await bot.send_message(user_id, "Продолжить общение:", reply_markup=after_chat_kb(partner_id))

        if notify_partner:
            await bot.send_message(
                partner_id,
                "🛑 **Собеседник завершил диалог.**",
                parse_mode="Markdown",
                reply_markup=main_menu_kb(),
            )
            await bot.send_message(partner_id, "Продолжить общение:", reply_markup=after_chat_kb(user_id))


@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    ref_id = None
    if command.args and command.args.isdigit():
        ref_id = int(command.args)

    user, rewarded_inviter = await db.get_or_create_user(message.from_user.id, ref_id)
    if rewarded_inviter and not FREE_MODE:
        try:
            await bot.send_message(
                rewarded_inviter,
                "🎉 **По твоей ссылке пришли 2 друга!**\nТебе начислен **VIP-доступ на 24 часа** бесплатно!",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    if user["is_banned"]:
        await message.answer("🚫 Ваш аккаунт заблокирован за нарушение правил.")
        return

    if not user["gender"]:
        await message.answer(
            "👋 **Добро пожаловать в Анонимный Чат!**\n\n"
            "Общайся с незнакомцами без регистрации и анкет.\n"
            "Поиск по полу сейчас **полностью бесплатный**!\n\n"
            "Для начала выбери свой пол:",
            parse_mode="Markdown",
            reply_markup=gender_kb(),
        )
    else:
        await message.answer(
            "👋 **Главное меню**\nВыбирай, кого будем искать:",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )


@dp.callback_query(F.data.startswith("set_gender:"))
async def cb_set_gender(callback: types.CallbackQuery):
    gender = callback.data.removeprefix("set_gender:")
    await db.set_gender(callback.from_user.id, gender)
    await callback.message.delete()
    await callback.message.answer(
        "✅ Пол сохранён! Выбирай режим поиска внизу экрана:",
        reply_markup=main_menu_kb(),
    )


@dp.callback_query(F.data.startswith("search:"))
async def cb_search(callback: types.CallbackQuery):
    target = callback.data.removeprefix("search:")
    await callback.answer()
    await start_search(callback.from_user.id, target)


@dp.message(F.text.in_({"🎲 Случайный собеседник", "🎲 Искать любого (быстрее)"}))
async def btn_random(message: types.Message):
    waiting_queue.pop(message.from_user.id, None)
    await start_search(message.from_user.id, "ANY")


@dp.message(F.text.in_({"👩 Искать девушку", "👩 Искать девушку 👑"}))
async def btn_find_f(message: types.Message):
    await start_search(message.from_user.id, "F")


@dp.message(F.text.in_({"👨 Искать парня", "👨 Искать парня 👑"}))
async def btn_find_m(message: types.Message):
    await start_search(message.from_user.id, "M")


@dp.message(F.text == "❌ Отменить поиск")
async def btn_cancel_search(message: types.Message):
    cancel_ai_timer(message.from_user.id)
    waiting_queue.pop(message.from_user.id, None)
    await message.answer("❌ Поиск отменён.", reply_markup=main_menu_kb())


@dp.message(F.text == "🛑 Завершить чат")
async def btn_stop_chat(message: types.Message):
    await stop_chat(message.from_user.id)


@dp.message(F.text == "➡️ Следующий")
async def btn_next_chat(message: types.Message):
    uid = message.from_user.id
    prev_target = last_target.get(uid, "ANY")
    await stop_chat(uid)
    await start_search(uid, prev_target)


@dp.message(F.text == "⚠️ Пожаловаться")
async def btn_report(message: types.Message):
    uid = message.from_user.id
    partner_id = active_chats.get(uid)
    if not partner_id:
        await message.answer("Ты сейчас не в диалоге.")
        return

    reps = await db.add_report(partner_id)
    await stop_chat(uid)
    await message.answer("🛡 Жалоба отправлена. Диалог завершён.")
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ Жалоба на пользователя `{partner_id}` (всего жалоб: {reps}).\nЗабанить: `/ban {partner_id}`",
                parse_mode="Markdown",
            )
        except Exception:
            pass


@dp.message(F.text == "👑 VIP-доступ")
async def btn_vip(message: types.Message):
    if FREE_MODE:
        await message.answer("🎉 Сейчас в боте действует акция — все функции и поиск по полу **полностью бесплатны**!", parse_mode="Markdown")
        return
    vip = await db.is_vip(message.from_user.id)
    status = "✅ **Активен**" if vip else "❌ **Не активен**"
    await message.answer(
        f"👑 **VIP-статус:** {status}\n\n"
        "**Что даёт VIP:**\n"
        "• Поиск собеседника строго по полу (девушки / парни)\n"
        "• Отправка фото, голосовых и кружочков в чате\n\n"
        "Выбери тариф в Telegram Stars:",
        parse_mode="Markdown",
        reply_markup=vip_shop_kb(),
    )


@dp.message(F.text == "👤 Профиль и пригласить друзей")
async def btn_profile(message: types.Message):
    user = await db.get_user(message.from_user.id)
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={message.from_user.id}"
    g_text = "Парень 👨" if user["gender"] == "M" else "Девушка 👩"
    await message.answer(
        f"👤 **Твой профиль**\n"
        f"• Пол: {g_text}\n"
        f"• Проведено чатов: {user['chats_count']}\n"
                f"• Приглашено друзей: {user['referrals']}\n"
        f"• ❤️ Получено лайков: {user_likes.get(message.from_user.id, 0)}\n\n"
        f"🔗 **Твоя ссылка для приглашения друзей** (отправь другу, чтобы в чате было больше людей):\n"
        f"`{ref_link}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Сменить пол", callback_data="change_gender")]
        ]),
    )


@dp.callback_query(F.data == "change_gender")
async def cb_change_gender(callback: types.CallbackQuery):
    await callback.message.answer("Выбери свой пол:", reply_markup=gender_kb())
    await callback.answer()


PRODUCTS = {
    "vip_1d": ("VIP на 24 часа", "Поиск по полу и медиа в чате на 1 день", 35),
    "vip_7d": ("VIP на 7 дней", "Поиск по полу и медиа в чате на 7 дней", 120),
    "vip_30d": ("VIP на 30 дней", "Поиск по полу и медиа в чате на 30 дней", 300),
    "reconnect": ("Возврат собеседника", "Повторное соединение с прошлым собеседником", 25),
}


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: types.CallbackQuery):
    item = callback.data.removeprefix("buy:")
    if item not in PRODUCTS:
        return
    title, desc, stars = PRODUCTS[item]
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=title,
        description=desc,
        payload=item,
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars)],
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload
    uid = message.from_user.id

    if payload == "vip_1d":
        await db.add_vip_hours(uid, 24)
        await message.answer("🎉 **VIP на 24 часа активирован!**", parse_mode="Markdown", reply_markup=main_menu_kb())
    elif payload == "vip_7d":
        await db.add_vip_hours(uid, 24 * 7)
        await message.answer("🎉 **VIP на 7 дней активирован!**", parse_mode="Markdown", reply_markup=main_menu_kb())
    elif payload == "vip_30d":
        await db.add_vip_hours(uid, 24 * 30)
        await message.answer("🎉 **VIP на 30 дней активирован!**", parse_mode="Markdown", reply_markup=main_menu_kb())
    elif payload == "reconnect":
        user = await db.get_user(uid)
        partner = user["last_partner"] if user else None
        if not partner or partner in active_chats:
            await db.add_vip_hours(uid, 24)
            await message.answer(
                "⚠️ Собеседник уже ушёл в другой чат, поэтому мы начислили тебе **VIP на 24 часа** в качестве компенсации!",
                parse_mode="Markdown",
                reply_markup=main_menu_kb(),
            )
        else:
            waiting_queue.pop(uid, None)
            waiting_queue.pop(partner, None)
            active_chats[uid] = partner
            active_chats[partner] = uid
            await bot.send_message(uid, "↩️ **Вы снова соединены с прошлым собеседником!**", parse_mode="Markdown", reply_markup=in_chat_kb())
            await bot.send_message(partner, "↩️ **Прошлый собеседник вернул вас в диалог!**", parse_mode="Markdown", reply_markup=in_chat_kb())


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    total, vips, banned = await db.get_stats()
    await message.answer(
        f"📊 **Статистика бота:**\n"
        f"• Всего пользователей: {total}\n"
        f"• Активных VIP: {vips}\n"
        f"• Забанено: {banned}\n"
        f"• Сейчас в поиске: {len(waiting_queue)}\n"
        f"• Сейчас в чатах: {len(active_chats) // 2}",
        parse_mode="Markdown",
    )


@dp.message(Command("vip"))
async def cmd_give_vip(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID or not command.args:
        return
    target_str, sep, days_str = command.args.partition(" ")
    if sep and target_str.isdigit() and days_str.isdigit():
        await db.add_vip_hours(int(target_str), int(days_str) * 24)
        await message.answer(f"✅ Выдан VIP пользователю `{target_str}` на {days_str} дн.", parse_mode="Markdown")


@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID or not command.args or not command.args.isdigit():
        return
    target = int(command.args)
    await db.set_ban(target, True)
    await stop_chat(target)
    await message.answer(f"🚫 Пользователь `{target}` забанен.", parse_mode="Markdown")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID or not command.args:
        return
    ids = await db.get_all_user_ids()
    sent = 0
    for uid in ids:
        try:
            await bot.send_message(uid, command.args)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await message.answer(f"📢 Рассылка завершена. Доставлено: {sent} из {len(ids)}.")

user_likes = {}
liked_pairs = set()

@dp.callback_query(F.data.startswith("like:"))
async def cb_like_partner(callback: types.CallbackQuery):
    uid = callback.from_user.id
    partner_id = int(callback.data.split(":")[1])
    if (uid, partner_id) in liked_pairs:
        await callback.answer("Ты уже оценил этого собеседника!", show_alert=True)
        return
    liked_pairs.add((uid, partner_id))
    user_likes[partner_id] = user_likes.get(partner_id, 0) + 1
    await callback.message.edit_text("💖 Спасибо! Твой лайк отправлен собеседнику.")
    await callback.answer("Лайк отправлен!")
    try:
        await bot.send_message(partner_id, f"💖 **Прошлый собеседник поставил тебе лайк!**\nВсего у тебя лайков: **{user_likes[partner_id]}**", parse_mode="Markdown")
    except Exception:
        pass
rps_games = {}
RPS_LABELS = {"rock": "🪨 Камень", "scissors": "✂️ Ножницы", "paper": "📄 Бумага"}

@dp.message(F.text == "✂️ Камень, ножницы, бумага")
async def btn_start_rps(message: types.Message):
    uid = message.from_user.id
    partner_id = active_chats.get(uid)
    if not partner_id:
        await message.answer("Игра доступна только в диалоге!")
        return
    rps_games[frozenset({uid, partner_id})] = {}
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🪨 Камень", callback_data="rps:rock"),
        InlineKeyboardButton(text="✂️ Ножницы", callback_data="rps:scissors"),
        InlineKeyboardButton(text="📄 Бумага", callback_data="rps:paper"),
    ]])
    await bot.send_message(uid, "🎮 **Камень, ножницы, бумага!** Делай выбор:", parse_mode="Markdown", reply_markup=kb)
    await bot.send_message(partner_id, "🎮 **Собеседник позвал в «Камень, ножницы, бумага»!** Делай выбор:", parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("rps:"))
async def cb_rps_choice(callback: types.CallbackQuery):
    uid = callback.from_user.id
    partner_id = active_chats.get(uid)
    if not partner_id:
        await callback.answer("Чат завершён.", show_alert=True)
        return
    choice = callback.data.split(":")[1]
    pair = frozenset({uid, partner_id})
    game = rps_games.setdefault(pair, {})
    if uid in game:
        await callback.answer("Ты уже выбрал! Ждём соперника.", show_alert=True)
        return
    game[uid] = choice
    await callback.message.edit_text(f"✅ Твой выбор: **{RPS_LABELS[choice]}**. Ждём соперника...", parse_mode="Markdown")
    await callback.answer()
    if partner_id not in game:
        await bot.send_message(partner_id, "⚡ Собеседник уже сделал ход! Твоя очередь.")
        return
    p_choice = game[partner_id]
    rps_games.pop(pair, None)
    wins = {("rock", "scissors"), ("scissors", "paper"), ("paper", "rock")}
    if choice == p_choice:
        r1 = r2 = "🤝 **Ничья!**"
    elif (choice, p_choice) in wins:
        r1, r2 = "🏆 **Ты победил!**", "😔 **Ты проиграл!**"
    else:
        r1, r2 = "😔 **Ты проиграл!**", "🏆 **Ты победил!**"
    await bot.send_message(uid, f"🎲 Итог: ты — {RPS_LABELS[choice]}, соперник — {RPS_LABELS[p_choice]}\n{r1}", parse_mode="Markdown")
    await bot.send_message(partner_id, f"🎲 Итог: ты — {RPS_LABELS[p_choice]}, соперник — {RPS_LABELS[choice]}\n{r2}", parse_mode="Markdown")


@dp.message()
async def relay_messages(message: types.Message):
    uid = message.from_user.id
        if uid in ai_chats:
        if not message.text:
            await message.answer("я щас без наушников и инет тупит, напиши текстом")
            return
        await bot.send_chat_action(uid, "typing")
        await asyncio.sleep(random.uniform(1.2, 2.4))
        reply = await generate_ai_reply(uid, message.text)
        if uid in ai_chats:
            await message.answer(reply)
        return
    partner_id = active_chats.get(uid)
    if not partner_id:
        await message.answer("Нажми кнопку поиска ниже, чтобы найти собеседника 👇", reply_markup=main_menu_kb())
        return

    is_media = bool(message.photo or message.video or message.voice or message.video_note or message.document)
    if not FREE_MODE and is_media and not await db.is_vip(uid):
        await message.answer("👑 Отправка фото, видео и голосовых доступна только для **VIP-пользователей** (защита от спама).", parse_mode="Markdown")
        return

    try:
        await message.copy_to(chat_id=partner_id)
    except Exception:
        await stop_chat(uid, notify_partner=False)
        await message.answer("⚠️ Собеседник заблокировал бота. Диалог завершён.", reply_markup=main_menu_kb())


async def health_check(request):
    return web.Response(text="Anon Chat Bot is running!")


async def main():
    await db.init_db(DATABASE_URL)
    app = web.Application()
    app.router.add_get("/", health_check)

    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
