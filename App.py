from flask import Flask, request
import requests
import os

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def telegram_bildir(mesaj):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mesaj,
        "parse_mode": "HTML"
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        sinyal = request.json
        action = sinyal.get("action", "")
        symbol = sinyal.get("symbol", "")
        price = sinyal.get("price", "")

        emoji = "🟢" if action == "LONG" else "🔴"

        mesaj = f"{emoji} <b>{action} SİNYALİ</b>\n📊 {symbol}\n💰 {price}"
        telegram_bildir(mesaj)
        return {"status": "ok"}, 200
    except Exception as e:
        print(f"Hata: {e}")
        return {"status": "error"}, 500

@app.route("/")
def index():
    return "Bot calisiyor", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
