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

# 💰 10 DOLARLIK İŞLEM AYARLARI
SPOT_BAKIYE = 10.0  
FUTURES_BAKIYE = 10.0  
KALDIRAC = 1  

SPOT_FEE_RATE = 0.0750 / 100
FUTURES_FEE_RATE = 0.0450 / 100

SYMBOLS = []
piyasa_verisi = {}
arbitraj_pozisyonlari = {}

SPOT_HASSASIYETLERI = {}
FUTURES_HASSASIYETLERI = {}
PRICE_HASSASIYETLERI = {}

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

def senkronize_et_mevcut_pozisyonlar():
    print("⏳ Binance Vadeli İşlemlerdeki aktif pozisyonlarınız kırıntı filtresiyle taranıyor...")
    try:
        account_info = client.futures_account()
        positions = account_info.get("positions", [])
        
        acik_sayac = 0
        for pos in positions:
            symbol_upper = pos.get("symbol")
            symbol_lower = symbol_upper.lower()
            
            if symbol_lower in arbitraj_pozisyonlari:
                amt = float(pos.get("positionAmt", 0))
                notional_degeri = abs(float(pos.get("notional", 0)))
                
                if amt != 0 and notional_degeri >= 4.0:
                    v_adet = abs(amt)
                    arbitraj_pozisyonlari[symbol_lower].update({
                        "durum": "CIKIS_BEKLIYOR",
                        "spot_adet": v_adet,             
                        "futures_adet": v_adet
                    })
                    acik_sayac += 1
                    print(f"⚠️ AKTİF POZİSYON KİLİTLENDİ: {symbol_upper} ({notional_degeri:.2f} USDT büyüklüğünde).")
        print(f"✅ Filtreleme tamamlandı. Toplam {acik_sayac} pozisyon hafızaya alındı.")
    except Exception as e:
        print(f"❌ Pozisyonlar senkronize edilirken hata: {e}")

def set_all_leverages():
    print("⏳ Pozisyon Modu 'One-Way' (Tek Yönlü) olarak zorlanıyor...")
    try:
        client.futures_change_position_mode(dualSidePosition="false")
        print("✅ Pozisyon Modu başarıyla Tek Yönlü (One-Way) yapıldı.")
    except BinanceAPIException as e:
        if e.code == -4059: 
            print("✅ Pozisyon Modu zaten Tek Yönlü (One-Way). Değişikliğe gerek yok.")
        else:
            print(f"⚠️ Pozisyon modu değiştirilemedi: {e.message}")
    except Exception as e:
        print(f"⚠️ Pozisyon modu genel hata: {e}")

    print(f"⏳ Kaldıraçlar {KALDIRAC}x olarak ayarlanıyor...")
    for symbol in SYMBOLS:
        try:
            client.futures_change_leverage(symbol=symbol.upper(), leverage=KALDIRAC)
            time.sleep(0.01)
        except Exception:
            pass

def tum_hassasiyetleri_yukle():
    print("⏳ Spot, Vadeli Lot ve Fiyat hassasiyetleri önbelleğe alınıyor...")
    try:
        spot_info = client.get_exchange_info()
        for market in spot_info['symbols']:
            sym = market['symbol'].lower()
            if sym in SYMBOLS:
                PRICE_HASSASIYETLERI[sym] = 2
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
        
        spot_order = client.create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_LIMIT, timeInForce=TIME_IN_FORCE_GTC, quantity=spot_quantity, price=s_price_str)
        futures_order = client.futures_create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_LIMIT, timeInForce=TIME_IN_FORCE_GTC, quantity=futures_quantity, price=f_price_str)
        
        return True, spot_quantity, futures_quantity, spot_order.get('orderId'), futures_order.get('orderId')
    except Exception as e:
        print(f"❌ Limit Giriş Hatası: {e}")
        return False, 0, 0, None, None

# --- 🚀 LİMİT EMİR MOTORLARI (KÂR KAPANIŞI) ---
def execute_limit_arbitrage_exit_targets(symbol, spot_qty, futures_qty, target_spot_exit_price, target_futures_exit_price):
    coin_label = symbol.upper()
    try:
        price_precision = PRICE_HASSASIYETLERI.get(symbol.lower(), 4)
        spot_precision = SPOT_HASSASIYETLERI.get(symbol.lower(), 2)
        
        güvenli_spot_qty = round(spot_qty * 0.9985, spot_precision) if spot_precision > 0 else int(spot_qty * 0.9985)
        
        s_exit_price_str = f"{target_spot_exit_price:.{price_precision}f}"
        f_exit_price_str = f"{target_futures_exit_price:.{price_precision}f}"
        
        print(f"🎯 {coin_label} KÂR LİMİT EMİRLERİ YAZILIYOR -> Sp Satış: {s_exit_price_str} | Fu Alış: {f_exit_price_str}")
        
        spot_close_order = client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_LIMIT, timeInForce=TIME_IN_FORCE_GTC, quantity=güvenli_spot_qty, price=s_exit_price_str)
        futures_close_order = client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_LIMIT, timeInForce=TIME_IN_FORCE_GTC, quantity=futures_qty, price=f_exit_price_str)
        
        return True, spot_close_order.get('orderId'), futures_close_order.get('orderId')
    except Exception as e:
        print(f"❌ Kâr Limit Emirleri Hatası: {e}")
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
            # 🛡️ GLOBAL BAKİYE KİLİDİ: Eğer halihazırda pusuya yatmış bir koin varsa yeni koin arama.
            su_anki_aktif_islem_sayisi = sum(1 for sym in SYMBOLS if arbitraj_pozisyonlari[sym]["durum"] != "BOS")
            if su_anki_aktif_islem_sayisi >= 1:
                print("⏳ Bakiyeniz ($10) şu anda bir koinde kilitli veya pusuda bekliyor. Tarama askıya alındı...")
                time.sleep(4.0)
                continue

            with data_lock:
                for symbol in SYMBOLS:
                    spot_fiyat = piyasa_verisi[symbol]["spot_price"]
                    futures_fiyat = piyasa_verisi[symbol]["futures_price"]
                    if not spot_fiyat or not futures_fiyat: continue
                        
                    anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
                    coin_label = symbol.upper()
                    pos = arbitraj_pozisyonlari[symbol]
                    
                    # STAGE 1: HİÇ POZİSYON YOKSA VE GİRİŞ ŞARTI OLUŞTUYSA
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
                                    "giris_futures_fiyat": futures_fiyat,
                                    "emir_giris_zamani": time.time()
                                })
                                telegram_bildir(f"⏳ <b>{coin_label} 10$ Limit Giriş Emirleri İletildi.</b>\nSp Alış: {spot_fiyat}\nFu Short: {futures_fiyat}")

                    # STAGE 2: GİRİŞ EMİRLERİ VERİLDİYSE -> ZAMAN AŞIMI GÜVENLİK KORUMASI (100 SANİYE)
                    elif pos["durum"] == "GIRIS_BEKLIYOR":
                        s_status = client.get_order(symbol=coin_label, orderId=pos["spot_entry_id"]).get("status")
                        f_status = client.futures_get_order(symbol=coin_label, orderId=pos["futures_entry_id"]).get("status")
                        
                        if s_status == "FILLED" and f_status == "FILLED":
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
                                    f"🚀 <b>{coin_label} GİRİŞLER DOLDU!</b>\n"
                                    f"🔒 Kâr Limitleri Tahtaya Asıldı:\n"
                                    f"📈 Spot Satış: {hedef_spot_cikis:.4f}\n"
                                    f"📉 Vadeli Alış: {hedef_futures_cikis:.4f}"
                                )
                        
                        # 🛡️ Acil Durum Yönetimi: 100 saniye boyunca limit emirler tahtada dolmazsa temizle
                        elif time.time() - pos.get("emir_giris_zamani", time.time()) > 100.0:
                            print(f"🚨 100sn Süre Aşımı: {coin_label} emirleri iptal ediliyor...")
                            
                            if s_status != "FILLED":
                                try: client.cancel_order(symbol=coin_label, orderId=pos["spot_entry_id"])
                                except Exception: pass
                            if f_status != "FILLED":
                                try: client.futures_cancel_order(symbol=coin_label, orderId=pos["futures_entry_id"])
                                except Exception: pass
                                
                            # Yarım bacak koruma kontrolü (Piyasa emriyle eşitleme)
                            if s_status == "FILLED" and f_status != "FILLED":
                                client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=pos["spot_adet"])
                                telegram_bildir(f"🛡️ <b>BACAK RİSKİ ÖNLENDİ:</b> 100sn içinde vadeli dolmadığı için spotlar piyasadan geri satıldı.")
                                
                            elif f_status == "FILLED" and s_status != "FILLED":
                                client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=pos["futures_adet"])
                                telegram_bildir(f"🛡️ <b>BACAK RİSKİ ÖNLENDİ:</b> 100sn içinde spot dolmadığı için vadeli short piyasadan kapatıldı.")
                            else:
                                print(f"🧹 {coin_label}: Eşleşmeyen pasif limit emirler 100sn sonunda temizlendi. Risk yok.")
                                
                            pos.update({"durum": "BOS", "spot_entry_id": None, "futures_entry_id": None})

                    # STAGE 3: KÂR EMİRLERİ TAHTADA ASILIYSA
                    elif pos["durum"] == "CIKIS_BEKLIYOR":
                        if not pos.get("spot_exit_id") or not pos.get("futures_exit_id"): continue
                            
                        s_cl_status = client.get_order(symbol=coin_label, orderId=pos["spot_exit_id"]).get("status")
                        f_cl_status = client.futures_get_order(symbol=coin_label, orderId=pos["futures_exit_id"]).get("status")
                        
                        if s_cl_status == "FILLED" and f_cl_status == "FILLED":
                            telegram_bildir(f"🎉 <b>🔒 ARBİTRAJ TAMAMLANDI (Limit PNL)</b>\n📊 <b>Koin:</b> {coin_label}\n💰 Limit kâr hedefleri tam fiyattan gerçekleşti!")
                            pos.update({"durum": "BOS", "spot_entry_id": None, "futures_entry_id": None, "spot_exit_id": None, "futures_exit_id": None})

        except Exception as e: 
            print(f"❌ Döngü hatası: {e}")
            traceback.print_exc()
        time.sleep(4.0)

if __name__ == "__main__":
    SYMBOLS = get_all_futures_symbols()
    piyasa_verisi = {symbol: {"spot_price": None, "futures_price": None} for symbol in SYMBOLS}
    arbitraj_pozisyonlari = {symbol: {"durum": "BOS", "spot_adet": 0.0, "futures_adet": 0.0, "spot_entry_id": None, "futures_entry_id": None, "spot_exit_id": None, "futures_exit_id": None} for symbol in SYMBOLS}
    
    set_all_leverages()
    tum_hassasiyetleri_yukle()
    
    senkronize_et_mevcut_pozisyonlar()
    
    telegram_bildir("🤖 <b>10$ Ayarlı / 100sn Korumalı Limit Arbitraj Botu Başlatıldı!</b>\nFiyat kaymaları engellendi, pusu moduna geçildi.")
    
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    arbitraj_tarama_dongusu()
