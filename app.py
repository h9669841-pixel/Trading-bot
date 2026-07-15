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

requests_proxies = None
if PROXY_URL:
    print(f"🌐 Statik IP tüneli hazırlandı. Sadece alım/satım işlemlerinde tetiklenecek.")
    requests_proxies = {"http": PROXY_URL, "https": PROXY_URL}

client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
if requests_proxies:
    client.session.proxies = requests_proxies

class TrendBotConfig:
    def __init__(self):
        self.TIMEFRAME = Client.KLINE_INTERVAL_15MINUTE  
        self.ISLEM_MARJIN = 1.0        
        self.KALDIRAC = 20             
        self.MAX_ACIK_POZISYON = 10     
        self.BOT_CALISIYOR = True
        self.COOLDOWN_SURESI = 0     
        self.SABIT_DOLAR_TP = 0.15     # Net kâr hedefi (Dolar)
        
        # === Sadece Bollinger & RSI Parametreleri ===
        self.BB_LEN = 20
        self.BB_MULT = 2.0
        
        self.RSI_LEN = 14
        self.RSI_OB = 70               # Aşırı Alım (Overbought) Sınırı
        self.RSI_OS = 30               # Aşırı Satım (Oversold) Sınırı
        
        # API Tarama Gecikmesi
        self.API_DELAY = 0.3

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
        varyans = sum((x - ort) ** 2 for x in pencere) / periyod
        res.append(math.sqrt(varyans))
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

def strateji_sinyal_uret(v, anlik_fiyat):
    kapanislar = list(v["kapanislar"])
    
    if not kapanislar or anlik_fiyat <= 0: return "HOLD"
    
    # Anlık fiyatı listenin sonuna ekleyip hesaplamayı taze tutuyoruz
    kapanislar.append(anlik_fiyat)

    L = len(kapanislar)
    gerekli_uzunluk = max(config.BB_LEN, config.RSI_LEN) + 3
    if L < gerekli_uzunluk: return "HOLD"

    # Bollinger Hesabı
    basis = sma(kapanislar, config.BB_LEN)
    dev = stdev(kapanislar, config.BB_LEN)
    upper_bb = basis[-1] + (config.BB_MULT * dev[-1])
    lower_bb = basis[-1] - (config.BB_MULT * dev[-1])

    # RSI Hesabı
    rsi_val = rsi_hesapla(kapanislar, config.RSI_LEN)

    # --- SİNYAL KOŞULLARI ---
    # LONG: Fiyat Alt Bollinger'ın altında VE RSI aşırı satım çizgisinin üzerinde (Yukarı dönme eğilimi)
    long_ok = (anlik_fiyat < lower_bb) and (rsi_val > config.RSI_OS)

    # SHORT: Fiyat Üst Bollinger'ın üzerinde VE RSI aşırı alım çizgisinin altında (Aşağı dönme eğilimi)
    short_ok = (anlik_fiyat > upper_bb) and (rsi_val < config.RSI_OB)

    if long_ok: return "BUY"
    elif short_ok: return "SELL"

    return "HOLD"

# --- 🌐 REST API ALTYAPI FONKSİYONLARI ---

def ilk_100_hacimli_coin_bul():
    try:
        ticker_url = "https://fapi.binance.com/v1/ticker/24hr"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(ticker_url, headers=headers, proxies=requests_proxies, timeout=15)
        
        if response.status_code != 200:
            print(f"❌ Binance API Hata Kodu Döndürdü: {response.status_code}")
            return []
            
        try:
            data = response.json()
        except ValueError:
            print("❌ Binance'den dönen veri JSON formatında değil! (HTML veya Boş Yanıt)")
            return []

        usdt_pairs = [x for x in data if isinstance(x, dict) and x.get("symbol", "").endswith("USDT")]
        sorted_by_volume = sorted(usdt_pairs, key=lambda k: float(k.get("quoteVolume", 0)), reverse=True)
        return [x["symbol"].lower() for x in sorted_by_volume[:100]]
    except Exception as e:
        print(f"❌ Hacim listesi alınamadı: {e}")
        return []

def kontrollu_coin_ekle(coin_adi):
    coin_lower = coin_adi.lower().strip()
    coin_upper = coin_lower.upper()
    if coin_lower in SYMBOLS: return True
    try:
        f_url = "https://fapi.binance.com/v1/exchangeInfo"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(f_url, headers=headers, proxies=requests_proxies, timeout=10)
        
        if response.status_code != 200:
            return False
            
        try:
            r = response.json()
        except ValueError:
            return False

        market_info = next((m for m in r.get("symbols", []) if m["symbol"] == coin_upper), None)
        if not market_info or market_info.get('status') != 'TRADING': return False
        
        time.sleep(0.20) 
        client.futures_change_leverage(symbol=coin_upper, leverage=config.KALDIRAC)
        try: 
            client.futures_change_margin_type(symbol=coin_upper, marginType="ISOLATED")
        except BinanceAPIException as e:
            if "No need to change" not in e.message: pass

        for f in market_info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step_size_str = str(f['stepSize']).rstrip('0')
                precision = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
                FUTURES_HASSASIYETLERI[coin_lower] = precision

        with data_lock:
            SYMBOLS.append(coin_lower)
            piyasa_verisi[coin_lower] = {"anlik_fiyat": 0.0, "kapanislar": []}
            aktif_pozisyonlar[coin_lower] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0}
            son_islem_zamanlari[coin_lower] = 0.0  
            emir_beklemede_durumu[coin_lower] = False
        return True
    except Exception: return False

def tek_coin_api_verisi_guncelle(s):
    try:
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={s.upper()}&interval={config.TIMEFRAME}&limit=60"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(url, headers=headers, proxies=requests_proxies, timeout=5)
        
        if response.status_code != 200:
            return False
            
        try:
            k = response.json()
        except ValueError:
            return False

        if not k or len(k) == 0: return False
        
        kapanislar_yeni = [float(x[4]) for x in k]
        anlik_fiyat_yeni = kapanislar_yeni[-1]  
        
        with data_lock:
            piyasa_verisi[s]["kapanislar"] = kapanislar_yeni[:-1] 
            piyasa_verisi[s]["anlik_fiyat"] = anlik_fiyat_yeni
        return True
    except Exception:
        return False

def acik_pozisyonlari_binanceden_guncelle():
    try:
        pozisyonlar = client.futures_position_information()
        with data_lock:
            for s in SYMBOLS:
                if not emir_beklemede_durumu.get(s, False):
                    aktif_pozisyonlar[s] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0}
            
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
        poz_buyuklugu = config.ISLEM_MARJIN * config.KALDIRAC

        rapor = (
            f"⚙️ <b>Bollinger & RSI REST API Botu</b>\n"
            f"• Sistem: {durum_str}\n"
            f"• Marjin: {config.ISLEM_MARJIN:.1f} USDT\n"
            f"• Kaldıraç: {config.KALDIRAC}x (İZOLE)\n"
            f"• Poz Büyüklüğü: {poz_buyuklugu:.1f} USDT\n"
            f"• Risk Limiti: {acik_pozlar}/{config.MAX_ACIK_POZISYON} Pozisyon\n"
            f"• TP Hedefi: {config.SABIT_DOLAR_TP} USD\n\n"
            f"⚡ <b>Açık İşlemler:</b>\n"
        )

        if acik_pozlar == 0:
            rapor += "Açık izole pozisyon bulunmuyor."
        else:
            for s in SYMBOLS:
                if aktif_pozisyonlar[s]["aktif"]:
                    p = aktif_pozisyonlar[s]
                    rapor += f"• {s.upper()} | {p['yon']} | Giriş: {p['giris_fiyati']}\n"
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

# --- 🎯 PURE API TARAMA MOTORU ---
def pure_api_tarama_dongusu():
    while True:
        try:
            if not config.BOT_CALISIYOR:
                time.sleep(1.0)
                continue
                
            su_an_ts = time.time()
            acik_pozisyonlari_binanceden_guncelle()  

            with data_lock:
                yerel_semboller = list(SYMBOLS)

            for symbol in yerel_semboller:
                if not config.BOT_CALISIYOR: break

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

                # ==========================================
                # 🎯 ÇIKIŞ MANTIĞI (SABİT DOLAR HEDEFİ)
                # ==========================================
                if pos["aktif"]:
                    maliyet = pos["giris_fiyati"]
                    adet = pos["adet"]
                    if maliyet <= 0 or adet <= 0: continue

                    if pos["yon"] == "LONG":
                        anlik_kar_dolar = (anlik_fiyat - maliyet) * adet
                    else:  # SHORT
                        anlik_kar_dolar = (maliyet - anlik_fiyat) * adet

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
                                client.futures_create_order(
                                    symbol=symbol.upper(), side=side_to_close, type=ORDER_TYPE_MARKET, 
                                    quantity=qty_to_close, reduceOnly=True
                                )
                                with data_lock:
                                    son_islem_zamanlari[symbol] = su_an_ts  
                                    aktif_pozisyonlar[symbol] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0}
                                telegram_bildir(f"💰 <b>{symbol.upper()} {pos['yon']} {round(anlik_kar_dolar, 3)}$ Kar ile Kapatıldı!</b>\nFiyat: {anlik_fiyat}")
                        except Exception as e:
                            print(f"❌ Kapatma hatası ({symbol}): {e}")
                        finally:
                            with data_lock: emir_beklemede_durumu[symbol] = False

                # ==========================================
                # 📈 GİRİŞ MANTIĞI (BOLLINGER & RSI)
                # ==========================================
                else:
                    if su_an_ts - son_islem < config.COOLDOWN_SURESI: 
                        time.sleep(config.API_DELAY)
                        continue
                    
                    with data_lock:
                        guncel_acik_pozisyon_sayisi = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])
                    if guncel_acik_pozisyon_sayisi >= config.MAX_ACIK_POZISYON: 
                        time.sleep(config.API_DELAY)
                        continue 

                    sinyal = strateji_sinyal_uret(v, anlik_fiyat)

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
                                print(f"⚠️ {symbol.upper()} bütçesi yetersiz kaldığından miktar sıfır (0) hesaplandı.")
                                with data_lock: emir_beklemede_durumu[symbol] = False
                                continue

                            if qty > 0:
                                if sinyal == "BUY":
                                    client.futures_create_order(symbol=symbol.upper(), side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
                                    with data_lock:
                                        aktif_pozisyonlar[symbol] = {"aktif": True, "yon": "LONG", "adet": qty, "giris_fiyati": anlik_fiyat}
                                    telegram_bildir(f"🚀 <b>{symbol.upper()} LONG Pozisyonu Açıldı!</b>\nFiyat: {anlik_fiyat}\nMiktar: {qty}")
                                        
                                elif sinyal == "SELL":
                                    client.futures_create_order(symbol=symbol.upper(), side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=qty)
                                    with data_lock:
                                        aktif_pozisyonlar[symbol] = {"aktif": True, "yon": "SHORT", "adet": qty, "giris_fiyati": anlik_fiyat}
                                    telegram_bildir(f"🚀 <b>{symbol.upper()} SHORT Pozisyonu Açıldı!</b>\nFiyat: {anlik_fiyat}\nMiktar: {qty}")
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
    print("🎬 Sadece Bollinger & RSI Botu Başlatılıyor...")
    
    hacimli_coinler = ilk_100_hacimli_coin_bul()
    print(f"📋 İlk etapta {len(hacimli_coinler)} adet hacimli coin tespit edildi.")
    
    eklenen_sayac = 0
    for c in hacimli_coinler:
        if kontrollu_coin_ekle(c):
            eklenen_sayac += 1
            
    print(f"✅ Filtreleri geçen {eklenen_sayac} coin tarama listesine eklendi.")
    
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=telegram_gelen_mesaj_dinleyici, daemon=True).start()
        telegram_bildir("🤖 <b>Bot Saf Bollinger & RSI Modunda Başlatıldı!</b>")
    
    print("⚡ Tüm sistemler aktif. Senkronize döngü başlıyor...")
    pure_api_tarama_dongusu()
