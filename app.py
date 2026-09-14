import os
import json
import time
import requests
import threading
import websocket
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

# 🌐 REST İstemcileri (Sadece İlk Kurulum ve Emirler İçin)
client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
order_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

if proxy_formatted:
    print(f"🌐 Proxy tüneli emir işlemleri için bağlandı: {proxy_formatted.split('@')[-1] if '@' in proxy_formatted else proxy_formatted}")
    order_client.session.proxies = {"http": proxy_formatted, "https": proxy_formatted}

class TrendBotConfig:
    def __init__(self):
        self.TIMEFRAME = Client.KLINE_INTERVAL_15MINUTE  # 15m
        self.ISLEM_MARJIN = 3.0                          # Min Notional filter için 3 USDT yapıldı
        self.KALDIRAC = 20                              # 50x yerine 20x risk/komisyon dengesi
        
        self.MAX_ACIK_POZISYON = 7
        self.BOT_CALISIYOR = True
        self.COOLDOWN_SURESI = 0
        self.TOP_COIN_LIMITI = 50                        # En yüksek hacimli ilk 50 Coin

        # === AKÜLASYON & BOLLINGER STRATEJİ PARAMETRELERİ ===
        self.CHANNEL_LEN = 47                            # Akülasyon Bandı Çubuk Sayısı
        self.BREAKOUT_VOL_MULT = 1.6                     # Kırılım Hacim Çarpanı
        self.BB_LEN = 20                                 # Bollinger Uzunluğu
        self.BB_MULT = 2.0                               # Bollinger Çarpanı
        self.SQUEEZE_THRESH = 0.03                       # %3 Sıkışma Eşik Değeri

        # 🎯 RİSK VE BREAKEVEN YÖNETİMİ
        self.TAKE_PROFIT_USD = 1.0                       # Net +1.0$ PNL
        self.STOP_LOSS_PERCENT = 1.2                     # %1.2 Fiyat Değişimi Stop Loss
        self.BREAKEVEN_TRIGGER_USD = 0.45                # Breakeven Aktif Eşik (+0.45$ PNL)
        self.BREAKEVEN_PROFIT_USD = 0.25                 # Breakeven Stop Hedef (+0.25$ PNL)

        self.HIZLI_TAKIP_PERIYODU = 1.0

config = TrendBotConfig()

SYMBOLS = []
piyasa_verisi = {}
aktif_pozisyonlar = {}
FUTURES_HASSASIYETLERI = {}
son_islem_zamanlari = {}
emir_beklemede_durumu = {}
son_kapatilan_mum_zamanlari = {}
data_lock = threading.Lock()

# --- 📊 EN YÜKSEK HACİMLİ COINLER (REST API - SADECE BAŞLANGIÇTA) ---
def en_yuksek_hacimli_coinleri_getir(limit=50):
    try:
        url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200: return []
        
        tickers = response.json()
        usdt_tickers = [
            t for t in tickers 
            if t['symbol'].endswith('USDT') and not t['symbol'].startswith('USDC')
        ]
        
        sorted_tickers = sorted(usdt_tickers, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)
        top_symbols = [t['symbol'].lower() for t in sorted_tickers[:limit]]
        print(f"🔥 En Yüksek Hacimli İlk {len(top_symbols)} Coin Tespit Edildi.")
        return top_symbols
    except Exception as e:
        print(f"❌ Hacim çekilirken hata: {e}")
        return []

# --- 🛠️ STRATEJİ ANALİZİ ---
def strateji_analiz(v):
    candles = list(v["klines"])
    if not candles or len(candles) < config.CHANNEL_LEN + 10:
        return "HOLD", 0.0, 0.0, False

    df = pd.DataFrame(candles)
    df = df.iloc[:, :6]
    df.columns = ['open_time', 'open', 'high', 'low', 'close', 'volume']
    
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)

    # Fakeout Önleyici: Son KAPANMIŞ mum (iloc[-2]) üzerinden strateji doğrulanır
    vol_sma = df['volume'].rolling(window=20).mean()
    is_high_volume = df['volume'].iloc[-2] >= (vol_sma.iloc[-2] * config.BREAKOUT_VOL_MULT)

    range_high = df['high'].iloc[:-2].tail(config.CHANNEL_LEN).max()
    range_low  = df['low'].iloc[:-2].tail(config.CHANNEL_LEN).min()

    bb_middle = df['close'].rolling(window=config.BB_LEN).mean()
    bb_std = df['close'].rolling(window=config.BB_LEN).std(ddof=0)
    bb_upper = bb_middle + (bb_std * config.BB_MULT)
    bb_lower = bb_middle - (bb_std * config.BB_MULT)

    curr_middle = bb_middle.iloc[-2]
    curr_upper = bb_upper.iloc[-2]
    curr_lower = bb_lower.iloc[-2]

    bb_width = (curr_upper - curr_lower) / curr_middle if curr_middle != 0 else 0
    is_squeezed = bb_width <= config.SQUEEZE_THRESH

    prev_close = df['close'].iloc[-3]
    curr_close = df['close'].iloc[-2]

    long_condition = (prev_close <= range_high) and (curr_close > range_high) and is_high_volume
    short_condition = (prev_close >= range_low) and (curr_close < range_low) and is_high_volume

    giris_sinyali = "HOLD"
    if long_condition:
        giris_sinyali = "BUY"
    elif short_condition:
        giris_sinyali = "SELL"

    return giris_sinyali, range_high, range_low, is_squeezed

# --- 📡 WEBSOCKET DİNLEYİCİ MOTORU ---
def websocket_baslat():
    def on_message(ws, message):
        try:
            msg = json.loads(message)
            if "data" not in msg: return
            
            data = msg["data"]
            symbol = data["s"].lower()
            kline = data["k"]
            
            anlik_fiyat = float(kline["c"])
            is_closed = kline["x"]  # Mum kapandı mı? (True/False)
            
            with data_lock:
                if symbol in piyasa_verisi:
                    piyasa_verisi[symbol]["anlik_fiyat"] = anlik_fiyat
                    
                    # WebSocket'ten gelen son mum formatını REST formatına uyarla
                    formatted_kline = [
                        kline["t"], kline["o"], kline["h"], kline["l"], 
                        kline["c"], kline["v"], kline["T"], kline["q"], 
                        kline["n"], kline["V"], kline["Q"], "0"
                    ]
                    
                    klines = piyasa_verisi[symbol]["klines"]
                    if klines:
                        # Aktif mum güncellenir veya kapandıysa yeni mum eklenir
                        if klines[-1][0] == kline["t"]:
                            klines[-1] = formatted_kline
                        else:
                            klines.append(formatted_kline)
                            if len(klines) > 300: klines.pop(0)
                    else:
                        klines.append(formatted_kline)
                        
                    if is_closed:
                        son_kapatilan_mum_zamanlari[symbol] = kline["t"]

        except Exception as e:
            pass

    def on_error(ws, error):
        print(f"⚠️ WebSocket Hatası: {error}")

    def on_close(ws, close_status_code, close_msg):
        print("🔌 WebSocket Bağlantısı Kaptıldı. Yeniden bağlanılıyor...")
        time.sleep(5)
        websocket_baslat()

    def run():
        # Tüm symbol'lerin 15m kline stream'lerini tek kanalda birleştir
        streams = [f"{s}@kline_{config.TIMEFRAME}" for s in SYMBOLS]
        stream_url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"
        
        ws = websocket.WebSocketApp(
            stream_url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        ws.run_forever()

    w_thread = threading.Thread(target=run, daemon=True)
    w_thread.start()
    print(f"⚡ WebSocket Akışı Başlatıldı ({len(SYMBOLS)} Sembol Canlı Dinleniyor)")

def kontrollu_coin_ekle(coin_adi, eski_pozisyon_mu=False):
    coin_lower = coin_adi.lower().strip()
    coin_upper = coin_lower.upper()
    if coin_lower in SYMBOLS: return True
    try:
        f_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(f_url, timeout=10).json()
        market_info = next((m for m in r.get("symbols", []) if m["symbol"] == coin_upper), None)
        if not market_info or market_info.get('status') != 'TRADING': return False
        
        if not eski_pozisyon_mu:
            try:
                order_client.futures_change_leverage(symbol=coin_upper, leverage=config.KALDIRAC)
                order_client.futures_change_margin_type(symbol=coin_upper, marginType="ISOLATED")
            except BinanceAPIException: pass

        for f in market_info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step_size_str = str(f['stepSize']).rstrip('0')
                precision = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
                FUTURES_HASSASIYETLERI[coin_lower] = precision

        # İlk geçmiş mumları REST ile al, devamını WebSocket canlı dolduracak
        klines_url = f"https://fapi.binance.com/fapi/v1/klines?symbol={coin_upper}&interval={config.TIMEFRAME}&limit=250"
        k_res = requests.get(klines_url, timeout=10).json()

        with data_lock:
            if coin_lower not in SYMBOLS:
                SYMBOLS.append(coin_lower)
            piyasa_verisi[coin_lower] = {
                "anlik_fiyat": float(k_res[-1][4]) if k_res else 0.0, 
                "klines": k_res
            }
            aktif_pozisyonlar[coin_lower] = {
                "aktif": False, "yon": None, "adet": 0.0, 
                "giris_fiyati": 0.0, "resmi_pnl": 0.0,
                "be_aktif": False, "be_stop_fiyati": 0.0
            }
            son_islem_zamanlari[coin_lower] = 0.0
            emir_beklemede_durumu[coin_lower] = False
            son_kapatilan_mum_zamanlari[coin_lower] = 0
        return True
    except Exception:
        return False

# 🔄 HESAP POZİSYON SENKRONİZASYONU
def acik_pozisyonlari_binanceden_guncelle():
    try:
        hesap_bilgisi = order_client.futures_account()
        pozisyonlar = hesap_bilgisi.get("positions", [])
        
        gelen_acik_semboller = set()
        for p in pozisyonlar:
            amt = float(p.get("positionAmt", 0))
            if amt != 0:
                gelen_acik_semboller.add(p.get("symbol", "").lower())

        with data_lock:
            for s in SYMBOLS:
                if not emir_beklemede_durumu.get(s, False):
                    if s not in gelen_acik_semboller:
                        aktif_pozisyonlar[s] = {
                            "aktif": False, "yon": None, "adet": 0.0, 
                            "giris_fiyati": 0.0, "resmi_pnl": 0.0,
                            "be_aktif": False, "be_stop_fiyati": 0.0
                        }

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

# --- 🎛️ TELEGRAM BİLDİRİM VE MESAJ DİNLEYİCİ ---
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
        durum_str = "🟢 WebSocket Tarama Aktif" if config.BOT_CALISIYOR else "🔴 Sistem Durduruldu"
        rapor = (
            f"⚙️ <b>Akülasyon Kırılımı & Squeeze Botu (WS v2)</b>\n"
            f"• Sistem: {durum_str}\n"
            f"• Takip Edilen Çiftler: Top {len(SYMBOLS)} Coin\n"
            f"• Risk Limiti: {acik_pozlar}/{config.MAX_ACIK_POZISYON} Poz.\n\n"
            f"⚡ <b>Açık İşlemler:</b>\n"
        )
        if acik_pozlar == 0:
            rapor += "Açık pozisyon bulunmuyor."
        else:
            for s in SYMBOLS:
                if aktif_pozisyonlar[s]["aktif"]:
                    p = aktif_pozisyonlar[s]
                    be_str = " (🛡️ BE)" if p.get("be_aktif") else ""
                    rapor += f"• {s.upper()} | {p['yon']} | PNL: <b>{round(p['resmi_pnl'], 3)}$</b>{be_str}\n"
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
# 🚀 ANLIK BREAKEVEN, TP VE SL DÖNGÜSÜ (WEBSOCKET VERİLERİYLE)
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

            for symbol in acik_semboller:
                with data_lock:
                    v = dict(piyasa_verisi[symbol])
                    pos = dict(aktif_pozisyonlar[symbol])
                    emir_beklemede = emir_beklemede_durumu.get(symbol, False)
                
                if emir_beklemede or pos["adet"] <= 0 or not pos["aktif"]: continue

                anlik_fiyat = v.get("anlik_fiyat", 0.0)
                if anlik_fiyat <= 0: continue

                kapatma_nedeni = None
                giris_fiyati = pos["giris_fiyati"]
                resmi_pnl = pos.get("resmi_pnl", 0.0)

                # 🛡️ BREAKEVEN TETİKLEME KONTROLÜ
                if resmi_pnl >= config.BREAKEVEN_TRIGGER_USD and not pos.get("be_aktif", False):
                    hedef_fiyat_farki = (config.BREAKEVEN_PROFIT_USD / pos["adet"])
                    be_stop_price = giris_fiyati + hedef_fiyat_farki if pos["yon"] == "LONG" else giris_fiyati - hedef_fiyat_farki

                    with data_lock:
                        aktif_pozisyonlar[symbol]["be_aktif"] = True
                        aktif_pozisyonlar[symbol]["be_stop_fiyati"] = be_stop_price

                    telegram_bildir(
                        f"🛡️ <b>{symbol.upper()} Breakeven Aktif!</b>\n"
                        f"• Anlık PNL: +{round(resmi_pnl, 3)}$\n"
                        f"• Stop Seviyesi: +0.25$ Kâr ({round(be_stop_price, 4)}) çekildi."
                    )

                # 🎯 1. KÂR AL (TP) KONTROLÜ
                if resmi_pnl >= config.TAKE_PROFIT_USD:
                    kapatma_nedeni = f"Net Kâr +{config.TAKE_PROFIT_USD}$ PNL Hedefi ({round(resmi_pnl, 2)}$)"

                # 🛡️ 2. BREAKEVEN STOP KONTROLÜ
                elif pos.get("be_aktif", False):
                    be_stop_price = pos.get("be_stop_fiyati", 0.0)
                    if pos["yon"] == "LONG" and anlik_fiyat <= be_stop_price:
                        kapatma_nedeni = f"Breakeven Stop Tetiklendi"
                    elif pos["yon"] == "SHORT" and anlik_fiyat >= be_stop_price:
                        kapatma_nedeni = f"Breakeven Stop Tetiklendi"

                # 🎯 3. YÜZDESEL ZARAR DURDUR (SL) KONTROLÜ
                if not kapatma_nedeni and not pos.get("be_aktif", False):
                    if pos["yon"] == "LONG":
                        fiyat_degisim = ((anlik_fiyat - giris_fiyati) / giris_fiyati) * 100
                        if fiyat_degisim <= -config.STOP_LOSS_PERCENT: kapatma_nedeni = f"%{config.STOP_LOSS_PERCENT} SL Tetiklendi"
                    elif pos["yon"] == "SHORT":
                        fiyat_degisim = ((giris_fiyati - anlik_fiyat) / giris_fiyati) * 100
                        if fiyat_degisim <= -config.STOP_LOSS_PERCENT: kapatma_nedeni = f"%{config.STOP_LOSS_PERCENT} SL Tetiklendi"

                if kapatma_nedeni:
                    with data_lock: emir_beklemede_durumu[symbol] = True
                    try:
                        precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                        qty_to_close = round(pos["adet"], precision) if precision > 0 else int(pos["adet"])
                        side_to_close = SIDE_SELL if pos["yon"] == "LONG" else SIDE_BUY
                        
                        if qty_to_close > 0:
                            order_client.futures_create_order(
                                symbol=symbol.upper(), side=side_to_close,
                                type=ORDER_TYPE_MARKET, quantity=qty_to_close, reduceOnly=True
                            )
                        with data_lock:
                            aktif_pozisyonlar[symbol]["aktif"] = False
                        
                        telegram_bildir(
                            f"🔴 <b>{symbol.upper()} {pos['yon']} Kapatıldı!</b>\n"
                            f"• Neden: <b>{kapatma_nedeni}</b>\n"
                            f"• Son PNL: {round(pos['resmi_pnl'], 3)}$"
                        )
                    except Exception as e:
                        print(f"❌ Kapatma hatası ({symbol}): {e}")
                    finally:
                        with data_lock: emir_beklemede_durumu[symbol] = False

            time.sleep(config.HIZLI_TAKIP_PERIYODU)
        except Exception as e:
            time.sleep(2.0)

# --- 🎯 GİRİŞ SİNYALİ TARAMA MOTORU ---
def websocket_tarama_dongusu():
    while True:
        try:
            if not config.BOT_CALISIYOR:
                time.sleep(1.0)
                continue
            
            with data_lock:
                kapali_olanlar = [s for s in SYMBOLS if not aktif_pozisyonlar[s]["aktif"]]
            
            for symbol in kapali_olanlar:
                with data_lock:
                    v = dict(piyasa_verisi[symbol])
                    pos = dict(aktif_pozisyonlar[symbol])
                    acik_poz_sayisi = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])

                if acik_poz_sayisi >= config.MAX_ACIK_POZISYON or pos["aktif"]: continue

                sinyal, range_high, range_low, is_squeezed = strateji_analiz(v)
                anlik_fiyat = v.get("anlik_fiyat", 0.0)

                if sinyal != "HOLD" and anlik_fiyat > 0:
                    with data_lock:
                        if emir_beklemede_durumu[symbol]: continue
                        emir_beklemede_durumu[symbol] = True
                    try:
                        precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                        qty = (config.ISLEM_MARJIN * config.KALDIRAC) / anlik_fiyat
                        qty = round(qty, precision) if precision > 0 else int(qty)
                        
                        if qty > 0:
                            side = SIDE_BUY if sinyal == "BUY" else SIDE_SELL
                            order_client.futures_create_order(symbol=symbol.upper(), side=side, type=ORDER_TYPE_MARKET, quantity=qty)
                            
                            with data_lock:
                                aktif_pozisyonlar[symbol] = {
                                    "aktif": True, "yon": "LONG" if sinyal == "BUY" else "SHORT",
                                    "adet": qty, "giris_fiyati": anlik_fiyat, "resmi_pnl": 0.0,
                                    "be_aktif": False, "be_stop_fiyati": 0.0
                                }
                            telegram_bildir(f"🚀 <b>{symbol.upper()} {sinyal} Pozisyonu Açıldı!</b>\nFiyat: {anlik_fiyat}")
                    except Exception as e:
                        print(f"❌ Giriş emri hatası ({symbol}): {e}")
                    finally:
                        with data_lock: emir_beklemede_durumu[symbol] = False

            time.sleep(2.0)
        except Exception as e:
            time.sleep(2.0)

# --- 🚀 ANA ÇALIŞTIRICI ---
if __name__ == "__main__":
    print("🎬 WebSocket Destekli Akülasyon & Squeeze Botu Başlatılıyor...")
    
    # 1. Hacimli coinleri al ve REST yapılandırmalarını tamamla
    top_50_coinler = en_yuksek_hacimli_coinleri_getir(limit=config.TOP_COIN_LIMITI)
    for c in top_50_coinler:
        kontrollu_coin_ekle(c)

    # 2. Canlı WebSocket Akışını Başlat (Artık REST ile fiyat çekilmeyecek)
    websocket_baslat()

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=telegram_gelen_mesaj_dinleyici, daemon=True).start()

    threading.Thread(target=hizli_acik_pozisyon_takip_dongusu, daemon=True).start()
    websocket_tarama_dongusu()
