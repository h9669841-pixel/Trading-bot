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

class MultiBotConfig:
    def __init__(self):
        self.TIMEFRAME = "1h"                           # 1 Saatlik grafik periyodu
        self.ISLEM_MARJIN = 100.0                        # Her coin için işlem başına ayrılacak USDT (Margin)
        self.KALDIRAC = 10                              # Kaldıraç oranı
        self.BOT_CALISIYOR = True
        self.POLLING_PERIYODU = 10.0                     # Fiyat güncelleme aralığı

        # 🪙 Takip Edilecek Coinler ve Pine Script'teki Optimize Profilleri
        self.PROFILES = {
            "solusdt": {
                "name": "SOL",
                "bb_len": 15,
                "bb_mult": 1.6,
                "use_squeeze": True,
                "squeeze_thresh": 0.08,
                "use_volume": True,
                "vol_len": 17,
                "vol_mult": 1.6,
                "stop_loss": 1.5,
                "take_profit": 4.0
            },
            "xrpusdt": {
                "name": "XRP",
                "bb_len": 18,
                "bb_mult": 1.6,
                "use_squeeze": True,
                "squeeze_thresh": 0.50,
                "use_volume": True,
                "vol_len": 17,
                "vol_mult": 1.4,
                "stop_loss": 1.5,
                "take_profit": 5.0
            }
        }

config = MultiBotConfig()

# 💰 SANAL CÜZDAN YÖNETİMİ (Paper Trading)
sanal_cuzdan = {
    "nakit_usdt": 10000.0,  # Toplam başlangıç sanal sermayesi ($10,000)
    "baslangic_sermaye": 10000.0
}

# Her coin için veri ve pozisyon havuzu (Doğru değişken adı: piyasa_verileri)
piyasa_verileri = {
    "solusdt": {"klines": [], "anlik_fiyat": 0.0, "hassasiyet": 2},
    "xrpusdt": {"klines": [], "anlik_fiyat": 0.0, "hassasiyet": 2}
}

aktif_pozisyonlar = {
    "solusdt": {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "resmi_pnl": 0.0},
    "xrpusdt": {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "resmi_pnl": 0.0}
}

data_lock = threading.Lock()

# --- 📊 GEÇMİŞ VE ANLIK VERİLERİ ÇEKME (SOL & XRP) ---
def veri_guncelle_ve_klines_al():
    headers = {"User-Agent": "Mozilla/5.0"}
    api_endpoints = [
        "https://api.binance.com/api/v3/klines",
        "https://data-api.binance.vision/api/v3/klines",
        "https://fapi.binance.com/fapi/v1/klines"
    ]
    
    for symbol in config.PROFILES.keys():
        symbol_upper = symbol.upper()
        k_res = None
        for base_url in api_endpoints:
            try:
                full_url = f"{base_url}?symbol={symbol_upper}&interval={config.TIMEFRAME}&limit=250"
                response = requests.get(full_url, headers=headers, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    if data and len(data) > 0:
                        k_res = data
                        break
            except Exception:
                continue
        
        if k_res:
            with data_lock:
                piyasa_verileri[symbol]["klines"] = k_res
                piyasa_verileri[symbol]["anlik_fiyat"] = float(k_res[-1][4])

def polling_veri_akis_dongusu():
    print("🔄 Çoklu Coin Polling Veri Akış Döngüsü Başlatıldı.")
    while True:
        try:
            veri_guncelle_ve_klines_al()
        except Exception as e:
            print(f"⚠️ Polling hata: {e}")
        time.sleep(config.POLLING_PERIYODU)

# --- 🛠️ STRATEJİ ANALİZİ ---
def strateji_analiz(symbol, v, profile):
    candles = list(v["klines"])
    if not candles or len(candles) < 50:
        return "HOLD"

    df = pd.DataFrame(candles)
    df = df.iloc[:, :6]
    df.columns = ['open_time', 'open', 'high', 'low', 'close', 'volume']
    
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)

    vol_sma = df['volume'].rolling(window=profile["vol_len"]).mean()
    is_volume_ok = not profile["use_volume"] or (df['volume'].iloc[-2] > (vol_sma.iloc[-2] * profile["vol_mult"]))

    basis = df['close'].rolling(window=profile["bb_len"]).mean()
    dev = profile["bb_mult"] * df['close'].rolling(window=profile["bb_len"]).std(ddof=0)
    upper_band = basis + dev
    lower_band = basis - dev

    bb_width = (upper_band - lower_band) / basis
    is_squeezed = not profile["use_squeeze"] or (bb_width.iloc[-2] <= profile["squeeze_thresh"])

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
def telegram_bildir(mesaj, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}
        if reply_markup: data["reply_markup"] = reply_markup
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=data, timeout=5)
    except Exception as e:
        print(f"❌ Telegram gönderim hatası: {e}")

def konsol_ve_telegram_bildir(mesaj):
    temiz_mesaj = mesaj.replace('<b>','').replace('</b>','')
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {temiz_mesaj}")
    telegram_bildir(mesaj)

def inline_menu_olustur():
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Bot Durumu", "callback_data": "cmd_durum"},
                {"text": "💰 Cüzdan Bakiyesi", "callback_data": "cmd_bakiye"}
            ],
            [
                {"text": "▶️ Botu Başlat", "callback_data": "cmd_baslat"},
                {"text": "⏸️ Botu Durdur", "callback_data": "cmd_durdur"}
            ]
        ]
    }

def durum_raporu_metni():
    with data_lock:
        durum_str = "🟢 SOL & XRP Çoklu Bot Aktif" if config.BOT_CALISIYOR else "🔴 Sistem Durduruldu"
        toplam_portfoy = sanal_cuzdan["nakit_usdt"]
        
        for sym, pos in aktif_pozisyonlar.items():
            if pos["aktif"]:
                toplam_portfoy += pos["resmi_pnl"]

        rapor = (
            f"⚙️ <b>SOL & XRP Kombine Bot Raporu</b>\n"
            f"• Sistem: {durum_str}\n"
            f"• Nakit Bakiye: <b>{round(sanal_cuzdan['nakit_usdt'], 2)}$</b>\n"
            f"• Toplam Varlık: <b>{round(toplam_portfoy, 2)}$</b>\n\n"
            f"⚡ <b>Açık İşlemler:</b>\n"
        )
        acik_var = False
        for sym, pos in aktif_pozisyonlar.items():
            if pos["aktif"]:
                acik_var = True
                coin_adi = config.PROFILES[sym]["name"]
                rapor += f"• {coin_adi}USDT | {pos['yon']} | Fiyat: {pos['giris_fiyati']} | PNL: <b>{round(pos['resmi_pnl'], 3)}$</b>\n"
        
        if not acik_var:
            rapor += "Açık pozisyon bulunmuyor."
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
                    if message and str(message.get("chat", {}).get("id")) == str(TELEGRAM_CHAT_ID):
                        text = message.get("text", "")
                        if text == "/start": 
                            telegram_bildir("🤖 <b>SOL & XRP Çoklu Bot Paneli Aktif!</b>", reply_markup=inline_menu_olustur())

                    callback_query = update.get("callback_query")
                    if callback_query and str(callback_query.get("message", {}).get("chat", {}).get("id")) == str(TELEGRAM_CHAT_ID):
                        query_id = callback_query.get("id")
                        data = callback_query.get("data", "")

                        if data == "cmd_durum":
                            telegram_bildir(durum_raporu_metni(), reply_markup=inline_menu_olustur())
                        elif data == "cmd_bakiye":
                            kar_zarar = sanal_cuzdan['nakit_usdt'] - sanal_cuzdan['baslangic_sermaye']
                            p_str = f"+{round(kar_zarar, 2)}$" if kar_zarar >= 0 else f"{round(kar_zarar, 2)}$"
                            telegram_bildir(f"💰 <b>Sanal Cüzdan Durumu</b>\n\n• Nakit: <b>{round(sanal_cuzdan['nakit_usdt'], 2)}$</b>\n• Toplam K/Z: <b>{p_str}</b>", reply_markup=inline_menu_olustur())
                        elif data == "cmd_baslat":
                            config.BOT_CALISIYOR = True
                            telegram_bildir("🚀 Bot tarama döngüsü <b>aktif.</b>", reply_markup=inline_menu_olustur())
                        elif data == "cmd_durdur":
                            config.BOT_CALISIYOR = False
                            telegram_bildir("⏸️ Bot tarama döngüsü <b>durduruldu.</b>", reply_markup=inline_menu_olustur())
        except Exception: 
            time.sleep(5)

# --- ⏰ HER 5 DAKİKADA BİR KONSOL RAPORU ---
def periyodik_konsol_rapor_dongusu():
    while True:
        time.sleep(300)
        if config.BOT_CALISIYOR:
            with data_lock:
                toplam_portfoy = sanal_cuzdan["nakit_usdt"]
                for sym, pos in aktif_pozisyonlar.items():
                    if pos["aktif"]:
                        toplam_portfoy += pos["resmi_pnl"]
                
                print(f"\n⏱️ [5 Dakikalık Konsol Raporu] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"• Nakit Bakiye: {round(sanal_cuzdan['nakit_usdt'], 2)}$")
                print(f"• Toplam Varlık: {round(toplam_portfoy, 2)}$")
                for sym, pos in aktif_pozisyonlar.items():
                    coin_adi = config.PROFILES[sym]["name"]
                    if pos["aktif"]:
                        print(f"• {coin_adi} Açık Pozisyon: {pos['yon']} | Fiyat: {pos['giris_fiyati']} | PNL: {round(pos['resmi_pnl'], 3)}$")
                    else:
                        print(f"• {coin_adi} Açık Pozisyon: Bulunmuyor.")
                print("-" * 40)

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
                islem_yapilacak_semboller = [s for s, p in aktif_pozisyonlar.items() if p["aktif"]]

            for symbol in islem_yapilacak_semboller:
                with data_lock:
                    v = dict(piyasa_verileri[symbol])
                    pos = dict(aktif_pozisyonlar[symbol])
                    profile = config.PROFILES[symbol]

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
                    aktif_pozisyonlar[symbol]["resmi_pnl"] = resmi_pnl

                kapatma_nedeni = None
                if fiyat_degisim_pct >= profile["take_profit"]:
                    kapatma_nedeni = f"Take-Profit Hedefi (+%{profile['take_profit']})"
                elif fiyat_degisim_pct <= -profile["stop_loss"]:
                    kapatma_nedeni = f"Stop-Loss Tetiklendi (-%{profile['stop_loss']})"

                if kapatma_nedeni:
                    with data_lock:
                        sanal_cuzdan["nakit_usdt"] += config.ISLEM_MARJIN + resmi_pnl
                        aktif_pozisyonlar[symbol]["aktif"] = False
                    
                    coin_adi = profile["name"]
                    konsol_ve_telegram_bildir(
                        f"🔴 <b>{coin_adi} {yon} Sanal Pozisyon Kapatıldı!</b>\n"
                        f"• Neden: <b>{kapatma_nedeni}</b>\n"
                        f"• Net PNL: <b>{round(resmi_pnl, 3)}$</b>\n"
                        f"• Yeni Nakit: {round(sanal_cuzdan['nakit_usdt'], 2)}$"
                    )

            time.sleep(1.0)
        except Exception as e:
            time.sleep(2.0)

# --- 🎯 GİRİŞ SİNYALİ TARAMA DÖNGÜSÜ ---
def multi_coin_tarama_dongusu():
    while True:
        try:
            if not config.BOT_CALISIYOR:
                time.sleep(2.0)
                continue
            
            for symbol, profile in config.PROFILES.items():
                with data_lock:
                    if aktif_pozisyonlar[symbol]["aktif"]:
                        continue
                    v = dict(piyasa_verileri[symbol])

                sinyal = strateji_analiz(symbol, v, profile)
                anlik_fiyat = v.get("anlik_fiyat", 0.0)

                if sinyal != "HOLD" and anlik_fiyat > 0:
                    with data_lock:
                        if sanal_cuzdan["nakit_usdt"] < config.ISLEM_MARJIN:
                            continue 
                        
                        sanal_cuzdan["nakit_usdt"] -= config.ISLEM_MARJIN
                        qty = (config.ISLEM_MARJIN * config.KALDIRAC) / anlik_fiyat
                        qty = round(qty, 2)
                        
                        if qty > 0:
                            yon_str = "LONG" if sinyal == "BUY" else "SHORT"
                            aktif_pozisyonlar[symbol].update({
                                "aktif": True, "yon": yon_str,
                                "adet": qty, "giris_fiyati": anlik_fiyat, "resmi_pnl": 0.0
                            })
                            coin_adi = profile["name"]
                            konsol_ve_telegram_bildir(
                                f"🚀 <b>{coin_adi} {sinyal} Sanal Pozisyon Açıldı!</b>\n"
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
    print("🎬 SOL & XRP Kombine Polling Trading Botu Başlatılıyor...")
    
    veri_guncelle_ve_klines_al()
    
    telegram_bildir(
        "🚀 <b>SOL & XRP Kombine Al-Sat Botu Başlatıldı!</b>\n"
        f"• Profiller: SOL (1h) & XRP (1h)\n"
        f"• Başlangıç Sermayesi: {sanal_cuzdan['nakit_usdt']}$",
        reply_markup=inline_menu_olustur()
    )

    threading.Thread(target=polling_veri_akis_dongusu, daemon=True).start()

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=telegram_gelen_mesaj_dinleyici, daemon=True).start()

    threading.Thread(target=periyodik_konsol_rapor_dongusu, daemon=True).start()
    threading.Thread(target=hizli_pozisyon_takip_dongusu, daemon=True).start()
    multi_coin_tarama_dongusu()
