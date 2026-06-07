import os
import json
import time
import requests
import threading
from binance.client import Client
from binance.enums import *
from websocket import WebSocketApp

# --- 🔑 GÜVENLİK VE API AYARLARI ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")

# 🌐 PROXY (SOCKS5 STATIK IP) AYARLARI
PROXY_URL = os.environ.get("PROXY_URL") 

requests_requests_proxies = None
binance_client_requests_params = {}

if PROXY_URL:
    print(f"🌐 SOCKS5 Statik IP Proxy Aktif Ediliyor: {PROXY_URL}")
    requests_requests_proxies = {
        "http": PROXY_URL,
        "https": PROXY_URL
    }
    binance_client_requests_params = {
        "proxies": requests_requests_proxies
    }

# Binance API İstemcisi SOCKS5 Desteğiyle Başlatılıyor
client = Client(
    BINANCE_API_KEY, 
    BINANCE_SECRET_KEY, 
    requests_params=binance_client_requests_params
)

# --- 📊 ARBİTRAJ STRATEJİ VE HESAP AYARLARI ---
GIRIS_MAKAS_YUZDE = 60  
CIKIS_MAKAS_YUZDE = 0.02  

SPOT_BAKIYE = 10.0       
FUTURES_BAKIYE = 10.0    

SPOT_FEE_RATE = 0.0750 / 100     
FUTURES_FEE_RATE = 0.0450 / 100  
# ----------------------------------------------

SYMBOLS = []
piyasa_verisi = {}
arbitraj_pozisyonlari = {}

def get_all_futures_symbols():
    try:
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(url, proxies=requests_requests_proxies, timeout=10)
        if r.status_code == 200:
            data = r.json()
            symbols = []
            for market in data.get("symbols", []):
                if market.get("quoteAsset") == "USDT" and market.get("status") == "TRADING":
                    symbols.append(market.get("symbol").lower())
            return symbols
    except Exception as e:
        print(f"Koin listesi çekilirken hata oluştu: {e}")
    return ["btcusdt", "ethusdt", "solusdt", "xrpusdt"]

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram değişkenleri eksik!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        print(f"Telegram hatası: {e}")

def net_kar_hesapla(giris_makas, kapanis_makas):
    brut_oran_farki = giris_makas - kapanis_makas
    brut_kazanc_usdt = SPOT_BAKIYE * (brut_oran_farki / 100)
    spot_toplam_komisyon = (SPOT_BAKIYE * SPOT_FEE_RATE) * 2
    futures_toplam_komisyon = (FUTURES_BAKIYE * FUTURES_FEE_RATE) * 2
    toplam_kesinti_usdt = spot_toplam_komisyon + futures_toplam_komisyon
    net_kazanc_usdt = brut_kazanc_usdt - toplam_kesinti_usdt
    return brut_kazanc_usdt, toplam_kesinti_usdt, net_kazanc_usdt

# --- 🎯 AKTİF EMİR YÖNETİM MOTORU ---
def execute_arbitrage_entry(symbol, spot_price, futures_price):
    coin_label = symbol.upper()
    try:
        client.futures_change_leverage(symbol=coin_label, leverage=1)
        spot_quantity = round(SPOT_BAKIYE / spot_price, 4)
        futures_quantity = round(FUTURES_BAKIYE / futures_price, 4)

        spot_order = client.create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=spot_quantity)
        futures_order = client.futures_create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=futures_quantity)
        return True, spot_quantity, futures_quantity
    except Exception as e:
        err_msg = f"❌ {coin_label} GİRİŞ EMİR HATASI: {e}"
        telegram_bildir(err_msg)
        return False, 0, 0

def execute_arbitrage_exit(symbol, spot_qty, futures_qty):
    coin_label = symbol.upper()
    try:
        client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=spot_qty)
        client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=futures_qty)
        return True
    except Exception as e:
        err_msg = f"❌ {coin_label} ÇIKIŞ EMİR HATASI: {e}"
        telegram_bildir(err_msg)
        return False

# --- 🌐 GLOBAL WEBSOCKET AKIŞLARI ---
def start_multi_spot_ws():
    def on_message(ws, message):
        data = json.loads(message)
        symbol = data.get("stream", "").split("@")[0]
        if symbol in piyasa_verisi:
            piyasa_verisi[symbol]["spot_price"] = float(data.get("data", {}).get("p", 0))

    def on_error(ws, error): print(f"Global Spot WS Hatası: {error}")
    def on_close(ws, c_code, c_msg): time.sleep(5); start_multi_spot_ws()

    streams = "/".join([f"{symbol}@trade" for symbol in SYMBOLS[:150]])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    
    ws_kwargs = {}
    if PROXY_URL:
        from urllib.parse import urlparse
        parsed_proxy = urlparse(PROXY_URL)
        ws_kwargs = {
            "http_proxy_host": parsed_proxy.hostname,
            "http_proxy_port": parsed_proxy.port,
            "http_proxy_auth": (parsed_proxy.username, parsed_proxy.password) if parsed_proxy.username else None,
            "proxy_type": "socks5"  # 🎯 SOCKS5 protokolü zorunlu kılındı
        }
        
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever(**ws_kwargs)

def start_multi_futures_ws():
    def on_message(ws, message):
        data = json.loads(message)
        symbol = data.get("stream", "").split("@")[0]
        if symbol in piyasa_verisi:
            piyasa_verisi[symbol]["futures_price"] = float(data.get("data", {}).get("p", 0))

    def on_error(ws, error): print(f"Global Futures WS Hatası: {error}")
    def on_close(ws, c_code, c_msg): time.sleep(5); start_multi_futures_ws()

    streams = "/".join([f"{symbol}@trade" for symbol in SYMBOLS[:150]])
    url = f"wss://fstream.binance.com/stream?streams={streams}"
    
    ws_kwargs = {}
    if PROXY_URL:
        from urllib.parse import urlparse
        parsed_proxy = urlparse(PROXY_URL)
        ws_kwargs = {
            "http_proxy_host": parsed_proxy.hostname,
            "http_proxy_port": parsed_proxy.port,
            "http_proxy_auth": (parsed_proxy.username, parsed_proxy.password) if parsed_proxy.username else None,
            "proxy_type": "socks5"  # 🎯 SOCKS5 protokolü zorunlu kılındı
        }
        
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever(**ws_kwargs)

def arbitraj_tarama_dongusu():
    global arbitraj_pozisyonlari
    while True:
        try:
            en_yuksek_makaslar = []
            for symbol in SYMBOLS[:150]:
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
                            telegram_bildir(f"🤖 <b>İŞLEME GİRİLDİ</b>\n\n📊 <b>Koin:</b> {coin_label}\n⚡ <b>Makas:</b> +%{anlik_makas:.4f}\n💵 <b>Net Kâr:</b> {net:.4f} USDT")
                else:
                    if anlik_makas <= CIKIS_MAKAS_YUZDE:
                        if execute_arbitrage_exit(symbol, pos["spot_adet"], pos["futures_adet"]):
                            brut, kesinti, net = net_kar_hesapla(pos["giris_makas"], anlik_makas)
                            telegram_bildir(f"🤝 <b>🔒 POZİSYON KAPATILDI</b>\n\n🎉 <b>NET REALİZE KÂR:</b> {net:.4f} USDT")
                            pos["aktif"] = False

            if en_yuksek_makaslar:
                en_yuksek_makaslar.sort(key=lambda x: x[1], reverse=True)
                print("\n💵 --- EN YÜKSEK 3 MAKAS ---")
                for i, item in enumerate(en_yuksek_makaslar[:3]):
                    print(f"{i+1}. [{item[0]}] +%{item[1]:.4f} | Sp: {item[2]} | Fu: {item[3]}")
        except Exception as e:
            print(f"Döngü hatası: {e}")
        time.sleep(2)

if __name__ == "__main__":
    SYMBOLS = get_all_futures_symbols()
    piyasa_verisi = {symbol: {"spot_price": None, "futures_price": None} for symbol in SYMBOLS}
    arbitraj_pozisyonlari = {symbol: {"aktif": False, "giris_makas": 0.0, "spot_adet": 0.0, "futures_adet": 0.0} for symbol in SYMBOLS}
    
    telegram_bildir(f"🤖 <b>SOCKS5 Destekli Robot Başlatıldı!</b>\n🎯 <b>Giriş Eşiği:</b> +%{GIRIS_MAKAS_YUZDE}")
    
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    arbitraj_tarama_dongusu()
