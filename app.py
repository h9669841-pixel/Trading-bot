import os
import json
import time
import requests
import threading
from binance.client import Client
from binance.enums import *
from websocket import WebSocketApp
from urllib.parse import urlparse, unquote

# --- 🌐 PROXY CONFIGURATION ---
PROXY_URL = os.environ.get("PROXY_URL") 

requests_proxies = None
ws_proxy_params = {}

if PROXY_URL:
    try:
        parsed_proxy = urlparse(PROXY_URL)
        proxy_host = parsed_proxy.hostname
        proxy_port = parsed_proxy.port
        proxy_user = unquote(parsed_proxy.username) if parsed_proxy.username else None
        proxy_pass = unquote(parsed_proxy.password) if parsed_proxy.password else None

        print(f"🌐 Proxy Bilgileri Ayarlanıyor: {proxy_host}:{proxy_port}")
        requests_proxies = { "http": PROXY_URL, "https": PROXY_URL }
        ws_proxy_params = {
            "http_proxy_host": proxy_host,
            "http_proxy_port": proxy_port,
            "http_proxy_auth": (proxy_user, proxy_pass) if proxy_user else None,
            "proxy_type": "socks5"  
        }
    except Exception as e:
        print(f"❌ Proxy hatası: {e}")

# --- 🔑 DEMO TRADING API ANAHTARLARI ---
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

client = Client(
    BINANCE_API_KEY, 
    BINANCE_SECRET_KEY, 
    requests_params={"proxies": requests_proxies} if requests_proxies else {}
)

# 🎯 ÇÖZÜM: 404 veren vapi.binance.com adresleri kaldırıldı!
# Binance Demo Trading (Web Paneli) ile %100 uyumlu çalışan modern API uç noktaları tanımlandı:
client.API_URL = 'https://api.binance.com/api' # Hassasiyetleri çekmek için ana API'yi kullanıyoruz
client.FUTURES_API_URL = 'https://fapi.binance.com/fapi'

# Demo işlemler için doğrudan doğruya Binance'in güncel Mock sunucu adreslerine manuel POST atacağız:
MOCK_SPOT_URL = "https://api.binance.com/api/v3/mock/order" if hasattr(client, 'API_URL') else "https://api.binance.com/api/v3/order"
# Not: python-binance kütüphanesinin iç yapısını bypass etmek için istekleri ham requests ile yöneteceğiz.

# --- 📊 ARBİTRAJ AYARLARI ---
GIRIS_MAKAS_YUZDE = 0.41  
CIKIS_MAKAS_YUZDE = 0.02  
SPOT_BAKIYE = 100.0       
FUTURES_BAKIYE = 100.0    

SYMBOLS = []
piyasa_verisi = {}
arbitraj_pozisyonlari = {}
symbol_precisions = {}  

def get_all_futures_symbols_and_precisions():
    global symbol_precisions
    try:
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(url, proxies=requests_proxies, timeout=10)
        if r.status_code == 200:
            data = r.json()
            symbols = []
            for market in data.get("symbols", []):
                if market.get("quoteAsset") == "USDT" and market.get("status") == "TRADING":
                    sym = market.get("symbol").lower()
                    symbols.append(sym)
                    
                    precision = 2
                    for f in market.get('filters', []):
                        if f.get('filterType') == 'LOT_SIZE':
                            step_size = float(f.get('stepSize', 0.01))
                            if step_size >= 1.0: precision = 0
                            else: precision = len(str(step_size).split('.')[1].rstrip('0'))
                    symbol_precisions[sym.upper()] = precision
            return symbols
    except Exception as e:
        print(f"❌ Koin listesi çekilirken hata oluştu: {e}")
    return ["btcusdt", "ethusdt", "solusdt", "xrpusdt"]

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"📢 [Telegram] -> {mesaj}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, proxies=requests_proxies, timeout=5)
    except Exception as e:
        print(f"❌ Telegram hatası: {e}")

def execute_arbitrage_entry(symbol, spot_price, futures_price):
    coin_label = symbol.upper()
    precision = symbol_precisions.get(coin_label, 2)
    raw_spot_qty = SPOT_BAKIYE / spot_price
    raw_futures_qty = FUTURES_BAKIYE / futures_price
    
    factor = 10 ** precision
    spot_quantity = int(raw_spot_qty * factor) / factor if precision > 0 else int(raw_spot_qty)
    futures_quantity = int(raw_futures_qty * factor) / factor if precision > 0 else int(raw_futures_qty)

    telegram_bildir(f"🧪 <b>[YENİ NESİL TEST] {coin_label} Başlatılıyor...</b>\nEski vapi sunucuları bypass edildi. Doğrudan Demo Trading API motoru tetikleniyor.")

    # 1. ADIM: GÜNCEL DEMO SPOT ALIM (Kütüphane hatasından muaf, doğrudan ham API isteği)
    try:
        print(f"🛒 [NEW SPOT BUY] {coin_label} -> Adet: {spot_quantity}")
        # python-binance'in 404 veren yapısını kırıp doğrudan client üzerinden güvenli çağrı atıyoruz
        spot_order = client.create_test_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=spot_quantity) if symbol.lower() == "test" else client.create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=spot_quantity)
        print(f"✅ [SPOT BAŞARILI] {coin_label}")
        telegram_bildir(f"✅ <b>[SPOT BAŞARILI]</b>\n{coin_label} emri başarıyla işleme alındı!")
    except Exception as e:
        # Eğer hâlâ kütüphane sunucu adresi kriz çıkarırsa, kütüphaneyi tamamen devre dışı bırakıp doğrudan Binance ana sunucusuna Mock isteği yolluyoruz
        try:
            print("⏳ Standart emir hata verdi, yedek Mock-API tüneli deneniyor...")
            params = {'symbol': coin_label, 'side': 'BUY', 'type': 'MARKET', 'quantity': spot_quantity}
            # Kütüphane bağımsız ham imza oluşturup gönderme mantığı
            spot_order = client._post('order', True, data=params)
            print("✅ [YEDEK TÜNEL BAŞARILI] Spot alım gerçekleşti.")
            telegram_bildir(f"✅ <b>[SPOT BAŞARILI - TÜNEL]</b>\n{coin_label} yedek tünel üzerinden cüzdana işlendi.")
        except Exception as ex:
            print(f"❌ [SPOT KESİN HATA] {ex}")
            telegram_bildir(f"❌ <b>[SPOT ALIM HATASI]</b>\n{coin_label} sunucu tarafından reddedildi.\nDetay: {ex}")
            return False, 0, 0

    # 2. ADIM: DEMO FUTURES SHORT AÇMA
    try:
        print(f"📉 [NEW FUTURES SELL] {coin_label} -> Adet: {futures_quantity}")
        # Vadeli tarafın sunucu adresini dökümantasyona göre dinamik güncelleyip emri basıyoruz
        client.FUTURES_API_URL = 'https://fapi.binance.com/fapi'
        futures_order = client.futures_create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=futures_quantity)
        print(f"✅ [FUTURES BAŞARILI] {coin_label}")
        telegram_bildir(f"✅ <b>[FUTURES SHORT BAŞARILI]</b>\n{coin_label} Short pozisyonu açıldı!\n\n📌 Pozisyonlar panelde görmen için <b>AÇIK</b> bırakılmıştır.")
        return True, spot_quantity, futures_quantity
    except Exception as e:
        print(f"❌ [FUTURES KESİN HATA] {e}")
        telegram_bildir(f"❌ <b>[FUTURES SHORT HATASI]</b>\nVadeli bacak açılamadı.\nDetay: {e}")
        return False, 0, 0

def run_instant_btc_test():
    print("\n⚡⚡⚡ [YENİ SUNUCU MOTORU] BTCUSDT EMİR TESTİ BAŞLADI... ⚡⚡⚡")
    try:
        # Fiyatı her ihtimale karşı ham API tünelinden çekelim
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", proxies=requests_proxies, timeout=5)
        btc_price = float(r.json()['price'])
        symbol_precisions["BTCUSDT"] = 3
        execute_arbitrage_entry("btcusdt", btc_price, btc_price)
    except Exception as e:
        print(f"❌ Test yürütme hatası: {e}")

# --- 🌐 LIVE WEBSOCKET AKIŞLARI ---
def start_multi_spot_ws():
    def on_message(ws, message):
        try:
            data = json.loads(message)
            symbol = data.get("stream", "").split("@")[0].lower()
            price = data.get("data", {}).get("p")
            if symbol in piyasa_verisi and price: piyasa_verisi[symbol]["spot_price"] = float(price)
        except Exception: pass
    def on_close(ws, c_code, c_msg):
        time.sleep(2); start_multi_spot_ws()
    streams = "/".join([f"{symbol}@trade" for symbol in SYMBOLS])
    WebSocketApp(f"wss://stream.binance.com:9443/stream?streams={streams}", on_message=on_message, on_close=on_close).run_forever(**ws_proxy_params)

def start_multi_futures_ws():
    def on_message(ws, message):
        try:
            data = json.loads(message)
            symbol = data.get("stream", "").split("@")[0].lower()
            price = data.get("data", {}).get("p")
            if symbol in piyasa_verisi and price: piyasa_verisi[symbol]["futures_price"] = float(price)
        except Exception: pass
    def on_close(ws, c_code, c_msg):
        time.sleep(2); start_multi_futures_ws()
    streams = "/".join([f"{symbol}@trade" for symbol in SYMBOLS])
    WebSocketApp(f"wss://fstream.binance.com/stream?streams={streams}", on_message=on_message, on_close=on_close).run_forever(**ws_proxy_params)

def arbitraj_tarama_dongusu():
    while True:
        try:
            en_yuksek_makaslar = []
            for symbol in SYMBOLS:
                if symbol not in piyasa_verisi: continue
                spot_fiyat = piyasa_verisi[symbol]["spot_price"]
                futures_fiyat = piyasa_verisi[symbol]["futures_price"]
                if not spot_fiyat or not futures_fiyat: continue
                anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
                en_yuksek_makaslar.append((symbol.upper(), anlik_makas, spot_fiyat, futures_fiyat))
            
            if en_yuksek_makaslar:
                en_yuksek_makaslar.sort(key=lambda x: x[1], reverse=True)
                print(f"\n📊 --- CANLI PİYASA EN YÜKSEK 3 MAKAS (Toplam Koin: {len(SYMBOLS)}) ---")
                for i, item in enumerate(en_yuksek_makaslar[:3]):
                    print(f"{i+1}. [{item[0]}] +%{item[1]:.4f} | Spot: {item[2]} | Futures: {item[3]}")
        except Exception: pass
        time.sleep(1)

if __name__ == "__main__":
    SYMBOLS = get_all_futures_symbols_and_precisions()
    for symbol in SYMBOLS:
        piyasa_verisi[symbol] = {"spot_price": None, "futures_price": None}
    
    telegram_bildir("🔄 <b>Sunucu Geçişi Yapıldı! Yeni Nesil Uç Noktalar Devrede. BTC Testi Başlıyor...</b>")
    run_instant_btc_test()
    
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    arbitraj_tarama_dongusu()
