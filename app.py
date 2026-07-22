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
    proxy_formatted = PROXY_URL if PROXY_URL.startswith("socks5://") else PROXY_URL.replace("socks5://", "socks5h://")

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
        self.TIMEFRAME = Client.KLINE_INTERVAL_1HOUR
        self.ISLEM_MARJIN = 2.0
        self.KALDIRAC = 10
        self.MAX_ACIK_POZISYON = 10
        self.BOT_CALISIYOR = True
        self.COOLDOWN_SURESI = 0
        self.SABIT_DOLAR_TP = 0.16  # 📌 CANLI CÜZDANDAN OKUNAN NET HEDEF PNL

        # === 🛡️ ÇİFT KADEMELİ GÜVENLİK AYARLARI ===
        self.DCA1_TETIK_YUZDE = 5.0
        self.DCA1_MARJIN = 2.0
        self.DCA2_TETIK_YUZDE = 5.5
        self.DCA2_EK_MARJIN = 2.0

        # === Bollinger & RSI Parametreleri ===
        self.BB_LEN = 20
        self.BB_MULT = 2.0
        self.RSI_LEN = 14
        self.RSI_OB = 77
        self.RSI_OS = 23

        self.API_DELAY = 0.5
        self.HIZLI_TAKIP_PERIYODU = 2.0

config = TrendBotConfig()
SYMBOLS = []
piyasa_verisi = {}
aktif_pozisyonlar = {}
FUTURES_HASSASIYETLERI = {}
son_islem_zamanlari = {}
emir_beklemede_durumu = {}
son_kapatilan_mum_zamanlari = {}  # 📌 YENİ: Kapatılan pozisyonun mum başlangıç zamanını (timestamp) tutar
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

def rsi_serisi_hesapla(kapanislar, periyod=14):
    """📌 YENİ: Geçmiş kırılımları kontrol edebilmek için tüm mumların RSI değerlerini liste olarak döner."""
    if len(kapanislar) < periyod + 1: 
        return [50.0] * len(kapanislar)
    
    kazanclar, kayiplar = [], []
    for i in range(1, len(kapanislar)):
        fark = kapanislar[i] - kapanislar[i-1]
        if fark > 0: 
            kazanclar.append(fark)
            kayiplar.append(0)
        else: 
            kazanclar.append(0)
            kayiplar.append(abs(fark))
            
    rsi_list = [50.0] * periyod
    ort_kazanc = sum(kazanclar[:periyod]) / periyod
    ort_kayip = sum(kayiplar[:periyod]) / periyod
    
    if ort_kayip <= 0.00000001: 
        rsi_list.append(100.0)
    else: 
        rsi_list.append(100.0 - (100.0 / (1.0 + (ort_kazanc / ort_kayip))))
    
    for i in range(periyod, len(kazanclar)):
        ort_kazanc = (ort_kazanc * (periyod - 1) + kazanclar[i]) / periyod
        ort_kayip = (ort_kayip * (periyod - 1) + kayiplar[i]) / periyod
        if ort_kayip <= 0.00000001: 
            rsi_list.append(100.0)
        else: 
            rsi_list.append(100.0 - (100.0 / (1.0 + (ort_kazanc / ort_kayip))))
            
    return rsi_list

def strateji_sinyal_uret(v, anlik_fiyat):
    """📌 YENİLENEN MOTOR: RSI aşırı bölgeden içeri girdiğinde (kırılım yaptığında) sinyal üretir."""
    kapanislar = list(v["kapanislar"])
    if not kapanislar or anlik_fiyat <= 0: return "HOLD", 50.0
    kapanislar.append(anlik_fiyat)
    L = len(kapanislar)
    gerekli_uzunluk = max(config.BB_LEN, config.RSI_LEN) + 3
    if L < gerekli_uzunluk: return "HOLD", 50.0
    
    # --- Bollinger Hesaplamaları ---
    basis = sma(kapanislar, config.BB_LEN)
    dev = stdev(kapanislar, config.BB_LEN)
    upper_bb = basis[-1] + (config.BB_MULT * dev[-1])
    lower_bb = basis[-1] - (config.BB_MULT * dev[-1])
    
    # --- RSI Serisi Hesaplamaları ---
    rsi_seri = rsi_serisi_hesapla(kapanislar, config.RSI_LEN)
    rsi_val_current = rsi_seri[-1]  # Anlık/Güncel mumun RSI değeri
    rsi_val_prev = rsi_seri[-2]     # Bir önceki kapanan mumun RSI değeri
    
    # Kırılım Şartları: 
    # LONG: Bir önceki mumda RSI 20'den küçüktü, ŞİMDİ 20 veya üzerine çıktı.
    long_ok = (rsi_val_prev < config.RSI_OS) and (rsi_val_current >= config.RSI_OS) and (anlik_fiyat < lower_bb)
    # SHORT: Bir önceki mumda RSI 80'den büyüktü, ŞİMDİ 80 veya altına indi.
    short_ok = (rsi_val_prev > config.RSI_OB) and (rsi_val_current <= config.RSI_OB) and (anlik_fiyat > upper_bb)
    
    if long_ok: return "BUY", rsi_val_current
    elif short_ok: return "SELL", rsi_val_current
    return "HOLD", rsi_val_current

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
            piyasa_verisi[coin_lower] = {"anlik_fiyat": 0.0, "kapanislar": [], "guncel_mum_zamani": 0}
            aktif_pozisyonlar[coin_lower] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "resmi_pnl": 0.0, "dca_kademe": 0}
            son_islem_zamanlari[coin_lower] = 0.0
            emir_beklemede_durumu[coin_lower] = False
            son_kapatilan_mum_zamanlari[coin_lower] = 0 # Başlangıç değeri atandı
        return True
    except Exception:
        return False

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
        
        # 📌 YENİ: Anlık tarama yapılan mumun başlangıç zaman damgasını (open time) alıyoruz.
        guncel_mum_zamani = k[-1][0]
        
        with data_lock:
            piyasa_verisi[s]["kapanislar"] = kapanislar_yeni[:-1]
            piyasa_verisi[s]["anlik_fiyat"] = anlik_fiyat_yeni
            piyasa_verisi[s]["guncel_mum_zamani"] = guncel_mum_zamani
        return True
    except Exception:
        return False

# --- 🎯 CANLI CÜZDAN ÜZERİNDEN NET PNL OKUMA FONKSİYONU ---
def acik_pozisyonlari_binanceden_guncelle():
    try:
        hesap_bilgisi = order_client.futures_account()
        pozisyonlar = hesap_bilgisi.get("positions", [])
        
        with data_lock:
            for s in SYMBOLS:
                if not emir_beklemede_durumu.get(s, False):
                    eski_kademe = aktif_pozisyonlar[s].get("dca_kademe", 0)
                    aktif_pozisyonlar[s] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "resmi_pnl": 0.0, "dca_kademe": eski_kademe}
            
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
                    else:
                        aktif_pozisyonlar[sym]["dca_kademe"] = 0
    except Exception as e:
        print(f"❌ Canlı Cüzdan PNL senkronizasyon hatası: {e}")

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
        durum_str = "🟢 Pure API Tarama" if config.BOT_CALISIYOR else "🔴 Sistem Durduruldu"
        rapor = (
            f"⚙️ <b>Bollinger & RSI Botu (Canlı Hesap Modu)</b>\n"
            f"• Sistem: {durum_str}\n"
            f"• Marjin: {config.ISLEM_MARJIN:.1f} USDT\n"
            f"• Kaldıraç: {config.KALDIRAC}x\n"
            f"• Risk Limiti: {acik_pozlar}/{config.MAX_ACIK_POZISYON} Poz.\n"
            f"• TP Hedefi (Borsa PNL): {config.SABIT_DOLAR_TP} USD\n\n"
            f"⚡ <b>Açık İşlemler:</b>\n"
        )
        if acik_pozlar == 0:
            rapor += "Açık izole pozisyon bulunmuyor."
        else:
            for s in SYMBOLS:
                if aktif_pozisyonlar[s]["aktif"]:
                    p = aktif_pozisyonlar[s]
                    rapor += f"• {s.upper()} | {p['yon']} | Canlı PNL: <b>{round(p['resmi_pnl'], 4)}$</b> | Kademe: {p.get('dca_kademe', 0)}/2\n"
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
# 🚀 HIZLI TAKİP DÖNGÜSÜ (KESİN CANLI PNL KAPATMA ENTEGRASYONU)
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
                with data_lock:
                    pos = dict(aktif_pozisyonlar[symbol])
                    emir_beklemede = emir_beklemede_durumu.get(symbol, False)
                
                if emir_beklemede or pos["adet"] <= 0: 
                    continue
                
                anlik_kar_dolar = pos["resmi_pnl"] 

                # 💰 HEDEF YAKALANDI MI? 
                if anlik_kar_dolar >= config.SABIT_DOLAR_TP:
                    with data_lock:
                        if emir_beklemede_durumu[symbol]: continue
                        emir_beklemede_durumu[symbol] = True
                    try:
                        precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                        faktor = 10 ** precision
                        qty_to_close = math.floor(pos["adet"] * faktor) / faktor if precision > 0 else int(pos["adet"])
                        side_to_close = SIDE_SELL if pos["yon"] == "LONG" else SIDE_BUY
                        
                        if qty_to_close > 0:
                            print(f"🎯 {symbol.upper()} Canlı Cüzdan PNL Hedefi Tetiklendi ({anlik_kar_dolar}$). Pozisyon kapatılıyor...")
                            order_client.futures_create_order(
                                symbol=symbol.upper(),
                                side=side_to_close,
                                type=ORDER_TYPE_MARKET,
                                quantity=qty_to_close,
                                reduceOnly=True
                            )
                        with data_lock:
                            son_islem_zamanlari[symbol] = su_an_ts
                            
                            # 📌 YENİ: Pozisyon kapandığı an, hangi munda kapandığını 'son_kapatilan_mum_zamanlari' değişkenine not ediyoruz.
                            if "guncel_mum_zamani" in piyasa_verisi[symbol]:
                                son_kapatilan_mum_zamanlari[symbol] = piyasa_verisi[symbol]["guncel_mum_zamani"]
                                
                            aktif_pozisyonlar[symbol] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "resmi_pnl": 0.0, "dca_kademe": 0}
                        telegram_bildir(f"💰 <b>{symbol.upper()} {pos['yon']} Canlı Cüzdan PNL: {round(anlik_kar_dolar, 3)}$ ile Kapatıldı!</b>")
                    except Exception as e:
                        print(f"❌ Kapatma emri gönderilemedi ({symbol}): {e}")
                        telegram_bildir(f"❌ <b>{symbol.upper()} Kapatılamadı!</b> Hata: {e}")
                    finally:
                        with data_lock: emir_beklemede_durumu[symbol] = False

            try:
                price_resp = requests.get("https://fapi.binance.com/fapi/v1/ticker/price", timeout=3)
                if price_resp.status_code == 200:
                    prices_list = price_resp.json()
                    price_map = {item["symbol"].lower(): float(item["price"]) for item in prices_list}
                    with data_lock:
                        for s in acik_semboller:
                            if s in price_map:
                                piyasa_verisi[s]["anlik_fiyat"] = price_map[s]
            except Exception as pe:
                print(f"⚠️ DCA fiyat sunucu uyarısı (Kâr almayı engellemez): {pe}")

            # DCA Güvenlik Kademe Kontrolleri
            for symbol in acik_semboller:
                with data_lock:
                    pos = dict(aktif_pozisyonlar[symbol])
                    anlik_fiyat = piyasa_verisi[symbol].get("anlik_fiyat", 0.0)
                    emir_beklemede = emir_beklemede_durumu.get(symbol, False)
                
                if emir_beklemede or anlik_fiyat <= 0 or pos["giris_fiyati"] <= 0 or not pos["aktif"]:
                    continue

                maliyet = pos["giris_fiyati"]
                if pos["yon"] == "LONG":
                    fiyat_sapma_yuzde = ((maliyet - anlik_fiyat) / maliyet) * 100
                else:
                    fiyat_sapma_yuzde = ((anlik_fiyat - maliyet) / maliyet) * 100

                # 🛡️ KADEME 1: (DCA Alımı)
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
                            telegram_bildir(f"⚠️ <b>{symbol.upper()} Terste (%{round(fiyat_sapma_yuzde, 2)})</b>\nDCA 1 Yapılıyor...")
                            order_client.futures_create_order(symbol=symbol.upper(), side=dca_side, type=ORDER_TYPE_MARKET, quantity=dca_qty)
                            with data_lock: aktif_pozisyonlar[symbol]["dca_kademe"] = 1
                            time.sleep(1.0)
                            acik_pozisyonlari_binanceden_guncelle()
                    except Exception as e:
                        print(f"❌ DCA Hatası: {e}")
                    finally:
                        with data_lock: emir_beklemede_durumu[symbol] = False
                
                # 🛡️ KADEME 2: (Marjin Ekleme)
                elif fiyat_sapma_yuzde >= config.DCA2_TETIK_YUZDE and pos.get("dca_kademe", 0) == 1:
                    with data_lock:
                        if emir_beklemede_durumu[symbol]: continue
                        emir_beklemede_durumu[symbol] = True
                    try:
                        telegram_bildir(f"🛡️ <b>{symbol.upper()} Riski Artıyor (%{round(fiyat_sapma_yuzde, 2)})</b>\nİzole teminata {config.DCA2_EK_MARJIN} USDT ekleniyor...")
                        order_client.futures_change_position_margin(symbol=symbol.upper(), amount=config.DCA2_EK_MARJIN, type=1)
                        with data_lock: aktif_pozisyonlar[symbol]["dca_kademe"] = 2
                        time.sleep(1.0)
                        acik_pozisyonlari_binanceden_guncelle()
                    except Exception as e:
                        print(f"❌ Marjin Ekleme Hatası: {e}")
                    finally:
                        with data_lock: emir_beklemede_durumu[symbol] = False
            
            time.sleep(config.HIZLI_TAKIP_PERIYODU)
        except Exception as e:
            print(f"❌ Takip döngüsü ana hatası: {e}")
            time.sleep(2.0)

# --- 🎯 YAVAŞ TARAMA MOTORU ---
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
                
                # 📌 YENİ: Aynı mum içinde tekrar pozisyon açmama kontrolü
                guncel_mum_ts = v.get("guncel_mum_zamani", 0)
                with data_lock:
                    son_kapatilan_mum_ts = son_kapatilan_mum_zamanlari.get(symbol, 0)
                
                if guncel_mum_ts == son_kapatilan_mum_ts and guncel_mum_ts != 0:
                    # Kapatılan mum hala bitmedi (aktif), o yüzden bu mumu es geçip sonraki coine atlıyoruz.
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
                
                sinyal, guncel_rsi = strateji_sinyal_uret(v, anlik_fiyat)
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
                            with data_lock: aktif_pozisyonlar[symbol] = {"aktif": True, "yon": "LONG", "adet": qty, "giris_fiyati": anlik_fiyat, "resmi_pnl": 0.0, "dca_kademe": 0}
                            telegram_bildir(f"🚀 <b>{symbol.upper()} LONG Açıldı!</b>\nFiyat: {anlik_fiyat}\nRSI: {round(guncel_rsi, 2)} (Aşırı satımdan geri döndü)")
                        elif sinyal == "SELL":
                            order_client.futures_create_order(symbol=symbol.upper(), side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=qty)
                            with data_lock: aktif_pozisyonlar[symbol] = {"aktif": True, "yon": "SHORT", "adet": qty, "giris_fiyati": anlik_fiyat, "resmi_pnl": 0.0, "dca_kademe": 0}
                            telegram_bildir(f"🚀 <b>{symbol.upper()} SHORT Açıldı!</b>\nFiyat: {anlik_fiyat}\nRSI: {round(guncel_rsi, 2)} (Aşırı alımdan geri döndü)")
                    except Exception as e:
                        print(f"❌ Emir hatası: {e}")
                    finally:
                        with data_lock: emir_beklemede_durumu[symbol] = False
                time.sleep(config.API_DELAY)
        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            time.sleep(2.0)

# --- 🚀 ANA ÇALIŞTIRICI SİSTEM ---
if __name__ == "__main__":
    print("🎬 Canlı Cüzdan PNL Kapatma & RSI Kırılım Korumalı Bot Başlatılıyor...")
    try:
        hesap_bilgisi = order_client.futures_account()
        mevcut_pozisyonlar = hesap_bilgisi.get("positions", [])
        for p in mevcut_pozisyonlar:
            amt = float(p.get("positionAmt", 0))
            sym = p.get("symbol", "").lower()
            if amt != 0:
                print(f"📦 Mevcut açık pozisyon listeye eklendi: {sym.upper()}")
                kontrollu_coin_ekle(sym, eski_pozisyon_mu=True)
    except Exception as e:
        print(f"❌ İlk pozisyon tarama hatası: {e}")

    hacimli_coinler = ilk_100_hacimli_coin_bul()
    eklenen_sayac = 0
    for c in hacimli_coinler:
        if kontrollu_coin_ekle(c, eski_pozisyon_mu=False): eklenen_sayac += 1
    print(f"✅ Filtreleri geçen {eklenen_sayac} coin tarama listesine eklendi.")

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=telegram_gelen_mesaj_dinleyici, daemon=True).start()
        telegram_bildir("🤖 <b>Bot RSI Kırılımı ve Aynı Mum Koruması ile Aktif!</b>\nEkstrem indikatör dönüşleri taranıyor.")

    threading.Thread(target=hizli_acik_pozisyon_takip_dongusu, daemon=True).start()
    pure_api_tarama_dongusu()
