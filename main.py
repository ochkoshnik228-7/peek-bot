import os
import sqlite3
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
import uvicorn
import telebot
from threading import Thread
import time

# === Настройки ===
BOT_TOKEN = "8314578862:AAFmgkZTLNaPFQCiDiqCZtUNeTxWK3MghFA"
WEBHOOK_URL = "https://peek-bot.onrender.com"
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"

bot = telebot.TeleBot(BOT_TOKEN)
app = FastAPI()

# === База данных ===
conn = sqlite3.connect("data.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance REAL
)""")
cursor.execute("""CREATE TABLE IF NOT EXISTS bets (
    bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    match TEXT,
    team TEXT,
    amount REAL,
    coef REAL,
    result_checked INTEGER DEFAULT 0
)""")
conn.commit()

# === Получение матчей с Winline (только CS) ===
def get_cs_matches():
    url = "https://m.winline.ru/stavki/sport/kibersport"
    resp = requests.get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    matches = []

    for match in soup.find_all("a", href=True):
        if "counter-strike" in match.get("href", "").lower():
            title = match.get_text(strip=True)
            link = "https://m.winline.ru" + match["href"]
            coef_tags = match.find_all("span")
            coefs = []
            for c in coef_tags:
                try:
                    val = float(c.get_text(strip=True).replace(",", "."))
                    coefs.append(val)
                except:
                    pass
            if len(coefs) >= 2:
                matches.append((title, coefs[0], coefs[1], link))
    return matches

# === Команды ===
@bot.message_handler(commands=["start"])
def start_cmd(message):
    user_id = message.from_user.id
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    if cursor.fetchone() is None:
        cursor.execute("INSERT INTO users (user_id, balance) VALUES (?, ?)", (user_id, 10000))
        conn.commit()
        bot.reply_to(message, "Добро пожаловать! На ваш счёт начислено 10000 Peek.")
    else:
        bot.reply_to(message, "С возвращением!")
        
@bot.message_handler(commands=["balance"])
def balance_cmd(message):
    user_id = message.from_user.id
    cursor.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    bal = cursor.fetchone()
    if bal:
        bot.reply_to(message, f"Ваш баланс: {bal[0]} Peek")
    else:
        bot.reply_to(message, "Вы ещё не начали. Напишите /start")

@bot.message_handler(commands=["matches"])
def matches_cmd(message):
    matches = get_cs_matches()
    if not matches:
        bot.reply_to(message, "Сейчас нет матчей по CS.")
        return
    text = "Доступные матчи:\n"
    for i, m in enumerate(matches, start=1):
        text += f"{i}. {m[0]} — {m[1]} / {m[2]}\n"
    bot.reply_to(message, text)

@bot.message_handler(commands=["help"])
def help_cmd(message):
    bot.reply_to(message, "/start — начать игру и получить 10000 Peek\n/balance — баланс\n/matches — список матчей\n/pick1 — ставка на первую команду\n/pick2 — ставка на вторую команду")

@bot.message_handler(commands=["pick1", "pick2"])
def pick_cmd(message):
    try:
        matches = get_cs_matches()
        if not matches:
            bot.reply_to(message, "Нет доступных матчей.")
            return
        match = matches[0]  # для примера берём первый матч
        team = "Первая команда" if message.text == "/pick1" else "Вторая команда"
        coef = match[1] if team == "Первая команда" else match[2]
        user_id = message.from_user.id
        cursor.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        bal = cursor.fetchone()
        if not bal:
            bot.reply_to(message, "Сначала /start")
            return
        amount = 1000  # фиксированная ставка для примера
        if bal[0] < amount:
            bot.reply_to(message, "Недостаточно Peek.")
            return
        cursor.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, user_id))
        cursor.execute("INSERT INTO bets (user_id, match, team, amount, coef) VALUES (?, ?, ?, ?, ?)",
                       (user_id, match[0], team, amount, coef))
        conn.commit()
        bot.reply_to(message, f"Ставка принята: {team} ({coef}). Сумма: {amount} Peek")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")

# === Проверка результатов ===
def check_results():
    while True:
        cursor.execute("SELECT * FROM bets WHERE result_checked=0")
        for bet in cursor.fetchall():
            bet_id, user_id, match, team, amount, coef, result_checked = bet
            # Тут можно парсить страницу матча, но для примера просто рандом
            import random
            win = random.choice([True, False])
            if win:
                prize = round(amount * coef, 2)
                cursor.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (prize, user_id))
                bot.send_message(user_id, f"Поздравляем! Вы выиграли {prize} Peek. Баланс обновлён.")
            else:
                bot.send_message(user_id, f"Упс... вы проиграли :(")
            cursor.execute("UPDATE bets SET result_checked=1 WHERE bet_id=?", (bet_id,))
            conn.commit()
        time.sleep(60)

# === Webhook ===
@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    json_str = await request.body()
    update = telebot.types.Update.de_json(json_str.decode("utf-8"))
    bot.process_new_updates([update])
    return "ok"

@app.on_event("startup")
async def startup():
    requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={WEBHOOK_URL}{WEBHOOK_PATH}")
    Thread(target=check_results, daemon=True).start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

