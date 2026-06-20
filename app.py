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
        
        requests_proxies = {
            "http": PROXY_URL,
            "https": PROXY_URL
        }
        
        ws_proxy_params = {
            "http_proxy_host": proxy_host,
            "http_proxy_port": proxy_port,
            "http_proxy_auth": (proxy_user, proxy_pass) if proxy_user else None,
            "proxy_type": "socks5"  
        }
    except Exception as e:
        print(f"❌ Proxy ayrıştırma hatası: {e}")

# --- 🔑 GÜVENLİK VE API AYARLARI ---
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# 🎯 DEMO (TESTNET) AYARI
USE_TESTNET = True 

client = Client(
    BINANCE_API_KEY, 
    BINANCE_SECRET_KEY, 
    testnet=USE_TESTNET,
    requests_params={"proxies": requests_proxies} if requests_proxies else {}
)

# --- 📊 ARBİTRAJ STRATEJİ VE HESAP AYARLARI ---
GIRIS_MAKAS_YUZDE = 0.41  
CIKIS_MAKAS_YUZDE = 0.02  

SPOT_BAKIYE = 15.0       
FUTURES_BAKIYE = 15.0    

SPOT_FEE_RATE = 0.0750 / 100     
FUTURES_FEE_RATE = 0.0450 / 100  
# ----------------------------------------------

SYMBOLS = []
piyasa_verisi = {}
arbitraj_pozisyonlari = {}

def get_all_futures_symbols():
    try:
        if USE_TESTNET:
            url = "https://testnet.binancefuture.com/fapi/v1/exchangeInfo"
        else:
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
        traceback.print_exc()
    return ["btcusdt", "ethusdt", "solusdt", "xrpusdt"]

def set_all_leverages():
    print("⏳ Tüm sembollerin kaldıracı 1x olarak ayarlanıyor...")
    for symbol in SYMBOLS[:150]:
        try:
            client.futures_change_leverage(symbol=symbol.upper(), leverage=1)
            time.sleep(0.1) 
        except BinanceAPIException as b_err:
            print(f"⚠️ {symbol.upper()} kaldıraç değiştirilemedi: {b_err.message}")
        except Exception as e:
            print(f"❌ Kaldıraç ayar hatası ({symbol.upper()}): {e}")

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"📢 [Telegram Değişkenleri Eksik] -> {mesaj}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, proxies=requests_proxies, timeout=5)
    except Exception as e:
        print(f"❌ Telegram hatası: {e}")

def net_kar_hesapla(giris_makas, kapanis_makas):
    brut_oran_farki = giris_makas - kapanis_makas
    brut_kazanc_usdt = SPOT_BAKIYE * (brut_oran_farki / 100)
    spot_toplam_komisyon = (SPOT_BAKIYE * SPOT_FEE_RATE) * 2
    futures_toplam_komisyon = (FUTURES_BAKIYE * FUTURES_FEE_RATE) * 2
    toplam_kesinti_usdt = spot_toplam_komisyon + futures_toplam_komisyon
    net_kazanc_usdt = brut_kazanc_usdt - toplam_kesinti_usdt
    return brut_kazanc_usdt, toplam_kesinti_usdt, net_kazanc_usdt

def get_lot_size_precision(symbol):
    try:
        info = client.get_symbol_info(symbol.upper())
        if info and 'filters' in info:
            for f in info['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    step_size = float(f['stepSize'])
                    if step_size >= 1.0:
                        return 0
                    return len(str(step_size).split('.')[1].rstrip('0'))
    except Exception as e:
        print(f"⚠️ {symbol} için LOT_SIZE hassasiyeti alınamadı, varsayılan 2 kullanılacak: {e}")
    return 2

def execute_arbitrage_entry(symbol, spot_price, futures_price):
    coin_label = symbol.upper()
    try:
        precision = get_lot_size_precision(coin_label)
        raw_spot_qty = SPOT_BAKIYE / spot_price
        raw_futures_qty = FUTURES_BAKIYE / futures_price
        
        factor = 10 ** precision
        spot_quantity = int(raw_spot_qty * factor) / factor if precision > 0 else int(raw_spot_qty)
        futures_quantity = int(raw_futures_qty * factor) / factor if precision > 0 else int(raw_futures_qty)

        print(f"⚙️ {coin_label} Hassasiyet: {precision} | Emir Adetleri -> Spot: {spot_quantity}, Futures: {futures_quantity}")

        spot_order = client.create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=spot_quantity)
        futures_order = client.futures_create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=futures_quantity)
        return True, spot_quantity, futures_quantity
    except BinanceAPIException as e:
        err_msg = f"❌ <b>{coin_label} BORSASAL GİRİŞ HATASI:</b>\nKod: {e.code}\nMesaj: {e.message}"
        print(err_msg)
        traceback.print_exc()
        telegram_bildir(err_msg)
        return False, 0, 0
    except Exception as e:
        err_msg = f"❌ {coin_label} SİSTEMSEL GİRİŞ HATASI: {e}"
        print(err_msg)
        traceback.print_exc()
        telegram_bildir(err_msg)
        return False, 0, 0

def execute_arbitrage_exit(symbol, spot_qty, futures_qty):
    coin_label = symbol.upper()
    try:
        client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=spot_qty)
        client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=futures_qty)
        return True
    except BinanceAPIException as e:
        err_msg = f"❌ <b>{coin_label} BORSASAL ÇIKIŞ HATASI (BACAK AÇIK KALDI!):</b>\nKod: {e.code}\nMesaj: {e.message}"
        print(err_msg)
        traceback.print_exc()
        telegram_bildir(err_msg)
        return False
    except Exception as e:
        err_msg = f"❌ {coin_label} SİSTEMSEL ÇIKIŞ HATASI: {e}"
        print(err_msg)
        traceback.print_exc()
        telegram_bildir(err_msg)
        return False

# --- 🌐 GLOBAL WEBSOCKET AKIŞLARI (KÖKTEN ÇÖZÜM SÜRÜMÜ) ---

# 🎯 Spot Testnet'in 404 hatasını ezmek için tekli bağlantı işleyicisi
def connect_single_spot_ws(symbol):
    def on_message(ws, message):
        data = json.loads(message)
        # Tekli stream yapısında veri doğrudan veya 'p' içinde gelebilir
        price = data.get("p") or data.get("data", {}).get("p")
        if price:
            piyasa_verisi[symbol]["spot_price"] = float(price)

    def on_error(ws, error):
        pass # Log kirliliği olmaması için sessizce geçilir
        
    def on_close(ws, c_code, c_msg):
        time.sleep(5)
        connect_single_spot_ws(symbol)

    url = f"wss://testnet.binance.vision/ws/{symbol}@trade"
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever(**ws_proxy_params)

def start_multi_spot_ws():
    if USE_TESTNET:
        # 🎯 Testnet modunda 404 yememek için koinleri tek tek Thread olarak ayağa kaldırıyoruz
        active_symbols = ["btcusdt", "ethusdt", "solusdt", "xrpusdt", "bnbusdt", "adausdt", "dogeusdt", "trxusdt", "linkusdt", "dotusdt"]
        print(f"📡 Spot (TESTNET): {len(active_symbols)} koin için izole tüneller açılıyor...")
        for symbol in active_symbols:
            threading.Thread(target=connect_single_spot_ws, args=(symbol,), daemon=True).start()
            time.sleep(0.2) # Proxy'yi boğmamak için küçük bir nefes payı
    else:
        # Canlı ortamda devasa toplu stream mimarisi
        def on_message(ws, message):
            data = json.loads(message)
            symbol = data.get("stream", "").split("@")[0]
            if symbol in piyasa_verisi:
                piyasa_verisi[symbol]["spot_price"] = float(data.get("data", {}).get("p", 0))

        def on_close(ws, c_code, c_msg):
            time.sleep(5)
            start_multi_spot_ws()

        active_symbols = [s for s in SYMBOLS[:100]]
        streams = "/".join([f"{symbol}@trade" for symbol in active_symbols])
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        print(f"📡 Spot (CANLI): {len(active_symbols)} koin havuzdan dinleniyor...")
        WebSocketApp(url, on_message=on_message, on_close=on_close).run_forever(**ws_proxy_params)

def start_multi_futures_ws():
    def on_message(ws, message):
        data = json.loads(message)
        stream_name = data.get("stream", "") or data.get("stream")
        symbol = stream_name.split("@")[0] if stream_name else symbol
        # Eğer veri düz formatta geldiyse ayıkla
        price = data.get("data", {}).get("p") or data.get("p")
        if symbol in piyasa_verisi and price:
            piyasa_verisi[symbol]["futures_price"] = float(price)

    def on_error(ws, error): 
        print(f"❌ Global Futures WS Hatası: {error}")
        
    def on_close(ws, c_code, c_msg): 
        time.sleep(5)
        start_multi_futures_ws()

    if USE_TESTNET:
        active_symbols = ["btcusdt", "ethusdt", "solusdt", "xrpusdt", "bnbusdt", "adausdt", "dogeusdt", "trxusdt", "linkusdt", "dotusdt"]
        streams = "/".join([f"{symbol}@trade" for symbol in active_symbols])
        url = f"wss://fstream.binancefuture.com/stream?streams={streams}"
    else:
        active_symbols = [s for s in SYMBOLS[:100]]
        streams = "/".join([f"{symbol}@trade" for symbol in active_symbols])
        url = f"wss://fstream.binance.com/stream?streams={streams}"
    
    print(f"📡 Futures WS Bağlantısı Açılıyor: {len(active_symbols)} koin dinleniyor...")
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever(**ws_proxy_params)

def arbitraj_tarama_dongusu():
    global arbitraj_pozisyonlari
    while True:
        try:
            en_yuksek_makaslar = []
            for symbol in SYMBOLS[:150]:
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
                            telegram_bildir(f"🤖 <b>İŞLEME GİRİLDİ (TESTNET)</b>\n\n📊 <b>Koin:</b> {coin_label}\n⚡ <b>Makas:</b> +%{anlik_makas:.4f}\n💵 <b>Tahmini Net Kâr:</b> {net:.4f} USDT")
                else:
                    if anlik_makas <= CIKIS_MAKAS_YUZDE:
                        if execute_arbitrage_exit(symbol, pos["spot_adet"], pos["futures_adet"]):
                            brut, kesinti, net = net_kar_hesapla(pos["giris_makas"], anlik_makas)
                            telegram_bildir(f"🤝 <b>🔒 POZİSYON KAPATILDI (TESTNET)</b>\n\n🎉 <b>NET REALİZE KÂR:</b> {net:.4f} USDT")
                            pos["aktif"] = False

            if en_yuksek_makaslar:
                en_yuksek_makaslar.sort(key=lambda x: x[1], reverse=True)
                print("\n💵 --- EN YÜKSEK 3 MAKAS ---")
                for i, item in enumerate(en_yuksek_makaslar[:3]):
                    print(f"{i+1}. [{item[0]}] +%{item[1]:.4f} | Sp: {item[2]} | Fu: {item[3]}")
        except Exception as e:
            print(f"❌ Döngü hatası: {e}")
            traceback.print_exc()
        time.sleep(2)

if __name__ == "__main__":
    SYMBOLS = get_all_futures_symbols()
    piyasa_verisi = {symbol: {"spot_price": None, "futures_price": None} for symbol in SYMBOLS}
    arbitraj_pozisyonlari = {symbol: {"aktif": False, "giris_makas": 0.0, "spot_adet": 0.0, "futures_adet": 0.0} for symbol in SYMBOLS}
    
    set_all_leverages()
    
    telegram_bildir(f"🤖 <b>SOCKS5 Destekli Demo Robotu Başlatıldı!</b>\n🎯 <b>Giriş Eşiği:</b> +%{GIRIS_MAKAS_YUZDE}")
    
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    arbitraj_tarama_dongusu()
