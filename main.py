# main.py
import os
import re
import sqlite3
import asyncio
from datetime import datetime, timedelta
import time
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
import uvicorn
import json

# ---------------- CONFIG ----------------
# Твой токен (вставлен по просьбе пользователя)
BOT_TOKEN = "8314578862:AAFmgkZTLNaPFQCiDiqCZtUNeTxWK3MghFA"

# Укажи в Render переменную окружения WEBHOOK_URL = https://<your-service>.onrender.com
WEBHOOK_BASE = os.getenv("WEBHOOK_URL")  # пример: https://peek-bot.onrender.com
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_FULL = (WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH) if WEBHOOK_BASE else None

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# БД и поведение
DB_FILE = "peek_bot.db"
START_BALANCE = 10000.0
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))  # фон. интервал, сек

# Источник матчей
WINLINE_MOBILE = "https://m.winline.ru/stavki/sport/kibersport"

# ---------------- APP ----------------
app = FastAPI()

# ---------------- DB init ----------------
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  balance REAL
);
CREATE TABLE IF NOT EXISTS bets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  match_id TEXT,
  match_name TEXT,
  team TEXT,
  amount REAL,
  coef REAL,
  placed_at TEXT,
  status TEXT
);
CREATE TABLE IF NOT EXISTS matches_cache (
  match_id TEXT PRIMARY KEY,
  name TEXT,
  team1 TEXT,
  team2 TEXT,
  start_ts INTEGER,
  coef1 REAL,
  coef2 REAL,
  link TEXT,
  status TEXT
);
""")
conn.commit()

# in-memory sessions for awaiting amount input
user_sessions = {}  # user_id -> {"stage":"await_amount","match_id":..., "team":..., "coef":..., "match_name":...}

# ---------------- Telegram helpers ----------------
def tg_send(chat_id: int, text: str, reply_markup: dict = None, parse_mode: str = "HTML"):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print("tg_send error:", e)

def tg_answer_callback(callback_id: str, text: str = None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    try:
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json=payload, timeout=10)
    except Exception as e:
        print("tg_answer_callback error:", e)

# ---------------- Winline parsing ----------------
def fetch(url: str):
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print("fetch error:", e, url)
        return None

def parse_matches_from_winline(html: str):
    """
    Парсит мобильную страницу Winline и возвращает список матчей Counter-Strike,
    которые начинаются в будущем (start_ts > now). Возвращает список dict:
    {match_id, name, team1, team2, start_ts, coef1, coef2, link}
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")

    results = []
    now_ts = int(datetime.utcnow().timestamp())
    # На мобильной странице события часто представлены ссылками <a> — ищем все и фильтруем
    anchors = soup.find_all("a", href=True)
    for a in anchors:
        # текст самого блока
        block_text = a.get_text(" ", strip=True)
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        combined = (block_text + " " + parent_text).lower()

        # фильтруем по Counter-Strike
        if not ("counter" in combined or "counter-strike" in combined or "counter strike" in combined):
            continue

        # исключаем Live: часто появляется слово live или «в эфире»
        if "live" in combined or "в эфире" in combined or "онлайн" in combined:
            continue

        # Попытка получить ссылку на событие
        href = a["href"]
        link = href if href.startswith("http") else ("https://m.winline.ru" + href)

        # Попытка получить команды (формат 'TEAM1 / TEAM2' или 'TEAM1 — TEAM2')
        m = re.search(r"([A-Za-z0-9\-\.\s]{2,60})\s*[—\-\\/]\s*([A-Za-z0-9\-\.\s]{2,60})", block_text)
        if not m:
            # попробовать внутри родителя
            m = re.search(r"([A-Za-z0-9\-\.\s]{2,60})\s*[—\-\\/]\s*([A-Za-z0-9\-\.\s]{2,60})", parent_text)
        if not m:
            # запасной вариант: найти два больших слов рядом
            parts = block_text.split("/")
            if len(parts) >= 2:
                team1 = parts[0].strip()
                team2 = parts[1].strip()
                name = f"{team1} / {team2}"
            else:
                continue
        else:
            team1 = m.group(1).strip()
            team2 = m.group(2).strip()
            name = f"{team1} / {team2}"

        # коэффициенты — искать ближайшие числа
        coef1 = coef2 = None
        nearby_texts = a.parent.find_all(text=re.compile(r"\d+[\.,]\d+")) if a.parent else []
        nums = []
        for t in nearby_texts:
            s = t.strip().replace(",", ".")
            try:
                nums.append(float(s))
            except:
                pass
        if len(nums) >= 2:
            coef1, coef2 = nums[0], nums[1]

        # время старта: поиск атрибутов data-time/data-start, или попытка парсинга даты в тексте
        start_ts = None
        for attr in ("data-time","data-start","data-unix","data-ts"):
            if a.has_attr(attr):
                try:
                    v = int(a[attr])
                    if v > 1e10:
                        v = v // 1000
                    start_ts = v
                    break
                except:
                    pass
        if start_ts is None:
            # попытка найти дату/время в тексте, формат может быть '12:30', '01.08 20:00' и т.п.
            # найдем шаблон dd.mm hh:mm или hh:mm
            dtm = re.search(r"(\d{1,2}\.\d{1,2}\s+\d{1,2}:\d{2})", parent_text)
            if dtm:
                try:
                    txt = dtm.group(1)
                    # добавить текущий год
                    now = datetime.utcnow()
                    parsed = datetime.strptime(txt + f" {now.year}", "%d.%m %H:%M %Y")
                    start_ts = int(parsed.timestamp())
                except:
                    start_ts = None
            else:
                tm = re.search(r"(\d{1,2}:\d{2})", parent_text)
                if tm:
                    try:
                        now = datetime.utcnow()
                        hhmm = tm.group(1)
                        parsed = datetime.strptime(hhmm, "%H:%M").replace(year=now.year, month=now.month, day=now.day)
                        # если уже прошло сегодня — возможно завтра
                        if int(parsed.timestamp()) < now_ts - 60:
                            parsed = parsed + timedelta(days=1)
                        start_ts = int(parsed.timestamp())
                    except:
                        start_ts = None

        # фильтр: оставляем только предстоящие (start_ts in future)
        if start_ts is None:
            # если времени нет — пропускаем (надёжнее)
            continue
        if start_ts <= now_ts:
            # начало уже в прошлом или сейчас — пропускаем (мы хотим предстоящие, не live)
            continue

        match_id = re.sub(r"\W+", "_", link)
        results.append({
            "match_id": match_id,
            "name": name,
            "team1": team1,
            "team2": team2,
            "start_ts": start_ts,
            "coef1": coef1,
            "coef2": coef2,
            "link": link
        })

    # unique
    uniq = {}
    for m in results:
        uniq[m["match_id"]] = m
    return list(uniq.values())

def update_matches_cache():
    html = fetch(WINLINE_MOBILE)
    matches = parse_matches_from_winline(html)
    for m in matches:
        cur.execute("""
            INSERT OR REPLACE INTO matches_cache
            (match_id, name, team1, team2, start_ts, coef1, coef2, link, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (m["match_id"], m["name"], m["team1"], m["team2"], m["start_ts"] or 0,
              m["coef1"] or 0.0, m["coef2"] or 0.0, m["link"], "upcoming"))
    conn.commit()
    return matches

# ---------------- Commands logic ----------------
def cmd_start(chat_id: int, user_id: int):
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    if not r:
        cur.execute("INSERT INTO users (user_id, balance) VALUES (?, ?)", (user_id, START_BALANCE))
        conn.commit()
        tg_send(chat_id, f"🎉 Привет! Тебе начислено {START_BALANCE:.0f} Peek.")
    else:
        tg_send(chat_id, "Вы уже зарегистрированы. /balance — посмотреть баланс.")

def cmd_balance(chat_id: int, user_id: int):
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    if not r:
        tg_send(chat_id, "Сначала отправьте /start")
        return
    tg_send(chat_id, f"💼 Баланс: {r[0]:.2f} Peek")

def cmd_help(chat_id: int):
    txt = ("📘 Как ставить:\n"
           "1) /matches — получить список предстоящих матчей Counter-Strike (не Live).\n"
           "2) Нажать на матч — появятся кнопки Поставить на 1 или 2 команду.\n"
           "3) Ввести сумму (число). Сумма списывается сразу.\n"
           "4) По окончании матча бот пришлёт результат и выплату при выигрыше (amount * coef).\n")
    tg_send(chat_id, txt)

def cmd_matches(chat_id: int):
    matches = update_matches_cache()
    if not matches:
        tg_send(chat_id, "⚠️ Нет предстоящих матчей Counter-Strike.")
        return
    kb = {"inline_keyboard": []}
    for m in matches:
        start = datetime.utcfromtimestamp(m["start_ts"]).strftime("%Y-%m-%d %H:%M UTC")
        label = f"{m['team1']} / {m['team2']} — {start}"
        kb["inline_keyboard"].append([{"text": label, "callback_data": f"match|{m['match_id']}"}])
    tg_send(chat_id, "Выберите матч (предстоящие, не Live):", reply_markup=kb)

def cmd_place_pick(chat_id: int, user_id: int, match_id: str, pick:int):
    cur.execute("SELECT name, team1, team2, coef1, coef2, start_ts FROM matches_cache WHERE match_id=?", (match_id,))
    row = cur.fetchone()
    if not row:
        tg_send(chat_id, "Информация по матчу недоступна.")
        return
    name, t1, t2, c1, c2, start_ts = row
    team = t1 if pick == 1 else t2
    coef = c1 if pick == 1 else c2
    # start interaction: ask amount
    user_sessions[user_id] = {"stage":"await_amount", "match_id":match_id, "team":team, "coef":coef, "match_name":name}
    tg_send(chat_id, f"Вы выбрали <b>{team}</b> ({coef}). Введите сумму в Peek (например: 100).")

# ---------------- Webhook receiver ----------------
@app.post(WEBHOOK_PATH)
async def webhook_receiver(request: Request):
    data = await request.json()
    # handle message
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text = msg.get("text","").strip()

        # if awaiting amount
        sess = user_sessions.get(user_id)
        if sess and sess.get("stage") == "await_amount":
            # expect a number
            if re.match(r"^\d+(\.\d+)?$", text):
                amount = float(text)
                # check balance
                cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
                r = cur.fetchone()
                if not r:
                    tg_send(chat_id, "Сначала отправьте /start")
                    user_sessions.pop(user_id, None)
                    return {"ok": True}
                balance = r[0]
                if amount <= 0 or amount > balance:
                    tg_send(chat_id, f"Неверная сумма. Баланс: {balance:.2f} Peek")
                    return {"ok": True}
                # store bet
                cur.execute("INSERT INTO bets (user_id, match_id, match_name, team, amount, coef, placed_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (user_id, sess["match_id"], sess["match_name"], sess["team"], amount, sess["coef"], datetime.utcnow().isoformat(), "pending"))
                cur.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
                conn.commit()
                tg_send(chat_id, f"✅ Ставка принята: {amount:.2f} Peek на <b>{sess['team']}</b> (коэф {sess['coef']}). Удачи!")
                user_sessions.pop(user_id, None)
                return {"ok": True}
            else:
                tg_send(chat_id, "Пожалуйста, введите сумму числом (например: 150).")
                return {"ok": True}

        # commands
        if text == "/start":
            cmd_start(chat_id, user_id)
            return {"ok": True}
        if text == "/balance":
            cmd_balance(chat_id, user_id)
            return {"ok": True}
        if text == "/help":
            cmd_help(chat_id)
            return {"ok": True}
        if text == "/matches":
            cmd_matches(chat_id)
            return {"ok": True}
        # support typed /pick1_<id> or /pick2_<id>
        m = re.match(r"^/pick([12])_?(.+)?$", text)
        if m:
            pick = int(m.group(1))
            mid = m.group(2)
            if not mid:
                tg_send(chat_id, "Неверная команда /pick. Используйте кнопки после /matches.")
                return {"ok": True}
            cmd_place_pick(chat_id, user_id, mid, pick)
            return {"ok": True}

        tg_send(chat_id, "Неизвестная команда. Введите /help.")
        return {"ok": True}

    # callback_query
    if "callback_query" in data:
        cb = data["callback_query"]
        cid = cb["id"]
        from_id = cb["from"]["id"]
        data_str = cb.get("data","")
        if data_str.startswith("match|"):
            _, mid = data_str.split("|",1)
            # show pick buttons
            cur.execute("SELECT name, team1, team2, coef1, coef2 FROM matches_cache WHERE match_id=?", (mid,))
            row = cur.fetchone()
            if not row:
                tg_send(from_id, "Информация по матчу недоступна.")
                tg_answer_callback(cid, "Ошибка")
                return {"ok": True}
            name, t1, t2, c1, c2 = row
            text = f"Вы выбрали: <b>{name}</b>\n1) {t1} — {c1}\n2) {t2} — {c2}\n\nНажмите кнопку для ставки:"
            kb = {"inline_keyboard":[
                [{"text": f"Поставить на {t1}", "callback_data": f"pick|{mid}|1"}],
                [{"text": f"Поставить на {t2}", "callback_data": f"pick|{mid}|2"}]
            ]}
            tg_send(from_id, text, reply_markup=kb)
            tg_answer_callback(cid)
            return {"ok": True}
        if data_str.startswith("pick|"):
            _, mid, picknum = data_str.split("|",2)
            picknum = int(picknum)
            cmd_place_pick(from_id, from_id, mid, picknum)
            tg_answer_callback(cid, "ОК")
            return {"ok": True}

    return {"ok": True}

# ---------------- Winner parsing ----------------
def parse_winner_from_match_page(html: str, team1: str, team2: str):
    if not html:
        return None
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True).lower()
    # check for explicit winner words near team name
    for team in (team1, team2):
        if not team:
            continue
        tn = team.lower()
        # look for patterns like "team ... победил" or "team win"
        if re.search(re.escape(tn) + r".{0,40}(выигр|побед|win|winner|won)", text, re.IGNORECASE):
            return team
    # check score patterns like "2:0" nearby team names
    m = re.search(r"(\d+)\s*[:\-]\s*(\d+)", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            return team1
        if b > a:
            return team2
    return None

# ---------------- Background checker ----------------
async def background_checker():
    await asyncio.sleep(5)
    while True:
        try:
            # find pending bets and their matches
            cur.execute("SELECT DISTINCT match_id FROM bets WHERE status='pending'")
            rows = cur.fetchall()
            if not rows:
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            for (mid,) in rows:
                cur.execute("SELECT link, team1, team2 FROM matches_cache WHERE match_id=?", (mid,))
                info = cur.fetchone()
                if not info:
                    continue
                link, team1, team2 = info
                html = fetch(link) if link else None
                winner = parse_winner_from_match_page(html, team1, team2)
                if winner:
                    # process bets
                    cur.execute("SELECT id, user_id, team, amount, coef FROM bets WHERE match_id=? AND status='pending'", (mid,))
                    bets = cur.fetchall()
                    for bet_id, uid, team, amount, coef in bets:
                        if team == winner:
                            payout = amount * (coef if coef and coef > 0 else 1.0)
                            cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, uid))
                            cur.execute("UPDATE bets SET status='won' WHERE id=?", (bet_id,))
                            tg_send(uid, f"🎉 Поздравляем! Ваша ставка на <b>{team}</b> выиграла. Выплата: {payout:.2f} Peek.")
                        else:
                            cur.execute("UPDATE bets SET status='lost' WHERE id=?", (bet_id,))
                            tg_send(uid, f"☹️ Ваша ставка на <b>{team}</b> проиграла.")
                    conn.commit()
            await asyncio.sleep(CHECK_INTERVAL)
        except Exception as e:
            print("background_checker error:", e)
            await asyncio.sleep(CHECK_INTERVAL)

# ---------------- Startup ----------------
@app.on_event("startup")
async def on_startup():
    # pre-cache matches once
    update_matches_cache()
    # start background checker
    asyncio.create_task(background_checker())
    # set webhook automatically if WEBHOOK_FULL provided
    if WEBHOOK_FULL:
        try:
            resp = requests.get(f"{TELEGRAM_API}/setWebhook", params={"url": WEBHOOK_FULL}, timeout=10)
            print("setWebhook response:", resp.status_code, resp.text)
        except Exception as e:
            print("setWebhook error:", e)

# ---------------- Run ----------------
if __name__ == "__main__":
    # запускаем как веб-сервис; Render задаёт PORT в переменных среды
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

