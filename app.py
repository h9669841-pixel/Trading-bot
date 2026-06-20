import os
import json
import time
import requests
import threading
import traceback
import socket  # 🎯 Python'ın yerel soket kütüphanesi
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException
from websocket import WebSocketApp

# --- 🌐 GLOBAL SOCKS5 ENJEKSİYONU (ÇEKİRDEK SEVİYESİNDE) ---
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
        
        # Python'ın tüm soket trafiğini (REST ve WS) global olarak SOCKS5 proxy'sine gömüyoruz.
        socks.set_default_proxy(
            socks.SOCKS5, 
            addr=proxy_host, 
            port=proxy_port, 
            username=proxy_user, 
            password=proxy_pass,
            rdns=True # DNS sorgularını da proxy üzerinden güvenli çözümler
        )
        socket.socket = socks.socksocket # Tüm sistemi SOCKS5 soketine yamala
        
    except ImportError:
        print("❌ HATA: SOCKS5 aktif edilemedi. Lütfen terminalde 'pip install PySocks' çalıştırın.")

# --- 🔑 GÜVENLİK VE API AYARLARI ---
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# 🎯 DEMO (TESTNET) AYARI: demo.binance.com anahtarlarının çalışması için testnet=True yapıldı.
# İleride gerçek parayla canlı hesaba geçmek istersen sadece testnet=False yapman yeterli!
USE_TESTNET = True 
client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, testnet=USE_TESTNET)

# --- 📊 ARBİTRAJ STRATEJİ VE HESAP AYARLARI ---
GIRIS_MAKAS_YUZDE = 0.41  # 🎯 41 olan değer makul arbitraj seviyesi olan %0.41'e düzeltildi
CIKIS_MAKAS_YUZDE = 0.02  

SPOT_BAKIYE = 15.0       # 🎯 Binance minimum emir limitine (MIN_NOTIONAL) takılmamak için 15 USDT yapıldı
FUTURES_BAKIYE = 15.0    

SPOT_FEE_RATE = 0.0750 / 100     
FUTURES_FEE_RATE = 0.0450 / 100  
# ----------------------------------------------

SYMBOLS = []
piyasa_verisi = {}
arbitraj_pozisyonlari = {}

def get_all_futures_symbols():
    try:
        # Testnet moduna göre exchange info endpoint'ini seçiyoruz
        if USE_TESTNET:
            url = "https://testnet.binancefuture.com/fapi/v1/exchangeInfo"
        else:
            url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
            
        r = requests.get(url, timeout=10)
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
            time.sleep(0.1) # Rate limit koruması
        except BinanceAPIException as b_err:
            # Testnet ortamında bazı koinlerin kaldıracı değiştirilemeyebilir, loglayıp geçiyoruz
            print(f"⚠️ {symbol.upper()} kaldıraç değiştirilemedi: {b_err.message}")
        except Exception as e:
            print(f"❌ Kaldıraç ayar hatası ({symbol.upper()}): {e}")

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"📢 [Telegram Değişkenleri Eksik] -> {mesaj}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, timeout=5)
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

# --- 🎯 DİNAMİK LOT SIZE HESAPLAMA FONKSİYONU ---
def get_lot_size_precision(symbol):
    """Koinin borsadaki izin verilen maksimum virgülden sonraki hane sayısını (Step Size) bulur"""
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

# --- 🎯 AKTİF EMİR YÖNETİM MOTORU ---
def execute_arbitrage_entry(symbol, spot_price, futures_price):
    coin_label = symbol.upper()
    try:
        precision = get_lot_size_precision(coin_label)
        
        raw_spot_qty = SPOT_BAKIYE / spot_price
        raw_futures_qty = FUTURES_BAKIYE / futures_price
        
        # 🎯 LOT_SIZE Hatası Almamak İçin Güvenli Aşağı Yuvarlama (Truncate)
        factor = 10 ** precision
        spot_quantity = int(raw_spot_qty * factor) / factor if precision > 0 else int(raw_spot_qty)
        futures_quantity = int(raw_futures_qty * factor) / factor if precision > 0 else int(raw_futures_qty)

        print(f"⚙️ {coin_label} Hassasiyet: {precision} | Emir Adetleri -> Spot: {spot_quantity}, Futures: {futures_quantity}")

        # Emirler fırlatılıyor
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

# --- 🌐 GLOBAL WEBSOCKET AKIŞLARI ---
def start_multi_spot_ws():
    def on_message(ws, message):
        data = json.loads(message)
        symbol = data.get("stream", "").split("@")[0]
        if symbol in piyasa_verisi:
            piyasa_verisi[symbol]["spot_price"] = float(data.get("data", {}).get("p", 0))

    def on_error(ws, error): 
        print(f"❌ Global Spot WS Hatası: {error}")
        traceback.print_exc()
        
    def on_close(ws, c_code, c_msg): 
        print(f"🔄 Spot WS kapandı. 5 saniye sonra yeniden bağlanıyor...")
        time.sleep(5)
        start_multi_spot_ws()

    streams = "/".join([f"{symbol}@trade" for symbol in SYMBOLS[:150]])
    
    # 🎯 Testnet durumuna göre WebSocket ana akış adresini seçiyoruz
    if USE_TESTNET:
        url = f"wss://testnet.binance.vision/stream?streams={streams}"
    else:
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever()

def start_multi_futures_ws():
    def on_message(ws, message):
        data = json.loads(message)
        symbol = data.get("stream", "").split("@")[0]
        if symbol in piyasa_verisi:
            piyasa_verisi[symbol]["futures_price"] = float(data.get("data", {}).get("p", 0))

    def on_error(ws, error): 
        print(f"❌ Global Futures WS Hatası: {error}")
        traceback.print_exc()
        
    def on_close(ws, c_code, c_msg): 
        print(f"🔄 Futures WS kapandı. 5 saniye sonra yeniden bağlanıyor...")
        time.sleep(5)
        start_multi_futures_ws()

    streams = "/".join([f"{symbol}@trade" for symbol in SYMBOLS[:150]])
    
    # 🎯 Testnet durumuna göre Vadeli İşlemler WebSocket adresini seçiyoruz
    if USE_TESTNET:
        url = f"wss://fstream.binancefuture.com/stream?streams={streams}"
    else:
        url = f"wss://fstream.binance.com/stream?streams={streams}"
    
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever()

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
    
    # Kaldıraç ayarları başlangıçta testnet modunda yapılır
    set_all_leverages()
    
    telegram_bildir(f"🤖 <b>SOCKS5 Enjeksiyonlu Demo Robotu Başlatıldı!</b>\n🎯 <b>Giriş Eşiği:</b> +%{GIRIS_MAKAS_YUZDE}")
    
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    arbitraj_tarama_dongusu()
