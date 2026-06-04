import os
import time
import hmac
import hashlib
import requests
import numpy as np

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET = os.environ.get("BINANCE_SECRET")

BB_LEN = 30
BB_MULT = 2
RSI_LEN = 14
RSI_OB = 55
RSI_OS = 40
SYMBOL = "XBTUSD"
BINANCE_SYMBOL = "BTCUSDT"
INTERVAL = 5
QUANTITY = 0.001
TESTNET_URL = "https://testnet.binance.vision"

TP_YUZDE = 1.0   # %1 kar
SL_YUZDE = 5.0   # %5 zarar

# Açık pozisyon takibi
pozisyon = {
    "var": False,
    "yon": None,       # BUY veya SELL
    "giris": None,     # Giriş fiyatı
    "tp": None,        # Take profit fiyatı
    "sl": None         # Stop loss fiyatı
}

def telegram_bildir(mesaj):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mesaj,
        "parse_mode": "HTML"
    })
    print(f"Telegram yanıt: {r.status_code}")

def get_candles():
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": SYMBOL, "interval": INTERVAL}
    r = requests.get(url, params=params)
    data = r.json()

    if data.get("error"):
        print(f"Kraken hata: {data['error']}")
        return None, None

    result = list(data["result"].values())[0]
    closes = [float(d[4]) for d in result]
    opens  = [float(d[1]) for d in result]
    return closes, opens

def imza_olustur(params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    imza = hmac.new(
        BINANCE_SECRET.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()
    return query + f"&signature={imza}"

def islem_ac(action):
    params = {
        "symbol": BINANCE_SYMBOL,
        "side": action,
        "type": "MARKET",
        "quantity": QUANTITY,
        "timestamp": int(time.time() * 1000)
    }
    query = imza_olustur(params)
    url = f"{TESTNET_URL}/api/v3/order?{query}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    r = requests.post(url, headers=headers)
    return r.json()

def sma(data, period):
    return np.mean(data[-period:])

def stdev(data, period):
    return np.std(data[-period:])

def calc_rsi(closes, period):
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def pozisyon_kontrol(close):
    global pozisyon

    if not pozisyon["var"]:
        return

    giris = pozisyon["giris"]
    yon = pozisyon["yon"]
    tp = pozisyon["tp"]
    sl = pozisyon["sl"]

    if yon == "BUY":
        kar = ((close - giris) / giris) * 100
        if close >= tp:
            sonuc = islem_ac("SELL")
            mesaj = f"✅ <b>TAKE PROFIT!</b>\n📊 BTC/USD\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📈 Kar: +%{kar:.2f}\n🧪 TESTNET"
            telegram_bildir(mesaj)
            pozisyon["var"] = False
        elif close <= sl:
            sonuc = islem_ac("SELL")
            mesaj = f"🛑 <b>STOP LOSS!</b>\n📊 BTC/USD\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📉 Zarar: %{kar:.2f}\n🧪 TESTNET"
            telegram_bildir(mesaj)
            pozisyon["var"] = False

    elif yon == "SELL":
        kar = ((giris - close) / giris) * 100
        if close <= tp:
            sonuc = islem_ac("BUY")
            mesaj = f"✅ <b>TAKE PROFIT!</b>\n📊 BTC/USD\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📈 Kar: +%{kar:.2f}\n🧪 TESTNET"
            telegram_bildir(mesaj)
            pozisyon["var"] = False
        elif close >= sl:
            sonuc = islem_ac("BUY")
            mesaj = f"🛑 <b>STOP LOSS!</b>\n📊 BTC/USD\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📉 Zarar: %{kar:.2f}\n🧪 TESTNET"
            telegram_bildir(mesaj)
            pozisyon["var"] = False

def analiz():
    global pozisyon

    closes, opens = get_candles()

    if closes is None:
        print("Veri alınamadı, atlanıyor...")
        return

    if len(closes) < BB_LEN + 1:
        print(f"Yeterli veri yok: {len(closes)} mum")
        return

    basis = sma(closes, BB_LEN)
    dev = stdev(closes, BB_LEN)
    mult = BB_MULT * 0.85
    bb_upper = basis + dev * mult
    bb_lower = basis - dev * mult

    rsi_val    = calc_rsi(closes, RSI_LEN)
    close      = closes[-1]
    prev_close = closes[-2]

    print(f"Fiyat: {close:.2f} | RSI: {rsi_val:.1f} | BB_U: {bb_upper:.2f} | BB_L: {bb_lower:.2f} | Pozisyon: {pozisyon['yon'] if pozisyon['var'] else 'Yok'}")

    # Önce açık pozisyon TP/SL kontrolü
    pozisyon_kontrol(close)

    # Pozisyon yoksa yeni sinyal ara
    if not pozisyon["var"]:
        buy_signal  = (prev_close <= bb_lower) and (close > bb_lower) and (rsi_val < RSI_OS)
        sell_signal = (prev_close >= bb_upper) and (close < bb_upper) and (rsi_val > RSI_OB)

        if buy_signal:
            sonuc = islem_ac("BUY")
            if "orderId" in sonuc:
                tp_fiyat = close * (1 + TP_YUZDE / 100)
                sl_fiyat = close * (1 - SL_YUZDE / 100)
                pozisyon.update({
                    "var": True,
                    "yon": "BUY",
                    "giris": close,
                    "tp": tp_fiyat,
                    "sl": sl_fiyat
                })
                mesaj = f"🟢 <b>BUY İŞLEMİ AÇILDI</b>\n📊 BTC/USD\n💰 Fiyat: {close:.2f}\n📈 RSI: {rsi_val:.1f}\n🎯 TP: {tp_fiyat:.2f} (+%{TP_YUZDE})\n🛑 SL: {sl_fiyat:.2f} (-%{SL_YUZDE})\n🧪 TESTNET"
            else:
                mesaj = f"🟢 <b>BUY SİNYALİ</b>\n📊 BTC/USD\n💰 Fiyat: {close:.2f}\n⚠️ İşlem açılamadı: {sonuc.get('msg', '')}"
            telegram_bildir(mesaj)

        elif sell_signal:
            sonuc = islem_ac("SELL")
            if "orderId" in sonuc:
                tp_fiyat = close * (1 - TP_YUZDE / 100)
                sl_fiyat = close * (1 + SL_YUZDE / 100)
                pozisyon.update({
                    "var": True,
                    "yon": "SELL",
                    "giris": close,
                    "tp": tp_fiyat,
                    "sl": sl_fiyat
                })
                mesaj = f"🔴 <b>SELL İŞLEMİ AÇILDI</b>\n📊 BTC/USD\n💰 Fiyat: {close:.2f}\n📈 RSI: {rsi_val:.1f}\n🎯 TP: {tp_fiyat:.2f} (-%{TP_YUZDE})\n🛑 SL: {sl_fiyat:.2f} (+%{SL_YUZDE})\n🧪 TESTNET"
            else:
                mesaj = f"🔴 <b>SELL SİNYALİ</b>\n📊 BTC/USD\n💰 Fiyat: {close:.2f}\n⚠️ İşlem açılamadı: {sonuc.get('msg', '')}"
            telegram_bildir(mesaj)

if __name__ == "__main__":
    print("Bot başladı...")
    telegram_bildir("🤖 <b>Bot Başladı!</b>\n📊 Sinyal + Al-Sat modu\n🎯 TP: %1 | 🛑 SL: %5\n🧪 Testnet aktif")
    while True:
        try:
            analiz()
        except Exception as e:
            print(f"Hata: {e}")
        time.sleep(300)
