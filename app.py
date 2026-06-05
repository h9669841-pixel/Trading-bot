import os
import time
import hmac
import hashlib
import requests
import numpy as np

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
BYBIT_API_KEY = os.environ.get("BINANCE_API_KEY")  # Railway env adınız buysa dokunmayın
BYBIT_SECRET = os.environ.get("BINANCE_SECRET")

BB_LEN = 30
BB_MULT = 2.0
RSI_LEN = 14
RSI_OB = 55
RSI_OS = 40
BYBIT_SYMBOL = "BTCUSDT"
INTERVAL = "5"  # Bybit için string "5" olmalı
QUANTITY = "0.01"
TESTNET_URL = "https://api-testnet.bybit.com"

TP_YUZDE = 3.0
SL_YUZDE = 5.0
BREAKEVEN_YUZDE = 1.0

pozisyon = {
    "var": False,
    "yon": None,
    "giris": None,
    "tp": None,
    "sl": None,
    "breakeven": False
}

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram itimatnameleri eksik!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": mesaj,
            "parse_mode": "HTML"
        })
        print(f"Telegram yanıt: {r.status_code}")
    except Exception as e:
        print(f"Telegram gönderilemedi: {e}")

# VERİ KAYNAĞI BYBIT OLARAK DEĞİŞTİRİLDİ (Uyuşmazlık çözüldü)
def get_candles():
    url = f"{TESTNET_URL}/v5/market/klines"
    params = {
        "category": "linear",
        "symbol": BYBIT_SYMBOL,
        "interval": INTERVAL,
        "limit": 50
    }
    try:
        r = requests.get(url, params=params)
        data = r.json()
        if data.get("retCode") != 0:
            print(f"Bybit Veri Hatası: {data.get('retMsg')}")
            return None, None
        
        # Bybit listeyi yeniden eskiye döndürür [en yeni, ..., en eski]
        # Bize eskiden yeniye lazım, o yüzden tersine çeviriyoruz [::-1]
        listeler = data["result"]["list"][::-1]
        closes = [float(d[4]) for d in listeler]
        opens = [float(d[1]) for d in listeler]
        return closes, opens
    except Exception as e:
        print(f"Mum verisi çekme hatası: {e}")
        return None, None

def imza_olustur(params_str, timestamp):
    # V5 POST işlemlerinde imza: timestamp + API_KEY + recv_window + JSON_BODY_STRING
    recv_window = "5000"
    sign_str = timestamp + BYBIT_API_KEY + recv_window + params_str
    imza = hmac.new(
        BYBIT_SECRET.encode(),
        sign_str.encode(),
        hashlib.sha256
    ).hexdigest()
    return imza

def islem_ac(action):
    # Tek yönlü (One-way) mod için positionIdx: 0. Hedge mod ise Buy için 1, Sell için 2 yapın.
    import json
    params = {
        "category": "linear",
        "symbol": BYBIT_SYMBOL,
        "side": "Buy" if action == "BUY" else "Sell",
        "orderType": "Market",
        "qty": QUANTITY,
        "positionIdx": 0 
    }
    
    timestamp = str(int(time.time() * 1000))
    json_body = json.dumps(params)
    imza = imza_olustur(json_body, timestamp)
    
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-SIGN": imza,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type": "application/json"
    }
    
    url = f"{TESTNET_URL}/v5/order/create"
    try:
        r = requests.post(url, data=json_body, headers=headers)
        res_json = r.json()
        print(f"Bybit işlem yanıtı: {res_json}")
        return res_json
    except Exception as e:
        print(f"API İstek Hatası: {e}")
        return {"retCode": -1, "retMsg": str(e)}

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

    if yon == "BUY":
        kar = ((close - giris) / giris) * 100
        if kar >= BREAKEVEN_YUZDE and not pozisyon["breakeven"]:
            pozisyon["sl"] = giris
            pozisyon["breakeven"] = True
            mesaj = f"🔒 <b>BREAKEVEN AKTİF!</b>\n📊 BTC/USDT\n💰 Giriş: {giris:.2f}\n🛑 SL → {giris:.2f}"
            telegram_bildir(mesaj)

        if close >= tp:
            sonuc = islem_ac("SELL")  # Pozisyonu kapatmak için ters işlem
            if sonuc.get("retCode") == 0:
                mesaj = f"✅ <b>TAKE PROFIT!</b>\n📊 BTC/USDT\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📈 Kar: +%{kar:.2f}"
                pozisyon["var"] = False
                pozisyon["breakeven"] = False
                telegram_bildir(mesaj)
        elif close <= pozisyon["sl"]:
            sonuc = islem_ac("SELL")
            if sonuc.get("retCode") == 0:
                mesaj = f"🛑 <b>STOP LOSS!</b>\n📊 BTC/USDT\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}"
                pozisyon["var"] = False
                pozisyon["breakeven"] = False
                telegram_bildir(mesaj)

    elif yon == "SELL":
        kar = ((giris - close) / giris) * 100
        if kar >= BREAKEVEN_YUZDE and not pozisyon["breakeven"]:
            pozisyon["sl"] = giris
            pozisyon["breakeven"] = True
            mesaj = f"🔒 <b>BREAKEVEN AKTİF!</b>\n📊 BTC/USDT\n💰 Giriş: {giris:.2f}\n🛑 SL → {giris:.2f}"
            telegram_bildir(mesaj)

        if close <= tp:
            sonuc = islem_ac("BUY")
            if sonuc.get("retCode") == 0:
                mesaj = f"✅ <b>TAKE PROFIT!</b>\n📊 BTC/USDT\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📈 Kar: +%{kar:.2f}"
                pozisyon["var"] = False
                pozisyon["breakeven"] = False
                telegram_bildir(mesaj)
        elif close >= pozisyon["sl"]:
            sonuc = islem_ac("BUY")
            if sonuc.get("retCode") == 0:
                mesaj = f"🛑 <b>STOP LOSS!</b>\n📊 BTC/USDT\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}"
                pozisyon["var"] = False
                pozisyon["breakeven"] = False
                telegram_bildir(mesaj)

def analiz():
    global pozisyon
    closes, opens = get_candles()

    if closes is None or len(closes) < BB_LEN + 1:
        print("Yeterli veri yok veya çekilemedi, atlanıyor...")
        return

    basis = sma(closes, BB_LEN)
    dev = stdev(closes, BB_LEN)
    mult = BB_MULT * 0.85
    bb_upper = basis + dev * mult
    bb_lower = basis - dev * mult

    rsi_val    = calc_rsi(closes, RSI_LEN)
    close      = closes[-1]
    prev_close = closes[-2]

    print(f"Fiyat: {close:.2f} | RSI: {rsi_val:.1f} | BB_U: {bb_upper:.2f} | BB_L: {bb_lower:.2f}")

    # Önce mevcut pozisyonu kontrol et
    if pozisyon["var"]:
        pozisyon_kontrol(close)
    # Eğer pozisyon yoksa YENİ SİNYAL ara
    else:
        buy_signal  = (prev_close <= bb_lower) and (close > bb_lower) and (rsi_val < RSI_OS)
        sell_signal = (prev_close >= bb_upper) and (close < bb_upper) and (rsi_val > RSI_OB)

        if buy_signal:
            sonuc = islem_ac("BUY")
            if sonuc.get("retCode") == 0:
                tp_fiyat = close * (1 + TP_YUZDE / 100)
                sl_fiyat = close * (1 - SL_YUZDE / 100)
                pozisyon.update({
                    "var": True,
                    "yon": "BUY",
                    "giris": close,
                    "tp": tp_fiyat,
                    "sl": sl_fiyat,
                    "breakeven": False
                })
                mesaj = f"🟢 <b>BUY İŞLEMİ AÇILDI</b>\n🎯 TP: {tp_fiyat:.2f}\n🛑 SL: {sl_fiyat:.2f}"
                telegram_bildir(mesaj)

        elif sell_signal:
            sonuc = islem_ac("SELL")
            if sonuc.get("retCode") == 0:
                tp_fiyat = close * (1 - TP_YUZDE / 100)
                sl_fiyat = close * (1 + SL_YUZDE / 100)
                pozisyon.update({
                    "var": True,
                    "yon": "SELL",
                    "giris": close,
                    "tp": tp_fiyat,
                    "sl": sl_fiyat,
                    "breakeven": False
                })
                mesaj = f"🔴 <b>SELL İŞLEMİ AÇILDI</b>\n🎯 TP: {tp_fiyat:.2f}\n🛑 SL: {sl_fiyat:.2f}"
                telegram_bildir(mesaj)

if __name__ == "__main__":
    print("Bot başladı...")
    while True:
        try:
            analiz()
        except Exception as e:
            print(f"Döngü Hatası: {e}")
        time.sleep(300)
