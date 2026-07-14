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
        self.ISLEM_MARJIN = 1.0        # İşlem başına marjin (USDT)
        self.KALDIRAC = 20             # Kaldıraç oranı
        self.MAX_ACIK_POZISYON = 10     # Aynı anda açık kalabilecek maksimum pozisyon sayısı
        self.BOT_CALISIYOR = True
        self.COOLDOWN_SURESI = 300     # İşlem sonrası aynı coinde bekleme süresi (Saniye)
        self.SABIT_DOLAR_TP = 0.10     # Net kâr hedefi (Dolar)
        
        # === İndikatör Parametreleri ===
        self.BB_LEN = 20               # Bollinger Bandı Periyodu
        self.BB_MULT = 2.0             # Bollinger Bandı Standart Sapma Çarpanı
        
        self.USE_RSI_FILTER = True     # Giriş için RSI filtresi kullanılsın mı?
        self.RSI_LEN = 14
        self.RSI_OB = 70               # Aşırı Alım (Overbought) Sınırı
        self.RSI_OS = 30               # Aşırı Satım (Oversold) Sınırı
        
        # API Tarama Gecikmesi (İstekler arası hafif esneme)
        self.API_DELAY = 0.3

config = TrendBotConfig()

SYMBOLS = [] 
piyasa_verisi = {}
aktif_pozisyonlar = {}
FUTURES_HASSASIYETLERI = {}
FUTURES_FIYAT_HASSASIYETLERI = {} # Fiyat yuvarlama hassasiyetleri
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

def bollinger_bands(kapanislar, periyod=20, carpan=2.0):
    """Bollinger Bandı (Üst, Orta, Alt) değerlerini hesaplar."""
    if len(kapanislar) < periyod:
        return [0.0], [0.0], [0.0]
    orta_bant = sma(kapanislar, periyod)
    sapmalar = stdev(kapanislar, periyod)
    
    ust_bant = [orta_bant[i] + (sapmalar[i] * carpan) for i in range(len(kapanislar))]
    alt_bant = [orta_bant[i] - (sapmalar[i] * carpan) for i in range(len(kapanislar))]
    
    return ust_bant, orta_bant, alt_bant

# --- 🌐 REST API ALTYAPI FONKSİYONLARI ---

def ilk_100_hacimli_coin_bul():
    try:
        ticker_url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        response = requests.get(ticker_url, timeout=15).json()
        usdt_pairs = [x for x in response if x["symbol"].endswith("USDT")]
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
        f_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(f_url, timeout=10).json()
        market_info = next((m for m in r.get("symbols", []) if m["symbol"] == coin_upper), None)
        if not market_info or market_info.get('status') != 'TRADING': return False
        
        time.sleep(0.20) 
        client.futures_change_leverage(symbol=coin_upper, leverage=config.KALDIRAC)
        try: client.futures_change_margin_type(symbol=coin_upper, marginType="ISOLATED")
        except BinanceAPIException as e:
            if "No need to change" not in e.message: pass

        # Adet hassasiyeti (LOT_SIZE)
        for f in market_info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step_size_str = str(f['stepSize']).rstrip('0')
                precision = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
                FUTURES_HASSASIYETLERI[coin_lower] = precision
            # Fiyat hassasiyeti (PRICE_FILTER)
            if f['filterType'] == 'PRICE_FILTER':
                tick_size_str = str(f['tickSize']).rstrip('0')
                price_precision = 0 if '.' not in tick_size_str else len(tick_size_str.split('.')[1])
                FUTURES_FIYAT_HASSASIYETLERI[coin_lower] = price_precision

        with data_lock:
            SYMBOLS.append(coin_lower)
            piyasa_verisi[coin_lower] = {"anlik_fiyat": 0.0, "kapanislar": [], "yuksekler": [], "dusukler": []}
            aktif_pozisyonlar[coin_lower] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0}
            son_islem_zamanlari[coin_lower] = 0.0  
            emir_beklemede_durumu[coin_lower] = False
        return True
    except Exception: return False

def tek_coin_api_verisi_guncelle(s):
    try:
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={s.upper()}&interval={config.TIMEFRAME}&limit=100"
        k = requests.get(url, timeout=5).json()
        if not k or len(k) == 0: return False
        
        kapanislar_yeni = [float(x[4]) for x in k]
        yuksekler_yeni = [float(x[2]) for x in k]
        dusukler_yeni = [float(x[3]) for x in k]
        anlik_fiyat_yeni = kapanislar_yeni[-1]  
        
        with data_lock:
            piyasa_verisi[s]["kapanislar"] = kapanislar_yeni[:-1] 
            piyasa_verisi[s]["yuksekler"] = yuksekler_yeni[:-1]
            piyasa_verisi[s]["dusukler"] = dusukler_yeni[:-1]
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
        durum_str = "🟢 Aktif (BB & RSI Modu - Sadece Sabit TP Çıkışlı)" if config.BOT_CALISIYOR else "🔴 Sistem Durduruldu"
        poz_buyuklugu = config.ISLEM_MARJIN * config.KALDIRAC

        rapor = (
            f"⚙️ <b>Squeeze Bollinger & RSI Botu</b>\n"
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
                        telegram_bildir("🤖 <b>Bot Kontrol Paneli Aktif! (Bollinger & RSI)</b>", reply_markup=ana_menu_olustur())
                    elif text == "📊 Bot Durumu":
                        telegram_bildir(telegram_canli_rapor_uret(), reply_markup=ana_menu_olustur())
                    elif text == "▶️ Botu Başlat":
                        config.BOT_CALISIYOR = True
                        telegram_bildir("🚀 Bot tarama döngüsü ve işlem girişi <b>aktif.</b>", reply_markup=ana_menu_olustur())
                    elif text == "⏸️ Botu Durdur":
                        config.BOT_CALISIYOR = False
                        telegram_bildir("⏸️ Bot tarama döngüsü <b>durduruldu.</b>", reply_markup=ana_menu_olustur())
        except Exception: time.sleep(5)

# --- 🎯 AKTİF AL/SAT MOTORU (BOLLINGER & RSI STRATEJİSİ) ---
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
                toplam_acik_pozisyon_sayisi = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])

            for symbol in yerel_semboller:
                if not config.BOT_CALISIYOR: break

                # 1. Coin'in güncel mum verisini API'den çek
                if not tek_coin_api_verisi_guncelle(symbol):
                    time.sleep(config.API_DELAY)
                    continue

                with data_lock:
                    v = dict(piyasa_verisi[symbol])
                    pos = dict(aktif_pozisyonlar[symbol])
                    cooldown_bitti_mi = (su_an_ts - son_islem_zamanlari.get(symbol, 0.0)) > config.COOLDOWN_SURESI

                if not v["anlik_fiyat"] or v["anlik_fiyat"] <= 0 or len(v["kapanislar"]) < config.BB_LEN:
                    time.sleep(config.API_DELAY)
                    continue
                
                anlik_fiyat = v["anlik_fiyat"]
                kapanislar_listesi = v["kapanislar"]

                # İndikatör Hesaplamaları
                ust_bantlar, orta_bantlar, alt_bantlar = bollinger_bands(kapanislar_listesi, config.BB_LEN, config.BB_MULT)
                ust_bant, orta_bant, alt_bant = ust_bantlar[-1], orta_bantlar[-1], alt_bantlar[-1]
                son_rsi = rsi_hesapla(kapanislar_listesi, config.RSI_LEN)

                # ==========================================
                # 🎯 ÇIKIŞ MANTIĞI (Yalnızca Sabit TP Kontrolü)
                # ==========================================
                if pos["aktif"]:
                    maliyet = pos["giris_fiyati"]
                    adet = pos["adet"]
                    if maliyet <= 0 or adet <= 0: continue

                    if pos["yon"] == "LONG":
                        anlik_kar_dolar = (anlik_fiyat - maliyet) * adet
                    else:  # SHORT
                        anlik_kar_dolar = (maliyet - anlik_fiyat) * adet

                    # Çıkış Koşulu: Sadece Sabit Dolar TP Hedefine ulaşıldığında kapatılır
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
                                telegram_bildir(f"💰 <b>{symbol.upper()} {pos['yon']} Kapatıldı!</b>\nKar: {round(anlik_kar_dolar, 3)}$\nFiyat: {anlik_fiyat}")
                        except Exception as e:
                            print(f"❌ Kapatma hatası ({symbol}): {e}")
                        finally:
                            with data_lock: emir_beklemede_durumu[symbol] = False

                # ==========================================
                # 📥 GİRİŞ MANTIĞI (BOLLINGER & RSI STRATEJİSİ)
                # ==========================================
                else:
                    # Risk ve Cooldown Kontrolü
                    if toplam_acik_pozisyon_sayisi < config.MAX_ACIK_POZISYON and cooldown_bitti_mi:
                        
                        long_girebilir_mi = anlik_fiyat <= alt_bant
                        short_girebilir_mi = anlik_fiyat >= ust_bant

                        # Opsiyonel RSI Filtresi Ekleme
                        if config.USE_RSI_FILTER:
                            long_girebilir_mi = long_girebilir_mi and (son_rsi <= config.RSI_OS)
                            short_girebilir_mi = short_girebilir_mi and (son_rsi >= config.RSI_OB)

                        gonderilecek_yon = None
                        if long_girebilir_mi:
                            gonderilecek_yon = "LONG"
                        elif short_girebilir_mi:
                            gonderilecek_yon = "SHORT"

                        if gonderilecek_yon:
                            with data_lock:
                                if emir_beklemede_durumu[symbol]: continue
                                emir_beklemede_durumu[symbol] = True

                            try:
                                # Lot Miktarı Hesaplama
                                marjin = config.ISLEM_MARJIN
                                kaldirac = config.KALDIRAC
                                toplam_islem_boyutu = marjin * kaldirac
                                nominal_qty = toplam_islem_boyutu / anlik_fiyat

                                # Lot Yuvarlama (Adet Hassasiyeti)
                                precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                                faktor = 10 ** precision
                                final_qty = math.floor(nominal_qty * faktor) / faktor if precision > 0 else int(nominal_qty)

                                if final_qty > 0:
                                    side = SIDE_BUY if gonderilecek_yon == "LONG" else SIDE_SELL
                                    client.futures_create_order(
                                        symbol=symbol.upper(), side=side, type=ORDER_TYPE_MARKET, quantity=final_qty
                                    )
                                    with data_lock:
                                        son_islem_zamanlari[symbol] = su_an_ts
                                        aktif_pozisyonlar[symbol] = {"aktif": True, "yon": gonderilecek_yon, "adet": final_qty, "giris_fiyati": anlik_fiyat}
                                        toplam_acik_pozisyon_sayisi += 1
                                    
                                    telegram_bildir(
                                        f"🚀 <b>{symbol.upper()} Pozisyon Açıldı!</b>\nYön: {gonderilecek_yon}\nMiktar: {final_qty}\nFiyat: {anlik_fiyat}\nRSI: {round(son_rsi, 2)}"
                                    )
                            except Exception as e:
                                print(f"❌ Pozisyon Açma Hatası ({symbol}): {e}")
                            finally:
                                with data_lock: emir_beklemede_durumu[symbol] = False
                
                time.sleep(config.API_DELAY)

        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            time.sleep(2.0)

# --- 🚀 ANA ÇALIŞTIRICI SİSTEM ---
if __name__ == "__main__":
    print("🎬 Squeeze Bollinger & RSI Botu Başlatılıyor...")
    
    hacimli_coinler = ilk_100_hacimli_coin_bul()
    print(f"📋 İlk etapta {len(hacimli_coinler)} adet hacimli coin tespit edildi.")
    
    eklenen_sayac = 0
    for c in hacimli_coinler:
        if kontrollu_coin_ekle(c):
            eklenen_sayac += 1
            
    print(f"✅ Filtreleri geçen {eklenen_sayac} coin tarama listesine eklendi.")
    
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=telegram_gelen_mesaj_dinleyici, daemon=True).start()
        telegram_bildir("🤖 <b>Bollinger & RSI Al/Sat Botu Başlatıldı! (Sadece Sabit TP ile Çıkış)</b>")
    
    print("⚡ Tüm sistemler aktif. Tarama ve işlem döngüsü başlıyor...")
    pure_api_tarama_dongusu()
