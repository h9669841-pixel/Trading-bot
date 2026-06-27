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

# --- 📊 ARBİTRAJ STRATEJİ VE HESAP AYARLARI (HACİMLİ PARİTELER İÇİN) ---
GIRIS_MAKAS_YUZDE = 0.50       # Derin tahtalarda %0.50 temiz ve kaymasız kâr bırakır
CIKIS_MAKAS_YUZDE = 0.05       # Makasın tam kapandığı saniyeyi kovalıyoruz

SPOT_BAKIYE = 10.0  # Spot limitine takılmamak için 11$ kilitli
FUTURES_BAKIYE = 10.0  
KALDIRAC = 1  

SPOT_FEE_RATE = 0.0750 / 100
FUTURES_FEE_RATE = 0.0450 / 100

# 🎯 HACİMLİ VE GÜVENLİ PARİTE LİSTESİ (Kör Noktasız Tarama)
SYMBOLS = ["dydxusdt", "opusdt", "arbusdt", "ldousdt", "tiausdt"]
piyasa_verisi = {}
arbitraj_pozisyonlari = {}

SPOT_HASSASIYETLERI = {}
FUTURES_HASSASIYETLERI = {}

# 🔒 Veri Güvenlik Kilidi
data_lock = threading.Lock()

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
                
                if amt != 0 and notional_degeri >= 5.0:
                    v_adet = abs(amt)
                    
                    arbitraj_pozisyonlari[symbol_lower].update({
                        "aktif": True,
                        "giris_makas": GIRIS_MAKAS_YUZDE, 
                        "spot_adet": v_adet,             
                        "futures_adet": v_adet
                    })
                    acik_sayac += 1
                    print(f"⚠️ AKTİF POZİSYON KİLİTLENDİ: {symbol_upper} ({notional_degeri:.2f} USDT büyüklüğünde).")
                elif amt != 0 and notional_degeri < 5.0:
                    print(f"🧹 KIRINTI ELENDİ: {symbol_upper} üzerinde {notional_degeri:.2f} USDT'lik ufak bir parça var.")
        
        print(f"✅ Filtreleme tamamlandı. Toplam {acik_sayac} gerçek pozisyon başarıyla koruma altına alındı.")
    except Exception as e:
        print(f"❌ Pozisyonlar senkronize edilirken hata: {e}. Bot boş hafızayla başlıyor.")

def set_all_leverages():
    print("⏳ Pozisyon Modu 'One-Way' (Tek Yönlü) olarak zorlanıyor...")
    try:
        client.futures_change_position_mode(dualSidePosition="false")
        print("✅ Pozisyon Modu başarıyla Tek Yönlü (One-Way) yapıldı.")
    except BinanceAPIException as e:
        if e.code == -4059: print("✅ Pozisyon Modu zaten Tek Yönlü (One-Way).")
        else: print(f"⚠️ Pozisyon modu değiştirilemedi: {e.message}")
    except Exception as e: print(f"⚠️ Pozisyon modu genel hata: {e}")

    print(f"⏳ Kaldıraçlar {KALDIRAC}x olarak ayarlanıyor...")
    for symbol in SYMBOLS:
        try:
            client.futures_change_leverage(symbol=symbol.upper(), leverage=KALDIRAC)
        except Exception: pass

def tum_hassasiyetleri_yukle():
    print("⏳ Lot hassasiyetleri önbelleğe alınıyor...")
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
    except Exception as e: print(f"❌ Spot hassasiyet yükleme hatası: {e}")

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
    except Exception as e: print(f"❌ Vadeli hassasiyet yükleme hatası: {e}")

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
    net_kazanc_usdt = brut_kazanc_usdt - toplam_kesinti_usdt
    return brut_kazanc_usdt, toplam_kesinti_usdt, net_kazanc_usdt

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
    except BinanceAPIException as e: sonuclar['spot_hata'] = f"Kod: {e.code}, Mesaj: {e.message}"
    except Exception as e: sonuclar['spot_hata'] = str(e)

def _hizli_cikis_gonder_futures(coin_label, quantity, sonuclar):
    try: sonuclar['futures'] = client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=quantity)
    except BinanceAPIException as e: sonuclar['futures_hata'] = f"Kod: {e.code}, Mesaj: {e.message}"
    except Exception as e: sonuclar['futures_hata'] = str(e)

def execute_arbitrage_entry(symbol, spot_price, futures_price):
    coin_label = symbol.upper()
    try:
        spot_precision = SPOT_HASSASIYETLERI.get(symbol.lower(), 2)
        futures_precision = FUTURES_HASSASIYETLERI.get(symbol.lower(), 2)
        
        raw_spot_qty = SPOT_BAKIYE / spot_price
        raw_futures_qty = (FUTURES_BAKIYE / KALDIRAC) / futures_price
        
        tahmini_notional = raw_futures_qty * KALDIRAC * futures_price
        if tahmini_notional < 5.1: return False, 0, 0
        
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
        spot_precision = SPOT_HASSASIYETLERI.get(symbol.lower(), 2)
        
        güvenli_spot_qty = spot_qty * 0.9985
        güvenli_spot_qty = float(int(güvenli_spot_qty * (10 ** spot_precision))) / (10 ** spot_precision) if spot_precision > 0 else int(güvenli_spot_qty)
        if spot_precision == 0: güvenli_spot_qty = int(güvenli_spot_qty)
            
        emir_sonuclari = {}
        t1 = threading.Thread(target=_hizli_cikis_gonder_spot, args=(coin_label, güvenli_spot_qty, emir_sonuclari))
        t2 = threading.Thread(target=_hizli_cikis_gonder_futures, args=(coin_label, futures_qty, emir_sonuclari))
        
        t1.start(); t2.start(); t1.join(); t2.join()
        
        if 'spot_hata' in emir_sonuclari or 'futures_hata' in emir_sonuclari:
            hata_mesaji = f"🚨 <b>{coin_label} POZİSYON KAPATILIRKEN HATA OLUŞTU!</b>\n"
            if 'spot_hata' in emir_sonuclari: hata_mesaji += f"❌ <b>Spot Çıkış Hatası:</b> <code>{emir_sonuclari['spot_hata']}</code>\n"
            if 'futures_hata' in emir_sonuclari: hata_mesaji += f"❌ <b>Vadeli Çıkış Hatası:</b> <code>{emir_sonuclari['futures_hata']}</code>\n"
            hata_mesaji += "⚠️ Lütfen borsa hesabınızı manuel olarak kontrol edin!"
            print(hata_mesaji)
            telegram_bildir(hata_mesaji)
            return False
            
        return True
    except Exception as e:
        print(f"❌ Kritik Çıkış Hatası: {e}"); return False

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
                    
                    if not pos["aktif"]:
                        # 5 koinin durumunu saniyede bir ekrana basarak canlı izlemeni sağlar
                        if anlik_makas > 0.10: 
                            print(f"📊 [İZLEME] {coin_label} Makas: +%{anlik_makas:.3f} | Sp: {spot_fiyat} | Fu: {futures_fiyat}")
                        
                        if anlik_makas >= GIRIS_MAKAS_YUZDE:
                            _, _, net = net_kar_hesapla(anlik_makas, CIKIS_MAKAS_YUZDE)
                            if net <= 0: continue
                                
                            basarili, s_qty, f_qty = execute_arbitrage_entry(symbol, spot_fiyat, futures_fiyat)
                            if basarili:
                                pos.update({"aktif": True, "giris_makas": anlik_makas, "spot_adet": s_qty, "futures_adet": f_qty})
                                telegram_bildir(f"🤖 <b>{coin_label} İŞLEME GİRİLDİ (+)</b>\n\n⚡ <b>Giriş Makası:</b> +%{anlik_makas:.4f}\n💵 <b>Tahmini Net Kâr:</b> {net:.4f} USDT")
                    else:
                        print(f"⏳ [POZİSYONDASIN] {coin_label} Hedef Kapanış: +%{CIKIS_MAKAS_YUZDE:.2f} | Anlık Makas: +%{anlik_makas:.4f}")
                        if anlik_makas <= CIKIS_MAKAS_YUZDE:
                            if pos["aktif"]:
                                if execute_arbitrage_exit(symbol, pos["spot_adet"], pos["futures_adet"]):
                                    brut, kesinti, net = net_kar_hesapla(pos["giris_makas"], anlik_makas)
                                    telegram_bildir(f"🤝 <b>🔒 POZİSYON KAPATILDI</b>\n🎉 Koin: {coin_label}\nNet Realize Kâr: {net:.4f} USDT")
                                    pos["aktif"] = False
                            
        except Exception as e: 
            print(f"❌ Döngü hatası: {e}")
            traceback.print_exc()
        time.sleep(1.0) # 👈 1 saniyede bir hızlı tarama döngüsü

if __name__ == "__main__":
    # 🎯 Tamamen güvenli ve hacimli seçkin koin listesi
    SYMBOLS = ["dydxusdt", "opusdt", "arbusdt", "ldousdt", "tiausdt"]
    
    piyasa_verisi = {symbol: {"spot_price": None, "futures_price": None} for symbol in SYMBOLS}
    arbitraj_pozisyonlari = {symbol: {"aktif": False, "giris_makas": 0.0, "spot_adet": 0.0, "futures_adet": 0.0} for symbol in SYMBOLS}
    
    set_all_leverages()
    tum_hassasiyetleri_yukle()
    senkronize_et_mevcut_pozisyonlar()
    
    telegram_bildir("🎯 <b>5 Hacimli Koin Odaklı Güvenli Arbitraj Botu Başlatıldı!</b>")
    
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    arbitraj_tarama_dongusu()
