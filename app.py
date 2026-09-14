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
        self.TIMEFRAME = Client.KLINE_INTERVAL_15MINUTE  # 15m
        self.ISLEM_MARJIN = 2.0                          # 2 USDT
        self.KALDIRAC = 50                              # 50x
        
        self.MAX_ACIK_POZISYON = 7
        self.BOT_CALISIYOR = True
        self.COOLDOWN_SURESI = 0
        self.TOP_COIN_LIMITI = 50                        # 📊 En yüksek hacimli ilk 50 Coin

        # === AKÜLASYON & BOLLINGER STRATEJİ PARAMETRELERİ ===
        self.CHANNEL_LEN = 47                            # Akülasyon Bandı Çubuk Sayısı
        self.BREAKOUT_VOL_MULT = 1.6                     # Kırılım Hacim Çarpanı (1.6x)
        self.BB_LEN = 20                                 # Bollinger Uzunluğu
        self.BB_MULT = 2.0                               # Bollinger Çarpanı
        self.SQUEEZE_THRESH = 0.03                       # %3 Sıkışma Eşik Değeri

        # 🎯 RİSK VE BREAKEVEN YÖNETİMİ
        self.TAKE_PROFIT_USD = 1.0                       # 💵 Kâr Al Hedefi: Net +1.0$ PNL
        self.STOP_LOSS_PERCENT = 2.0                     # %2 Zarar Durdur Hedefi (Fiyat Değişimi)
        self.BREAKEVEN_TRIGGER_USD = 0.45                # 🛡️ Breakeven Aktif Olma Eşiği (+0.45$ PNL)
        self.BREAKEVEN_PROFIT_USD = 0.25                 # 🛡️ Breakeven Stop Kâr Hedefi (+0.25$ PNL)

        self.API_DELAY = 0.5
        self.HIZLI_TAKIP_PERIYODU = 2.0

config = TrendBotConfig()

SYMBOLS = []
piyasa_verisi = {}
aktif_pozisyonlar = {}
FUTURES_HASSASIYETLERI = {}
son_islem_zamanlari = {}
emir_beklemede_durumu = {}
son_kapatilan_mum_zamanlari = {}
data_lock = threading.Lock()

# --- 📊 EN YÜKSEK HACİMLİ 50 COIN GETİR (REST API) ---
def en_yuksek_hacimli_coinleri_getir(limit=50):
    try:
        url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            print("❌ Hacim verisi alınamadı!")
            return []
        
        tickers = response.json()
        
        # Sadece aktif USDT paritelerini filtrele ve 24h quoteVolume (USDT Hacmi) değerine göre sırala
        usdt_tickers = [
            t for t in tickers 
            if t['symbol'].endswith('USDT') and not t['symbol'].startswith('USDC')
        ]
        
        sorted_tickers = sorted(usdt_tickers, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)
        top_symbols = [t['symbol'].lower() for t in sorted_tickers[:limit]]
        
        print(f"🔥 Binance Futures En Yüksek Hacimli İlk {len(top_symbols)} Coin Tespit Edildi.")
        return top_symbols
    except Exception as e:
        print(f"❌ Hacim sıralaması çekilirken hata: {e}")
        return []

# --- 🛠️ STRATEJİ ANALİZİ ---
def strateji_analiz(v, anlik_fiyat):
    candles = list(v["klines"])
    if not candles or len(candles) < config.CHANNEL_LEN + 10:
        return "HOLD", 0.0, 0.0, False

    df = pd.DataFrame(candles)
    df = df.iloc[:, :6]
    df.columns = ['open_time', 'open', 'high', 'low', 'close', 'volume']

    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['open'] = df['open'].astype(float)
    df['volume'] = df['volume'].astype(float)
    
    df.iloc[-1, df.columns.get_loc('close')] = anlik_fiyat

    vol_sma = df['volume'].rolling(window=20).mean()
    is_high_volume = df['volume'].iloc[-1] >= (vol_sma.iloc[-1] * config.BREAKOUT_VOL_MULT)

    range_high = df['high'].iloc[:-1].tail(config.CHANNEL_LEN).max()
    range_low  = df['low'].iloc[:-1].tail(config.CHANNEL_LEN).min()

    bb_middle = df['close'].rolling(window=config.BB_LEN).mean()
    bb_std = df['close'].rolling(window=config.BB_LEN).std(ddof=0)
    bb_upper = bb_middle + (bb_std * config.BB_MULT)
    bb_lower = bb_middle - (bb_std * config.BB_MULT)

    curr_middle = bb_middle.iloc[-1]
    curr_upper = bb_upper.iloc[-1]
    curr_lower = bb_lower.iloc[-1]

    bb_width = (curr_upper - curr_lower) / curr_middle if curr_middle != 0 else 0
    is_squeezed = bb_width <= config.SQUEEZE_THRESH

    prev_close = df['close'].iloc[-2]
    curr_close = df['close'].iloc[-1]

    long_condition = (prev_close <= range_high) and (curr_close > range_high) and is_high_volume
    short_condition = (prev_close >= range_low) and (curr_close < range_low) and is_high_volume

    giris_sinyali = "HOLD"
    if long_condition:
        giris_sinyali = "BUY"
    elif short_condition:
        giris_sinyali = "SELL"

    return giris_sinyali, range_high, range_low, is_squeezed

# --- 🌐 REST API ALTYAPI FONKSİYONLARI ---
def kontrollu_coin_ekle(coin_adi, eski_pozisyon_mu=False):
    coin_lower = coin_adi.lower().strip()
    coin_upper = coin_lower.upper()
    if coin_lower in SYMBOLS: return True
    try:
        f_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(f_url, headers=headers, timeout=10)
        if response.status_code != 200: return False
        r = response.json()
        market_info = next((m for m in r.get("symbols", []) if m["symbol"] == coin_upper), None)
        if not market_info or market_info.get('status') != 'TRADING': return False
        time.sleep(0.15)
        
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
            aktif_pozisyonlar[coin_lower] = {
                "aktif": False, 
                "yon": None, 
                "adet": 0.0, 
                "giris_fiyati": 0.0, 
                "resmi_pnl": 0.0,
                "be_aktif": False,
                "be_stop_fiyati": 0.0
            }
            son_islem_zamanlari[coin_lower] = 0.0
            emir_beklemede_durumu[coin_lower] = False
            son_kapatilan_mum_zamanlari[coin_lower] = 0
        return True
    except Exception:
        return False

def tek_coin_api_verisi_guncelle(s):
    try:
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

# 🔄 BİNANCE HESAP VE CÜZDAN SENKRONİZASYONU (KAPANAN POZLARI TEMİZLER)
def acik_pozisyonlari_binanceden_guncelle():
    try:
        hesap_bilgisi = order_client.futures_account()
        pozisyonlar = hesap_bilgisi.get("positions", [])
        
        gelen_acik_semboller = set()
        for p in pozisyonlar:
            amt = float(p.get("positionAmt", 0))
            if amt != 0:
                sym = p.get("symbol", "").lower()
                gelen_acik_semboller.add(sym)

        with data_lock:
            # 1. Binance'te pozisyonu artık 0 görünen coinlerin durumunu pasife çek
            for s in SYMBOLS:
                if not emir_beklemede_durumu.get(s, False):
                    if s not in gelen_acik_semboller:
                        aktif_pozisyonlar[s] = {
                            "aktif": False, 
                            "yon": None, 
                            "adet": 0.0, 
                            "giris_fiyati": 0.0, 
                            "resmi_pnl": 0.0,
                            "be_aktif": False,
                            "be_stop_fiyati": 0.0
                        }

            # 2. Binance'te miktarı 0 olmayan aktif pozisyonları güncelle
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

# --- 🎛️ TELEGRAM YÖNETİMİ ---
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
        durum_str = "🟢 Top 50 Hacim Listesi Taranıyor" if config.BOT_CALISIYOR else "🔴 Sistem Durduruldu"
        rapor = (
            f"⚙️ <b>Akülasyon Kırılımı & Squeeze Botu</b>\n"
            f"• Sistem: {durum_str}\n"
            f"• Takip Edilen Çiftler: Top {len(SYMBOLS)} Hacimli Coin\n"
            f"• Periyot: 15m\n"
            f"• Hedef (TP): <b>+{config.TAKE_PROFIT_USD}$ Kâr Al</b>\n"
            f"• Breakeven: <b>+{config.BREAKEVEN_TRIGGER_USD}$ 'da Tetiklenir -> +{config.BREAKEVEN_PROFIT_USD}$ Stop</b>\n"
            f"• Stop Loss (SL): <b>%{config.STOP_LOSS_PERCENT} Zarar Durdur</b>\n"
            f"• Risk Limiti: {acik_pozlar}/{config.MAX_ACIK_POZISYON} Poz.\n\n"
            f"⚡ <b>Açık İşlemler:</b>\n"
        )
        if acik_pozlar == 0:
            rapor += "Açık izole pozisyon bulunmuyor."
        else:
            for s in SYMBOLS:
                if aktif_pozisyonlar[s]["aktif"]:
                    p = aktif_pozisyonlar[s]
                    be_str = " (🛡️ BE Aktif)" if p.get("be_aktif") else ""
                    rapor += f"• {s.upper()} | {p['yon']} | PNL: <b>{round(p['resmi_pnl'], 4)}$</b>{be_str}\n"
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
# 🚀 AÇIK POZİSYON KONTROL, BREAKEVEN (45 cent -> 25 cent), TP VE SL DÖNGÜSÜ
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

                kapatma_nedeni = None
                giris_fiyati = pos["giris_fiyati"]
                resmi_pnl = pos.get("resmi_pnl", 0.0)

                # 🛡️ BREAKEVEN TETİKLEME KONTROLÜ (+0.45$ PNL görünce aktif et)
                if resmi_pnl >= config.BREAKEVEN_TRIGGER_USD and not pos.get("be_aktif", False):
                    hedef_fiyat_farki = (config.BREAKEVEN_PROFIT_USD / pos["adet"])
                    
                    if pos["yon"] == "LONG":
                        be_stop_price = giris_fiyati + hedef_fiyat_farki
                    else: # SHORT
                        be_stop_price = giris_fiyati - hedef_fiyat_farki

                    with data_lock:
                        aktif_pozisyonlar[symbol]["be_aktif"] = True
                        aktif_pozisyonlar[symbol]["be_stop_fiyati"] = be_stop_price

                    pos["be_aktif"] = True
                    pos["be_stop_fiyati"] = be_stop_price

                    print(f"🛡️ {symbol.upper()} için Breakeven Aktif! (+0.45$ PNL görüldü. Stop: +0.25$ PNL / Fiyat: {be_stop_price})")
                    telegram_bildir(
                        f"🛡️ <b>{symbol.upper()} Breakeven Aktif!</b>\n"
                        f"• Anlık PNL: +{round(resmi_pnl, 3)}$\n"
                        f"• Stop Seviyesi: +0.25$ Kâr ({round(be_stop_price, 4)}) çekildi."
                    )

                # 🎯 1. DOLAR BAZLI KÂR AL (TP) KONTROLÜ (Anlık PNL >= 1.0$)
                if resmi_pnl >= config.TAKE_PROFIT_USD:
                    kapatma_nedeni = f"Net Kâr +{config.TAKE_PROFIT_USD}$ PNL Hedefine Ulaşıldı ({round(resmi_pnl, 2)}$)"

                # 🛡️ 2. BREAKEVEN STOP KONTROLÜ (Aktifse ve fiyat +0.25$ kâr seviyesine gerilerse)
                elif pos.get("be_aktif", False):
                    be_stop_price = pos.get("be_stop_fiyati", 0.0)
                    if pos["yon"] == "LONG" and anlik_fiyat <= be_stop_price:
                        kapatma_nedeni = f"Breakeven (Maliyet +0.25$ Kâr Stopu) Tetiklendi"
                    elif pos["yon"] == "SHORT" and anlik_fiyat >= be_stop_price:
                        kapatma_nedeni = f"Breakeven (Maliyet +0.25$ Kâr Stopu) Tetiklendi"

                # 🎯 3. YÜZDESEL ZARAR DURDUR (SL) KONTROLÜ (Breakeven henüz aktif değilse çalışır)
                if not kapatma_nedeni and not pos.get("be_aktif", False):
                    if pos["yon"] == "LONG":
                        fiyat_degisim_yuzdesi = ((anlik_fiyat - giris_fiyati) / giris_fiyati) * 100
                        if fiyat_degisim_yuzdesi <= -config.STOP_LOSS_PERCENT:
                            kapatma_nedeni = f"%{config.STOP_LOSS_PERCENT} Zarar Durdur (SL) Tetiklendi"

                    elif pos["yon"] == "SHORT":
                        fiyat_degisim_yuzdesi = ((giris_fiyati - anlik_fiyat) / giris_fiyati) * 100
                        if fiyat_degisim_yuzdesi <= -config.STOP_LOSS_PERCENT:
                            kapatma_nedeni = f"%{config.STOP_LOSS_PERCENT} Zarar Durdur (SL) Tetiklendi"

                if kapatma_nedeni:
                    with data_lock:
                        if emir_beklemede_durumu[symbol]: continue
                        emir_beklemede_durumu[symbol] = True
                    try:
                        precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                        qty_to_close = round(pos["adet"], precision) if precision > 0 else int(pos["adet"])
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
                            
                            aktif_pozisyonlar[symbol] = {
                                "aktif": False, 
                                "yon": None, 
                                "adet": 0.0, 
                                "giris_fiyati": 0.0, 
                                "resmi_pnl": 0.0,
                                "be_aktif": False,
                                "be_stop_fiyati": 0.0
                            }
                        
                        telegram_bildir(
                            f"🔴 <b>{symbol.upper()} {pos['yon']} Kapatıldı!</b>\n"
                            f"• Neden: <b>{kapatma_nedeni}</b>\n"
                            f"• Son PNL: {round(pos['resmi_pnl'], 3)}$\n"
                            f"• Kapanış Fiyatı: {anlik_fiyat}"
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
                
                if not tek_coin_api_verisi_guncelle(symbol):
                    time.sleep(config.API_DELAY)
                    continue
                with data_lock:
                    v = dict(piyasa_verisi[symbol])
                    pos = dict(aktif_pozisyonlar[symbol])
                    son_islem = son_islem_zamanlari[symbol]
                if len(v["klines"]) < config.CHANNEL_LEN + 10 or not v["anlik_fiyat"] or v["anlik_fiyat"] <= 0:
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
                
                sinyal, range_high, range_low, is_squeezed = strateji_analiz(v, anlik_fiyat)
                
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
                        qty = round(qty, precision) if precision > 0 else int(qty)
                        if qty <= 0:
                            with data_lock: emir_beklemede_durumu[symbol] = False
                            continue

                        squeeze_info = " (Bollinger Sıkışması Var ⚡)" if is_squeezed else ""

                        if sinyal == "BUY":
                            order_client.futures_create_order(symbol=symbol.upper(), side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
                            with data_lock: 
                                aktif_pozisyonlar[symbol] = {
                                    "aktif": True, 
                                    "yon": "LONG", 
                                    "adet": qty, 
                                    "giris_fiyati": anlik_fiyat, 
                                    "resmi_pnl": 0.0,
                                    "be_aktif": False,
                                    "be_stop_fiyati": 0.0
                                }
                                son_islem_zamanlari[symbol] = time.time()
                            telegram_bildir(
                                f"🚀 <b>{symbol.upper()} LONG Açıldı!</b>\n"
                                f"Fiyat: {anlik_fiyat}\n"
                                f"Kanal Tavanı: {range_high}\n"
                                f"Hedef: +{config.TAKE_PROFIT_USD}$ TP | Stop: %{config.STOP_LOSS_PERCENT} SL\n"
                                f"Sinyal: Akülasyon Direnci Kırılımı (1.6x Hacim){squeeze_info}"
                            )
                        elif sinyal == "SELL":
                            order_client.futures_create_order(symbol=symbol.upper(), side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=qty)
                            with data_lock: 
                                aktif_pozisyonlar[symbol] = {
                                    "aktif": True, 
                                    "yon": "SHORT", 
                                    "adet": qty, 
                                    "giris_fiyati": anlik_fiyat, 
                                    "resmi_pnl": 0.0,
                                    "be_aktif": False,
                                    "be_stop_fiyati": 0.0
                                }
                                son_islem_zamanlari[symbol] = time.time()
                            telegram_bildir(
                                f"🚀 <b>{symbol.upper()} SHORT Açıldı!</b>\n"
                                f"Fiyat: {anlik_fiyat}\n"
                                f"Kanal Tabanı: {range_low}\n"
                                f"Hedef: +{config.TAKE_PROFIT_USD}$ TP | Stop: %{config.STOP_LOSS_PERCENT} SL\n"
                                f"Sinyal: Akülasyon Desteği Kırılımı (1.6x Hacim){squeeze_info}"
                            )
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
    print("🎬 Akülasyon Kırılımı & Squeeze Botu Başlatılıyor...")
    try:
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

    # 📊 24 saatlik hacme göre ilk 50 coin çekiliyor
    top_50_coinler = en_yuksek_hacimli_coinleri_getir(limit=config.TOP_COIN_LIMITI)
    
    eklenen_sayac = 0
    for c in top_50_coinler:
        if kontrollu_coin_ekle(c, eski_pozisyon_mu=False): 
            eklenen_sayac += 1
    print(f"✅ En yüksek hacimli {eklenen_sayac} adet parite tarama listesine eklendi.")

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=telegram_gelen_mesaj_dinleyici, daemon=True).start()
        telegram_bildir(
            f"🤖 <b>Akülasyon Kırılımı & Squeeze Botu Aktif!</b>\n"
            f"• Tarama: Top {eklenen_sayac} Hacimli Coin\n"
            f"• Parametreler: 47 Çubuk Kanal | 1.6x Hacim\n"
            f"• Risk Mantığı: +1.0$ TP | Breakeven (45¢ -> 25¢) | %2 SL"
        )

    threading.Thread(target=hizli_acik_pozisyon_takip_dongusu, daemon=True).start()
    pure_api_tarama_dongusu()
