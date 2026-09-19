import os
import random
import string
import logging
import asyncio
from datetime import date

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient
from telethon.tl.functions.account import CheckUsernameRequest
from telethon.errors import UsernameInvalidError, UsernameOccupiedError, SessionPasswordNeededError

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "8205193243:AAG9fjSnrP3wSvxDpHepVcW0ya-h0jRQk50"
ADMIN_ID = 8872934046

API_ID = 31799721
API_HASH = "eb2181220b3b8a0b6a7f93cd8075a559"
PHONE_NUMBER = "+77024728757"

DB_NAME = "novera_search.db"

PRICES = {
    "1d": {"label": "1 день", "stars": "15 звезд", "rub": "20₽", "kzt": "144тг"},
    "3d": {"label": "3 дня", "stars": "30 звезд", "rub": "40₽", "kzt": "288тг"},
    "1w": {"label": "неделя", "stars": "65 звезд", "rub": "75₽", "kzt": "540тг"},
    "1m": {"label": "месяц", "stars": "125 звезд", "rub": "150₽", "kzt": "1100тг"},
    "life": {"label": "навсегда", "stars": "250 звезд", "rub": "300₽", "kzt": "2200тг"},
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
telethon_client = TelegramClient('checker_session', API_ID, API_HASH)

# --- СОСТОЯНИЯ FSM ---
class PaymentState(StatesGroup):
    waiting_for_receipt = State()

class AuthState(StatesGroup):
    waiting_for_code = State()
    waiting_for_password = State()

# --- БОК РАБОТЫ С БАЗОЙ ДАННЫХ ---
async def init_db():
    db_dir = os.path.dirname(DB_NAME)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                is_premium INTEGER DEFAULT 0,
                premium_until TEXT,
                searches_today INTEGER DEFAULT 0,
                last_search_date TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS saved_usernames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                duration TEXT,
                price TEXT,
                currency TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)
        await db.commit()

async def get_or_create_user(user_id: int, username: str):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_premium, searches_today, last_search_date FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await db.execute("INSERT INTO users (user_id, username, last_search_date) VALUES (?, ?, ?)", (user_id, username, today))
                await db.commit()
                return {"is_premium": False, "searches_today": 0}
            
            is_premium, searches_today, last_search_date = row
            if last_search_date != today:
                await db.execute("UPDATE users SET searches_today = 0, last_search_date = ? WHERE user_id = ?", (today, user_id))
                await db.commit()
                searches_today = 0
                
            return {"is_premium": bool(is_premium), "searches_today": searches_today}

async def increment_search_count(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET searches_today = searches_today + 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def save_username_to_buffer(user_id: int, username: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO saved_usernames (user_id, username) VALUES (?, ?)", (user_id, username))
        await db.commit()

# --- БЛОК ПРОВЕРКИ ЮЗЕРНЕЙМОВ ---
def generate_candidate(length: int) -> str:
    return ''.join(random.choices(string.ascii_lowercase, k=length))

async def check_fragment(username: str) -> bool:
    url = f"https://fragment.com/username/{username}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                html = await resp.text()
                if "Unavailable" in html or "Auction" in html or "Sold" in html:
                    return False
                return True
            return True

async def check_telegram(username: str) -> bool:
    try:
        result = await telethon_client(CheckUsernameRequest(username=username))
        return result
    except (UsernameOccupiedError, UsernameInvalidError):
        return False
    except Exception:
        return False

async def find_free_username(length: int) -> str:
    while True:
        candidate = generate_candidate(length)
        if await check_telegram(candidate):
            if await check_fragment(candidate):
                return candidate

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Поиск", callback_data="menu_search")],
        [InlineKeyboardButton(text="Подписка", callback_data="menu_sub")],
        [InlineKeyboardButton(text="Профиль", callback_data="menu_profile")],
        [InlineKeyboardButton(text="Поддержка", callback_data="menu_support")]
    ])

def get_search_length_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="5 букв (Премиум)", callback_data="search_len_5")],
        [InlineKeyboardButton(text="6 букв", callback_data="search_len_6")],
        [InlineKeyboardButton(text="Назад", callback_data="main_menu")]
    ])

def get_result_keyboard(username: str, length: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Далее", callback_data=f"next_search_{length}"),
            InlineKeyboardButton(text="Сохранить", callback_data=f"save_{username}")
        ]
    ])

def get_subscription_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день", callback_data="sub_1d")],
        [InlineKeyboardButton(text="3 дня", callback_data="sub_3d")],
        [InlineKeyboardButton(text="Неделя", callback_data="sub_1w")],
        [InlineKeyboardButton(text="Месяц", callback_data="sub_1m")],
        [InlineKeyboardButton(text="Навсегда", callback_data="sub_life")],
        [InlineKeyboardButton(text="Назад", callback_data="main_menu")]
    ])

def get_currency_keyboard(period_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="₽ рубли", callback_data=f"pay_{period_key}_rub"),
            InlineKeyboardButton(text="⭐️ звезды", callback_data=f"pay_{period_key}_stars"),
            InlineKeyboardButton(text="₸ тенге", callback_data=f"pay_{period_key}_kzt")
        ],
        [InlineKeyboardButton(text="Назад", callback_data="menu_sub")]
    ])

# --- АВТОРИЗАЦИЯ ОВНЕРА (/auth) ---
@dp.message(Command("auth"))
async def cmd_auth(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if not telethon_client.is_connected():
        await telethon_client.connect()

    if await telethon_client.is_user_authorized():
        await message.answer("✅ Сессия Telethon уже авторизована и готова к работе!")
        return

    await message.answer("⏳ Отправляем запрос кода авторизации на номер +77024728757...")
    
    try:
        sent_code = await telethon_client.send_code_request(PHONE_NUMBER)
        await state.update_data(phone_code_hash=sent_code.phone_code_hash)
        await state.set_state(AuthState.waiting_for_code)
        await message.answer("📩 Код подтверждения отправлен в Telegram.\nПришлите код в чат (можно в формате `123.45` или `12345`):", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка при отправке кода: {e}")

@dp.message(AuthState.waiting_for_code)
async def process_auth_code(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    raw_code = message.text.strip()
    clean_code = "".join(filter(str.isdigit, raw_code))

    if not clean_code:
        await message.answer("❌ Неверный формат кода. Отправьте код цифрами (например, `123.45` или `12345`).")
        return

    data = await state.get_data()
    phone_code_hash = data.get("phone_code_hash")

    try:
        await telethon_client.sign_in(phone=PHONE_NUMBER, code=clean_code, phone_code_hash=phone_code_hash)
        await state.clear()
        await message.answer("🎉 Авторизация аккаунта владельца успешно завершена! Поиск юзернеймов активен.")
    except SessionPasswordNeededError:
        await state.set_state(AuthState.waiting_for_password)
        await message.answer("🔐 На аккаунте включен 2FA (облачный пароль). Введите ваш 2FA пароль:")
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Ошибка авторизации: {e}")

@dp.message(AuthState.waiting_for_password)
async def process_auth_password(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    password = message.text.strip()
    try:
        await telethon_client.sign_in(password=password)
        await state.clear()
        await message.answer("🎉 Авторизация с 2FA успешно завершена!")
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Ошибка при вводе пароля: {e}")

# --- ОБРАБОТЧИКИ МЕНЮ И ПОИСКА ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_name = message.from_user.first_name
    await get_or_create_user(message.from_user.id, message.from_user.username or "")
    
    text = f"привет, {user_name}\nздесь ты можешь найти юзернейм"
    await message.answer(text, reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "main_menu")
async def back_to_main(call: types.CallbackQuery):
    user_name = call.from_user.first_name
    await call.message.edit_text(f"привет, {user_name}\nздесь ты можешь найти юзернейм", reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "menu_search")
async def search_option(call: types.CallbackQuery):
    await call.message.edit_text("Выберите длину юзернейма для поиска:", reply_markup=get_search_length_keyboard())

@dp.callback_query(F.data.startswith("search_len_"))
async def start_search(call: types.CallbackQuery):
    length = int(call.data.split("_")[-1])
    user_data = await get_or_create_user(call.from_user.id, call.from_user.username or "")
    
    if length == 5 and not user_data["is_premium"]:
        await call.answer("❌ Поиск 5-значных юзернеймов доступен только пользователям с премиумом!", show_alert=True)
        return
        
    if not user_data["is_premium"] and user_data["searches_today"] >= 5:
        await call.answer("❌ Вы исчерпали лимит (5 поисков в день). Оформите подписку для безлимита!", show_alert=True)
        return

    await call.message.edit_text("🔎 Ищем свободный юзернейм, подождите...")
    
    found_username = await find_free_username(length)
    if not user_data["is_premium"]:
        await increment_search_count(call.from_user.id)
        
    text = f"Юзернейм найден!\n@{found_username}"
    await call.message.edit_text(text, reply_markup=get_result_keyboard(found_username, length))

@dp.callback_query(F.data.startswith("next_search_"))
async def next_search(call: types.CallbackQuery):
    await start_search(call)

@dp.callback_query(F.data.startswith("save_"))
async def save_username(call: types.CallbackQuery):
    username_to_save = call.data.split("_")[1]
    await save_username_to_buffer(call.from_user.id, username_to_save)
    await call.answer(f"✅ Юзернейм @{username_to_save} сохранен в буфер!", show_alert=True)

# --- ПОДПИСКА И ОПЛАТА ---
@dp.callback_query(F.data == "menu_sub")
async def sub_menu(call: types.CallbackQuery):
    text = (
        "Подписка в NoveraSearch:\n"
        "• безлимит на поиск\n"
        "• поиск 5 значных юзернеймов\n\n"
        "цены:\n"
        "1 день - 15 звезд / 20₽ / 144тг\n"
        "3 дня - 30 звезд / 40₽ / 288тг\n"
        "неделя - 65 звезд / 75₽ / 540тг\n"
        "месяц - 125 звезд / 150₽ / 1100тг\n"
        "навсегда: 250 звезд / 300₽ / 2200тг"
    )
    await call.message.edit_text(text, reply_markup=get_subscription_keyboard())

@dp.callback_query(F.data.startswith("sub_"))
async def select_duration(call: types.CallbackQuery):
    period_key = call.data.split("_")[1]
    await call.message.edit_text("выберите способ оплаты", reply_markup=get_currency_keyboard(period_key))

@dp.callback_query(F.data.startswith("pay_"))
async def process_payment_choice(call: types.CallbackQuery, state: FSMContext):
    _, period_key, currency = call.data.split("_")
    period_info = PRICES[period_key]
    price_str = period_info[currency]
    duration_label = period_info["label"]
    
    if currency == "rub":
        reqs = "Т-банк: `+79313716777` (Получатель: Тимур/Наталья)"
        text = f"Отправьте {price_str} на реквизиты ({reqs}).\n\nПосле оплаты отправьте чек/скриншот в этот чат."
    elif currency == "kzt":
        reqs = "Карта: `4400 4303 5526 3416` (Получатель: Мерейхан.Т)"
        text = f"Отправьте {price_str} на реквизиты ({reqs}).\n\nПосле оплаты отправьте чек/скриншот в этот чат."
    else:  # stars
        text = f"Отправьте {price_str} на аккаунт @fegote.\n\nПосле отправки скиньте скриншот подтверждения в этот чат."
        
    await state.update_data(period=duration_label, price=price_str, currency=currency)
    await state.set_state(PaymentState.waiting_for_receipt)
    await call.message.edit_text(text, parse_mode="Markdown")

@dp.message(PaymentState.waiting_for_receipt)
async def handle_receipt(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    await message.answer("Спасибо! Ваша заявка отправлена администратору на проверку.")
    
    admin_text = (
        "Заявка на подписку!\n"
        f"Пользователь: @{message.from_user.username or 'без_юзернейма'}\n"
        f"ID: `{message.from_user.id}`\n"
        f"Цена: {data['price']}\n"
        f"На какой срок купили: {data['period']}"
    )
    
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="выдать", callback_data=f"adm_grant_{message.from_user.id}"),
            InlineKeyboardButton(text="отменить", callback_data=f"adm_cancel_{message.from_user.id}")
        ]
    ])
    
    if message.photo:
        await bot.send_photo(ADMIN_ID, photo=message.photo[-1].file_id, caption=admin_text, reply_markup=admin_kb, parse_mode="Markdown")
    else:
        await bot.send_message(ADMIN_ID, text=f"{admin_text}\n\n(Чек отправлен текстом/документом)", reply_markup=admin_kb, parse_mode="Markdown")

# --- АДМИНИСТРАТИВНЫЙ БЛОК ---
@dp.callback_query(F.data.startswith("adm_grant_"))
async def admin_grant(call: types.CallbackQuery):
    target_user_id = int(call.data.split("_")[-1])
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET is_premium = 1 WHERE user_id = ?", (target_user_id,))
        await db.commit()
        
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Подписка выдана!", show_alert=True)
    await bot.send_message(target_user_id, "🎉 Ваша заявка одобрена! Премиум подписка активирована.")

@dp.callback_query(F.data.startswith("adm_cancel_"))
async def admin_cancel(call: types.CallbackQuery):
    target_user_id = int(call.data.split("_")[-1])
    
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Заявка отменена.", show_alert=True)
    await bot.send_message(target_user_id, "❌ Ваша заявка на оплату подписки была отклонена.")

# --- СТАРТ ПРИЛОЖЕНИЯ ---
async def main():
    logging.basicConfig(level=logging.INFO)
    
    # 1. Автоматическая инициализация базы данных
    await init_db()
    logging.info("База данных инициализирована.")
    
    # 2. Подключение Telethon
    await telethon_client.connect()
    
    # 3. Старт бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
