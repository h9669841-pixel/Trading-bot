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
GIRIS_MAKAS_YUZDE = 0.60       
CIKIS_MAKAS_YUZDE = 0.10       

SPOT_BAKIYE = 15.0  
FUTURES_BAKIYE = 15.0  

SPOT_FEE_RATE = 0.0750 / 100
FUTURES_FEE_RATE = 0.0450 / 100

SYMBOLS = []
piyasa_verisi = {}
arbitraj_pozisyonlari = {}

SPOT_HASSASIYETLERI = {}
FUTURES_HASSASIYETLERI = {}

# 🛠️ ÇÖZÜM: Koinlerin birbirine karışmasını engelleyecek Global Kilit (Lock)
order_lock = threading.Lock()

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
            filtered_symbols = symbols[:150]
            print(f"🎯 Binance Vadeli İşlemlerden ilk {len(filtered_symbols)} aktif sembol başarıyla çekildi.")
            return filtered_symbols
    except Exception as e:
        print(f"❌ Koin listesi çekilirken hata oluştu: {e}")
    return ["btcusdt", "ethusdt", "solusdt", "xrpusdt"]

def set_all_leverages():
    print("⏳ Pozisyon Modu 'One-Way' (Tek Yönlü) olarak zorlanıyor...")
    try:
        client.futures_change_position_mode(dualSidePosition="false")
        print("✅ Pozisyon Modu başarıyla Tek Yönlü (One-Way) yapıldı.")
    except BinanceAPIException as e:
        if e.code == -4059: 
            print("✅ Pozisyon Modu zaten Tek Yönlü (One-Way).")
        else:
            print(f"⚠️ Pozisyon modu değiştirilemedi: {e.message}")
    except Exception as e:
        print(f"⚠️ Pozisyon modu genel hata: {e}")

    print("⏳ Kaldıraçlar 5x olarak ayarlanıyor...")
    for symbol in SYMBOLS:
        try:
            client.futures_change_leverage(symbol=symbol.upper(), leverage=5)
            time.sleep(0.02)
        except Exception:
            pass

def tum_hassasiyetleri_yukle():
    print("⏳ Spot ve Vadeli Lot hassasiyetleri önbelleğe alınıyor...")
    try:
        spot_info = client.get_exchange_info()
        for market in spot_info['symbols']:
            sym = market['symbol'].lower()
            if sym in SYMBOLS:
                for f in market['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size_str = str(f['stepSize']).rstrip('0')
                        precision = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
                        SPOT_HASSASIYETLERI[sym] = precision
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
                            precision = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
                            FUTURES_HASSASIYETLERI[sym] = precision
        print(f"✅ Çift yönlü hassasiyet haritası kaydedildi. S:{len(SPOT_HASSASIYETLERI)} | F:{len(FUTURES_HASSASIYETLERI)}")
    except Exception as e:
        print(f"❌ Vadeli hassasiyet yükleme hatası: {e}")

    for sym in SYMBOLS:
        if sym not in SPOT_HASSASIYETLERI: SPOT_HASSASIYETLERI[sym] = 2
        if sym not in FUTURES_HASSASIYETLERI: FUTURES_HASSASIYETLERI[sym] = 2

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, timeout=5)
    except Exception: pass

def net_kar_hesapla(giris_makas, kapanis_makas):
    brut_oran_farki = giris_makas - kapanis_makas
    brut_kazanc_usdt = SPOT_BAKIYE * (brut_oran_farki / 100)
    toplam_kesinti_usdt = ((SPOT_BAKIYE * SPOT_FEE_RATE) * 2) + ((FUTURES_BAKIYE * FUTURES_FEE_RATE) * 2)
    return brut_kazanc_usdt - toplam_kesinti_usdt

# --- 🚀 EMİR MOTORLARI ---
def _hizli_emir_gonder_spot(coin_label, quantity, sonuclar):
    try: sonuclar['spot'] = client.create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=quantity)
    except Exception as e: sonuclar['spot_hata'] = e

def _hizli_emir_gonder_futures(coin_label, quantity, sonuclar):
    try: sonuclar['futures'] = client.futures_create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=quantity)
    except BinanceAPIException as e: sonuclar['futures_hata'] = f"Binance Borsası Emri Reddetti -> Kod: {e.code}, Mesaj: {e.message}"
    except Exception as e: sonuclar['futures_hata'] = f"Sistemsel Bağlantı Hatası -> {e}"

def _hizli_cikis_gonder_spot(coin_label, quantity, sonuclar):
    try: sonuclar['spot'] = client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=quantity)
    except Exception as e: sonuclar['spot_hata'] = e

def _hizli_cikis_gonder_futures(coin_label, quantity, sonuclar):
    try: sonuclar['futures'] = client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=quantity)
    except Exception as e: sonuclar['futures_hata'] = e

def execute_arbitrage_entry(symbol, spot_price, futures_price):
    coin_label = symbol.upper()
    try:
        spot_precision = SPOT_HASSASIYETLERI.get(symbol.lower(), 2)
        futures_precision = FUTURES_HASSASIYETLERI.get(symbol.lower(), 2)
        
        raw_spot_qty = SPOT_BAKIYE / spot_price
        raw_futures_qty = FUTURES_BAKIYE / futures_price
        
        spot_quantity = float(int(raw_spot_qty * (10 ** spot_precision))) / (10 ** spot_precision) if spot_precision > 0 else int(raw_spot_qty)
        futures_quantity = float(int(raw_futures_qty * (10 ** futures_precision))) / (10 ** futures_precision) if futures_precision > 0 else int(raw_futures_qty)
        
        if spot_precision == 0: spot_quantity = int(spot_quantity)
        if futures_precision == 0: futures_quantity = int(futures_quantity)
            
        print(f"⚙️ {coin_label} İşleme Gönderiliyor | Sp Adet: {spot_quantity} | Fu Adet: {futures_quantity}")
        
        emir_sonuclari = {}
        t1 = threading.Thread(target=_hizli_emir_gonder_spot, args=(coin_label, spot_quantity, emir_sonuclari))
        t2 = threading.Thread(target=_hizli_emir_gonder_futures, args=(coin_label, futures_quantity, emir_sonuclari))
        
        t1.start(); t2.start(); t1.join(); t2.join()
        
        if 'spot_hata' in emir_sonuclari or 'futures_hata' in emir_sonuclari:
            if 'futures_hata' in emir_sonuclari:
                hata_raporu = f"⚠️ <b>{coin_label} VADELİ EMİR REDDEDİLDİ!</b>\nNeden: <code>{emir_sonuclari['futures_hata']}</code>"
                print(hata_raporu)
                telegram_bildir(hata_raporu)
            
            if 'spot' in emir_sonuclari and 'futures_hata' in emir_sonuclari:
                print(f"🔄 Risk Koruma: Alınan spot {coin_label} koinleri piyasa fiyatından hemen geri satılıyor...")
                client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=spot_quantity)
            return False, 0, 0
            
        return True, spot_quantity, futures_quantity
    except Exception as e:
        print(f"❌ Giriş Hatası: {e}"); return False, 0, 0

def execute_arbitrage_exit(symbol, spot_qty, futures_qty):
    coin_label = symbol.upper()
    try:
        emir_sonuclari = {}
        t1 = threading.Thread(target=_hizli_cikis_gonder_spot, args=(coin_label, spot_qty, emir_sonuclari))
        t2 = threading.Thread(target=_hizli_cikis_gonder_futures, args=(coin_label, futures_qty, emir_sonuclari))
        
        t1.start(); t2.start(); t1.join(); t2.join()
        return True
    except Exception as e:
        print(f"❌ Çıkış Hatası: {e}"); return False

# --- 🌐 WEBSOCKET SÜRÜCÜLERİ ---
def start_multi_spot_ws():
    def on_message(ws, message):
        data = json.loads(message)
        symbol = data.get("stream", "").split("@")[0]
        if symbol in piyasa_verisi: piyasa_verisi[symbol]["spot_price"] = float(data.get("data", {}).get("p", 0))
    WebSocketApp(f"wss://stream.binance.com:9443/stream?streams={'/'.join([f'{s}@trade' for s in SYMBOLS])}", on_message=on_message).run_forever()

def start_multi_futures_ws():
    def on_message(ws, message):
        data = json.loads(message)
        symbol = data.get("stream", "").split("@")[0]
        if symbol in piyasa_verisi: piyasa_verisi[symbol]["futures_price"] = float(data.get("data", {}).get("p", 0))
    WebSocketApp(f"wss://fstream.binance.com/stream?streams={'/'.join([f'{s}@trade' for s in SYMBOLS])}", on_message=on_message).run_forever()

# --- 🎯 ARBİTRAJ MOTORU ---
def arbitraj_tarama_dongusu():
    global arbitraj_pozisyonlari
    while True:
        try:
            en_yuksek_makaslar = []
            for symbol in SYMBOLS:
                spot_fiyat = piyasa_verisi[symbol]["spot_price"]
                futures_fiyat = piyasa_verisi[symbol]["futures_price"]
                if not spot_fiyat or not futures_fiyat: continue
                    
                anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
                coin_label = symbol.upper()
                
                if anlik_makas > 0:
                    en_yuksek_makaslar.append((coin_label, anlik_makas, spot_fiyat, futures_fiyat))
                
                pos = arbitraj_pozisyonlari[symbol]
                
                if not pos["aktif"]:
                    if anlik_makas >= GIRIS_MAKAS_YUZDE:
                        net = net_kar_hesapla(anlik_makas, CIKIS_MAKAS_YUZDE)
                        if net <= 0: continue
                            
                        # 🛠️ ÇÖZÜM: Aynı anda sadece BİR koin işlem yapabilir. Diğer koinlerin sinyalleri kilit çözülene kadar bekler.
                        with order_lock:
                            # Kilidin içine girdiğinde fiyatı ve aktiflik durumunu son bir kez daha doğrula (Double-check)
                            if not pos["aktif"]:
                                basarili, s_qty, f_qty = execute_arbitrage_entry(symbol, spot_fiyat, futures_fiyat)
                                if basarili:
                                    pos.update({"aktif": True, "giris_makas": anlik_makas, "spot_adet": s_qty, "futures_adet": f_qty})
                                    telegram_bildir(f"🤖 <b>İŞLEME GİRİLDİ (+)</b>\n\n📊 <b>Koin:</b> {coin_label}\n⚡ <b>Makas:</b> +%{anlik_makas:.4f}\n💵 <b>Tahmini Net Kâr:</b> {net:.4f} USDT")
                else:
                    if anlik_makas <= CIKIS_MAKAS_YUZDE:
                        with order_lock:
                            if pos["aktif"]:
                                if execute_arbitrage_exit(symbol, pos["spot_adet"], pos["futures_adet"]):
                                    brut, kesinti, net = net_kar_hesapla(pos["giris_makas"], anlik_makas)
                                    telegram_bildir(f"🤝 <b>🔒 POZİSYON KAPATILDI</b>\n🎉 Net Realize Kâr: {net:.4f} USDT")
                                    pos["aktif"] = False
                            
            if en_yuksek_makaslar:
                en_yuksek_makaslar.sort(key=lambda x: x[1], reverse=True)
                print("\n💵 --- EN YÜKSEK 3 ARTI (+) MAKAS ---")
                for i, item in enumerate(en_yuksek_makaslar[:3]):
                    print(f"{i+1}. [{item[0]}] +%{item[1]:.4f} | Sp: {item[2]} | Fu: {item[3]}")
                    
        except Exception as e: print(f"❌ Döngü hatası: {e}")
        time.sleep(0.3) # Döngü nefes alma süresini 0.3 saniyeye çekerek işlemciyi ve API'yi rahatlatıyoruz

if __name__ == "__main__":
    SYMBOLS = get_all_futures_symbols()
    piyasa_verisi = {symbol: {"spot_price": None, "futures_price": None} for symbol in SYMBOLS}
    arbitraj_pozisyonlari = {symbol: {"aktif": False, "giris_makas": 0.0, "spot_adet": 0.0, "futures_adet": 0.0} for symbol in SYMBOLS}
    
    set_all_leverages()
    tum_hassasiyetleri_yukle()
    
    telegram_bildir("🤖 <b>Thread Lock Koruması Devrede!</b>\nSinyal ve koin kaymaları engellendi.")
    
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    arbitraj_tarama_dongusu()
