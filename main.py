import os
import sqlite3
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from flask import Flask, request
import telebot

BOT_TOKEN = "8314578862:AAFmgkZTLNaPFQCiDiqCZtUNeTxWK3MghFA"
WEBHOOK_URL = "https://peek-bot.onrender.com"  # замени на свой Render URL

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== База данных =====
conn = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance REAL
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS bets (
    bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    match_id TEXT,
    team TEXT,
    amount REAL,
    coef REAL,
    status TEXT
)
""")
conn.commit()

# ===== Парсинг матчей с Winline =====
def get_cs_matches():
    url = "https://m.winline.ru/stavki/sport/kibersport"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(r.text, "lxml")
    matches = []
    now_ts = int(datetime.utcnow().timestamp())
    seen = set()

    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True).lower()
        if "counter-strike" not in text:
            continue
        if "live" in text or "в эфире" in text:
            continue

        href = a["href"]
        link = href if href.startswith("http") else "https://m.winline.ru" + href
        parts = a.get_text(" ", strip=True).split("/")
        if len(parts) != 2:
            continue

        team1, team2 = parts[0].strip(), parts[1].strip()
        nums = []
        nearby = a.parent.find_all(text=True) if a.parent else []
        for t in nearby:
            t = t.strip().replace(",", ".")
            if t.replace(".", "", 1).isdigit():
                try:
                    nums.append(float(t))
                except:
                    pass
        if len(nums) < 2:
            continue

        coef1, coef2 = nums[0], nums[1]
        start_ts = None
        for attr in ("data-time", "data-start", "data-unix"):
            if a.has_attr(attr):
                try:
                    v = int(a[attr])
                    if v > 1e10:
                        v //= 1000
                    start_ts = v
                    break
                except:
                    pass
        if start_ts is None or start_ts <= now_ts:
            continue

        match_id = link
        if match_id in seen:
            continue
        seen.add(match_id)

        matches.append({
            "match_id": match_id,
            "team1": team1,
            "team2": team2,
            "coef1": coef1,
            "coef2": coef2,
            "start_ts": start_ts
        })
    return matches

# ===== Команды =====
@bot.message_handler(commands=["start"])
def start(msg):
    user_id = msg.from_user.id
    cursor.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO users (user_id, balance) VALUES (?, ?)", (user_id, 10000))
        conn.commit()
        bot.send_message(user_id, "Добро пожаловать! На ваш счет начислено 10000 Peek.")
    else:
        bot.send_message(user_id, "С возвращением!")
    bot.send_message(user_id, "Доступные команды:\n/matches - список матчей\n/balance - баланс\n/help - помощь")

@bot.message_handler(commands=["balance"])
def balance(msg):
    user_id = msg.from_user.id
    cursor.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if row:
        bot.send_message(user_id, f"Ваш баланс: {row[0]:.2f} Peek")
    else:
        bot.send_message(user_id, "Вы не зарегистрированы. Напишите /start")

@bot.message_handler(commands=["matches"])
def matches(msg):
    match_list = get_cs_matches()
    if not match_list:
        bot.send_message(msg.chat.id, "Нет предстоящих матчей по CS.")
        return
    text = "Предстоящие матчи:\n"
    for i, m in enumerate(match_list, start=1):
        text += f"{i}. {m['team1']} ({m['coef1']}) / {m['team2']} ({m['coef2']})\n"
    bot.send_message(msg.chat.id, text)

@bot.message_handler(commands=["help"])
def help_cmd(msg):
    bot.send_message(msg.chat.id,
    "/start — приветствие + 10000 Peek\n"
    "/balance — баланс\n"
    "/matches — список матчей\n"
    "/pick1 — поставить на первую команду\n"
    "/pick2 — поставить на вторую команду"
    )

# ===== Flask Webhook =====
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = telebot.types.Update.de_json(request.stream.read().decode("utf-8"))
    bot.process_new_updates([update])
    return "ok", 200

@app.route("/", methods=["GET"])
def index():
    return "Bot is running", 200

if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


