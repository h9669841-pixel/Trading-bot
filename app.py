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
GIRIS_MAKAS_YUZDE = 0.45       # Sadece +%0.45 ve üzerindeki fırsatları avlar
CIKIS_MAKAS_YUZDE = 0.10       # Makas +%0.10'un altına daraldığında kârı kilitler ve çıkar

# 25 USDT cüzdan bakiyenizin ucu ucuna sıkışmaması için güvenlik tamponlu bakiye
SPOT_BAKIYE = 22.0  
FUTURES_BAKIYE = 22.0

SPOT_FEE_RATE = 0.0750 / 100
FUTURES_FEE_RATE = 0.0450 / 100

SYMBOLS = []
piyasa_verisi = {}
arbitraj_pozisyonlari = {}
SEMBOL_HASSASIYETLERI = {}

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
            # İlk 150 koini filtrele ve döndür
            filtered_symbols = symbols[:150]
            print(f"🎯 Binance Vadeli İşlemlerden ilk {len(filtered_symbols)} aktif sembol başarıyla çekildi.")
            return filtered_symbols
    except Exception as e:
        print(f"❌ Koin listesi çekilirken hata oluştu: {e}")
        traceback.print_exc()
    return ["btcusdt", "ethusdt", "solusdt", "xrpusdt"]

def set_all_leverages():
    print("⏳ Kaldıraçlar 5x ve Cross olarak ayarlanıyor...")
    for symbol in SYMBOLS:
        try:
            client.futures_change_leverage(symbol=symbol.upper(), leverage=5)
            time.sleep(0.05)
        except Exception:
            pass

def tum_hassasiyetleri_yukle():
    print("⏳ Lot hassasiyetleri önbelleğe alınıyor...")
    try:
        exchange_info = client.get_exchange_info()
        for market in exchange_info['symbols']:
            sym = market['symbol'].lower()
            if sym in SYMBOLS:
                for f in market['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size_str = str(f['stepSize']).rstrip('0')
                        precision = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
                        SEMBOL_HASSASIYETLERI[sym] = precision
        print(f"✅ Hassasiyet haritası kaydedildi. Toplam: {len(SEMBOL_HASSASIYETLERI)}")
    except Exception as e:
        print(f"❌ Hassasiyet yükleme hatası: {e}")
        for sym in SYMBOLS: SEMBOL_HASSASIYETLERI[sym] = 2

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

# --- 🚀 REKABETÇİ VE HIZLI EMİR MOTORLARI ---
def _hizli_emir_gonder_spot(coin_label, quantity, sonuclar):
    try: sonuclar['spot'] = client.create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=quantity)
    except Exception as e: sonuclar['spot_hata'] = e

def _hizli_emir_gonder_futures(coin_label, quantity, sonuclar):
    try:
        client.futures_change_leverage(symbol=coin_label, leverage=5)
        sonuclar['futures'] = client.futures_create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=quantity)
    except Exception as e: sonuclar['futures_hata'] = e

def _hizli_cikis_gonder_spot(coin_label, quantity, sonuclar):
    try: sonuclar['spot'] = client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=quantity)
    except Exception as e: sonuclar['spot_hata'] = e

def _hizli_cikis_gonder_futures(coin_label, quantity, sonuclar):
    try: sonuclar['futures'] = client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=quantity)
    except Exception as e: sonuclar['futures_hata'] = e

def execute_arbitrage_entry(symbol, spot_price, futures_price):
    coin_label = symbol.upper()
    try:
        precision = SEMBOL_HASSASIYETLERI.get(symbol.lower(), 2)
        raw_spot_qty = SPOT_BAKIYE / spot_price
        raw_futures_qty = FUTURES_BAKIYE / futures_price
        
        spot_quantity = float(int(raw_spot_qty * (10 ** precision))) / (10 ** precision) if precision > 0 else int(raw_spot_qty)
        futures_quantity = float(int(raw_futures_qty * (10 ** precision))) / (10 ** precision) if precision > 0 else int(raw_futures_qty)
        
        if precision == 0:
            spot_quantity = int(spot_quantity)
            futures_quantity = int(futures_quantity)
            
        print(f"⚙️ {coin_label} İşleme Gönderiliyor | Adetler -> Sp: {spot_quantity}, Fu: {futures_quantity}")
        
        emir_sonuclari = {}
        t1 = threading.Thread(target=_hizli_emir_gonder_spot, args=(coin_label, spot_quantity, emir_sonuclari))
        t2 = threading.Thread(target=_hizli_emir_gonder_futures, args=(coin_label, futures_quantity, emir_sonuclari))
        
        t1.start(); t2.start(); t1.join(); t2.join()
        
        if 'spot_hata' in emir_sonuclari or 'futures_hata' in emir_sonuclari:
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
                
                # Sadece pozitif (+) makasları sıralamaya ve radara ekle
                if anlik_makas > 0:
                    en_yuksek_makaslar.append((coin_label, anlik_makas, spot_fiyat, futures_fiyat))
                
                pos = arbitraj_pozisyonlari[symbol]
                
                if not pos["aktif"]:
                    # Sadece belirlenen giriş limitini aşan ARTI (+) makaslarda işleme gir
                    if anlik_makas >= GIRIS_MAKAS_YUZDE:
                        net = net_kar_hesapla(anlik_makas, CIKIS_MAKAS_YUZDE)
                        if net <= 0: continue
                            
                        basarili, s_qty, f_qty = execute_arbitrage_entry(symbol, spot_fiyat, futures_fiyat)
                        if basarili:
                            pos.update({"aktif": True, "giris_makas": anlik_makas, "spot_adet": s_qty, "futures_adet": f_qty})
                            telegram_bildir(f"🤖 <b>İŞLEME GİRİLDİ (+)</b>\n\n📊 <b>Koin:</b> {coin_label}\n⚡ <b>Makas:</b> +%{anlik_makas:.4f}\n💵 <b>Tahmini Net Kâr:</b> {net:.4f} USDT")
                else:
                    # Pozisyon kapatma kontrolü
                    if anlik_makas <= CIKIS_MAKAS_YUZDE:
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
        time.sleep(0.2)

if __name__ == "__main__":
    SYMBOLS = get_all_futures_symbols()
    piyasa_verisi = {symbol: {"spot_price": None, "futures_price": None} for symbol in SYMBOLS}
    arbitraj_pozisyonlari = {symbol: {"aktif": False, "giris_makas": 0.0, "spot_adet": 0.0, "futures_adet": 0.0} for symbol in SYMBOLS}
    set_all_leverages(); tum_hassasiyetleri_yukle()
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    arbitraj_tarama_dongusu()
