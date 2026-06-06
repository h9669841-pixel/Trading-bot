import os
import time
import hmac
import hashlib
import requests
import numpy as np
import urllib.parse

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# 🔑 GERÇEK BİNANCE ANAHTARLARINIZ (Railway'e bunları girin)
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")  
BINANCE_SECRET = os.environ.get("BINANCE_SECRET")

# --- STANDART VE KARARLI STRATEJİ AYARLARI ---
BB_LEN = 20            
BB_MULT = 2.0          
RSI_LEN = 14           
RSI_OB = 70            
RSI_OS = 30            
INTERVAL = 5           # 5 Dakikalık gerçek mumlar izlenir
# -----------------------------------------------

SYMBOL = "BTCUSDT"               
MAINNET_URL = "https://fapi.binance.com" # ✨ Gerçek Binance Vadeli İşlemler Adresi

TP_YUZDE = 1.5         
SL_YUZDE = 1.0         
BREAKEVEN_YUZDE = 0.4  

# Sanal pozisyon takip hafızası
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
    url = f"{MAINNET_URL}/fapi/v1/klines"
    binance_interval = f"{INTERVAL}m" if INTERVAL < 60 else "1h"
    
    params = {
        "symbol": SYMBOL,
        "interval": binance_interval,
        "limit": 100
    }
    try:
        r = requests.get(url, params=params)
        data = r.json()
        closes = [float(candle[4]) for candle in data]
        opens = [float(candle[1]) for candle in data]
        return closes, opens
    except Exception as e:
        print(f"Binance gerçek mum verisi çekme hatası: {e}")
        return None, None

def islem_ac_PASIF(action):
    # 🔒 GÜVENLİK DUVARI: Bu fonksiyon borsaya asla istek ATMAZ.
    # Sadece kodun akışını bozmamak için her zaman başarılıymış gibi davranır.
    print(f"🔒 [SİMÜLASYON] {action} emri borsa yerine simüle edildi. Gerçek işlem açılmadı.")
    return {"retCode": 0, "retMsg": "Success"}

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
            mesaj = f"🔒 <b>[Sanal] BREAKEVEN AKTİF!</b>\n📊 BTCUSDT (Binance)\n💰 Giriş: {giris:.2f}\n📈 Kar: +%{kar:.2f}\n🛑 Sanal SL → {giris:.2f} (Giriş Seviyesi)"
            telegram_bildir(mesaj)

        if close >= tp:
            islem_ac_PASIF("SELL")
            mesaj = f"✅ <b>[Sanal] TAKE PROFIT HEDEFİNE ULAŞILDI!</b>\n📊 BTCUSDT (Binance)\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📈 Kar: +%{kar:.2f}\n⚠️ <i>Gerçek işlem açılmamıştır, bilgi amaçlıdır.</i>"
            telegram_bildir(mesaj)
            pozisyon["var"] = False
            pozisyon["breakeven"] = False
            
        elif close <= pozisyon["sl"]:
            islem_ac_PASIF("SELL")
            if pozisyon["breakeven"]:
                mesaj = f"🔒 <b>[Sanal] BREAKEVEN ÇIKIŞI Yapıldı</b>\n📊 BTCUSDT\n💰 Giriş/Çıkış: {close:.2f}\n➡️ Risk sıfırlandı."
            else:
                mesaj = f"🛑 <b>[Sanal] STOP LOSS SEVİYESİNE DEĞDİ!</b>\n📊 BTCUSDT (Binance)\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📉 Zarar: %{kar:.2f}"
            telegram_bildir(mesaj)
            pozisyon["var"] = False
            pozisyon["breakeven"] = False

    elif yon == "SELL":
        kar = ((giris - close) / giris) * 100

        if kar >= BREAKEVEN_YUZDE and not pozisyon["breakeven"]:
            pozisyon["sl"] = giris
            pozisyon["breakeven"] = True
            mesaj = f"🔒 <b>[Sanal] BREAKEVEN AKTİF!</b>\n📊 BTCUSDT\n💰 Giriş: {giris:.2f}\n📈 Kar: +%{kar:.2f}\n🛑 Sanal SL → {giris:.2f}"
            telegram_bildir(mesaj)

        if close <= tp:
            islem_ac_PASIF("BUY")
            mesaj = f"✅ <b>[Sanal] TAKE PROFIT HEDEFİNE ULAŞILDI!</b>\n📊 BTCUSDT (Binance)\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📈 Kar: +%{kar:.2f}\n⚠️ <i>Gerçek işlem açılmamıştır, bilgi amaçlıdır.</i>"
            telegram_bildir(mesaj)
            pozisyon["var"] = False
            pozisyon["breakeven"] = False
            
        elif close >= pozisyon["sl"]:
            islem_ac_PASIF("BUY")
            if pozisyon["breakeven"]:
                mesaj = f"🔒 <b>[Sanal] BREAKEVEN ÇIKIŞI Yapıldı</b>\n📊 BTCUSDT\n💰 Giriş/Çıkış: {close:.2f}\n➡️ Risk sıfırlandı."
            else:
                mesaj = f"🛑 <b>[Sanal] STOP LOSS SEVİYESİNE DEĞDİ!</b>\n📊 BTCUSDT (Binance)\n💰 Giriş: {giris:.2f}\n💰 Çıkış: {close:.2f}\n📉 Zarar: %{kar:.2f}"
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

    print(f"Gerçek Fiyat: {close:.2f} | RSI: {rsi_val:.1f} | BB_U: {bb_upper:.2f} | BB_L: {bb_lower:.2f}")

    if pozisyon["var"]:
        pozisyon_kontrol(close)
    else:
        buy_signal  = (prev_close <= bb_lower or close <= bb_lower) and (rsi_val <= RSI_OS)
        sell_signal = (prev_close >= bb_upper or close >= bb_upper) and (rsi_val >= RSI_OB)

        if buy_signal:
            sonuc = islem_ac_PASIF("BUY")
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
                mesaj = f"🔔 <b>[SİNYAL] BUY (LONG) ZAMANI</b>\n📊 BTCUSDT (Binance Gerçek Veri)\n💰 Mevcut Fiyat: {close:.2f}\n🎯 Hedef TP: {tp_fiyat:.2f}\n🛑 Güvenlik SL: {sl_fiyat:.2f}\n\n⚠️ <i>Bot otomatik işlem açmamıştır. Manuel açabilirsiniz.</i>"
                telegram_bildir(mesaj)

        elif sell_signal:
            sonuc = islem_ac_PASIF("SELL")
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
                mesaj = f"🔔 <b>[SİNYAL] SELL (SHORT) ZAMANI</b>\n📊 BTCUSDT (Binance Gerçek Veri)\n💰 Mevcut Fiyat: {close:.2f}\n🎯 Hedef TP: {tp_fiyat:.2f}\n🛑 Güvenlik SL: {sl_fiyat:.2f}\n\n⚠️ <i>Bot otomatik işlem açmamıştır. Manuel açabilirsiniz.</i>"
                telegram_bildir(mesaj)

if __name__ == "__main__":
    print("Bot güvenli sinyal modunda başladı...")
    telegram_bildir("🛡️ <b>Binance Canlı Veri - Sadece Sinyal & Simülasyon Motoru Aktif!</b>\nPara riske atılmadan piyasa 5m mumlarla taranıyor. Cüzdanınız %100 güvende.")
    
    while True:
        try:
            analiz()
        except Exception as e:
            print(f"Hata döngüsü yakalandı: {e}")
        time.sleep(60)
