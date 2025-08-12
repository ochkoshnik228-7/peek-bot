from fastapi import FastAPI, Request
import requests
import os

TOKEN = "8314578862:AAFmgkZTLNaPFQCiDiqCZtUNeTxWK3MghFA"
WEBHOOK_PATH = f"/webhook/{TOKEN}"
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TOKEN}"

app = FastAPI()

# Пример: проверка, что сервер жив
@app.get("/")
async def root():
    return {"status": "ok"}

# Приём апдейтов от Telegram
@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    update = await request.json()
    print(update)  # для дебага

    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")

        if text == "/start":
            send_message(chat_id, "Привет! Я твой Peek-бот. Скоро будут ставки!")
        else:
            send_message(chat_id, f"Ты написал: {text}")

    return {"ok": True}

def send_message(chat_id, text):
    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text
    })

# Установка вебхука (можно вызвать один раз при запуске)
@app.on_event("startup")
async def set_webhook():
    webhook_url = os.getenv("WEBHOOK_URL") + WEBHOOK_PATH
    requests.get(f"{TELEGRAM_API_URL}/setWebhook", params={"url": webhook_url})
