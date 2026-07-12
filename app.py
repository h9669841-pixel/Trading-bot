import os
import json
import time
import requests
import threading
import traceback
import math
from datetime import datetime
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException
from websocket import WebSocketApp

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
        self.RSI_PERIYOD = 14
        self.RSI_ASTR_SATIM = 32       
        self.RSI_ASTR_ALIM = 68        
        self.BOLLINGER_PERIYOD = 20
        self.BOLLINGER_STANDART_SAPMA = 2
        self.TAHMINI_TP_YUZDE = 0.010   
        self.BOT_CALISIYOR = True
        self.COOLDOWN_SURESI = 300     # ⏱️ 5 Dakika zaman kilidi

config = TrendBotConfig()

SYMBOLS = [] 
piyasa_verisi = {}
aktif_pozisyonlar = {}
FUTURES_HASSASIYETLERI = {}
son_islem_zamanlari = {}  

data_lock = threading.Lock()
listen_key = None

# --- 🛠️ TEKNİK ANALİZ MOTORU ---
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

def bollinger_bands(kapanislar, periyod=20, standart_sapma=2):
    if len(kapanislar) < periyod: return 0.0, 0.0, 0.0
    veri = kapanislar[-periyod:]
    orta_bant = sum(veri) / periyod
    varyans = sum((x - orta_bant) ** 2 for x in veri) / periyod
    if varyans <= 0: varyans = 0.00000001  
    ust_bant = orta_bant + (standart_sapma * math.sqrt(varyans))
    alt_bant = orta_bant - (standart_sapma * math.sqrt(varyans))
    return ust_bant, orta_bant, alt_bant

def fibonacci_seviyelerini_hesapla(yuksekler, dusukler):
    if not yuksekler or not dusukler: return {}
    en_yuksek = max(yuksekler[-40:])
    en_dusuk = min(dusukler[-40:])
    fark = en_yuksek - en_dusuk
    if fark <= 0: fark = 0.00000001  
    return {
        "fib_618": en_yuksek - (0.618 * fark),
        "fib_786": en_yuksek - (0.786 * fark),
        "fib_236": en_yuksek - (0.236 * fark),
        "fib_382": en_yuksek - (0.382 * fark)
    }

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
        
        client.futures_change_leverage(symbol=coin_upper, leverage=config.KALDIRAC)
        try: client.futures_change_margin_type(symbol=coin_upper, marginType="ISOLATED")
        except BinanceAPIException as e:
            if "No need to change" not in e.message: pass

        for f in market_info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step_size_str = str(f['stepSize']).rstrip('0')
                precision = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
                FUTURES_HASSASIYETLERI[coin_lower] = precision

        with data_lock:
            SYMBOLS.append(coin_lower)
            piyasa_verisi[coin_lower] = {"anlik_fiyat": None, "kapanislar": [], "yuksekler": [], "dusukler": []}
            aktif_pozisyonlar[coin_lower] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0}
            son_islem_zamanlari[coin_lower] = 0.0  
        return True
    except Exception: return False

# 🔍 MEVCUT AÇIK POZİSYONLARI ZORUNLU SENKRONİZE EDEN YENİ SİHİRLİ FONKSİYON
def acik_pozisyonlari_binanceden_guncelle():
    """WebSocket gecikmelerine karşı API'den açık pozisyonları zorla çeker."""
    try:
        # Proxy ayarları atanmış client üzerinden güvenli istek atıyoruz
        pozisyonlar = client.futures_position_information()
        with data_lock:
            # Önce tüm listeyi temizle (kapandıysa sıfırlansın)
            for s in SYMBOLS:
                aktif_pozisyonlar[s] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0}
            
            # Sadece adeti 0'dan farklı olan aktif pozları haritaya işle
            for p in pozisyonlar:
                sym = p.get("symbol", "").lower()
                if sym in aktif_pozisyonlar:
                    amt = float(p.get("positionAmt", 0))
                    entry_price = float(p.get("entryPrice", 0))
                    if amt != 0:
                        aktif_pozisyonlar[sym]["aktif"] = True
                        aktif_pozisyonlar[sym]["yon"] = "LONG" if amt > 0 else "SHORT"
                        aktif_pozisyonlar[sym]["adet"] = abs(amt)
                        aktif_pozisyonlar[sym]["giris_fiyati"] = entry_price
        print("🔄 [API Senkronizasyonu] Mevcut açık pozisyonlar başarıyla güncellendi.")
    except Exception as e:
        print(f"❌ Pozisyon senkronizasyon hatası: {e}")

def tüm_gecmis_verileri_guncelle():
    for s in SYMBOLS:
        try:
            url = f"https://fapi.binance.com/fapi/v1/klines?symbol={s.upper()}&interval={config.TIMEFRAME}&limit=60"
            k = requests.get(url, timeout=10).json()
            with data_lock:
                piyasa_verisi[s]["kapanislar"] = [float(x[4]) for x in k]
                piyasa_verisi[s]["yuksekler"] = [float(x[2]) for x in k]
                piyasa_verisi[s]["dusukler"] = [float(x[3]) for x in k]
                piyasa_verisi[s]["anlik_fiyat"] = float(k[-1][4])
        except Exception: pass

# --- 🎛️ TELEGRAM KLAVYE MENÜSÜ VE MESAJ YÖNETİMİ ---
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
    # Rapor üretilmeden hemen önce API'den bir kez daha zorunlu senkronizasyon yapalım ki veriler %100 doğru olsun
    acik_pozisyonlari_binanceden_guncelle()
    
    with data_lock:
        acik_pozlar = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])
        durum_str = "🟢 İzole Mod Aktif" if config.BOT_CALISIYOR else "🔴 Sistem Durduruldu"
        poz_buyuklugu = config.ISLEM_MARJIN * config.KALDIRAC

        rapor = (
            f"⚙️ <b>İzole 20x Avcı Botu</b>\n"
            f"• Sistem: {durum_str}\n"
            f"• Marjin: {config.ISLEM_MARJIN:.1f} USDT\n"
            f"• Kaldıraç: {config.KALDIRAC}x (İZOLE)\n"
            f"• Poz Büyüklüğü: {poz_buyuklugu:.1f} USDT\n"
            f"• Risk Limiti: {acik_pozlar}/{config.MAX_ACIK_POZISYON} Pozisyon\n"
            f"• TP Hedefi: %{config.TAHMINI_TP_YUZDE * 100:.1f}\n\n"
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

# --- 🔐 HESAP WEBSOCKET'İ ---
def on_user_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("e") == "ACCOUNT_UPDATE":
            positions = data.get("a", {}).get("P", [])
            for p in positions:
                sym = p.get("s", "").lower()
                if sym in aktif_pozisyonlar:
                    pa = float(p.get("pa", 0))
                    ep = float(p.get("ep", 0))
                    with data_lock:
                        if pa == 0:
                            if aktif_pozisyonlar[sym]["aktif"]:
                                aktif_pozisyonlar[sym]["aktif"] = False
                        else:
                            aktif_pozisyonlar[sym]["aktif"] = True
                            aktif_pozisyonlar[sym]["yon"] = "LONG" if pa > 0 else "SHORT"
                            aktif_pozisyonlar[sym]["adet"] = abs(pa)
                            aktif_pozisyonlar[sym]["giris_fiyati"] = ep
    except Exception: pass

def start_user_data_ws():
    global listen_key
    try:
        listen_key = client.futures_stream_get_listen_key()
        threading.Thread(target=lambda: (time.sleep(1800), client.futures_stream_keepalive(listenKey=listen_key)), daemon=True).start()
        ws = WebSocketApp(f"wss://fstream.binance.com/ws/{listen_key}", on_message=on_user_message, on_close=lambda ws,c,m: time.sleep(5))
        ws.run_forever(reconnect=5)
    except Exception: pass

# --- 🎯 KOTA DOSTU HİBRİT MOTOR ---
def hibrit_tarama_dongusu():
    last_kline_sync = 0
    while True:
        try:
            if not config.BOT_CALISIYOR:
                time.sleep(2.0); continue
                
            su_an_ts = time.time()
            if su_an_ts - last_kline_sync > 300:
                tüm_gecmis_verileri_guncelle()
                acik_pozisyonlari_binanceden_guncelle()  # 🔄 Her 5 dakikada bir pozisyonları API'den zorunlu check et
                last_kline_sync = su_an_ts

            prices_raw = requests.get("https://fapi.binance.com/fapi/v1/ticker/price", timeout=10).json()
            prices_dict = {x["symbol"].lower(): float(x["price"]) for x in prices_raw}

            with data_lock:
                for s in SYMBOLS:
                    if s in prices_dict: piyasa_verisi[s]["anlik_fiyat"] = prices_dict[s]

                guncel_acik_pozisyon_sayisi = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])

                for symbol in SYMBOLS:
                    v = piyasa_verisi[symbol]
                    pos = aktif_pozisyonlar[symbol]
                    
                    if len(v["kapanislar"]) < 20 or not v["anlik_fiyat"] or v["anlik_fiyat"] <= 0: continue
                    anlik_fiyat = v["anlik_fiyat"]
                    
                    if su_an_ts - son_islem_zamanlari[symbol] < config.COOLDOWN_SURESI:
                        continue

                    # 📈 GİRİŞ VE EKLEME MANTIĞI
                    if guncel_acik_pozisyon_sayisi < config.MAX_ACIK_POZISYON:
                        ust_bant, _, alt_bant = bollinger_bands(v["kapanislar"])
                        rsi = rsi_hesapla(v["kapanislar"])
                        fib = fibonacci_seviyelerini_hesapla(v["yuksekler"], v["dusukler"])
                        
                        if not fib or "fib_618" not in fib: continue

                        precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                        qty = (config.ISLEM_MARJIN * config.KALDIRAC) / anlik_fiyat
                        qty = float(int(qty * (10 ** precision))) / (10 ** precision) if precision > 0 else int(qty)
                        if qty <= 0: continue

                        # LONG EMİR
                        if anlik_fiyat <= alt_bant and rsi <= config.RSI_ASTR_SATIM and pos["yon"] != "SHORT":
                            if abs(anlik_fiyat - fib["fib_618"]) / anlik_fiyat < 0.006 or abs(anlik_fiyat - fib["fib_786"]) / anlik_fiyat < 0.006:
                                try:
                                    client.futures_create_order(symbol=symbol.upper(), side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
                                    son_islem_zamanlari[symbol] = su_an_ts  
                                    islem_tipi = "Ekleme Yapıldı" if pos["aktif"] else "Yeni Pozisyon"
                                    telegram_bildir(f"🚀 <b>{symbol.upper()} LONG {islem_tipi}!</b>\nFiyat: {anlik_fiyat}")
                                except Exception: pass
                                    
                        # SHORT EMİR
                        elif anlik_fiyat >= ust_bant and rsi >= config.RSI_ASTR_ALIM and pos["yon"] != "LONG":
                            if abs(anlik_fiyat - fib["fib_236"]) / anlik_fiyat < 0.006 or abs(anlik_fiyat - fib["fib_382"]) / anlik_fiyat < 0.006:
                                try:
                                    client.futures_create_order(symbol=symbol.upper(), side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=qty)
                                    son_islem_zamanlari[symbol] = su_an_ts  
                                    islem_tipi = "Ekleme Yapıldı" if pos["aktif"] else "Yeni Pozisyon"
                                    telegram_bildir(f"🚀 <b>{symbol.upper()} SHORT {islem_tipi}!</b>\nFiyat: {anlik_fiyat}")
                                except Exception: pass
                    
                    # 🎯 ÇIKIŞ MANTIĞI
                    if pos["aktif"]:
                        maliyet = pos["giris_fiyati"]
                        if maliyet <= 0: continue
                        fark_yuzde = (anlik_fiyat - maliyet) / maliyet
                        
                        if pos["yon"] == "LONG" and fark_yuzde >= config.TAHMINI_TP_YUZDE:
                            try:
                                client.futures_create_order(symbol=symbol.upper(), side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=pos["adet"])
                                son_islem_zamanlari[symbol] = 0.0  
                                telegram_bildir(f"💰 <b>{symbol.upper()} LONG Kar Alındı!</b>\nNet Kar: %{config.TAHMINI_TP_YUZDE * 100:.1f}")
                            except Exception: pass
                                    
                        elif pos["yon"] == "SHORT" and fark_yuzde <= -config.TAHMINI_TP_YUZDE:
                            try:
                                client.futures_create_order(symbol=symbol.upper(), side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=pos["adet"])
                                son_islem_zamanlari[symbol] = 0.0  
                                telegram_bildir(f"💰 <b>{symbol.upper()} SHORT Kar Alındı!</b>\nNet Kar: %{config.TAHMINI_TP_YUZDE * 100:.1f}")
                            except Exception: pass

        except Exception: traceback.print_exc()
        time.sleep(1.0)

if __name__ == "__main__":
    aday_listesi = ilk_100_hacimli_coin_bul()
    if not aday_listesi: exit()
    for coin in aday_listesi: kontrollu_coin_ekle(coin)
    
    # 🚀 İLK AÇILIŞTA MEVCUT POZİSYONLARI APIden ÇEKİP HAFIZAYI DOLDURUYORUZ
    tüm_gecmis_verileri_guncelle()
    acik_pozisyonlari_binanceden_guncelle()
    
    threading.Thread(target=start_user_data_ws, daemon=True).start()
    threading.Thread(target=telegram_gelen_mesaj_dinleyici, daemon=True).start()
    
    time.sleep(2.0)
    telegram_bildir(telegram_canli_rapor_uret(), reply_markup=ana_menu_olustur())
    hibrit_tarama_dongusu()
