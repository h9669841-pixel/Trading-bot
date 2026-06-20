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

# --- 🔑 GERÇEK BİNANCE API ANAHTARLARI ---
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Gerçek canlı borsaya bağlanıyoruz
client = Client(
    BINANCE_API_KEY, 
    BINANCE_SECRET_KEY, 
    requests_params={"proxies": requests_proxies} if requests_proxies else {}
)

# --- 📊 ARBİTRAJ STRATEJİ AYARLARI (GERÇEK HESAP) ---
GIRIS_MAKAS_YUZDE = 0.41  
CIKIS_MAKAS_YUZDE = 0.02  

# ⚠️ Burayı cüzdanındaki bütçeye göre ayarlayabilirsin (Örn: 10.0 dolar)
SPOT_BAKIYE = 10.0       
FUTURES_BAKIYE = 10.0    

SPOT_FEE_RATE = 0.0750 / 100     
FUTURES_FEE_RATE = 0.0450 / 100  

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
    return ["btcusdt", "ethusdt", "solusdt"]

def set_all_leverages():
    print(f"⏳ Gerçek hesaptaki koinlerin kaldıraçları 1x olarak ayarlanıyor...")
    for symbol in SYMBOLS:
        try:
            client.futures_change_leverage(symbol=symbol.upper(), leverage=1)
            time.sleep(0.1)
        except Exception:
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
    topham_kesinti_usdt = ((SPOT_BAKIYE * SPOT_FEE_RATE) * 2) + ((FUTURES_BAKIYE * FUTURES_FEE_RATE) * 2)
    return brut_kazanc_usdt, topham_kesinti_usdt, brut_kazanc_usdt - topham_kesinti_usdt

def execute_arbitrage_entry(symbol, spot_price, futures_price):
    coin_label = symbol.upper()
    precision = symbol_precisions.get(coin_label, 2)
    raw_spot_qty = SPOT_BAKIYE / spot_price
    raw_futures_qty = FUTURES_BAKIYE / futures_price
    
    factor = 10 ** precision
    spot_quantity = int(raw_spot_qty * factor) / factor if precision > 0 else int(raw_spot_qty)
    futures_quantity = int(raw_futures_qty * factor) / factor if precision > 0 else int(raw_futures_qty)

    if spot_quantity <= 0 or futures_quantity <= 0:
        return False, 0, 0

    # 1. ADIM: GERÇEK SPOT ALIM
    spot_basarili = False
    try:
        print(f"🛒 [REAL SPOT BUY] {coin_label} -> Adet: {spot_quantity}")
        client.create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=spot_quantity)
        spot_basarili = True
    except Exception as e:
        print(f"❌ [SPOT REAL HATA] {coin_label}: {e}")
        telegram_bildir(f"❌ <b>[SPOT ALIM BAŞARISIZ]</b>\n{coin_label} gerçek borsada alınamadı.\nDetay: {e}")
        return False, 0, 0

    # 2. ADIM: GERÇEK FUTURES SHORT
    futures_basarili = False
    if spot_basarili:
        try:
            print(f"📉 [REAL FUTURES SELL] {coin_label} -> Adet: {futures_quantity}")
            client.futures_create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=futures_quantity)
            futures_basarili = True
        except Exception as e:
            print(f"❌ [FUTURES REAL HATA] {coin_label}: {e}")
            telegram_bildir(f"❌ <b>[FUTURES SHORT BAŞARISIZ]</b>\n{coin_label} short pozisyon açılamadı.\nDetay: {e}")
            
            # Acil Durum Ters İşlem (Risk Yönetimi)
            print(f"🚨 Acil Durum: Satın alınan spot mallar piyasadan geri satılıyor...")
            try: client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=spot_quantity)
            except Exception: pass
            return False, 0, 0

    return True, spot_quantity, futures_quantity

def execute_arbitrage_exit(symbol, spot_qty, futures_qty):
    coin_label = symbol.upper()
    try:
        print(f"🤝 [REAL EXIT] {coin_label} Kapatılıyor...")
        client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=spot_qty)
        client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=futures_qty)
        return True
    except Exception as e:
        print(f"❌ Gerçek hesap kapatma hatası: {e}")
        telegram_bildir(f"🚨 <b>🚨 DİKKAT: POZİSYON KAPATILAMADI!</b>\n{coin_label} kapatılırken hata alındı el ile kontrol et!\nDetay: {e}")
        return False

# --- 🌐 LIVE WEBSOCKET AKIŞLARI ---
def start_multi_spot_ws():
    def on_message(ws, message):
        try:
            data = json.loads(message)
            symbol = data.get("stream", "").split("@")[0].lower()
            price = data.get("data", {}).get("p")
            if symbol in piyasa_verisi and price: piyasa_verisi[symbol]["spot_price"] = float(price)
        except Exception: pass
    def on_close(ws, c_code, c_msg): time.sleep(2); start_multi_spot_ws()
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
    def on_close(ws, c_code, c_msg): time.sleep(2); start_multi_futures_ws()
    streams = "/".join([f"{symbol}@trade" for symbol in SYMBOLS])
    WebSocketApp(f"wss://fstream.binance.com/stream?streams={streams}", on_message=on_message, on_close=on_close).run_forever(**ws_proxy_params)

def arbitraj_tarama_dongusu():
    global arbitraj_pozisyonlari
    print("🎯 Arbitraj tarama döngüsü aktif hale getirildi. Fırsatlar bekleniyor...")
    while True:
        try:
            en_yuksek_makaslar = []
            for symbol in SYMBOLS:
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
                            telegram_bildir(f"🤖 <b>GERÇEK HESAP: ARBİTRAJ BAŞLADI</b>\n\n📊 <b>Koin:</b> {coin_label}\n⚡ <b>Giriş Makası:</b> +%{anlik_makas:.4f}\n💵 <b>Net Hedef Kâr:</b> {net:.4f} USDT")
                else:
                    if anlik_makas <= CIKIS_MAKAS_YUZDE:
                        if execute_arbitrage_exit(symbol, pos["spot_adet"], pos["futures_adet"]):
                            brut, kesinti, net = net_kar_hesapla(pos["giris_makas"], anlik_makas)
                            telegram_bildir(f"🎉 <b>KÂR KİLİTLENDİ: POZİSYON KAPATILDI</b>\n\n📊 <b>Koin:</b> {coin_label}\n🎉 <b>Net Realize Kazanç:</b> {net:.4f} USDT")
                            pos["aktif"] = False

            if en_yuksek_makaslar:
                en_yuksek_makaslar.sort(key=lambda x: x[1], reverse=True)
                print(f"\n📊 --- CANLI GERÇEK PİYASA EN YÜKSEK 3 MAKAS (Toplam Koin: {len(SYMBOLS)}) ---")
                for i, item in enumerate(en_yuksek_makaslar[:3]):
                    print(f"{i+1}. [{item[0]}] +%{item[1]:.4f} | Spot: {item[2]} | Futures: {item[3]}")
        except Exception as e:
            print(f"❌ Döngü hatası: {e}")
        time.sleep(1)

if __name__ == "__main__":
    SYMBOLS = get_all_futures_symbols_and_precisions()
    for symbol in SYMBOLS:
        piyasa_verisi[symbol] = {"spot_price": None, "futures_price": None}
        arbitraj_pozisyonlari[symbol] = {"aktif": False, "giris_makas": 0.0, "spot_adet": 0.0, "futures_adet": 0.0}
    
    set_all_leverages()
    telegram_bildir(f"🚀 <b>Gerçek Arbitraj Robotu Canlıda Aktif!</b>\n🎯 İzlenen koin sayısı: {len(SYMBOLS)}")
    
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    arbitraj_tarama_dongusu()
