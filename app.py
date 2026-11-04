import os
import json
import time
import requests
import threading
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
        self.TIMEFRAME = "1h"                           # 1 Saatlik grafik periyodu
        self.ISLEM_MARJIN = 100.0                        # Sanal işlem başına ayrılacak USDT (Margin)
        self.KALDIRAC = 10                              # Kaldıraç oranı
        
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

        self.POLLING_PERIYODU = 10.0                     # Her 10 saniyede bir fiyatı yenile (Polling)

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
data_lock = threading.Lock()

# --- 📊 XRP GEÇMİŞ VE ANLIK VERİLERİNİ ÇEKME (Polling Tabanlı) ---
def veri_guncelle_ve_klines_al():
    global FUTURES_HASSASIYETI
    symbol_upper = config.TARGET_SYMBOL.upper()
    
    api_endpoints = [
        "https://api.binance.com/api/v3/klines",
        "https://data-api.binance.vision/api/v3/klines",
        "https://fapi.binance.com/fapi/v1/klines",
        "https://api1.binance.com/api/v3/klines"
    ]
    
    k_res = None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    for klines_url in api_endpoints:
        try:
            full_url = f"{klines_url}?symbol={symbol_upper}&interval={config.TIMEFRAME}&limit=250"
            response = requests.get(full_url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                k_res = response.json()
                if k_res and len(k_res) > 0:
                    break
        except Exception:
            continue

    if not k_res:
        return False

    try:
        with data_lock:
            piyasa_verisi["klines"] = k_res
            piyasa_verisi["anlik_fiyat"] = float(k_res[-1][4]) if k_res else 0.0
        return True
    except Exception:
        return False

# --- 🔄 POLLING DÖNGÜSÜ (WebSocket Yerine Düzenli Sorgu) ---
def polling_veri_akis_dongusu():
    print("🔄 Polling Tabanlı Veri Akış Döngüsü Başlatıldı.")
    while True:
        try:
            veri_guncelle_ve_klines_al()
        except Exception as e:
            print(f"⚠️ Polling veri güncelleme hatası: {e}")
        time.sleep(config.POLLING_PERIYODU)

# --- 🛠️ STRATEJİ ANALİZİ ---
def strateji_analiz(v):
    candles = list(v["klines"])
    if not candles or len(candles) < 50:
        return "HOLD"

    df = pd.DataFrame(candles)
    df = df.iloc[:, :6]
    df.columns = ['open_time', 'open', 'high', 'low', 'close', 'volume']
    
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)

    vol_sma = df['volume'].rolling(window=config.VOL_LEN).mean()
    is_volume_ok = not config.USE_VOLUME or (df['volume'].iloc[-2] > (vol_sma.iloc[-2] * config.VOL_MULT))

    basis = df['close'].rolling(window=config.BB_LEN).mean()
    dev = config.BB_MULT * df['close'].rolling(window=config.BB_LEN).std(ddof=0)
    upper_band = basis + dev
    lower_band = basis - dev

    bb_width = (upper_band - lower_band) / basis
    is_squeezed = not config.USE_SQUEEZE or (bb_width.iloc[-2] <= config.SQUEEZE_THRESH)

    prev_close = df['close'].iloc[-3]
    curr_close = df['close'].iloc[-2]
    curr_upper = upper_band.iloc[-2]
    curr_lower = lower_band.iloc[-2]

    long_condition = (prev_close <= curr_upper) and (curr_close > curr_upper) and is_squeezed and is_volume_ok
    short_condition = (prev_close >= curr_lower) and (curr_close < curr_lower) and is_squeezed and is_volume_ok

    if long_condition:
        return "BUY"
    elif short_condition:
        return "SELL"
    return "HOLD"

# --- 🎛️ BİLDİRİM VE KONSOL FONKSİYONLARI ---
def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=data, timeout=5)
    except Exception as e:
        print(f"❌ Telegram gönderim hatası: {e}")

def konsol_ve_telegram_bildir(mesaj):
    temiz_mesaj = mesaj.replace('<b>','').replace('</b>','')
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {temiz_mesaj}")
    telegram_bildir(mesaj)

def ana_menu_olustur():
    return {"keyboard": [[{"text": "📊 Bot Durumu"}, {"text": "💰 Cüzdan Bakiyesi"}], [{"text": "▶️ Botu Başlat"}, {"text": "⏸️ Botu Durdur"}]], "resize_keyboard": True}

def durum_raporu_metni():
    with data_lock:
        durum_str = "🟢 XRP Polling Trading Aktif" if config.BOT_CALISIYOR else "🔴 Sistem Durduruldu"
        toplam_portfoy = sanal_cuzdan["nakit_usdt"]
        if aktif_pozisyon["aktif"]:
            toplam_portfoy += aktif_pozisyon["resmi_pnl"]

        rapor = (
            f"⚙️ <b>XRP Akülasyon Botu (Durum Raporu)</b>\n"
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
                    if text == "/start": 
                        telegram_bildir("🤖 <b>XRP Sanal Bot Paneli Aktif!</b>")
                    elif text == "📊 Bot Durumu": 
                        telegram_bildir(durum_raporu_metni())
                    elif text == "💰 Cüzdan Bakiyesi":
                        kar_zarar = sanal_cuzdan['nakit_usdt'] - sanal_cuzdan['baslangic_sermaye']
                        p_str = f"+{round(kar_zarar, 2)}$" if kar_zarar >= 0 else f"{round(kar_zarar, 2)}$"
                        telegram_bildir(f"💰 <b>Sanal Cüzdan Durumu</b>\n\n• Nakit: <b>{round(sanal_cuzdan['nakit_usdt'], 2)}$</b>\n• Toplam K/Z: <b>{p_str}</b>")
                    elif text == "▶️ Botu Başlat":
                        config.BOT_CALISIYOR = True
                        telegram_bildir("🚀 Bot tarama döngüsü <b>aktif.</b>")
                    elif text == "⏸️ Botu Durdur":
                        config.BOT_CALISIYOR = False
                        telegram_bildir("⏸️ Bot tarama döngüsü <b>durduruldu.</b>")
        except Exception: time.sleep(5)

# --- ⏰ HER 5 DAKİKADA BİR SADECE RAILWAY KONSOLUNA RAPOR ---
def periyodik_konsol_rapor_dongusu():
    while True:
        time.sleep(300) # 5 Dakika
        if config.BOT_CALISIYOR:
            with data_lock:
                toplam_portfoy = sanal_cuzdan["nakit_usdt"]
                if aktif_pozisyon["aktif"]:
                    toplam_portfoy += aktif_pozisyon["resmi_pnl"]
                
                print(f"\n⏱️ [5 Dakikalık Konsol Raporu] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"• Nakit Bakiye: {round(sanal_cuzdan['nakit_usdt'], 2)}$")
                print(f"• Toplam Varlık: {round(toplam_portfoy, 2)}$")
                if aktif_pozisyon["aktif"]:
                    print(f"• Açık Pozisyon: {aktif_pozisyon['yon']} | Fiyat: {aktif_pozisyon['giris_fiyati']} | PNL: {round(aktif_pozisyon['resmi_pnl'], 3)}$")
                else:
                    print("• Açık Pozisyon: Bulunmuyor.\n")

# =====================================================================
# 🚀 PNL, STOP-LOSS VE TAKE-PROFIT TAKİP DÖNGÜSÜ
# =====================================================================
def hizli_pozisyon_takip_dongusu():
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

            if fiyat_degisim_pct >= config.TAKE_PROFIT_PERCENT:
                kapatma_nedeni = f"Take-Profit Hedefi (+%{config.TAKE_PROFIT_PERCENT})"
            elif fiyat_degisim_pct <= -config.STOP_LOSS_PERCENT:
                kapatma_nedeni = f"Stop-Loss Tetiklendi (-%{config.STOP_LOSS_PERCENT})"

            if kapatma_nedeni:
                with data_lock:
                    sanal_cuzdan["nakit_usdt"] += config.ISLEM_MARJIN + resmi_pnl
                    aktif_pozisyon["aktif"] = False
                
                konsol_ve_telegram_bildir(
                    f"🔴 <b>XRP {yon} Sanal Pozisyon Kapatıldı!</b>\n"
                    f"• Neden: <b>{kapatma_nedeni}</b>\n"
                    f"• Net PNL: <b>{round(resmi_pnl, 3)}$</b>\n"
                    f"• Yeni Nakit: {round(sanal_cuzdan['nakit_usdt'], 2)}$"
                )

            time.sleep(1.0)
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
                        continue 
                    
                    sanal_cuzdan["nakit_usdt"] -= config.ISLEM_MARJIN
                    qty = (config.ISLEM_MARJIN * config.KALDIRAC) / anlik_fiyat
                    qty = round(qty, FUTURES_HASSASIYETI) if FUTURES_HASSASIYETI > 0 else int(qty)
                    
                    if qty > 0:
                        yon_str = "LONG" if sinyal == "BUY" else "SHORT"
                        aktif_pozisyon.update({
                            "aktif": True, "yon": yon_str,
                            "adet": qty, "giris_fiyati": anlik_fiyat, "resmi_pnl": 0.0
                        })
                        konsol_ve_telegram_bildir(
                            f"🚀 <b>XRP {sinyal} Sanal Pozisyon Açıldı!</b>\n"
                            f"• Yön: {yon_str}\n"
                            f"• Fiyat: {anlik_fiyat}\n"
                            f"• Adet: {qty}\n"
                            f"• Marjin: {config.ISLEM_MARJIN}$ ({config.KALDIRAC}x)"
                        )
            
            time.sleep(5.0)
        except Exception as e:
            time.sleep(3.0)

# --- 🚀 ANA ÇALIŞTIRICI ---
if __name__ == "__main__":
    print("🎬 XRP Polling Tabanlı Paper Trading Botu Başlatılıyor...")
    
    if veri_guncelle_ve_klines_al():
        konsol_ve_telegram_bildir(
            "🚀 <b>XRP Sanal Al-Sat Botu Başlatıldı! (Polling Modu)</b>\n"
            f"• Parite: XRPUSDT (1h)\n"
            f"• Başlangıç Sermayesi: {sanal_cuzdan['nakit_usdt']}$"
        )

        # 🔄 Arka planda periyodik fiyat güncelleyen polling iş parçacığı
        threading.Thread(target=polling_veri_akis_dongusu, daemon=True).start()

        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            threading.Thread(target=telegram_gelen_mesaj_dinleyici, daemon=True).start()

        threading.Thread(target=periyodik_konsol_rapor_dongusu, daemon=True).start()
        threading.Thread(target=hizli_pozisyon_takip_dongusu, daemon=True).start()
        xrp_tarama_dongusu()
