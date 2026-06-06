import os
import time
import hmac
import hashlib
import requests
import numpy as np
import base64
import urllib.parse

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# DEĞİŞKEN İSİMLERİ DOĞRUDAN KRAKEN OLARAK GÜNCELLENDİ:
KRAKEN_API_KEY = os.environ.get("KRAKEN_API_KEY")  
KRAKEN_SECRET = os.environ.get("KRAKEN_SECRET")

# --- AGRESİF HIZLI AYARLAR ---
BB_LEN = 14            
BB_MULT = 1.3          
RSI_LEN = 7            
RSI_OB = 50            
RSI_OS = 50            
INTERVAL = 1           
# -----------------------------

SYMBOL = "XBTUSD"               
KRAKEN_FUTURES_SYMBOL = "pi_xbtusd" 
QUANTITY = "0.01"
TESTNET_URL = "https://demo-futures.kraken.com"

TP_YUZDE = 1.0         
SL_YUZDE = 2.0         
BREAKEVEN_YUZDE = 0.3  

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
        print("Telegram değişkenleri eksik!")
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
        print(f"Telegram hatası: {e}")

def get_candles():
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": SYMBOL, "interval": INTERVAL}
    try:
        r = requests.get(url, params=params)
        data = r.json()
    except Exception as e:
        print(f"JSON parse hatası: {e}")
        return None, None

    if data.get("error"):
        print(f"Kraken hata: {data['error']}")
        return None, None

    try:
        result = list(data["result"].values())[0]
        closes = [float(d[4]) for d in result]
        opens  = [float(d[1]) for d in result]
        return closes, opens
    except Exception as e:
        print(f"Veri ayrıştırma hatası: {e}")
        return None, None

def imza_olustur(endpoint, post_data_str, nonce):
    if not KRAKEN_SECRET:
        print("HATA: KRAKEN_SECRET bulunamadı!")
        return ""
    
    message = post_data_str + nonce + endpoint
    sha256_hash = hashlib.sha256(message.encode('utf-8')).digest()
    
    secret_clean = KRAKEN_SECRET.strip()
    secret_bytes = base64.b64decode(secret_clean)
    
    mac = hmac.new(secret_bytes, sha256_hash, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode('utf-8')

def islem_ac(action):
    if not KRAKEN_API_KEY or not KRAKEN_SECRET:
        return {"retCode": -1, "retMsg": "Railway üzerinde Kraken API Anahtarları eksik!"}

    endpoint = "/derivatives/api/v3/sendorder"
    url = f"{TESTNET_URL}{endpoint}"
    
    closes, _ = get_candles()
    if closes:
        current_price = closes[-1]
        target_price = current_price * 1.002 if action == "BUY" else current_price * 0.998
    else:
        return {"retCode": -1, "retMsg": "Fiyat alınamadı"}

    nonce = str(int(time.time() * 1000))
    
    post_params = {
        "orderType": "lmt",
        "symbol": KRAKEN_FUTURES_SYMBOL,
        "side": "buy" if action == "BUY" else "sell",
        "size": QUANTITY,
        "price": f"{target_price:.1f}",
        "cliOrdId": f"bot_{nonce}"
    }
    
    post_data_str = urllib.parse.urlencode(post_params)
    imza = imza_olustur(endpoint, post_data_str, nonce)
    
    headers = {
        "APIKey": KRAKEN_API_KEY.strip(),
        "Nonce": nonce,
        "Authent": imza,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    try:
        r = requests.post(url, data=post_data_str, headers=headers)
        res_json = r.json()
        print(f"Kraken Futures yanıt: {res_json}")
        
        if res_json.get("result") == "success":
            return {"retCode": 0, "retMsg": "Success"}
        else:
            hata_mesaji = res_json.get("error", "Bilinmeyen Kraken Hatası")
            return {"retCode": -1, "retMsg": hata_mesaji}
    except Exception as e:
        print(f"Kraken istek hatası: {e}")
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
            mesaj = f"🔒 <b>BREAKEVEN AKTİF!</b>\n📊 BTC/USD (Kraken)\n💰 Giriş: {giris:.2f}\n📈 Kar: +%{kar:.2f}\n🛑 SL → {giris:.2f}"
            telegram_bildir(mesaj)

        if close >= tp:
            sonuc = islem_ac("SELL")
            if sonuc.get("retCode") == 0:
                mesaj = f"✅ <b>TAKE PROFIT!</b>\n📊 BTC/USD (Kraken)\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📈 Kar: +%{kar:.2f}\n🧪 KRAKEN SANDBOX"
                telegram_bildir(mesaj)
                pozisyon["var"] = False
                pozisyon["breakeven"] = False
        elif close <= pozisyon["sl"]:
            sonuc = islem_ac("SELL")
            if sonuc.get("retCode") == 0:
                if pozisyon["breakeven"]:
                    mesaj = f"🔒 <b>BREAKEVEN ÇIKIŞI</b>\n📊 BTC/USD (Kraken)\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n➡️ Sıfır zarar\n🧪 KRAKEN SANDBOX"
                else:
                    mesaj = f"🛑 <b>STOP LOSS!</b>\n📊 BTC/USD (Kraken)\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📉 Zarar: %{kar:.2f}\n🧪 KRAKEN SANDBOX"
                telegram_bildir(mesaj)
                pozisyon["var"] = False
                pozisyon["breakeven"] = False

    elif yon == "SELL":
        kar = ((giris - close) / giris) * 100

        if kar >= BREAKEVEN_YUZDE and not pozisyon["breakeven"]:
            pozisyon["sl"] = giris
            pozisyon["breakeven"] = True
            mesaj = f"🔒 <b>BREAKEVEN AKTİF!</b>\n📊 BTC/USD\n💰 Giriş: {giris:.2f}\n📈 Kar: +%{kar:.2f}\n🛑 SL → {giris:.2f}"
            telegram_bildir(mesaj)

        if close <= tp:
            sonuc = islem_ac("BUY")
            if sonuc.get("retCode") == 0:
                mesaj = f"✅ <b>TAKE PROFIT!</b>\n📊 BTC/USD (Kraken)\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📈 Kar: +%{kar:.2f}\n🧪 KRAKEN SANDBOX"
                telegram_bildir(mesaj)
                pozisyon["var"] = False
                pozisyon["breakeven"] = False
        elif close >= pozisyon["sl"]:
            sonuc = islem_ac("BUY")
            if sonuc.get("retCode") == 0:
                if pozisyon["breakeven"]:
                    mesaj = f"🔒 <b>BREAKEVEN ÇIKIŞI</b>\n📊 BTC/USD (Kraken)\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n➡️ Sıfır zarar\n🧪 KRAKEN SANDBOX"
                else:
                    mesaj = f"🛑 <b>STOP LOSS!</b>\n📊 BTC/USD (Kraken)\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📉 Zarar: %{kar:.2f}\n🧪 KRAKEN SANDBOX"
                telegram_bildir(mesaj)
                pozisyon["var"] = False
                pozisyon["breakeven"] = False

def analiz():
    global pozisyon
    closes, opens = get_candles()

    if closes is None or len(closes) < BB_LEN + 1:
        print("Yeterli veri yok veya çekilemedi, atlanıyor...")
        return

    basis = sma(closes, BB_LEN)
    dev = stdev(closes, BB_LEN)
    
    bb_upper = basis + dev * BB_MULT
    bb_lower = basis - dev * BB_MULT

    rsi_val    = calc_rsi(closes, RSI_LEN)
    close      = closes[-1]
    prev_close = closes[-2]

    print(f"Fiyat: {close:.2f} | RSI: {rsi_val:.1f} | BB_U: {bb_upper:.2f} | BB_L: {bb_lower:.2f}")

    if pozisyon["var"]:
        pozisyon_kontrol(close)
    else:
        buy_signal  = (prev_close <= bb_lower or close <= bb_lower) and (rsi_val <= RSI_OS)
        sell_signal = (prev_close >= bb_upper or close >= bb_upper) and (rsi_val >= RSI_OB)

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
                mesaj = f"🟢 <b>BUY İŞLEMİ AÇILDI</b>\n📊 BTC/USD (Kraken)\n💰 Fiyat: {close:.2f}\n🎯 TP: {tp_fiyat:.2f}\n🛑 SL: {sl_fiyat:.2f}"
            else:
                mesaj = f"🟢 <b>BUY SİNYALİ</b>\n⚠️ İşlem açılamadı: {sonuc.get('retMsg', 'Bilinmeyen Hata')}"
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
                mesaj = f"🔴 <b>SELL İŞLEMİ AÇILDI</b>\n📊 BTC/USD (Kraken)\n💰 Fiyat: {close:.2f}\n🎯 TP: {tp_fiyat:.2f}\n🛑 SL: {sl_fiyat:.2f}"
            else:
                mesaj = f"🔴 <b>SELL SİNYALİ</b>\n⚠️ İşlem açılamadı: {sonuc.get('retMsg', 'Bilinmeyen Hata')}"
            telegram_bildir(mesaj)

if __name__ == "__main__":
    print("Bot başladı...")
    telegram_bildir("🐙 <b>Kraken Değişken İsimleri Eşitlendi!</b>\n⚙️ Railway üzerinde KRAKEN_API_KEY ve KRAKEN_SECRET aranıyor...")
    
    while True:
        try:
            analiz()
        except Exception as e:
            print(f"Hata döngüsü yakalandı: {e}")
        time.sleep(60)
