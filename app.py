import os
import json
import time
import requests
import threading
import math
import numpy as np
import pandas as pd
from datetime import datetime
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

# --- 🔑 GÜVENLİK VE API AYARLARI ---
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
PROXY_URL = os.environ.get("PROXY_URL")

proxy_formatted = None
if PROXY_URL:
    proxy_formatted = PROXY_URL if PROXY_URL.startswith("socks5://") else PROXY_URL.replace("socks5://", "socks5h://")

# 🌐 1. Genel İstemci (Fiyat ve Mum Verileri İçin - PROXY'SİZ)
client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

# 🔐 2. Emir ve Cüzdan İstemcisi (Hesap İşlemleri İçin - PROXY'Lİ)
order_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

if proxy_formatted:
    print(f"🌐 Proxy tüneli SADECE hesap/emir işlemleri için bağlandı: {proxy_formatted.split('@')[-1] if '@' in proxy_formatted else proxy_formatted}")
    order_client.session.proxies = {"http": proxy_formatted, "https": proxy_formatted}
else:
    print("⚠️ PROXY_URL bulunamadı! Tüm işlemler yerel ağ üzerinden yapılacak.")

class TrendBotConfig:
    def __init__(self):
        self.TIMEFRAME = Client.KLINE_INTERVAL_30MINUTE  # 30m
        self.ISLEM_MARJIN = 2.0                          # 2 USDT
        self.KALDIRAC = 50                              # 50x
        
        self.MAX_ACIK_POZISYON = 7
        self.BOT_CALISIYOR = True
        self.COOLDOWN_SURESI = 0

        # === STRATEJİ PARAMETRELERİ ===
        self.EMA_TREND_PERIOD = 200
        self.RSI_PERIOD = 14
        self.PIVOT_LOOKBACK = 3
        self.SUPERTREND_ATR_PERIOD = 10
        self.SUPERTREND_FACTOR = 3.0

        self.API_DELAY = 0.5
        self.HIZLI_TAKIP_PERIYODU = 2.0

config = TrendBotConfig()

# 📌 Takip Edilecek Özel Parite Listesi
OZEL_COIN_LISTESI = [
    "btcusdt",
    "ethusdt",
    "solusdt",
    "xrpusdt",
    "xauusdt",
    "zecusdt",
    "spcxusdt"
]

SYMBOLS = []
piyasa_verisi = {}
aktif_pozisyonlar = {}
FUTURES_HASSASIYETLERI = {}
son_islem_zamanlari = {}
emir_beklemede_durumu = {}
son_kapatilan_mum_zamanlari = {}
data_lock = threading.Lock()

# --- 🛠️ MATEMATİKSEL İNDİKATÖR FONKSİYONLARI ---
def hesapla_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def hesapla_supertrend(df, atr_period=10, factor=3.0):
    high = df['high']
    low = df['low']
    close = df['close']
    
    tr1 = pd.Series(high - low)
    tr2 = pd.Series(abs(high - close.shift(1)))
    tr3 = pd.Series(abs(low - close.shift(1)))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(atr_period).mean()

    hl2 = (high + low) / 2
    basic_upper = hl2 + (factor * atr)
    basic_lower = hl2 - (factor * atr)

    upper_band = basic_upper.copy()
    lower_band = basic_lower.copy()

    for i in range(1, len(df)):
        if basic_upper.iloc[i] < upper_band.iloc[i-1] or close.iloc[i-1] > upper_band.iloc[i-1]:
            upper_band.iloc[i] = basic_upper.iloc[i]
        else:
            upper_band.iloc[i] = upper_band.iloc[i-1]

        if basic_lower.iloc[i] > lower_band.iloc[i-1] or close.iloc[i-1] < lower_band.iloc[i-1]:
            lower_band.iloc[i] = basic_lower.iloc[i]
        else:
            lower_band.iloc[i] = lower_band.iloc[i-1]

    direction = pd.Series(1, index=df.index)
    supertrend = pd.Series(0.0, index=df.index)

    for i in range(1, len(df)):
        if direction.iloc[i-1] == 1:
            if close.iloc[i] < lower_band.iloc[i-1]:
                direction.iloc[i] = -1
                supertrend.iloc[i] = upper_band.iloc[i]
            else:
                direction.iloc[i] = 1
                supertrend.iloc[i] = lower_band.iloc[i]
        else:
            if close.iloc[i] > upper_band.iloc[i-1]:
                direction.iloc[i] = 1
                supertrend.iloc[i] = lower_band.iloc[i]
            else:
                direction.iloc[i] = -1
                supertrend.iloc[i] = upper_band.iloc[i]

    return supertrend, direction

def strateji_analiz(v, anlik_fiyat):
    candles = list(v["klines"])
    if not candles or len(candles) < 210:
        return "HOLD", False, 0.0, 0.0, 0

    df = pd.DataFrame(candles, columns=['open_time', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['open'] = df['open'].astype(float)
    
    df.iloc[-1, df.columns.get_loc('close')] = anlik_fiyat

    df['ema200'] = df['close'].ewm(span=config.EMA_TREND_PERIOD, adjust=False).mean()
    df['rsi'] = hesapla_rsi(df['close'], config.RSI_PERIOD)
    st_series, st_direction = hesapla_supertrend(df, config.SUPERTREND_ATR_PERIOD, config.SUPERTREND_FACTOR)

    curr_close = df['close'].iloc[-1]
    curr_open = df['open'].iloc[-1]
    curr_ema = df['ema200'].iloc[-1]
    curr_st = st_series.iloc[-1]
    curr_st_dir = st_direction.iloc[-1]

    # Hidden Divergence Tespiti
    lb = config.PIVOT_LOOKBACK
    hidden_bull = False
    hidden_bear = False

    rsi_vals = df['rsi'].values
    low_vals = df['low'].values
    high_vals = df['high'].values

    # Pivot Low
    pivots_low = []
    for i in range(len(df) - lb - 1, len(df) - 30, -1):
        if i - lb < 0 or i + lb >= len(df): continue
        is_pivot = True
        for j in range(1, lb + 1):
            if rsi_vals[i] >= rsi_vals[i-j] or rsi_vals[i] >= rsi_vals[i+j]:
                is_pivot = False
                break
        if is_pivot:
            pivots_low.append((i, rsi_vals[i], low_vals[i]))
            if len(pivots_low) == 2: break

    if len(pivots_low) == 2:
        p_curr, rsi_curr, price_curr = pivots_low[0]
        p_prev, rsi_prev, price_prev = pivots_low[1]
        if price_curr > price_prev and rsi_curr < rsi_prev and curr_close > curr_ema:
            hidden_bull = True

    # Pivot High
    pivots_high = []
    for i in range(len(df) - lb - 1, len(df) - 30, -1):
        if i - lb < 0 or i + lb >= len(df): continue
        is_pivot = True
        for j in range(1, lb + 1):
            if rsi_vals[i] <= rsi_vals[i-j] or rsi_vals[i] <= rsi_vals[i+j]:
                is_pivot = False
                break
        if is_pivot:
            pivots_high.append((i, rsi_vals[i], high_vals[i]))
            if len(pivots_high) == 2: break

    if len(pivots_high) == 2:
        p_curr, rsi_curr, price_curr = pivots_high[0]
        p_prev, rsi_prev, price_prev = pivots_high[1]
        if price_curr < price_prev and rsi_curr > rsi_prev and curr_close < curr_ema:
            hidden_bear = True

    giris_sinyali = "HOLD"
    if hidden_bull and curr_close > curr_ema and curr_close > curr_open:
        giris_sinyali = "BUY"
    elif hidden_bear and curr_close < curr_ema and curr_close < curr_open:
        giris_sinyali = "SELL"

    return giris_sinyali, curr_close, curr_ema, curr_st, curr_st_dir

# --- 🌐 REST API ALTYAPI FONKSİYONLARI ---
def kontrollu_coin_ekle(coin_adi, eski_pozisyon_mu=False):
    coin_lower = coin_adi.lower().strip()
    coin_upper = coin_lower.upper()
    if coin_lower in SYMBOLS: return True
    try:
        # 🟢 PROXY'SİZ (Public Fiyat Bilgisi)
        f_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(f_url, headers=headers, timeout=10)
        if response.status_code != 200: return False
        r = response.json()
        market_info = next((m for m in r.get("symbols", []) if m["symbol"] == coin_upper), None)
        if not market_info or market_info.get('status') != 'TRADING': return False
        time.sleep(0.20)
        
        # 🔐 PROXY'Lİ (Kaldıraç ve Marjin Ayarı)
        if not eski_pozisyon_mu:
            try:
                order_client.futures_change_leverage(symbol=coin_upper, leverage=config.KALDIRAC)
                order_client.futures_change_margin_type(symbol=coin_upper, marginType="ISOLATED")
            except BinanceAPIException as e:
                if "No need to change" not in e.message: pass
        for f in market_info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step_size_str = str(f['stepSize']).rstrip('0')
                precision = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
                FUTURES_HASSASIYETLERI[coin_lower] = precision
        with data_lock:
            if coin_lower not in SYMBOLS:
                SYMBOLS.append(coin_lower)
            piyasa_verisi[coin_lower] = {"anlik_fiyat": 0.0, "klines": [], "guncel_mum_zamani": 0}
            aktif_pozisyonlar[coin_lower] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "resmi_pnl": 0.0}
            son_islem_zamanlari[coin_lower] = 0.0
            emir_beklemede_durumu[coin_lower] = False
            son_kapatilan_mum_zamanlari[coin_lower] = 0
        return True
    except Exception:
        return False

def tek_coin_api_verisi_guncelle(s):
    try:
        # 🟢 PROXY'SİZ (Public Mum Verileri - Kota Dostu)
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={s.upper()}&interval={config.TIMEFRAME}&limit=250"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code != 200: return False
        k = response.json()
        if not k or len(k) == 0: return False
        
        anlik_fiyat_yeni = float(k[-1][4])
        guncel_mum_zamani = k[-1][0]
        
        with data_lock:
            piyasa_verisi[s]["klines"] = k
            piyasa_verisi[s]["anlik_fiyat"] = anlik_fiyat_yeni
            piyasa_verisi[s]["guncel_mum_zamani"] = guncel_mum_zamani
        return True
    except Exception:
        return False

def acik_pozisyonlari_binanceden_guncelle():
    try:
        # 🔐 PROXY'Lİ (Canlı Cüzdan ve Hesap Kontrolü)
        hesap_bilgisi = order_client.futures_account()
        pozisyonlar = hesap_bilgisi.get("positions", [])
        
        with data_lock:
            for s in SYMBOLS:
                if not emir_beklemede_durumu.get(s, False):
                    aktif_pozisyonlar[s] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "resmi_pnl": 0.0}
            
            for p in pozisyonlar:
                sym = p.get("symbol", "").lower()
                if sym in aktif_pozisyonlar:
                    if emir_beklemede_durumu.get(sym, False): continue
                    amt = float(p.get("positionAmt", 0))
                    entry_price = float(p.get("entryPrice", 0))
                    unrealized_pnl = float(p.get("unrealizedProfit", 0.0))
                    
                    if amt != 0:
                        aktif_pozisyonlar[sym]["aktif"] = True
                        aktif_pozisyonlar[sym]["yon"] = "LONG" if amt > 0 else "SHORT"
                        aktif_pozisyonlar[sym]["adet"] = abs(amt)
                        aktif_pozisyonlar[sym]["giris_fiyati"] = entry_price
                        aktif_pozisyonlar[sym]["resmi_pnl"] = unrealized_pnl
    except Exception as e:
        print(f"❌ Cüzdan senkronizasyon hatası: {e}")

# --- 🎛️ TELEGRAM YÖNETİMİ (PROXY'SİZ) ---
def telegram_bildir(mesaj, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}
        if reply_markup: data["reply_markup"] = reply_markup
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=data, timeout=5)
    except Exception: pass

def ana_menu_olustur():
    return {"keyboard": [[{"text": "📊 Bot Durumu"}], [{"text": "▶️ Botu Başlat"}, {"text": "⏸️ Botu Durdur"}]], "resize_keyboard": True, "one_time_keyboard": False}

def telegram_canli_rapor_uret():
    acik_pozisyonlari_binanceden_guncelle()
    with data_lock:
        acik_pozlar = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])
        durum_str = "🟢 Özel Liste Taranıyor" if config.BOT_CALISIYOR else "🔴 Sistem Durduruldu"
        rapor = (
            f"⚙️ <b>Hidden Divergence Botu</b>\n"
            f"• Sistem: {durum_str}\n"
            f"• Takip Edilen Çiftler: {len(SYMBOLS)}\n"
            f"• Periyot: 30m\n"
            f"• Marjin: {config.ISLEM_MARJIN:.1f} USDT\n"
            f"• Kaldıraç: {config.KALDIRAC}x\n"
            f"• Risk Limiti: {acik_pozlar}/{config.MAX_ACIK_POZISYON} Poz.\n"
            f"• Çıkış Stratejisi: Supertrend & 200 EMA Kırılımı\n\n"
            f"⚡ <b>Açık İşlemler:</b>\n"
        )
        if acik_pozlar == 0:
            rapor += "Açık izole pozisyon bulunmuyor."
        else:
            for s in SYMBOLS:
                if aktif_pozisyonlar[s]["aktif"]:
                    p = aktif_pozisyonlar[s]
                    rapor += f"• {s.upper()} | {p['yon']} | PNL: <b>{round(p['resmi_pnl'], 4)}$</b>\n"
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
                    if text == "/start": telegram_bildir("🤖 <b>Bot Kontrol Paneli Aktif!</b>", reply_markup=ana_menu_olustur())
                    elif text == "📊 Bot Durumu": telegram_bildir(telegram_canli_rapor_uret(), reply_markup=ana_menu_olustur())
                    elif text == "▶️ Botu Başlat":
                        config.BOT_CALISIYOR = True
                        telegram_bildir("🚀 Bot tarama döngüsü <b>aktif.</b>", reply_markup=ana_menu_olustur())
                    elif text == "⏸️ Botu Durdur":
                        config.BOT_CALISIYOR = False
                        telegram_bildir("⏸️ Bot tarama döngüsü <b>durduruldu.</b>", reply_markup=ana_menu_olustur())
        except Exception: time.sleep(5)

# =====================================================================
# 🚀 AÇIK POZİSYON İNDİKATÖR KONTROL VE ÇIKIŞ DÖNGÜSÜ
# =====================================================================
def hizli_acik_pozisyon_takip_dongusu():
    while True:
        try:
            if not config.BOT_CALISIYOR:
                time.sleep(1.0)
                continue
            
            acik_pozisyonlari_binanceden_guncelle()
            
            with data_lock:
                acik_semboller = [s for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"]]
            
            if not acik_semboller:
                time.sleep(1.0)
                continue

            su_an_ts = time.time()

            for symbol in acik_semboller:
                # 🟢 PROXY'SİZ (Fiyat Güncelleme)
                if not tek_coin_api_verisi_guncelle(symbol):
                    continue

                with data_lock:
                    v = dict(piyasa_verisi[symbol])
                    pos = dict(aktif_pozisyonlar[symbol])
                    emir_beklemede = emir_beklemede_durumu.get(symbol, False)
                
                if emir_beklemede or pos["adet"] <= 0 or not pos["aktif"]: 
                    continue

                anlik_fiyat = v.get("anlik_fiyat", 0.0)
                if anlik_fiyat <= 0: continue

                _, _, ema200, supertrend, supertrend_dir = strateji_analiz(v, anlik_fiyat)

                kapatma_nedeni = None

                if pos["yon"] == "LONG":
                    if supertrend_dir == -1 or anlik_fiyat < supertrend:
                        kapatma_nedeni = "Supertrend Altına İndi (Kâr Al / Trend Dönüşü)"
                    elif anlik_fiyat < ema200:
                        kapatma_nedeni = "200 EMA Altına Saptı (Stop Loss)"

                elif pos["yon"] == "SHORT":
                    if supertrend_dir == 1 or anlik_fiyat > supertrend:
                        kapatma_nedeni = "Supertrend Üstüne Çıktı (Kâr Al / Trend Dönüşü)"
                    elif anlik_fiyat > ema200:
                        kapatma_nedeni = "200 EMA Üstüne Saptı (Stop Loss)"

                # 🔐 PROXY'Lİ (Pozisyon Kapatma Emri)
                if kapatma_nedeni:
                    with data_lock:
                        if emir_beklemede_durumu[symbol]: continue
                        emir_beklemede_durumu[symbol] = True
                    try:
                        precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                        faktor = 10 ** precision
                        qty_to_close = math.floor(pos["adet"] * faktor) / faktor if precision > 0 else int(pos["adet"])
                        side_to_close = SIDE_SELL if pos["yon"] == "LONG" else SIDE_BUY
                        
                        if qty_to_close > 0:
                            print(f"🎯 {symbol.upper()} Pozisyonu Kapatılıyor. Neden: {kapatma_nedeni}")
                            order_client.futures_create_order(
                                symbol=symbol.upper(),
                                side=side_to_close,
                                type=ORDER_TYPE_MARKET,
                                quantity=qty_to_close,
                                reduceOnly=True
                            )
                        with data_lock:
                            son_islem_zamanlari[symbol] = su_an_ts
                            if "guncel_mum_zamani" in piyasa_verisi[symbol]:
                                son_kapatilan_mum_zamanlari[symbol] = piyasa_verisi[symbol]["guncel_mum_zamani"]
                            
                            aktif_pozisyonlar[symbol] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "resmi_pnl": 0.0}
                        
                        telegram_bildir(
                            f"🔴 <b>{symbol.upper()} {pos['yon']} Kapatıldı!</b>\n"
                            f"• Neden: {kapatma_nedeni}\n"
                            f"• Son PNL: {round(pos['resmi_pnl'], 3)}$\n"
                            f"• Fiyat: {anlik_fiyat}"
                        )
                    except Exception as e:
                        print(f"❌ Kapatma emri hatası ({symbol}): {e}")
                    finally:
                        with data_lock: emir_beklemede_durumu[symbol] = False

            time.sleep(config.HIZLI_TAKIP_PERIYODU)
        except Exception as e:
            print(f"❌ Takip döngüsü hatası: {e}")
            time.sleep(2.0)

# --- 🎯 GİRİŞ SİNYALİ TARAMA MOTORU ---
def pure_api_tarama_dongusu():
    while True:
        try:
            if not config.BOT_CALISIYOR:
                time.sleep(1.0)
                continue
            su_an_ts = time.time()
            with data_lock:
                kapali_olanlar = [s for s in SYMBOLS if not aktif_pozisyonlar[s]["aktif"]]
            
            for symbol in kapali_olanlar:
                if not config.BOT_CALISIYOR: break
                with data_lock:
                    if aktif_pozisyonlar[symbol]["aktif"]: continue
                
                # 🟢 PROXY'SİZ (Mum/Grafik Taraması)
                if not tek_coin_api_verisi_guncelle(symbol):
                    time.sleep(config.API_DELAY)
                    continue
                with data_lock:
                    v = dict(piyasa_verisi[symbol])
                    pos = dict(aktif_pozisyonlar[symbol])
                    son_islem = son_islem_zamanlari[symbol]
                if len(v["klines"]) < 210 or not v["anlik_fiyat"] or v["anlik_fiyat"] <= 0:
                    time.sleep(config.API_DELAY)
                    continue
                
                anlik_fiyat = v["anlik_fiyat"]
                
                guncel_mum_ts = v.get("guncel_mum_zamani", 0)
                with data_lock:
                    son_kapatilan_mum_ts = son_kapatilan_mum_zamanlari.get(symbol, 0)
                
                if guncel_mum_ts == son_kapatilan_mum_ts and guncel_mum_ts != 0:
                    time.sleep(config.API_DELAY)
                    continue
                
                if not pos["aktif"]:
                    if su_an_ts - son_islem < config.COOLDOWN_SURESI:
                        time.sleep(config.API_DELAY)
                        continue
                with data_lock:
                    guncel_acik_pozisyon_sayisi = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])
                if guncel_acik_pozisyon_sayisi >= config.MAX_ACIK_POZISYON:
                    time.sleep(config.API_DELAY)
                    continue
                
                sinyal, curr_close, guncel_ema, guncel_st, supertrend_dir = strateji_analiz(v, anlik_fiyat)
                
                # 🔐 PROXY'Lİ (İşlem Açma Emri)
                if sinyal != "HOLD":
                    with data_lock:
                        guncel_acik_pozisyon_sayisi = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])
                        if guncel_acik_pozisyon_sayisi >= config.MAX_ACIK_POZISYON or emir_beklemede_durumu[symbol] or aktif_pozisyonlar[symbol]["aktif"]:
                            time.sleep(config.API_DELAY)
                            continue
                        emir_beklemede_durumu[symbol] = True
                    try:
                        precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                        qty = (config.ISLEM_MARJIN * config.KALDIRAC) / anlik_fiyat
                        qty = float(int(qty * (10 ** precision))) / (10 ** precision) if precision > 0 else int(qty)
                        if qty <= 0:
                            with data_lock: emir_beklemede_durumu[symbol] = False
                            continue
                        if sinyal == "BUY":
                            order_client.futures_create_order(symbol=symbol.upper(), side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
                            with data_lock: 
                                aktif_pozisyonlar[symbol] = {"aktif": True, "yon": "LONG", "adet": qty, "giris_fiyati": anlik_fiyat, "resmi_pnl": 0.0}
                                son_islem_zamanlari[symbol] = time.time()
                            telegram_bildir(f"🚀 <b>{symbol.upper()} LONG Açıldı!</b>\nFiyat: {anlik_fiyat}\n200 EMA: {round(guncel_ema, 4)}\nSupertrend: {round(guncel_st, 4)}\nSinyal: Hidden Bullish Divergence")
                        elif sinyal == "SELL":
                            order_client.futures_create_order(symbol=symbol.upper(), side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=qty)
                            with data_lock: 
                                aktif_pozisyonlar[symbol] = {"aktif": True, "yon": "SHORT", "adet": qty, "giris_fiyati": anlik_fiyat, "resmi_pnl": 0.0}
                                son_islem_zamanlari[symbol] = time.time()
                            telegram_bildir(f"🚀 <b>{symbol.upper()} SHORT Açıldı!</b>\nFiyat: {anlik_fiyat}\n200 EMA: {round(guncel_ema, 4)}\nSupertrend: {round(guncel_st, 4)}\nSinyal: Hidden Bearish Divergence")
                    except Exception as e:
                        print(f"❌ Emir hatası: {e}")
                    finally:
                        with data_lock: emir_beklemede_durumu[symbol] = False
                time.sleep(config.API_DELAY)
        except Exception as e:
            print(f"❌ Ana tarama hatası: {e}")
            time.sleep(2.0)

# --- 🚀 ANA ÇALIŞTIRICI SİSTEM ---
if __name__ == "__main__":
    print("🎬 Proxy Tasarruflu Bot Başlatılıyor...")
    try:
        # 🔐 PROXY'Lİ (Cüzdan Bilgisi Kontrolü)
        hesap_bilgisi = order_client.futures_account()
        mevcut_pozisyonlar = hesap_bilgisi.get("positions", [])
        for p in mevcut_pozisyonlar:
            amt = float(p.get("positionAmt", 0))
            sym = p.get("symbol", "").lower()
            if amt != 0:
                print(f"📦 Mevcut açık pozisyon eklendi: {sym.upper()}")
                kontrollu_coin_ekle(sym, eski_pozisyon_mu=True)
    except Exception as e:
        print(f"❌ İlk pozisyon tarama hatası: {e}")

    eklenen_sayac = 0
    for c in OZEL_COIN_LISTESI:
        if kontrollu_coin_ekle(c, eski_pozisyon_mu=False): 
            eklenen_sayac += 1
    print(f"✅ Belirlediğiniz {eklenen_sayac} adet özel parite tarama listesine eklendi.")

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=telegram_gelen_mesaj_dinleyici, daemon=True).start()
        telegram_bildir(f"🤖 <b>Proxy Optimized Bot Aktif!</b>\nFiyatlar public kaynaktan (Proxy'siz) çekiliyor, sadece emirler Proxy ile atılıyor.")

    threading.Thread(target=hizli_acik_pozisyon_takip_dongusu, daemon=True).start()
    pure_api_tarama_dongusu()
