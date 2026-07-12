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

# 🌐 Proxy Sözlüğü Oluşturma (Sadece özel isteklerde kullanılacak)
requests_proxies = None
if PROXY_URL:
    print(f"🌐 Statik IP tüneli hazırlandı. Sadece alım/satım işlemlerinde tetiklenecek.")
    requests_proxies = {
        "http": PROXY_URL,
        "https": PROXY_URL
    }

# Binance istemcisini başlatıyoruz
client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

# 🛠️ KLASİK EMİR MOTORUNU PROXY İLE YANILTIYORUZ 
# Bu sayede koddaki 'client' üzerinden giden her emir otomatik olarak proxy kullanır.
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

config = TrendBotConfig()

SYMBOLS = [] 
piyasa_verisi = {}
aktif_pozisyonlar = {}
FUTURES_HASSASIYETLERI = {}

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

# 💸 PROXY KULLANMAZ (Kota Dostu)
def ilk_100_hacimli_coin_bul():
    try:
        ticker_url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        # proxies parametresi eklemedik, Railway'in kendi ipsinden çeker
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
        # Fiyat ve Market bilgisini kotalı proxy ile harcamamak için direkt çekiyoruz
        f_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(f_url, timeout=10).json()
        market_info = next((m for m in r.get("symbols", []) if m["symbol"] == coin_upper), None)
        if not market_info or market_info.get('status') != 'TRADING': return False
        
        # Kaldıraç ve Marjin ayarları API üzerinden yapıldığı için Statik IP ister (Proxy devrede)
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
        return True
    except Exception: return False

# 💸 PROXY KULLANMAZ (Kota Dostu)
def tüm_gecmis_verileri_guncelle():
    for s in SYMBOLS:
        try:
            # Candlestick (Mum) geçmişini proxy kullanmadan çekmek için Binance kütüphanesinin proxy'siz bir kopyası yerine
            # direkt url üzerinden çekebiliriz veya client'ın proxy ayarını anlık ezebiliriz. En temizi direkt çekmek:
            url = f"https://fapi.binance.com/fapi/v1/klines?symbol={s.upper()}&interval={config.TIMEFRAME}&limit=60"
            k = requests.get(url, timeout=10).json()
            with data_lock:
                piyasa_verisi[s]["kapanislar"] = [float(x[4]) for x in k]
                piyasa_verisi[s]["yuksekler"] = [float(x[2]) for x in k]
                piyasa_verisi[s]["dusukler"] = [float(x[3]) for x in k]
                piyasa_verisi[s]["anlik_fiyat"] = float(k[-1][4])
        except Exception: pass

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, timeout=5)
    except Exception: pass

# --- 🔐 HESAP VE EMİR TAKİP WEBSOCKET'İ (USER DATA STREAM) ---
def on_user_message(ws, message):
    try:
        data = json.loads(message)
        event_type = data.get("e")
        if event_type == "ACCOUNT_UPDATE":
            positions = data.get("a", {}).get("P", [])
            for p in positions:
                sym = p.get("s", "").lower()
                if sym in aktif_pozisyonlar:
                    pa = float(p.get("pa", 0))
                    ep = float(p.get("ep", 0))
                    with data_lock:
                        if pa == 0:
                            if aktif_pozisyonlar[sym]["aktif"]:
                                print(f"ℹ️ [WS Raporu] {sym.upper()} Pozisyonu kapandı.")
                                aktif_pozisyonlar[sym]["aktif"] = False
                        else:
                            aktif_pozisyonlar[sym]["aktif"] = True
                            aktif_pozisyonlar[sym]["yon"] = "LONG" if pa > 0 else "SHORT"
                            aktif_pozisyonlar[sym]["adet"] = abs(pa)
                            aktif_pozisyonlar[sym]["giris_fiyati"] = ep
    except Exception: pass

def listen_key_keep_alive():
    global listen_key
    while True:
        try:
            time.sleep(1800)
            if listen_key:
                client.futures_stream_keepalive(listenKey=listen_key)
        except Exception: pass

def start_user_data_ws():
    global listen_key
    try:
        # listenKey alma işlemi API üzerinden olduğu için Statik IP (Proxy) kullanır
        listen_key = client.futures_stream_get_listen_key()
        threading.Thread(target=listen_key_keep_alive, daemon=True).start()
        
        # WebSocket bağlantısının kendisinde proxy kullanmıyoruz (Zaten sadece hesap değişince veri akar, kota yemez)
        ws_url = f"wss://fstream.binance.com/ws/{listen_key}"
        ws = WebSocketApp(
            ws_url,
            on_message=on_user_message,
            on_error=lambda ws, err: print(f"⚠️ Hesap WS Hatası: {err}"),
            on_close=lambda ws, c, m: time.sleep(5)
        )
        ws.run_forever(reconnect=5)
    except Exception as e:
        print(f"❌ Hesap takip hattı başlatılamadı: {e}")

# --- 🎯 KOTA DOSTU HİBRİT MOTOR ---
def hibrit_tarama_dongusu():
    last_kline_sync = 0
    while True:
        try:
            if not config.BOT_CALISIYOR:
                time.sleep(2.0); continue
                
            su_an_ts = time.time()
            print(f"🔄 [{datetime.now().strftime('%H:%M:%S')}] Tarama yapılıyor... 15 saniyede bir proxy'siz fiyat çekiliyor.")

            if su_an_ts - last_kline_sync > 300:
                tüm_gecmis_verileri_guncelle()
                last_kline_sync = su_an_ts

            # 💸 PROXY KULLANMAZ: Fiyatları genel internetten çeker, kotayı korur!
            prices_raw = requests.get("https://fapi.binance.com/fapi/v1/ticker/price", timeout=10).json()
            prices_dict = {x["symbol"].lower(): float(x["price"]) for x in prices_raw}

            with data_lock:
                for s in SYMBOLS:
                    if s in prices_dict:
                        piyasa_verisi[s]["anlik_fiyat"] = prices_dict[s]

                guncel_acik_pozisyon_sayisi = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])

                for symbol in SYMBOLS:
                    v = piyasa_verisi[symbol]
                    pos = aktif_pozisyonlar[symbol]
                    
                    if len(v["kapanislar"]) < 20 or not v["anlik_fiyat"] or v["anlik_fiyat"] <= 0: 
                        continue
                    
                    anlik_fiyat = v["anlik_fiyat"]
                    
                    # 📈 GİRİŞ MANTIĞI (🔒 Otomatik Proxy ile Gönderilir)
                    if not pos["aktif"]:
                        if guncel_acik_pozisyon_sayisi >= config.MAX_ACIK_POZISYON: continue

                        ust_bant, _, alt_bant = bollinger_bands(v["kapanislar"])
                        rsi = rsi_hesapla(v["kapanislar"])
                        fib = fibonacci_seviyelerini_hesapla(v["yuksekler"], v["dusukler"])
                        
                        if not fib or "fib_618" not in fib: continue

                        precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                        qty = (config.ISLEM_MARJIN * config.KALDIRAC) / anlik_fiyat
                        qty = float(int(qty * (10 ** precision))) / (10 ** precision) if precision > 0 else int(qty)
                        if qty <= 0: continue

                        # LONG GİRİŞ
                        if anlik_fiyat <= alt_bant and rsi <= config.RSI_ASTR_SATIM:
                            if abs(anlik_fiyat - fib["fib_618"]) / anlik_fiyat < 0.006 or abs(anlik_fiyat - fib["fib_786"]) / anlik_fiyat < 0.006:
                                try:
                                    client.futures_create_order(symbol=symbol.upper(), side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
                                    telegram_bildir(f"⚡ <b>{symbol.upper()} LONG Sinyali Gönderildi</b>")
                                except Exception: pass
                                    
                        # SHORT GİRİŞ
                        elif anlik_fiyat >= ust_bant and rsi >= config.RSI_ASTR_ALIM:
                            if abs(anlik_fiyat - fib["fib_236"]) / anlik_fiyat < 0.006 or abs(anlik_fiyat - fib["fib_382"]) / anlik_fiyat < 0.006:
                                try:
                                    client.futures_create_order(symbol=symbol.upper(), side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=qty)
                                    telegram_bildir(f"⚡ <b>{symbol.upper()} SHORT Sinyali Gönderildi</b>")
                                except Exception: pass
                    
                    # 🎯 ÇIKIŞ MANTIĞI (🔒 Otomatik Proxy ile Gönderilir)
                    else:
                        maliyet = pos["giris_fiyati"]
                        if maliyet <= 0: continue
                        fark_yuzde = (anlik_fiyat - maliyet) / maliyet
                        
                        # LONG ÇIKIŞ
                        if pos["yon"] == "LONG" and fark_yuzde >= config.TAHMINI_TP_YUZDE:
                            try:
                                client.futures_create_order(symbol=symbol.upper(), side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=pos["adet"])
                                telegram_bildir(f"🎯 <b>{symbol.upper()} LONG Kâr Alındı!</b>")
                            except Exception: pass
                                    
                        # SHORT ÇIKIŞ
                        elif pos["yon"] == "SHORT" and fark_yuzde <= -config.TAHMINI_TP_YUZDE:
                            try:
                                try:
                                    client.futures_create_order(symbol=symbol.upper(), side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=pos["adet"])
                                    telegram_bildir(f"🎯 <b>{symbol.upper()} SHORT Kâr Alındı!</b>")
                                except Exception: pass
                            except Exception: pass

        except Exception: traceback.print_exc()
        time.sleep(15.0)

if __name__ == "__main__":
    aday_listesi = ilk_100_hacimli_coin_bul()
    if not aday_listesi: exit()
        
    for coin in aday_listesi: kontrollu_coin_ekle(coin)
    tüm_gecmis_verileri_guncelle()
    
    # Arka planda hesap hareketlerini izleyen hafif hattı açıyoruz
    threading.Thread(target=start_user_data_ws, daemon=True).start()
    
    time.sleep(2.0)
    telegram_bildir(f"🤖 <b>Kota Korumalı Akıllı Bot Başladı!</b>")
    hibrit_tarama_dongusu()
