import os
import asyncio
import json
import random
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from aiogram.dispatcher.filters import Command
from aiogram.contrib.fsm_storage.memory import MemoryStorage
import aioredis
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))
REDIS_URL = os.getenv("REDIS_URL")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
redis = None
scheduler = AsyncIOScheduler()

QUIZ_DURATION_MINUTES = 2 * 24 * 60  # 2 дня
QUESTION_TIMEOUT = 35
TOTAL_QUESTIONS = 30

ALL_QUESTIONS = []

def load_all_questions():
    global ALL_QUESTIONS
    with open("questions.json", "r", encoding="utf-8") as f:
        ALL_QUESTIONS = json.load(f)

async def generate_weekly_questions():
    all_used = await redis.get("used_questions")
    used_indices = json.loads(all_used) if all_used else []

    available = [i for i in range(len(ALL_QUESTIONS)) if i not in used_indices]
    if len(available) < TOTAL_QUESTIONS:
        used_indices = []
        available = list(range(len(ALL_QUESTIONS)))

    weekly = random.sample(available, TOTAL_QUESTIONS)
    used_indices += weekly

    await redis.set("weekly_questions", json.dumps(weekly))
    await redis.set("used_questions", json.dumps(used_indices))

async def get_weekly_questions():
    data = await redis.get("weekly_questions")
    if data:
        return json.loads(data)
    return []

async def send_invitation(user_id):
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("✅ Участвовать", callback_data="start_quiz"))
    try:
        await bot.send_message(user_id, "🔥 Приглашаем на еженедельную викторину! Жми, чтобы начать:", reply_markup=keyboard)
    except Exception as e:
        print(f"[ERROR] Не удалось отправить приглашение {user_id}: {e}")

async def send_question(user_id, question_index):
    weekly_q_indices = await get_weekly_questions()
    if question_index >= len(weekly_q_indices):
        await finish_quiz(user_id)
        return

    q_data = ALL_QUESTIONS[weekly_q_indices[question_index]]
    text = f"Вопрос {question_index + 1} из {TOTAL_QUESTIONS}:\n\n{q_data['question']}"
    options = q_data['options']

    keyboard = InlineKeyboardMarkup(row_width=2)
    for i, option in enumerate(options):
        keyboard.insert(InlineKeyboardButton(option, callback_data=f"answer:{i}:{question_index}"))

    await redis.set(f"{user_id}:q{question_index}:answered", "0", expire=QUESTION_TIMEOUT + 5)
    await bot.send_message(user_id, text, reply_markup=keyboard)
    asyncio.create_task(question_timer(user_id, question_index))

async def question_timer(user_id, question_index):
    await asyncio.sleep(QUESTION_TIMEOUT)
    answered = await redis.get(f"{user_id}:q{question_index}:answered")
    if answered != b"1":
        await redis.set(f"{user_id}:q{question_index}:skipped", "1")
        await bot.send_message(user_id, "⏳ Время вышло! Следующий вопрос:")
        await send_question(user_id, question_index + 1)

@dp.callback_query_handler(lambda c: c.data.startswith("answer:"))
async def handle_answer(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    parts = callback_query.data.split(":")
    answer_index = int(parts[1])
    question_index = int(parts[2])

    await redis.set(f"{user_id}:q{question_index}:answered", "1")
    weekly_q_indices = await get_weekly_questions()
    q_data = ALL_QUESTIONS[weekly_q_indices[question_index]]
    correct_index = q_data['correct_index']

    if answer_index == correct_index:
        await redis.incr(f"{user_id}:correct_answers")
        await callback_query.answer("✅ Верно!", show_alert=False)
    else:
        await callback_query.answer("❌ Неверно", show_alert=False)

    await send_question(user_id, question_index + 1)

async def finish_quiz(user_id):
    correct = await redis.get(f"{user_id}:correct_answers") or b"0"
    await redis.set(f"{user_id}:finished", 1)
    await bot.send_message(user_id, f"🏁 Викторина завершена!\nВы правильно ответили на {int(correct)} из {TOTAL_QUESTIONS} вопросов.")

@dp.callback_query_handler(lambda c: c.data == "start_quiz")
async def start_quiz(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    already_finished = await redis.get(f"{user_id}:finished")
    if already_finished == b"1":
        await callback_query.message.answer("⚠️ Вы уже прошли викторину. Используйте /resetme, чтобы начать заново (только для админа).")
        return
    await redis.set(f"{user_id}:correct_answers", 0)
    await redis.set(f"{user_id}:finished", 0)
    await redis.sadd("registered_users", user_id)
    await send_question(user_id, 0)

@dp.message_handler(commands=["resetall"])
async def reset_all_users(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("⛔ Только админ может сбросить прогресс всех пользователей.")
        return
    users = await redis.smembers("registered_users")
    for uid in users:
        uid_int = int(uid)
        keys = [f"{uid_int}:correct_answers", f"{uid_int}:finished"]
        for i in range(TOTAL_QUESTIONS):
            keys.append(f"{uid_int}:q{i}:answered")
            keys.append(f"{uid_int}:q{i}:skipped")
        await redis.delete(*keys)
    await message.reply("✅ Прогресс всех пользователей сброшен. Вы можете снова тестировать викторину.")

@dp.message_handler(commands=["allstats"])
async def show_all_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("⛔ Только админ может смотреть общую статистику.")
        return
    users = await redis.smembers("registered_users")
    report = []
    for uid in users:
        uid_int = int(uid)
        correct = await redis.get(f"{uid_int}:correct_answers") or b"0"
        finished = await redis.get(f"{uid_int}:finished") or b"0"

        try:
            user_info = await bot.get_chat(uid_int)
            display_name = user_info.full_name
            if user_info.username:
                display_name += f" (@{user_info.username})"
        except:
            display_name = f"{uid_int}"

        report.append(f"👤 {display_name}: {'✅' if finished == b'1' else '❌'} | Правильных: {int(correct)}")

    await message.reply("📊 Общая статистика:\n" + "\n".join(report))
@dp.message_handler(commands=["start"])
async def start_handler(message: types.Message):
    if message.chat.type != "private" and message.from_user.id != ADMIN_ID:
        return
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("👤 Перейти к боту", url=f"https://t.me/{(await bot.get_me()).username}"),
        InlineKeyboardButton("✅ Участвовать", callback_data="start_quiz")
    )
    await message.answer("Привет! Готов к викторине? Нажми кнопку ниже:", reply_markup=keyboard)

@dp.message_handler(commands=["adminstart", "admin_start"])
async def admin_start(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("⛔ Только админ может запускать викторину.")
        return

    await generate_weekly_questions()

    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("👤 Перейти к боту", url=f"https://t.me/{(await bot.get_me()).username}"),
        InlineKeyboardButton("✅ Участвовать", callback_data="start_quiz")
    )

    try:
        await bot.send_message(GROUP_CHAT_ID, "📢 Викторина начинается! Перейдите к боту и нажмите \"Участвовать\":", reply_markup=keyboard)
    except Exception as e:
        print(f"[ERROR] Не удалось отправить сообщение в группу: {e}")

    registered_users = await redis.smembers("registered_users")
    for uid in registered_users:
        try:
            await send_invitation(int(uid))
        except Exception as e:
            print(f"Ошибка при отправке приглашения {uid}: {e}")

@dp.message_handler(commands=["resetme"])
async def reset_user(message: types.Message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        await message.reply("⛔ Только админ может сбросить прогресс.")
        return

    keys = [f"{user_id}:correct_answers", f"{user_id}:finished"]
    for i in range(TOTAL_QUESTIONS):
        keys.append(f"{user_id}:q{i}:answered")
        keys.append(f"{user_id}:q{i}:skipped")
    await redis.delete(*keys)
    await redis.srem("registered_users", user_id)
    await message.reply("✅ Прогресс сброшен. Вы можете пройти викторину заново.")

@dp.message_handler(commands=["stats"])
async def show_stats(message: types.Message):
    user_id = message.from_user.id
    correct = await redis.get(f"{user_id}:correct_answers") or b"0"
    finished = await redis.get(f"{user_id}:finished") or b"0"
    await message.reply(f"📊 Ваша статистика:\nПройдено: {'Да' if finished == b'1' else 'Нет'}\nПравильных ответов: {int(correct)}")

@dp.message_handler(commands=["allstats"])
async def show_all_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("⛔ Только админ может смотреть общую статистику.")
        return
    users = await redis.smembers("registered_users")
    report = []
    for uid in users:
        uid_int = int(uid)
        correct = await redis.get(f"{uid_int}:correct_answers") or b"0"
        finished = await redis.get(f"{uid_int}:finished") or b"0"
        report.append(f"👤 {uid_int}: {'✅' if finished == b'1' else '❌'} | Правильных: {int(correct)}")
    await message.reply("📊 Общая статистика:\n" + "\n".join(report))

@dp.message_handler(commands=["stopbot"])
async def stop_bot(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("⛔ Только админ может остановить бота.")
        return
    await message.reply("🛑 Бот будет остановлен.")
    await bot.session.close()
    scheduler.shutdown(wait=False)
    exit()

async def scheduled_quiz():
    await generate_weekly_questions()
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("👤 Перейти к боту", url=f"https://t.me/{(await bot.get_me()).username}"),
        InlineKeyboardButton("✅ Участвовать", callback_data="start_quiz")
    )

    try:
        await bot.send_message(GROUP_CHAT_ID, "📢 Наступила пятница! Викторина начинается!", reply_markup=keyboard)
    except Exception as e:
        print(f"[ERROR] Не удалось отправить сообщение в группу: {e}")

    registered_users = await redis.smembers("registered_users")
    for uid in registered_users:
        try:
            await send_invitation(int(uid))
        except Exception as e:
            print(f"Ошибка при автоотправке приглашения {uid}: {e}")

async def on_startup(_):
    global redis
    redis = await aioredis.create_redis_pool(REDIS_URL)
    load_all_questions()
    scheduler.add_job(scheduled_quiz, 'cron', day_of_week='fri', hour=18, minute=0)
    scheduler.start()
    print(f"Бот запущен. GROUP_CHAT_ID = {GROUP_CHAT_ID}")

if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
