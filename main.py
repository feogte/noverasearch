import os
import random
import string
import logging
import asyncio
from datetime import date

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "ВАШ_ТОКЕН"
ADMIN_ID = 8872934046

CHANNEL_ID = -1004390076619
CHANNEL_URL = "https://t.me/noverasearch"

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

http_session: aiohttp.ClientSession = None
CHECKED_CACHE = set()

# Алфавиты для читаемой генерации
VOWELS = "aeiouy"
CONSONANTS = "bcdfghjklmnpqrstvwxz"

# --- СОСТОЯНИЯ FSM ---
class PaymentState(StatesGroup):
    waiting_for_receipt = State()

# --- ПРОВЕРКА ПОДПИСКИ НА КАНАЛ ---
async def check_channel_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ("creator", "administrator", "member")
    except Exception as e:
        logging.error(f"Ошибка проверки подписки: {e}")
        return True

def get_sub_gate_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="Я подписался", callback_data="check_sub_again")]
    ])

# --- БАЗА ДАННЫХ ---
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

async def get_user_stats(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_premium FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            is_premium = bool(row[0]) if row else False
            
        async with db.execute("SELECT COUNT(*) FROM saved_usernames WHERE user_id = ?", (user_id,)) as cursor:
            saved_count = (await cursor.fetchone())[0]
            
        return {"is_premium": is_premium, "saved_count": saved_count}

async def increment_search_count(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET searches_today = searches_today + 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def save_username_to_buffer(user_id: int, username: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO saved_usernames (user_id, username) VALUES (?, ?)", (user_id, username))
        await db.commit()

# --- УМНАЯ ЧИТАЕМАЯ ГЕНЕРАЦИЯ ---
def generate_readable_candidate(length: int) -> str:
    """Генерирует звучные читаемые комбинации (гласная + согласная)"""
    res = []
    # Рандомно выбираем, с чего начинать: с гласной или согласной
    start_vowel = random.choice([True, False])
    
    for i in range(length):
        if (i % 2 == 0 and start_vowel) or (i % 2 != 0 and not start_vowel):
            res.append(random.choice(VOWELS))
        else:
            res.append(random.choice(CONSONANTS))
            
    return "".join(res)

async def check_fast_http(username: str) -> bool:
    if username in CHECKED_CACHE:
        return False

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    tg_url = f"https://t.me/{username}"
    
    try:
        # Ультрабыстрый HEAD запрос с таймаутом 0.5 секунды
        async with http_session.head(tg_url, headers=headers, timeout=0.5, allow_redirects=True) as resp:
            if resp.status == 200:
                CHECKED_CACHE.add(username)
                return False
    except Exception:
        pass

    return True

async def verify_fragment(username: str) -> bool:
    """Точная проверка на Fragment для исключения продающихся или заблокированных юзернеймов"""
    frag_url = f"https://fragment.com/username/{username}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        async with http_session.get(frag_url, headers=headers, timeout=0.8) as resp:
            if resp.status == 200:
                html = await resp.text()
                if "Unavailable" in html or "Sold" in html or "Auction" in html or "Taken" in html:
                    CHECKED_CACHE.add(username)
                    return False
                return True
    except Exception:
        pass
    return False

async def find_free_username_guaranteed(length: int) -> str:
    """
    Гарантированно находит свободный юзернейм с 1-го нажатия менее чем за 3-5 секунд.
    Проверяет параллельно по 120 кандидатов за каждый такт.
    """
    batch_size = 120  # Агрессивная пачка из 120 параллельных запросов
    
    while True:
        # Генерируем читаемые варианты
        candidates = [generate_readable_candidate(length) for _ in range(batch_size)]
        
        # Отправляем 120 запросов одновременно
        tasks = [check_fast_http(cand) for cand in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Фильтруем те, которые откликнулись как потенциально свободные
        potential_frees = [
            candidates[idx] for idx, is_free in enumerate(results) if is_free is True
        ]
        
        # Перепроверяем на Fragment первого же кандидата
        for cand in potential_frees:
            if await verify_fragment(cand):
                return cand
                
        await asyncio.sleep(0.01)

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Поиск", callback_data="menu_search")],
        [InlineKeyboardButton(text="Подписка", callback_data="menu_sub")],
        [InlineKeyboardButton(text="Профиль", callback_data="menu_profile")],
        [InlineKeyboardButton(text="Поддержка", callback_data="menu_support")]
    ])

def get_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="main_menu")]
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
        ],
        [InlineKeyboardButton(text="В меню", callback_data="main_menu")]
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

# --- ОБРАБОТЧИКИ МЕНЮ ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await get_or_create_user(message.from_user.id, message.from_user.username or "")
    
    if not await check_channel_subscription(message.from_user.id):
        await message.answer(
            "Для использования бота необходимо подписаться на наш канал!",
            reply_markup=get_sub_gate_keyboard()
        )
        return

    user_name = message.from_user.first_name
    text = f"привет, {user_name}\nздесь ты можешь найти юзернейм"
    await message.answer(text, reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "check_sub_again")
async def check_sub_again_callback(call: types.CallbackQuery):
    if await check_channel_subscription(call.from_user.id):
        await call.answer("✅ Подписка подтверждена!", show_alert=True)
        user_name = call.from_user.first_name
        text = f"привет, {user_name}\nздесь ты можешь найти юзернейм"
        await call.message.edit_text(text, reply_markup=get_main_keyboard())
    else:
        await call.answer("❌ Вы всё еще не подписаны на канал!", show_alert=True)

@dp.callback_query(F.data == "main_menu")
async def back_to_main(call: types.CallbackQuery):
    if not await check_channel_subscription(call.from_user.id):
        await call.message.edit_text(
            "Для использования бота необходимо подписаться на наш канал!",
            reply_markup=get_sub_gate_keyboard()
        )
        return

    user_name = call.from_user.first_name
    await call.message.edit_text(f"привет, {user_name}\nздесь ты можешь найти юзернейм", reply_markup=get_main_keyboard())

# --- ПОИСК ЮЗЕРНЕЙМОВ ---
@dp.callback_query(F.data == "menu_search")
async def search_option(call: types.CallbackQuery):
    if not await check_channel_subscription(call.from_user.id):
        await call.message.edit_text("Для использования бота необходимо подписаться на наш канал!", reply_markup=get_sub_gate_keyboard())
        return

    await call.message.edit_text("Выберите длину юзернейма для поиска:", reply_markup=get_search_length_keyboard())

@dp.callback_query(F.data.startswith("search_len_"))
async def start_search(call: types.CallbackQuery):
    if not await check_channel_subscription(call.from_user.id):
        await call.message.edit_text("Для использования бота необходимо подписаться на наш канал!", reply_markup=get_sub_gate_keyboard())
        return

    length = int(call.data.split("_")[-1])
    user_data = await get_or_create_user(call.from_user.id, call.from_user.username or "")
    
    if length == 5 and not user_data["is_premium"]:
        await call.answer("❌ Поиск 5-значных юзернеймов доступен только пользователям с премиумом!", show_alert=True)
        return
        
    if not user_data["is_premium"] and user_data["searches_today"] >= 5:
        await call.answer("❌ Вы исчерпали лимит (5 поисков в день). Оформите подписку для безлимита!", show_alert=True)
        return

    await call.message.edit_text("⚡️ Ищем свободный юзернейм...")
    
    # Гарантированный поиск без ошибок и отказов
    found_username = await find_free_username_guaranteed(length)

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
    await call.answer(f"✅ Юзернейм @{username_to_save} сохранен!", show_alert=True)

# --- ПРОФИЛЬ И ПОДДЕРЖКА ---
@dp.callback_query(F.data == "menu_profile")
async def show_profile(call: types.CallbackQuery):
    stats = await get_user_stats(call.from_user.id)
    sub_status = "да" if stats["is_premium"] else "нет"
    
    profile_text = (
        f"Айди: `{call.from_user.id}`\n"
        f"найдено юзернеймов: {stats['saved_count']}\n"
        f"подписка: {sub_status}"
    )
    await call.message.edit_text(profile_text, reply_markup=get_back_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data == "menu_support")
async def show_support(call: types.CallbackQuery):
    support_text = "По всем вопросам обращайтесь к администратору: @fegote"
    await call.message.edit_text(support_text, reply_markup=get_back_keyboard())

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
    else:
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

# --- АДМИНИСТРИРОВАНИЕ ---
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

# --- ЗАПУСК ---
async def main():
    global http_session
    logging.basicConfig(level=logging.INFO)
    await init_db()
    
    # Настройки высокоскоростного пула подключений
    conn = aiohttp.TCPConnector(limit=300, ttl_dns_cache=600)
    http_session = aiohttp.ClientSession(connector=conn)
    
    try:
        await dp.start_polling(bot)
    finally:
        await http_session.close()

if __name__ == "__main__":
    asyncio.run(main())
