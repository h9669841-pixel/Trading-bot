import os
import json
import time
import requests
import threading
import traceback
import socket
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException
from websocket import WebSocketApp

# --- 🌐 GLOBAL SOCKS5 ENJEKSİYONU ---
PROXY_URL = os.environ.get("PROXY_URL")
if PROXY_URL:
    try:
        import socks
        from urllib.parse import urlparse, unquote
        parsed_proxy = urlparse(PROXY_URL)
        proxy_host = parsed_proxy.hostname
        proxy_port = parsed_proxy.port
        proxy_user = unquote(parsed_proxy.username) if parsed_proxy.username else None
        proxy_pass = unquote(parsed_proxy.password) if parsed_proxy.password else None
        print(f"🌐 SOCKS5 Protokolü Çekirdeğe Enjekte Ediliyor: {proxy_host}:{proxy_port}")
        socks.set_default_proxy(socks.SOCKS5, addr=proxy_host, port=proxy_port, username=proxy_user, password=proxy_pass, rdns=True)
        socket.socket = socks.socksocket
    except ImportError:
        print("❌ HATA: PySocks eksik.")

# --- 🔑 GÜVENLİK VE API AYARLARI ---
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

# --- 📊 ARBİTRAJ STRATEJİ VE HESAP AYARLARI ---
GIRIS_MAKAS_YUZDE = 0.95       
CIKIS_MAKAS_YUZDE = 0.15       

SPOT_BAKIYE = 15.0  
FUTURES_BAKIYE = 15.0  
KALDIRAC = 1  

SPOT_FEE_RATE = 0.0750 / 100
FUTURES_FEE_RATE = 0.0450 / 100

SYMBOLS = []
piyasa_verisi = {}
arbitraj_pozisyonlari = {}

SPOT_HASSASIYETLERI = {}
FUTURES_HASSASIYETLERI = {}
PRICE_HASSASIYETLERI = {} # Fiyat adımı (tickSize) hassasiyetleri

data_lock = threading.Lock()

def get_all_futures_symbols():
    try:
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            symbols = []
            for market in data.get("symbols", []):
                if market.get("quoteAsset") == "USDT" and market.get("status") == "TRADING":
                    symbols.append(market.get("symbol").lower())
            return symbols[:150]
    except Exception as e:
        print(f"❌ Koin listesi çekilirken hata oluştu: {e}")
    return ["btcusdt", "ethusdt", "solusdt", "xrpusdt"]

def tum_hassasiyetleri_yukle():
    print("⏳ Spot, Vadeli Lot ve Fiyat hassasiyetleri önbelleğe alıyor...")
    try:
        spot_info = client.get_exchange_info()
        for market in spot_info['symbols']:
            sym = market['symbol'].lower()
            if sym in SYMBOLS:
                PRICE_HASSASIYETLERI[sym] = 2 # Varsayılan fiyat hassasiyeti
                for f in market['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size_str = str(f['stepSize']).rstrip('0')
                        SPOT_HASSASIYETLERI[sym] = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
                    if f['filterType'] == 'PRICE_FILTER':
                        tick_size_str = str(f['tickSize']).rstrip('0')
                        PRICE_HASSASIYETLERI[sym] = 0 if '.' not in tick_size_str else len(tick_size_str.split('.')[1])
    except Exception as e:
        print(f"❌ Spot hassasiyet yükleme hatası: {e}")

    try:
        f_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(f_url, timeout=10)
        if r.status_code == 200:
            f_data = r.json()
            for market in f_data.get("symbols", []):
                sym = market.get("symbol").lower()
                if sym in SYMBOLS:
                    for f in market.get("filters", []):
                        if f.get("filterType") == "LOT_SIZE":
                            step_size_str = str(f.get("stepSize")).rstrip('0')
                            FUTURES_HASSASIYETLERI[sym] = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
        print(f"✅ Çift yönlü hassasiyet haritası kaydedildi.")
    except Exception as e:
        print(f"❌ Vadeli hassasiyet yükleme hatası: {e}")

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, timeout=5)
    except Exception: pass

# --- 🚀 LİMİT EMİR MOTORLARI (GİRİŞ) ---
def execute_limit_arbitrage_entry(symbol, spot_price, futures_price):
    coin_label = symbol.upper()
    try:
        spot_precision = SPOT_HASSASIYETLERI.get(symbol.lower(), 2)
        futures_precision = FUTURES_HASSASIYETLERI.get(symbol.lower(), 2)
        price_precision = PRICE_HASSASIYETLERI.get(symbol.lower(), 4)
        
        raw_spot_qty = SPOT_BAKIYE / spot_price
        raw_futures_qty = FUTURES_BAKIYE / futures_price
        
        spot_quantity = round(raw_spot_qty, spot_precision) if spot_precision > 0 else int(raw_spot_qty)
        futures_quantity = round(raw_futures_qty, futures_precision) if futures_precision > 0 else int(raw_futures_qty)
        
        s_price_str = f"{spot_price:.{price_precision}f}"
        f_price_str = f"{futures_price:.{price_precision}f}"

        print(f"⏳ {coin_label} için LİMİT giriş emirleri gönderiliyor... Sp: {s_price_str} | Fu: {f_price_str}")
        
        # LIMIT emirler tahtaya pasif bırakılır (timeInForce=GTC)
        spot_order = client.create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_LIMIT, timeInForce=TIME_IN_FORCE_GTC, quantity=spot_quantity, price=s_price_str)
        futures_order = client.futures_create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_LIMIT, timeInForce=TIME_IN_FORCE_GTC, quantity=futures_quantity, price=f_price_str)
        
        return True, spot_quantity, futures_quantity, spot_order.get('orderId'), futures_order.get('orderId')
    except Exception as e:
        print(f"❌ Limit Giriş Hatası: {e}")
        return False, 0, 0, None, None

# --- 🚀 LİMİT EMİR MOTORLARI (KÂR KAPANIŞI - TAKE PROFIT) ---
def execute_limit_arbitrage_exit_targets(symbol, spot_qty, futures_qty, target_spot_exit_price, target_futures_exit_price):
    coin_label = symbol.upper()
    try:
        price_precision = PRICE_HASSASIYETLERI.get(symbol.lower(), 4)
        spot_precision = SPOT_HASSASIYETLERI.get(symbol.lower(), 2)
        
        güvenli_spot_qty = round(spot_qty * 0.9985, spot_precision) if spot_precision > 0 else int(spot_qty * 0.9985)
        
        s_exit_price_str = f"{target_spot_exit_price:.{price_precision}f}"
        f_exit_price_str = f"{target_futures_exit_price:.{price_precision}f}"
        
        print(f"🎯 {coin_label} KÂR HEDEFLİ LİMİT KAPANIŞ EMİRLERİ YAZILIYOR -> Sp Satış: {s_exit_price_str} | Fu Alış: {f_exit_price_str}")
        
        # Kâr realizasyonu için limit çıkış emirleri tahtaya asılır
        spot_close_order = client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_LIMIT, timeInForce=TIME_IN_FORCE_GTC, quantity=güvenli_spot_qty, price=s_exit_price_str)
        futures_close_order = client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_LIMIT, timeInForce=TIME_IN_FORCE_GTC, quantity=futures_qty, price=f_exit_price_str)
        
        return True, spot_close_order.get('orderId'), futures_close_order.get('orderId')
    except Exception as e:
        print(f"❌ Kâr Limit Emirleri Girilirken Hata: {e}")
        return False, None, None

# --- 🌐 WEBSOCKET SÜRÜCÜLERİ ---
def on_spot_message(ws, message):
    data = json.loads(message)
    symbol = data.get("stream", "").split("@")[0]
    with data_lock:
        if symbol in piyasa_verisi: piyasa_verisi[symbol]["spot_price"] = float(data.get("data", {}).get("p", 0))

def on_futures_message(ws, message):
    data = json.loads(message)
    symbol = data.get("stream", "").split("@")[0]
    with data_lock:
        if symbol in piyasa_verisi: piyasa_verisi[symbol]["futures_price"] = float(data.get("data", {}).get("p", 0))

def start_multi_spot_ws():
    WebSocketApp(f"wss://stream.binance.com:9443/stream?streams={'/'.join([f'{s}@trade' for s in SYMBOLS])}", on_message=on_spot_message).run_forever()

def start_multi_futures_ws():
    WebSocketApp(f"wss://fstream.binance.com/stream?streams={'/'.join([f'{s}@trade' for s in SYMBOLS])}", on_message=on_futures_message).run_forever()

# --- 🎯 ARBİTRAJ MOTORU ---
def arbitraj_tarama_dongusu():
    global arbitraj_pozisyonlari
    while True:
        try:
            with data_lock:
                for symbol in SYMBOLS:
                    spot_fiyat = piyasa_verisi[symbol]["spot_price"]
                    futures_fiyat = piyasa_verisi[symbol]["futures_price"]
                    if not spot_fiyat or not futures_fiyat: continue
                        
                    anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
                    coin_label = symbol.upper()
                    pos = arbitraj_pozisyonlari[symbol]
                    
                    # STAGE 1: HİÇ POZİSYON YOKSA VE GİRİŞ ŞARTI OLUŞTUYSA -> LİMİT GİRİŞ EMİRLERİNİ GİR
                    if pos["durum"] == "BOS":
                        if anlik_makas >= GIRIS_MAKAS_YUZDE:
                            basarili, s_qty, f_qty, s_id, f_id = execute_limit_arbitrage_entry(symbol, spot_fiyat, futures_fiyat)
                            if basarili:
                                pos.update({
                                    "durum": "GIRIS_BEKLIYOR",
                                    "spot_adet": s_qty,
                                    "futures_adet": f_qty,
                                    "spot_entry_id": s_id,
                                    "futures_entry_id": f_id,
                                    "giris_spot_fiyat": spot_fiyat,
                                    "giris_futures_fiyat": futures_fiyat
                                })
                                telegram_bildir(f"⏳ <b>{coin_label} Limit Giriş Emirleri Tahtaya İletildi.</b>\nSp Alış: {spot_fiyat}\nFu Short: {futures_fiyat}")

                    # STAGE 2: GİRİŞ EMİRLERİ VERİLDİYSE -> İKİ EMİR DE GERÇEKLEŞTİ Mİ KONTROL ET
                    elif pos["durum"] == "GIRIS_BEKLIYOR":
                        s_status = client.get_order(symbol=coin_label, orderId=pos["spot_entry_id"]).get("status")
                        f_status = client.futures_get_order(symbol=coin_label, orderId=pos["futures_entry_id"]).get("status")
                        
                        if s_status == "FILLED" and f_status == "FILLED":
                            # 🎯 MUHTEŞEM AN: İki limit emir de doldu! Şimdi kâr edecek çıkış fiyatlarını hesapla
                            # Spot çıkış hedefi: Giriş fiyatının biraz üzerinde satmak
                            # Vadeli çıkış hedefi: Giriş fiyatının biraz altında geri almak (Short kapatmak)
                            hedef_spot_cikis = pos["giris_spot_fiyat"] * (1 + (CIKIS_MAKAS_YUZDE / 2 / 100))
                            hedef_futures_cikis = pos["giris_futures_fiyat"] * (1 - (CIKIS_MAKAS_YUZDE / 2 / 100))
                            
                            basarili_hedef, s_cl_id, f_cl_id = execute_limit_arbitrage_exit_targets(
                                symbol, pos["spot_adet"], pos["futures_adet"], hedef_spot_cikis, hedef_futures_cikis
                            )
                            if basarili_hedef:
                                pos.update({
                                    "durum": "CIKIS_BEKLIYOR",
                                    "spot_exit_id": s_cl_id,
                                    "futures_exit_id": f_cl_id,
                                    "hedef_spot_cikis": hedef_spot_cikis,
                                    "hedef_futures_cikis": hedef_futures_cikis
                                })
                                telegram_bildir(
                                    f"🚀 <b>{coin_label} GİRİŞ EMİRLERİ GERÇEKLEŞTİ!</b>\n"
                                    f"🔒 Kâr Hedefli Çıkış Emirleri Tahtaya Asıldı:\n"
                                    f"📈 Spot Limit Satış: {hedef_spot_cikis:.4f}\n"
                                    f"📉 Vadeli Limit Alış (Short Kapatma): {hedef_futures_cikis:.4f}"
                                )
                        
                        # Eğer emirler uzun süre gerçekleşmezse iptal mekanizması eklenebilir (Güvenlik amaçlı)

                    # STAGE 3: KÂR EMİRLERİ TAHTADA ASILIYSA -> KAPANMALARINI BEKLE
                    elif pos["durum"] == "CIKIS_BEKLIYOR":
                        s_cl_status = client.get_order(symbol=coin_label, orderId=pos["spot_exit_id"]).get("status")
                        f_cl_status = client.futures_get_order(symbol=coin_label, orderId=pos["futures_exit_id"]).get("status")
                        
                        if s_cl_status == "FILLED" and f_cl_status == "FILLED":
                            telegram_bildir(
                                f"🎉 <b>🔒 ARBİTRAJ BAŞARIYLA TAMAMLANDI (Makers PNL)</b>\n"
                                f"📊 <b>Koin:</b> {coin_label}\n"
                                f"💰 Her iki limit emir de tam hedef fiyatlarından kârla kapandı!"
                            )
                            pos.update({"durum": "BOS", "spot_entry_id": None, "futures_entry_id": None, "spot_exit_id": None, "futures_exit_id": None})

        except Exception as e: 
            print(f"❌ Döngü hatası: {e}")
            traceback.print_exc()
        time.sleep(4.0)

if __name__ == "__main__":
    SYMBOLS = get_all_futures_symbols()
    piyasa_verisi = {symbol: {"spot_price": None, "futures_price": None} for symbol in SYMBOLS}
    
    # Yeni Durum Yönetimi Hafızası: "BOS", "GIRIS_BEKLIYOR", "CIKIS_BEKLIYOR"
    arbitraj_pozisyonlari = {symbol: {"durum": "BOS", "spot_adet": 0.0, "futures_adet": 0.0, "spot_entry_id": None, "futures_entry_id": None, "spot_exit_id": None, "futures_exit_id": None} for symbol in SYMBOLS}
    
    client.futures_change_position_mode(dualSidePosition="false")
    tum_hassasiyetleri_yukle()
    
    telegram_bildir("🤖 <b>Pusu Modu (Limit Emirli) Arbitraj Botu Yayında!</b>\nArtık piyasa emri ve fiyat kayması yok.")
    
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    arbitraj_tarama_dongusu()
