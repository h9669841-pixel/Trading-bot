import os
import json
import time
import requests
import threading
import websocket
import math
import pandas as pd
from datetime import datetime

# --- 🔑 GÜVENLİK VE API AYARLARI ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
PROXY_URL = os.environ.get("PROXY_URL")

proxy_formatted = None
if PROXY_URL:
    proxy_formatted = PROXY_URL if PROXY_URL.startswith("socks5://") else PROXY_URL.replace("socks5://", "socks5h://")

class XRPBotConfig:
    def __init__(self):
        self.TIMEFRAME = "1h"                           # 1 Saatlik grafik periyodu (Optimize edildiği periyot)
        self.ISLEM_MARJIN = 100.0                        # Sanal işlem başına ayrılacak USDT (Margin)
        self.KALDIRAC = 5                              # Kaldıraç oranı
        
        self.BOT_CALISIYOR = True
        self.TARGET_SYMBOL = "xrpusdt"                   # Sadece XRP odaklı takip

        # === XRP OPTİMİZE STRATEJİ PARAMETRELERİ ===
        self.BB_LEN = 18                                 # Bollinger Uzunluğu
        self.BB_MULT = 1.6                               # Bollinger Çarpanı
        self.USE_SQUEEZE = True
        self.SQUEEZE_THRESH = 0.50                       # Sıkışma Eşiği (Bandwidth Max)
        self.USE_VOLUME = True
        self.VOL_LEN = 17                                # Hacim MA Uzunluğu
        self.VOL_MULT = 1.4                              # Hacim Çarpanı

        # 🎯 RİSK YÖNETİMİ (Stop-Loss & Take-Profit)
        self.STOP_LOSS_PERCENT = 1.5                     # %1.5 Zarar Durdur
        self.TAKE_PROFIT_PERCENT = 5.0                   # %5.0 Kâr Al

        self.HIZLI_TAKIP_PERIYODU = 1.0

config = XRPBotConfig()

# 💰 SANAL CÜZDAN YÖNETİMİ (Paper Trading)
sanal_cuzdan = {
    "nakit_usdt": 10000.0,  # Başlangıç sanal sermayesi ($10,000)
    "baslangic_sermaye": 10000.0
}

piyasa_verisi = {
    "klines": [],
    "anlik_fiyat": 0.0
}
aktif_pozisyon = {
    "aktif": False, "yon": None, "adet": 0.0, 
    "giris_fiyati": 0.0, "resmi_pnl": 0.0
}
FUTURES_HASSASIYETI = 2
emir_beklemede = False
data_lock = threading.Lock()

# --- 📊 XRP GEÇMİŞ VERİLERİNİ ÇEKME ---
def xrp_verilerini_hazirla():
    global FUTURES_HASSASIYETI
    try:
        symbol_upper = config.TARGET_SYMBOL.upper()
        f_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(f_url, timeout=10).json()
        market_info = next((m for m in r.get("symbols", []) if m["symbol"] == symbol_upper), None)
        
        if market_info:
            for f in market_info['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    step_size_str = str(f['stepSize']).rstrip('0')
                    FUTURES_HASSASIYETI = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])

        klines_url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol_upper}&interval={config.TIMEFRAME}&limit=250"
        k_res = requests.get(klines_url, timeout=10).json()

        with data_lock:
            piyasa_verisi["klines"] = k_res
            piyasa_verisi["anlik_fiyat"] = float(k_res[-1][4]) if k_res else 0.0
            
        print(f"🔥 XRP Verileri Yüklendi. Anlık Fiyat: {piyasa_verisi['anlik_fiyat']}")
        return True
    except Exception as e:
        print(f"❌ XRP veri yükleme hatası: {e}")
        return False

# --- 🛠️ STRATEJİ ANALİZİ (XRP Optimize Kurallar) ---
def strateji_analiz(v):
    candles = list(v["klines"])
    if not candles or len(candles) < 50:
        return "HOLD"

    df = pd.DataFrame(candles)
    df = df.iloc[:, :6]
    df.columns = ['open_time', 'open', 'high', 'low', 'close', 'volume']
    
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)

    # Hacim Koşulu
    vol_sma = df['volume'].rolling(window=config.VOL_LEN).mean()
    is_volume_ok = not config.USE_VOLUME or (df['volume'].iloc[-2] > (vol_sma.iloc[-2] * config.VOL_MULT))

    # Bollinger Bantları
    basis = df['close'].rolling(window=config.BB_LEN).mean()
    dev = config.BB_MULT * df['close'].rolling(window=config.BB_LEN).std(ddof=0)
    upper_band = basis + dev
    lower_band = basis - dev

    # Sıkışma (Squeeze) Filtresi
    bb_width = (upper_band - lower_band) / basis
    is_squeezed = not config.USE_SQUEEZE or (bb_width.iloc[-2] <= config.SQUEEZE_THRESH)

    prev_close = df['close'].iloc[-3]
    curr_close = df['close'].iloc[-2]
    curr_upper = upper_band.iloc[-2]
    curr_lower = lower_band.iloc[-2]

    # Sinyal Koşulları (Pine Script birebir uyarlama)
    long_condition = (prev_close <= curr_upper) and (curr_close > curr_upper) and is_squeezed and is_volume_ok
    short_condition = (prev_close >= curr_lower) and (curr_close < curr_lower) and is_squeezed and is_volume_ok

    if long_condition:
        return "BUY"
    elif short_condition:
        return "SELL"
    return "HOLD"

# --- 📡 WEBSOCKET DİNLEYİCİ MOTORU (XRP 1h Stream) ---
def websocket_baslat():
    def on_message(ws, message):
        try:
            msg = json.loads(message)
            if "data" not in msg: return
            
            data = msg["data"]
            kline = data["k"]
            anlik_fiyat = float(kline["c"])
            
            with data_lock:
                piyasa_verisi["anlik_fiyat"] = anlik_fiyat
                formatted_kline = [
                    kline["t"], kline["o"], kline["h"], kline["l"], 
                    kline["c"], kline["v"], kline["T"], kline["q"], 
                    kline["n"], kline["V"], kline["Q"], "0"
                ]
                klines = piyasa_verisi["klines"]
                if klines:
                    if klines[-1][0] == kline["t"]:
                        klines[-1] = formatted_kline
                    else:
                        klines.append(formatted_kline)
                        if len(klines) > 300: klines.pop(0)
        except Exception:
            pass

    def on_error(ws, error):
        print(f"⚠️ WebSocket Hatası: {error}")

    def on_close(ws, close_status_code, close_msg):
        print("🔌 WebSocket Bağlantısı Koptu. Yeniden bağlanılıyor...")
        time.sleep(5)
        websocket_baslat()

    def run():
        stream_url = f"wss://fstream.binance.com/stream?streams={config.TARGET_SYMBOL}@kline_{config.TIMEFRAME}"
        ws = websocket.WebSocketApp(stream_url, on_message=on_message, on_error=on_error, on_close=on_close)
        ws.run_forever()

    threading.Thread(target=run, daemon=True).start()
    print(f"⚡ XRP WebSocket Akışı Başlatıldı ({config.TIMEFRAME})")

# --- 🎛️ TELEGRAM BİLDİRİM VE MENÜ ---
def telegram_bildir(mesaj, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}
        if reply_markup: data["reply_markup"] = reply_markup
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=data, timeout=5)
    except Exception: pass

def ana_menu_olustur():
    return {"keyboard": [[{"text": "📊 Bot Durumu"}, {"text": "💰 Cüzdan Bakiyesi"}], [{"text": "▶️ Botu Başlat"}, {"text": "⏸️ Botu Durdur"}]], "resize_keyboard": True}

def telegram_canli_rapor_uret():
    with data_lock:
        durum_str = "🟢 XRP Paper Trading Aktif" if config.BOT_CALISIYOR else "🔴 Sistem Durduruldu"
        toplam_portfoy = sanal_cuzdan["nakit_usdt"]
        if aktif_pozisyon["aktif"]:
            toplam_portfoy += aktif_pozisyon["resmi_pnl"]

        rapor = (
            f"⚙️ <b>XRP Akülasyon Botu (Paper Trading)</b>\n"
            f"• Sistem: {durum_str}\n"
            f"• Nakit Bakiye: <b>{round(sanal_cuzdan['nakit_usdt'], 2)}$</b>\n"
            f"• Toplam Varlık: <b>{round(toplam_portfoy, 2)}$</b>\n\n"
            f"⚡ <b>Açık İşlem:</b>\n"
        )
        if not aktif_pozisyon["aktif"]:
            rapor += "Açık pozisyon bulunmuyor."
        else:
            p = aktif_pozisyon
            rapor += f"• XRPUSDT | {p['yon']} | Fiyat: {p['giris_fiyati']} | PNL: <b>{round(p['resmi_pnl'], 3)}$</b>\n"
        return rapor

def telegram_gelen_mesaj_dinleyici():
    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"timeout": 10, "offset": offset}
            response = requests.get(url, params=params, timeout=15).json()
            if response.get("ok") and response.get("result"):
                for update in response["result"]:
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if not message or str(message.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID): continue
                    text = message.get("text", "")
                    if text == "/start": telegram_bildir("🤖 <b>XRP Sanal Bot Paneli Aktif!</b>", reply_markup=ana_menu_olustur())
                    elif text == "📊 Bot Durumu": telegram_bildir(telegram_canli_rapor_uret(), reply_markup=ana_menu_olustur())
                    elif text == "💰 Cüzdan Bakiyesi":
                        kar_zarar = sanal_cuzdan['nakit_usdt'] - sanal_cuzdan['baslangic_sermaye']
                        p_str = f"+{round(kar_zarar, 2)}$" if kar_zarar >= 0 else f"{round(kar_zarar, 2)}$"
                        telegram_bildir(f"💰 <b>Sanal Cüzdan Durumu</b>\n\n• Nakit: <b>{round(sanal_cuzdan['nakit_usdt'], 2)}$</b>\n• Toplam K/Z: <b>{p_str}</b>", reply_markup=ana_menu_olustur())
                    elif text == "▶️ Botu Başlat":
                        config.BOT_CALISIYOR = True
                        telegram_bildir("🚀 Bot tarama döngüsü <b>aktif.</b>", reply_markup=ana_menu_olustur())
                    elif text == "⏸️ Botu Durdur":
                        config.BOT_CALISIYOR = False
                        telegram_bildir("⏸️ Bot tarama döngüsü <b>durduruldu.</b>", reply_markup=ana_menu_olustur())
        except Exception: time.sleep(5)

# =====================================================================
# 🚀 PNL, STOP-LOSS VE TAKE-PROFIT TAKİP DÖNGÜSÜ
# =====================================================================
def hizli_pozisyon_takip_dongusu():
    global emir_beklemede
    while True:
        try:
            if not config.BOT_CALISIYOR:
                time.sleep(1.0)
                continue
            
            with data_lock:
                if not aktif_pozisyon["aktif"]:
                    time.sleep(1.0)
                    continue
                v = dict(piyasa_verisi)
                pos = dict(aktif_pozisyon)

            anlik_fiyat = v.get("anlik_fiyat", 0.0)
            if anlik_fiyat <= 0: continue

            giris_fiyati = pos["giris_fiyati"]
            yon = pos["yon"]
            adet = pos["adet"]

            # PNL Hesaplama
            if yon == "LONG":
                fiyat_farki = anlik_fiyat - giris_fiyati
                fiyat_degisim_pct = ((anlik_fiyat - giris_fiyati) / giris_fiyati) * 100
            else:
                fiyat_farki = giris_fiyati - anlik_fiyat
                fiyat_degisim_pct = ((giris_fiyati - anlik_fiyat) / giris_fiyati) * 100
            
            resmi_pnl = fiyat_farki * adet

            with data_lock:
                aktif_pozisyon["resmi_pnl"] = resmi_pnl

            kapatma_nedeni = None

            # 🎯 TP / SL Kontrolleri (%5 TP, %1.5 SL)
            if fiyat_degisim_pct >= config.TAKE_PROFIT_PERCENT:
                kapatma_nedeni = f"Take-Profit Hedefi (+%{config.TAKE_PROFIT_PERCENT})"
            elif fiyat_degisim_pct <= -config.STOP_LOSS_PERCENT:
                kapatma_nedeni = f"Stop-Loss Tetiklendi (-%{config.STOP_LOSS_PERCENT})"

            if kapatma_nedeni:
                with data_lock:
                    sanal_cuzdan["nakit_usdt"] += config.ISLEM_MARJIN + resmi_pnl
                    aktif_pozisyon["aktif"] = False
                
                telegram_bildir(
                    f"🔴 <b>XRP {yon} Sanal Kapatıldı!</b>\n"
                    f"• Neden: <b>{kapatma_nedeni}</b>\n"
                    f"• Net PNL: <b>{round(resmi_pnl, 3)}$</b>\n"
                    f"• Yeni Nakit: {round(sanal_cuzdan['nakit_usdt'], 2)}$"
                )

            time.sleep(config.HIZLI_TAKIP_PERIYODU)
        except Exception as e:
            time.sleep(2.0)

# --- 🎯 GİRİŞ SİNYALİ TARAMA DÖNGÜSÜ ---
def xrp_tarama_dongusu():
    while True:
        try:
            if not config.BOT_CALISIYOR:
                time.sleep(2.0)
                continue
            
            with data_lock:
                if aktif_pozisyon["aktif"]:
                    time.sleep(2.0)
                    continue
                v = dict(piyasa_verisi)

            sinyal = strateji_analiz(v)
            anlik_fiyat = v.get("anlik_fiyat", 0.0)

            if sinyal != "HOLD" and anlik_fiyat > 0:
                with data_lock:
                    if sanal_cuzdan["nakit_usdt"] < config.ISLEM_MARJIN:
                        continue # Yetersiz bakiye
                    
                    sanal_cuzdan["nakit_usdt"] -= config.ISLEM_MARJIN
                    qty = (config.ISLEM_MARJIN * config.KALDIRAC) / anlik_fiyat
                    qty = round(qty, FUTURES_HASSASIYETI) if FUTURES_HASSASIYETI > 0 else int(qty)
                    
                    if qty > 0:
                        yon_str = "LONG" if sinyal == "BUY" else "SHORT"
                        aktif_pozisyon.update({
                            "aktif": True, "yon": yon_str,
                            "adet": qty, "giris_fiyati": anlik_fiyat, "resmi_pnl": 0.0
                        })
                        telegram_bildir(f"🚀 <b>XRP {sinyal} Sanal Pozisyon Açıldı!</b>\nFiyat: {anlik_fiyat}\nAdet: {qty}")
            
            time.sleep(5.0)
        except Exception as e:
            time.sleep(3.0)

# --- 🚀 ANA ÇALIŞTIRICI ---
if __name__ == "__main__":
    print("🎬 XRP Optimize Edilmiş Paper Trading Botu Başlatılıyor...")
    
    if xrp_verilerini_hazirla():
        websocket_baslat()

        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            threading.Thread(target=telegram_gelen_mesaj_dinleyici, daemon=True).start()

        threading.Thread(target=hizli_pozisyon_takip_dongusu, daemon=True).start()
        xrp_tarama_dongusu()
