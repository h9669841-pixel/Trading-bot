import os
import json
import time
import requests
import threading
import math
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
    proxy_formatted = PROXY_URL
    if PROXY_URL.startswith("socks5://"):
        proxy_formatted = PROXY_URL.replace("socks5://", "socks5h://")

client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
order_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
if proxy_formatted:
    print(f"🌐 Emir ve Hesap istemcisi için statik IP tüneli hazırlandı: {proxy_formatted.split('@')[-1] if '@' in proxy_formatted else proxy_formatted}")
    order_client.session.proxies = {"http": proxy_formatted, "https": proxy_formatted}
else:
    print("⚠️ PROXY_URL bulunamadı! Tüm işlemler yerel ağ üzerinden yapılacak.")
    order_client = client

class TrendBotConfig:
    def __init__(self):
        self.TIMEFRAME = Client.KLINE_INTERVAL_15MINUTE  
        self.ISLEM_MARJIN = 5.0        # 🔴 Başlangıç İzole Marjini
        self.KALDIRAC = 10             
        self.MAX_ACIK_POZISYON = 10     
        self.BOT_CALISIYOR = True
        self.COOLDOWN_SURESI = 0     
        self.SABIT_DOLAR_TP = 0.15     
        
        # === 🛡️ ÇİFT KADEMELİ KORUMA AYARLARI ===
        self.DCA1_TETIK_YUZDE = 3.0    # %3 terte kalınca ek alım yap (Pozisyon büyüklüğü artar)
        self.DCA1_MARJIN = 5.0         
        
        self.DCA2_TETIK_YUZDE = 3.5    # %3.5 terte kalınca cüzdandan izole teminata nakit ekle (Pozisyon büyümez, liq uzaklaşır)
        self.DCA2_EK_MARJIN = 2.0      
        
        # 🚨 VOLATİLİTE FİLTRESİ (Süper Pump/Dump Önleyici)
        # Mum boyu %4'ü aştıysa, RSI kesişimi gelse bile "düşen bıçağı tutmamak" için işleme girmez.
        self.VOLATILITE_SINIRI = 4.0

        # === Bollinger & RSI Parametreleri ===
        self.BB_LEN = 20
        self.BB_MULT = 2.0
        self.RSI_LEN = 14
        self.RSI_OB = 80               # Aşırı Alım Sınırı (Giriş için yukarı kırıp aşağı inmeli)
        self.RSI_OS = 20               # Aşırı Satım Sınırı (Giriş için aşağı kırıp yukarı çıkmalı)
        
        self.API_DELAY = 0.5
        self.HIZLI_TAKIP_PERIYODU = 1.0 

config = TrendBotConfig()

SYMBOLS = [] 
piyasa_verisi = {}
aktif_pozisyonlar = {}
FUTURES_HASSASIYETLERI = {}
son_islem_zamanlari = {}        
emir_beklemede_durumu = {} 

data_lock = threading.Lock()

# --- 🛠️ MATEMATİKSEL İNDİKATÖR MOTORU ---

def sma(seri, periyod):
    if len(seri) < periyod: return [0.0] * len(seri)
    res = []
    current_sum = sum(seri[:periyod])
    res.append(current_sum / periyod)
    for i in range(periyod, len(seri)):
        current_sum += seri[i] - seri[i - periyod]
        res.append(current_sum / periyod)
    return [0.0] * (periyod - 1) + res

def stdev(seri, periyod):
    if len(seri) < periyod: return [0.0] * len(seri)
    res = [0.0] * (periyod - 1)
    for i in range(periyod, len(seri) + 1):
        pencere = seri[i - periyod:i]
        ort = sum(pencere) / periyod
        varyans = sum((x - ort) ** 2 for x in pencere)
        res.append(math.sqrt(varyans / periyod))
    return res

def rsi_hesapla(kapanislar, periyod=14):
    if len(kapanislar) < periyod + 1: return 50.0
    kazanclar, kayiplar = [], []
    for i in range(1, len(kapanislar)):
        fark = kapanislar[i] - kapanislar[i-1]
        if fark > 0: kazanclar.append(fark); kayiplar.append(0)
        else: kazanclar.append(0); kayiplar.append(abs(fark))
    ort_kazanc = sum(kazanclar[:periyod]) / periyod
    ort_kayip = sum(kayiplar[:periyod]) / periyod
    for i in range(periyod, len(kazanclar)):
        ort_kazanc = (ort_kazanc * (periyod - 1) + kazanclar[i]) / periyod
        ort_kayip = (ort_kayip * (periyod - 1) + kayiplar[i]) / periyod
    if ort_kayip <= 0.00000001: return 100.0  
    return 100.0 - (100.0 / (1.0 + (ort_kazanc / ort_kayip)))

# 🎛️ YENİLENEN STRATEJİ MOTORU (RSI KESİŞİM MANTIĞI)
def strateji_sinyal_uret(v, anlik_fiyat, en_yuksek_fiyat, en_dusuk_fiyat):
    kapanislar = list(v["kapanislar"])
    if not kapanislar or anlik_fiyat <= 0: return "HOLD", 50.0
    
    # Volatilite Filtresi Kontrolü
    mum_boyu_yuzde = ((en_yuksek_fiyat - en_dusuk_fiyat) / en_dusuk_fiyat) * 100
    if mum_boyu_yuzde >= config.VOLATILITE_SINIRI:
        return "HOLD", 50.0

    kapanislar.append(anlik_fiyat)
    L = len(kapanislar)
    gerekli_uzunluk = max(config.BB_LEN, config.RSI_LEN) + 5
    if L < gerekli_uzunluk: return "HOLD", 50.0

    basis = sma(kapanislar, config.BB_LEN)
    dev = stdev(kapanislar, config.BB_LEN)
    lower_bb = basis[-1] - (config.BB_MULT * dev[-1])
    upper_bb = basis[-1] + (config.BB_MULT * dev[-1])

    # 1. Güncel anlık RSI
    rsi_guncel = rsi_hesapla(kapanislar, config.RSI_LEN)
    
    # 2. Bir önceki mum kapanışındaki RSI (Son anlık fiyatı listeden çıkartarak hesaplıyoruz)
    rsi_onceki = rsi_hesapla(kapanislar[:-1], config.RSI_LEN)

    # LONG ŞARTI: Önceki RSI 20'nin (OS) altındaydı VE şu an 20'nin üzerine çıktı (Yukarı Kesti)
    long_ok = (rsi_onceki < config.RSI_OS) and (rsi_guncel >= config.RSI_OS) and (anlik_fiyat < lower_bb * 1.01)

    # SHORT ŞARTI: Önceki RSI 80'in (OB) üzerindeydi VE şu an 80'in altına indi (Aşağı Kesti)
    short_ok = (rsi_onceki > config.RSI_OB) and (rsi_guncel <= config.RSI_OB) and (anlik_fiyat > upper_bb * 0.99)

    if long_ok: return "BUY", rsi_guncel
    elif short_ok: return "SELL", rsi_guncel

    return "HOLD", rsi_guncel

# --- 🌐 REST API ALTYAPI FONKSİYONLARI ---

def ilk_100_hacimli_coin_bul():
    try:
        ticker_url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(ticker_url, headers=headers, timeout=15)
        if response.status_code != 200: return []
        data = response.json()
        usdt_pairs = [x for x in data if isinstance(x, dict) and x.get("symbol", "").endswith("USDT")]
        sorted_by_volume = sorted(usdt_pairs, key=lambda k: float(k.get("quoteVolume", 0)), reverse=True)
        return [x["symbol"].lower() for x in sorted_by_volume[:100]]
    except Exception as e:
        print(f"❌ Hacim listesi alınamadı: {e}")
        return []

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
        
        time.sleep(0.20) 
        
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
                piyasa_verisi[coin_lower] = {"anlik_fiyat": 0.0, "kapanislar": [], "en_yuksek": 0.0, "en_dusuk": 0.0}
                aktif_pozisyonlar[coin_lower] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "dca_kademe": 0}
                son_islem_zamanlari[coin_lower] = 0.0  
                emir_beklemede_durumu[coin_lower] = False
        return True
    except Exception: return False

def tek_coin_api_verisi_guncelle(s):
    try:
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={s.upper()}&interval={config.TIMEFRAME}&limit=60"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code != 200: return False
        k = response.json()
        if not k or len(k) == 0: return False
        
        kapanislar_yeni = [float(x[4]) for x in k]
        anlik_fiyat_yeni = kapanislar_yeni[-1]  
        
        en_yuksek = float(k[-1][2])
        en_dusuk = float(k[-1][3])
        
        with data_lock:
            piyasa_verisi[s]["kapanislar"] = kapanislar_yeni[:-1] 
            piyasa_verisi[s]["anlik_fiyat"] = anlik_fiyat_yeni
            piyasa_verisi[s]["en_yuksek"] = en_yuksek
            piyasa_verisi[s]["en_dusuk"] = en_dusuk
        return True
    except Exception: return False

def acik_pozisyonlari_binanceden_guncelle():
    try:
        pozisyonlar = order_client.futures_position_information()
        with data_lock:
            for s in SYMBOLS:
                if not emir_beklemede_durumu.get(s, False):
                    eski_kademe = aktif_pozisyonlar[s].get("dca_kademe", 0)
                    aktif_pozisyonlar[s] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "dca_kademe": eski_kademe}
            
            for p in pozisyonlar:
                sym = p.get("symbol", "").lower()
                if sym in aktif_pozisyonlar:
                    if emir_beklemede_durumu.get(sym, False): continue
                    amt = float(p.get("positionAmt", 0))
                    entry_price = float(p.get("entryPrice", 0))
                    if amt != 0:
                        aktif_pozisyonlar[sym]["aktif"] = True
                        aktif_pozisyonlar[sym]["yon"] = "LONG" if amt > 0 else "SHORT"
                        aktif_pozisyonlar[sym]["adet"] = abs(amt)
                        aktif_pozisyonlar[sym]["giris_fiyati"] = entry_price
                    else:
                        aktif_pozisyonlar[sym]["dca_kademe"] = 0
    except Exception as e:
        print(f"❌ Pozisyon senkronizasyon hatası: {e}")

# --- 🎛️ TELEGRAM YÖNETİMİ ---
def telegram_bildir(mesaj, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}
        if reply_markup: data["reply_markup"] = reply_markup
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=data, timeout=5)
    except Exception: pass

def ana_menu_olustur():
    return {
        "keyboard": [[{"text": "📊 Bot Durumu"}], [{"text": "▶️ Botu Başlat"}, {"text": "⏸️ Botu Durdur"}]],
        "resize_keyboard": True, "one_time_keyboard": False
    }

def telegram_canli_rapor_uret():
    acik_pozisyonlari_binanceden_guncelle()
    with data_lock:
        acik_pozlar = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])
        durum_str = "🟢 Pure API Tarama" if config.BOT_CALISIYOR else "🔴 Sistem Durduruldu"

        rapor = (
            f"⚙️ <b>RSI Rejection + İzole Koruma Botu</b>\n"
            f"• Sistem: {durum_str}\n"
            f"• Risk Limiti: {acik_pozlar}/{config.MAX_ACIK_POZISYON} Pozisyon\n"
            f"• TP Hedefi: {config.SABIT_DOLAR_TP} USD\n"
            f"• Güvenlik: %{config.VOLATILITE_SINIRI} Volatilite Filtresi 🚫\n"
            f"• Yapı: %100 Saf İzole Marjin (Borsa SL Yok)\n\n"
            f"⚡ <b>Açık İşlemler:</b>\n"
        )

        if acik_pozlar == 0:
            rapor += "Açık izole pozisyon bulunmuyor."
        else:
            for s in SYMBOLS:
                if aktif_pozisyonlar[s]["aktif"]:
                    p = aktif_pozisyonlar[s]
                    rapor += f"• {s.upper()} | {p['yon']} | Giriş: {p['giris_fiyati']} | Kademe: {p.get('dca_kademe', 0)}/2\n"
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
                        telegram_bildir("🤖 <b>Bot Kontrol Paneli Aktif!</b>", reply_markup=ana_menu_olustur())
                    elif text == "📊 Bot Durumu":
                        telegram_bildir(telegram_canli_rapor_uret(), reply_markup=ana_menu_olustur())
                    elif text == "▶️ Botu Başlat":
                        config.BOT_CALISIYOR = True
                        telegram_bildir("🚀 Bot tarama döngüsü <b>aktif.</b>", reply_markup=ana_menu_olustur())
                    elif text == "⏸️ Botu Durdur":
                        config.BOT_CALISIYOR = False
                        telegram_bildir("⏸️ Bot tarama döngüsü <b>durduruldu.</b>", reply_markup=ana_menu_olustur())
        except Exception: time.sleep(5)


# =====================================================================
# 🚀 HIZLI TAKİP DÖNGÜSÜ (AÇIK POZİSYON KORUMA KANALI)
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

            try:
                price_resp = requests.get("https://fapi.binance.com/v1/ticker/price", timeout=5)
                if price_resp.status_code == 200:
                    prices_list = price_resp.json()
                    price_map = {item["symbol"].lower(): float(item["price"]) for item in prices_list}
                    with data_lock:
                        for s in acik_semboller:
                            if s in price_map:
                                piyasa_verisi[s]["anlik_fiyat"] = price_map[s]
            except Exception as pe:
                print(f"⚠️ Fiyat güncelleme hatası: {pe}")

            su_an_ts = time.time()

            for symbol in acik_semboller:
                with data_lock:
                    pos = dict(aktif_pozisyonlar[symbol])
                    anlik_fiyat = piyasa_verisi[symbol]["anlik_fiyat"]
                    emir_beklemede = emir_beklemede_durumu.get(symbol, False)

                if emir_beklemede or anlik_fiyat <= 0:
                    continue

                maliyet = pos["giris_fiyati"]
                adet = pos["adet"]
                if maliyet <= 0 or adet <= 0:
                    continue

                if pos["yon"] == "LONG":
                    anlik_kar_dolar = (anlik_fiyat - maliyet) * adet
                    fiyat_sapma_yuzde = ((maliyet - anlik_fiyat) / maliyet) * 100
                else:  
                    anlik_kar_dolar = (maliyet - anlik_fiyat) * adet
                    fiyat_sapma_yuzde = ((anlik_fiyat - maliyet) / maliyet) * 100

                # 💰 A: KÂR ALMA (TAKE PROFIT)
                if anlik_kar_dolar >= config.SABIT_DOLAR_TP:
                    with data_lock:
                        if emir_beklemede_durumu[symbol]: continue
                        emir_beklemede_durumu[symbol] = True

                    try:
                        precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                        faktor = 10 ** precision
                        qty_to_close = math.floor(adet * faktor) / faktor if precision > 0 else int(adet)
                        side_to_close = SIDE_SELL if pos["yon"] == "LONG" else SIDE_BUY
                        
                        if qty_to_close > 0:
                            order_client.futures_create_order(
                                symbol=symbol.upper(), side=side_to_close, type=ORDER_TYPE_MARKET, 
                                quantity=qty_to_close, reduceOnly=True
                            )
                            with data_lock:
                                son_islem_zamanlari[symbol] = su_an_ts  
                                aktif_pozisyonlar[symbol] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "dca_kademe": 0}
                            telegram_bildir(f"💰 <b>{symbol.upper()} {pos['yon']} {round(anlik_kar_dolar, 3)}$ Kar ile Kapatıldı!</b>")
                    except Exception as e:
                        print(f"❌ Kapatma hatası ({symbol}): {e}")
                    finally:
                        with data_lock: emir_beklemede_durumu[symbol] = False

                # 🛡️ B: ÇİFT KADEMELİ KORUMA SİSTEMİ (İZOLE)
                else:
                    # KADEME 1: %3.0 Sapma (DCA Alımı)
                    if fiyat_sapma_yuzde >= config.DCA1_TETIK_YUZDE and pos.get("dca_kademe", 0) == 0:
                        with data_lock:
                            if emir_beklemede_durumu[symbol]: continue
                            emir_beklemede_durumu[symbol] = True

                        try:
                            precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                            dca_qty = (config.DCA1_MARJIN * config.KALDIRAC) / anlik_fiyat
                            dca_qty = float(int(dca_qty * (10 ** precision))) / (10 ** precision) if precision > 0 else int(dca_qty)

                            if dca_qty > 0:
                                dca_side = SIDE_BUY if pos["yon"] == "LONG" else SIDE_SELL
                                telegram_bildir(f"⚠️ <b>{symbol.upper()} %{round(fiyat_sapma_yuzde, 2)} Terste!</b> DCA 1 tetikleniyor...")
                                
                                order_client.futures_create_order(
                                    symbol=symbol.upper(), side=dca_side, type=ORDER_TYPE_MARKET, quantity=dca_qty
                                )
                                
                                with data_lock:
                                    aktif_pozisyonlar[symbol]["dca_kademe"] = 1
                                
                                time.sleep(1.0)
                                acik_pozisyonlari_binanceden_guncelle()
                                telegram_bildir(f"✅ <b>DCA 1 Başarılı! {symbol.upper()}</b> Maliyet optimize edildi.")
                        except Exception as e:
                            print(f"❌ DCA Kademe 1 Hatası ({symbol}): {e}")
                        finally:
                            with data_lock: emir_beklemede_durumu[symbol] = False

                    # KADEME 2: %3.5 Sapma (İzole Marjin Teminat Ekleme)
                    elif fiyat_sapma_yuzde >= config.DCA2_TETIK_YUZDE and pos.get("dca_kademe", 0) == 1:
                        with data_lock:
                            if emir_beklemede_durumu[symbol]: continue
                            emir_beklemede_durumu[symbol] = True

                        try:
                            telegram_bildir(f"🛡️ <b>{symbol.upper()} %{round(fiyat_sapma_yuzde, 2)} Terste!</b> İzole marjine {config.DCA2_EK_MARJIN} USDT nakit ekleniyor...")
                            
                            order_client.futures_change_position_margin(
                                symbol=symbol.upper(), amount=config.DCA2_EK_MARJIN, type=1  
                            )
                            
                            with data_lock:
                                aktif_pozisyonlar[symbol]["dca_kademe"] = 2
                            
                            time.sleep(1.0)
                            acik_pozisyonlari_binanceden_guncelle()
                            telegram_bildir(f"✅ <b>Güvenlik Teminatı Eklendi! {symbol.upper()}</b> Liq fiyatı izole havuz içinde uzaklaştırıldı.")
                        except Exception as e:
                            print(f"❌ Marjin Ekleme Hatası ({symbol}): {e}")
                        finally:
                            with data_lock: emir_beklemede_durumu[symbol] = False

            time.sleep(config.HIZLI_TAKIP_PERIYODU)
        except Exception as e:
            print(f"❌ Hızlı takip döngüsü hatası: {e}")
            time.sleep(2.0)


# --- 🎯 YAVAŞ TARAMA MOTORU (GİRİŞ SİNYALLERİNİ ARAR) ---
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

                if len(v["kapanislar"]) < 40 or not v["anlik_fiyat"] or v["anlik_fiyat"] <= 0:
                    time.sleep(config.API_DELAY)
                    continue
                
                anlik_fiyat = v["anlik_fiyat"]
                en_yuksek = v.get("en_yuksek", anlik_fiyat)
                en_dusuk = v.get("en_dusuk", anlik_fiyat)

                if not pos["aktif"]:
                    if su_an_ts - son_islem < config.COOLDOWN_SURESI: 
                        time.sleep(config.API_DELAY)
                        continue
                    
                    with data_lock:
                        guncel_acik_pozisyon_sayisi = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])
                    if guncel_acik_pozisyon_sayisi >= config.MAX_ACIK_POZISYON: 
                        time.sleep(config.API_DELAY)
                        continue 

                    sinyal, guncel_rsi = strateji_sinyal_uret(v, anlik_fiyat, en_yuksek, en_dusuk)

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

                            if qty > 0:
                                if sinyal == "BUY":
                                    order_client.futures_create_order(symbol=symbol.upper(), side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
                                    with data_lock:
                                        aktif_pozisyonlar[symbol] = {"aktif": True, "yon": "LONG", "adet": qty, "giris_fiyati": anlik_fiyat, "dca_kademe": 0}
                                    telegram_bildir(f"🚀 <b>{symbol.upper()} LONG (RSI Rejection) Açıldı!</b>\nFiyat: {anlik_fiyat}\nRSI: {round(guncel_rsi, 2)}")
                                        
                                elif sinyal == "SELL":
                                    order_client.futures_create_order(symbol=symbol.upper(), side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=qty)
                                    with data_lock:
                                        aktif_pozisyonlar[symbol] = {"aktif": True, "yon": "SHORT", "adet": qty, "giris_fiyati": anlik_fiyat, "dca_kademe": 0}
                                    telegram_bildir(f"🚀 <b>{symbol.upper()} SHORT (RSI Rejection) Açıldı!</b>\nFiyat: {anlik_fiyat}\nRSI: {round(guncel_rsi, 2)}")
                        except Exception as e:
                            print(f"❌ Emir gönderme hatası ({symbol}): {e}")
                        finally:
                            with data_lock: emir_beklemede_durumu[symbol] = False
                
                time.sleep(config.API_DELAY)
        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            time.sleep(2.0)

# --- 🚀 ANA ÇALIŞTIRICI SİSTEM ---
if __name__ == "__main__":
    print("🎬 RSI Rejection + İzole Güvenlik Botu Başlatılıyor...")
    
    print("🔍 Binance üzerindeki mevcut açık pozisyonlar taranıyor...")
    try:
        mevcut_pozisyonlar = order_client.futures_position_information()
        for p in mevcut_pozisyonlar:
            amt = float(p.get("positionAmt", 0))
            sym = p.get("symbol", "").lower()
            if amt != 0:
                print(f"📦 İçeride açık pozisyon bulundu: {sym.upper()} (Miktar: {amt}). Takip listesine kaydediliyor...")
                kontrollu_coin_ekle(sym, eski_pozisyon_mu=True)
    except Exception as e:
        print(f"❌ İlk pozisyon taramasında kritik hata: {e}")
    
    hacimli_coinler = ilk_100_hacimli_coin_bul()
    print(f"📋 İlk etapta {len(hacimli_coinler)} adet hacimli coin tespit edildi.")
    
    eklenen_sayac = 0
    for c in hacimli_coinler:
        if kontrollu_coin_ekle(c, eski_pozisyon_mu=False):
            eklenen_sayac += 1
            
    print(f"✅ Filtreleri geçen {eklenen_sayac} coin tarama listesine eklendi.")
    
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=telegram_gelen_mesaj_dinleyici, daemon=True).start()
        telegram_bildir("🤖 <b>Bot RSI Rejection Modunda Başlatıldı!</b>\nPozisyonlar izole yönetiliyor, donanım stopu deaktif.")
    
    threading.Thread(target=hizli_acik_pozisyon_takip_dongusu, daemon=True).start()
    print("⚡ Hızlı açık pozisyon kontrol kanalı aktif edildi.")

    print("⚡ Tüm sistemler aktif. Senkronize döngü başlıyor...")
    pure_api_tarama_dongusu()
