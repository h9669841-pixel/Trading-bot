import os
import json
import time
import requests
import threading
import traceback
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException
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
        print(f"❌ Proxy ayrıştırma hatası: {e}")

# --- 🔑 SİMÜLASYON (MOCK) API ANAHTARLARI ---
# demo.binance.com üzerinden aldığın API anahtarları buralara gelecek
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ⚠️ UYARI: python-binance kütüphanesinin standart 'testnet=True' modu eski testnet'e gider.
# demo.binance.com (Mock) hesabı için testnet=False kalmalı, ancak isteklerin yönlendirileceği base_url değiştirilmelidir!
client = Client(
    BINANCE_API_KEY, 
    BINANCE_SECRET_KEY, 
    requests_params={"proxies": requests_proxies} if requests_proxies else {}
)

# 🎯 SİMÜLASYON (MOCK TRADING) DUVARLARI: API isteklerini sanal hesaba yönlendiriyoruz
client.API_URL = 'https://vapi.binance.com/api'          # Sanal Spot API Uç Noktası
client.MARGIN_API_URL = 'https://vapi.binance.com/nvapi'
client.FUTURES_API_URL = 'https://vapi.binance.com/fapi' # Sanal Vadeli İşlemler API Uç Noktası

# --- 📊 ARBİTRAJ STRATEJİ VE HESAP AYARLARI ---
GIRIS_MAKAS_YUZDE = 0.41  
CIKIS_MAKAS_YUZDE = 0.02  

# 💰 Sanal hesabında 10k$ olduğu için işlem hacimlerini 100$ seviyesine çıkarttık
SPOT_BAKIYE = 100.0       
FUTURES_BAKIYE = 100.0    

SPOT_FEE_RATE = 0.0750 / 100     
FUTURES_FEE_RATE = 0.0450 / 100  

SYMBOLS = []
piyasa_verisi = {}
arbitraj_pozisyonlari = {}

def get_all_futures_symbols():
    try:
        # Fiyat karşılaştırması ve koin havuzu için listeyi canlı borsadan alıyoruz
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(url, proxies=requests_proxies, timeout=10)
        if r.status_code == 200:
            data = r.json()
            symbols = []
            for market in data.get("symbols", []):
                if market.get("quoteAsset") == "USDT" and market.get("status") == "TRADING":
                    symbols.append(market.get("symbol").lower())
            return symbols
    except Exception as e:
        print(f"❌ Koin listesi çekilirken hata oluştu: {e}")
    return ["btcusdt", "ethusdt", "solusdt", "xrpusdt"]

def set_all_leverages():
    print("⏳ Sanal hesaptaki kaldıraçlar 1x olarak senkronize ediliyor...")
    for symbol in SYMBOLS[:50]: # İlk 50 koin için kaldıraç ayarla
        try:
            client.futures_change_leverage(symbol=symbol.upper(), leverage=1)
            time.sleep(0.2) 
        except Exception as e:
            pass

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"📢 [Telegram] -> {mesaj}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, proxies=requests_proxies, timeout=5)
    except Exception as e:
        print(f"❌ Telegram hatası: {e}")

def net_kar_hesapla(giris_makas, kapanis_makas):
    brut_oran_farki = giris_makas - kapanis_makas
    brut_kazanc_usdt = SPOT_BAKIYE * (brut_oran_farki / 100)
    toplam_kesinti_usdt = ((SPOT_BAKIYE * SPOT_FEE_RATE) * 2) + ((FUTURES_BAKIYE * FUTURES_FEE_RATE) * 2)
    return brut_kazanc_usdt, toplam_kesinti_usdt, brut_kazanc_usdt - toplam_kesinti_usdt

def get_lot_size_precision(symbol):
    try:
        # Hassasiyet bilgisi vapi üzerinden sanal borsadan sorgulanıyor
        info = client.get_symbol_info(symbol.upper())
        if info and 'filters' in info:
            for f in info['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    step_size = float(f['stepSize'])
                    if step_size >= 1.0: return 0
                    return len(str(step_size).split('.')[1].rstrip('0'))
    except Exception as e:
        pass
    return 2

# --- 🎯 GERÇEK ZAMANLI S SİMÜLASYON EMİR EMİR MOTORU ---
def execute_arbitrage_entry(symbol, spot_price, futures_price):
    coin_label = symbol.upper()
    try:
        precision = get_lot_size_precision(coin_label)
        raw_spot_qty = SPOT_BAKIYE / spot_price
        raw_futures_qty = FUTURES_BAKIYE / futures_price
        
        factor = 10 ** precision
        spot_quantity = int(raw_spot_qty * factor) / factor if precision > 0 else int(raw_spot_qty)
        futures_quantity = int(raw_futures_qty * factor) / factor if precision > 0 else int(raw_futures_qty)

        print(f"🛒 {coin_label} Sanal Emir Gönderiliyor.. Adetler -> Spot: {spot_quantity}, Futures: {futures_quantity}")

        # Emirler vapi.binance.com üzerinden doğrudan demo hesabına gider
        spot_order = client.create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=spot_quantity)
        futures_order = client.futures_create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=futures_quantity)
        return True, spot_quantity, futures_quantity
    except BinanceAPIException as e:
        err_msg = f"❌ <b>Sanal Hesap İşlem Hatası ({coin_label}):</b>\n{e.message}"
        print(err_msg)
        telegram_bildir(err_msg)
        return False, 0, 0
    except Exception as e:
        return False, 0, 0

def execute_arbitrage_exit(symbol, spot_qty, futures_qty):
    coin_label = symbol.upper()
    try:
        client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=spot_qty)
        client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=futures_qty)
        return True
    except Exception as e:
        print(f"❌ Sanal Çıkış Hatası: {e}")
        return False

# --- 🌐 LIVE WEBSOCKET AKIŞLARI (GERÇEK BORSADAN ANLIK VERİ BESLEME) ---
def start_multi_spot_ws():
    def on_message(ws, message):
        data = json.loads(message)
        symbol = data.get("stream", "").split("@")[0].lower()
        price = data.get("data", {}).get("p")
        if symbol in piyasa_verisi and price:
            piyasa_verisi[symbol]["spot_price"] = float(price)

    def on_close(ws, c_code, c_msg):
        time.sleep(2)
        start_multi_spot_ws()

    # Gerçek borsanın en likit 50 koinini takibe alıyoruz
    active_symbols = [s for s in SYMBOLS[:50]]
    streams = "/".join([f"{symbol}@trade" for symbol in active_symbols])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    
    print(f"📡 Canlı Spot Veri Akışı Bağlandı: {len(active_symbols)} koin dinleniyor...")
    WebSocketApp(url, on_message=on_message, on_close=on_close).run_forever(**ws_proxy_params)

def start_multi_futures_ws():
    def on_message(ws, message):
        data = json.loads(message)
        symbol = data.get("stream", "").split("@")[0].lower()
        price = data.get("data", {}).get("p")
        if symbol in piyasa_verisi and price:
            piyasa_verisi[symbol]["futures_price"] = float(price)

    def on_close(ws, c_code, c_msg):
        time.sleep(2)
        start_multi_futures_ws()

    active_symbols = [s for s in SYMBOLS[:50]]
    streams = "/".join([f"{symbol}@trade" for symbol in active_symbols])
    url = f"wss://fstream.binance.com/stream?streams={streams}"
    
    print(f"📡 Canlı Futures Veri Akışı Bağlandı: {len(active_symbols)} koin dinleniyor...")
    WebSocketApp(url, on_message=on_message, on_close=on_close).run_forever(**ws_proxy_params)

def arbitraj_tarama_dongusu():
    global arbitraj_pozisyonlari
    while True:
        try:
            en_yuksek_makaslar = []
            tarama_listesi = SYMBOLS[:50]
            
            for symbol in tarama_listesi:
                if symbol not in piyasa_verisi: continue
                
                spot_fiyat = piyasa_verisi[symbol]["spot_price"]
                futures_fiyat = piyasa_verisi[symbol]["futures_price"]
                
                if not spot_fiyat or not futures_fiyat: continue

                anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
                coin_label = symbol.upper()
                en_yuksek_makaslar.append((coin_label, anlik_makas, spot_fiyat, futures_fiyat))

                pos = arbitraj_pozisyonlari[symbol]

                if not pos["aktif"]:
                    if anlik_makas >= GIRIS_MAKAS_YUZDE:
                        brut, kesinti, net = net_kar_hesapla(anlik_makas, CIKIS_MAKAS_YUZDE)
                        if net <= 0: continue
                        
                        basarili, s_qty, f_qty = execute_arbitrage_entry(symbol, spot_fiyat, futures_fiyat)
                        if basarili:
                            pos.update({"aktif": True, "giris_makas": anlik_makas, "spot_adet": s_qty, "futures_adet": f_qty})
                            telegram_bildir(f"🤖 <b>DEMO HESAP: İŞLEME GİRİLDİ</b>\n\n📊 <b>Koin:</b> {coin_label}\n⚡ <b>Canlı Makas:</b> +%{anlik_makas:.4f}\n💵 <b>Tahmini Net Kâr:</b> {net:.4f} USDT")
                else:
                    if anlik_makas <= CIKIS_MAKAS_YUZDE:
                        if execute_arbitrage_exit(symbol, pos["spot_adet"], pos["futures_adet"]):
                            brut, kesinti, net = net_kar_hesapla(pos["giris_makas"], anlik_makas)
                            telegram_bildir(f"🤝 <b>🔒 DEMO HESAP: POZİSYON KAPATILDI</b>\n\n🎉 <b>NET REALİZE KÂR:</b> {net:.4f} USDT")
                            pos["aktif"] = False

            if en_yuksek_makaslar:
                en_yuksek_makaslar.sort(key=lambda x: x[1], reverse=True)
                print("\n📊 --- CANLI PİYASA EN YÜKSEK 3 MAKAS ---")
                for i, item in enumerate(en_yuksek_makaslar[:3]):
                    print(f"{i+1}. [{item[0]}] +%{item[1]:.4f} | Spot: {item[2]} | Futures: {item[3]}")
        except Exception as e:
            print(f"❌ Döngü hatası: {e}")
        time.sleep(1)

if __name__ == "__main__":
    print("⏳ Canlı piyasa altyapısı ve Demo Hesap bağlantısı kuruluyor...")
    SYMBOLS = get_all_futures_symbols()
    
    for symbol in SYMBOLS:
        piyasa_verisi[symbol] = {"spot_price": None, "futures_price": None}
        arbitraj_pozisyonlari[symbol] = {"aktif": False, "giris_makas": 0.0, "spot_adet": 0.0, "futures_adet": 0.0}
    
    set_all_leverages()
    
    telegram_bildir(f"🚀 <b>Sanal İşlemler (Mock Trading) Botu Başlatıldı!</b>\n🎯 <b>Tarama:</b> Canlı Canlı 50 Koin\n📊 <b>Giriş Eşiği:</b> +%{GIRIS_MAKAS_YUZDE}")
    
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    arbitraj_tarama_dongusu()
