import os
import time
import hmac
import hashlib
import requests
import numpy as np

# ================= ENV =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BYBIT_API_KEY = os.environ.get("BINANCE_API_KEY")
BYBIT_SECRET = os.environ.get("BINANCE_SECRET")

# ================= SETTINGS =================
BB_LEN = 30
BB_MULT = 2.0
RSI_LEN = 14

RSI_OB = 55
RSI_OS = 40

SYMBOL = "XBTUSD"
BYBIT_SYMBOL = "BTCUSDT"
INTERVAL = 5

QUANTITY = "0.001"
TESTNET_URL = "https://api-testnet.bybit.com"

TP_YUZDE = 1.0
SL_YUZDE = 5.0

# ================= POSITION =================
pozisyon = {
    "var": False,
    "yon": None,
    "giris": None,
    "tp": None,
    "sl": None,
    "breakeven": False,
    "trailing": False,
    "max_fiyat": None
}

# ================= TELEGRAM =================
def telegram_bildir(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
    except:
        print("Telegram error")

# ================= DATA =================
def get_candles():
    try:
        url = "https://api.kraken.com/0/public/OHLC"
        r = requests.get(url, params={"pair": SYMBOL, "interval": INTERVAL}, timeout=10)
        data = r.json()

        if data.get("error"):
            return None, None

        key = [k for k in data["result"].keys() if k != "last"][0]
        candles = data["result"][key]

        closes = [float(x[4]) for x in candles]
        opens = [float(x[1]) for x in candles]

        return closes, opens

    except:
        return None, None

# ================= INDICATORS =================
def sma(data, n):
    return np.mean(data[-n:])

def stdev(data, n):
    return np.std(data[-n:])

def rsi(closes, n):
    diff = np.diff(closes)
    gain = np.where(diff > 0, diff, 0)
    loss = np.where(diff < 0, -diff, 0)

    ag = np.mean(gain[-n:])
    al = np.mean(loss[-n:])

    if al == 0:
        return 100

    rs = ag / al
    return 100 - (100 / (1 + rs))

# ================= BYBIT =================
def sign(params):
    ts = str(int(time.time() * 1000))
    recv = "5000"

    param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    payload = ts + BYBIT_API_KEY + recv + param_str

    sig = hmac.new(
        BYBIT_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

    return ts, sig

def order(side):
    try:
        params = {
            "category": "linear",
            "symbol": BYBIT_SYMBOL,
            "side": "Buy" if side == "BUY" else "Sell",
            "orderType": "Market",
            "qty": QUANTITY
        }

        ts, sig = sign(params)

        headers = {
            "X-BAPI-API-KEY": BYBIT_API_KEY,
            "X-BAPI-SIGN": sig,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": "5000"
        }

        r = requests.post(
            f"{TESTNET_URL}/v5/order/create",
            json=params,
            headers=headers,
            timeout=10
        )

        return r.json()

    except Exception as e:
        return {"retCode": -1, "retMsg": str(e)}

# ================= POSITION =================
def pozisyon_kontrol(close):
    global pozisyon

    if not pozisyon["var"]:
        return

    giris = pozisyon["giris"]
    yon = pozisyon["yon"]
    tp = pozisyon["tp"]

    # ================= BUY =================
    if yon == "BUY":
        kar = ((close - giris) / giris) * 100

        # BREAKEVEN
        if kar >= 1.0 and not pozisyon["breakeven"]:
            pozisyon["sl"] = giris
            pozisyon["breakeven"] = True
            telegram_bildir("🔒 Breakeven BUY")

        # TRAILING
        if kar >= 2.5:
            pozisyon["trailing"] = True

            if pozisyon["max_fiyat"] is None:
                pozisyon["max_fiyat"] = close

            if close > pozisyon["max_fiyat"]:
                pozisyon["max_fiyat"] = close

            new_sl = pozisyon["max_fiyat"] * 0.99

            if new_sl > pozisyon["sl"]:
                pozisyon["sl"] = new_sl

        # TP
        if close >= tp:
            order("SELL")
            telegram_bildir(f"TP BUY %+{kar:.2f}")
            pozisyon["var"] = False

        # SL
        elif close <= pozisyon["sl"]:
            order("SELL")
            telegram_bildir(f"SL BUY %{kar:.2f}")
            pozisyon["var"] = False

    # ================= SELL =================
    elif yon == "SELL":
        kar = ((giris - close) / giris) * 100

        if kar >= 1.0 and not pozisyon["breakeven"]:
            pozisyon["sl"] = giris
            pozisyon["breakeven"] = True
            telegram_bildir("🔒 Breakeven SELL")

        if kar >= 2.5:
            pozisyon["trailing"] = True

            if pozisyon["max_fiyat"] is None:
                pozisyon["max_fiyat"] = close

            if close < pozisyon["max_fiyat"]:
                pozisyon["max_fiyat"] = close

            new_sl = pozisyon["max_fiyat"] * 1.01

            if new_sl < pozisyon["sl"]:
                pozisyon["sl"] = new_sl

        if close <= tp:
            order("BUY")
            telegram_bildir(f"TP SELL %+{kar:.2f}")
            pozisyon["var"] = False

        elif close >= pozisyon["sl"]:
            order("BUY")
            telegram_bildir(f"SL SELL %{kar:.2f}")
            pozisyon["var"] = False

# ================= ANALYSIS =================
def analiz():
    global pozisyon

    closes, opens = get_candles()
    if closes is None or len(closes) < BB_LEN + 5:
        return

    basis = sma(closes, BB_LEN)
    dev = stdev(closes, BB_LEN)

    upper = basis + dev * BB_MULT
    lower = basis - dev * BB_MULT

    r = rsi(closes, RSI_LEN)

    close = closes[-1]
    prev = closes[-2]

    pozisyon_kontrol(close)

    if not pozisyon["var"]:
        buy = prev <= lower and close > lower and r < RSI_OS
        sell = prev >= upper and close < upper and r > RSI_OB

        if buy:
            res = order("BUY")
            if res.get("retCode") == 0:
                pozisyon.update({
                    "var": True,
                    "yon": "BUY",
                    "giris": close,
                    "tp": close * 1.01,
                    "sl": close * 0.95,
                    "breakeven": False,
                    "trailing": False,
                    "max_fiyat": None
                })
                telegram_bildir("🟢 BUY açıldı")

        elif sell:
            res = order("SELL")
            if res.get("retCode") == 0:
                pozisyon.update({
                    "var": True,
                    "yon": "SELL",
                    "giris": close,
                    "tp": close * 0.99,
                    "sl": close * 1.05,
                    "breakeven": False,
                    "trailing": False,
                    "max_fiyat": None
                })
                telegram_bildir("🔴 SELL açıldı")

# ================= MAIN LOOP =================
if __name__ == "__main__":
    print("Bot başladı")
    telegram_bildir("🤖 Bot aktif")

    while True:
        try:
            analiz()
        except Exception as e:
            print("Hata:", e)

        time.sleep(300)
