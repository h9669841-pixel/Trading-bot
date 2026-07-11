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
GIRIS_MAKAS_YUZDE = 10.50       
CIKIS_MAKAS_YUZDE = 0.05       

SPOT_BAKIYE = 11.0  
FUTURES_BAKIYE = 11.0  
KALDIRAC = 1  

SPOT_FEE_RATE = 0.0750 / 100
FUTURES_FEE_RATE = 0.0450 / 100

# 🎯 [GENİŞLETİLMİŞ HAVUZ] - Tam 50 Adet En Yüksek Hacimli Arbitraj Koini
ADAY_SYMBOLS = [
    # --- Mevcut Güvenli Koinler (25 Adet) ---
    "dydxusdt", "opusdt", "arbusdt", "ldousdt", "tiausdt", 
    "solusdt", "avaxusdt", "linkusdt", "suiusdt", "ethusdt", 
    "bnbusdt", "xrpusdt", "adausdt", "dotusdt", "maticusdt",
    "btcusdt", "dogeusdt", "shibusdt", "nearusdt", "ftmusdt",
    "atomusdt", "ltcusdt", "uniusdt", "aptusdt", "filusdt",
    
    # --- Yeni Eklenen Yüksek Hacimli Koinler (25 Adet) ---
    "injusdt", "seiusdt", "fetusdt", "renderusdt", "flokusdt",
    "pepeusdt", "bonkusdt", "wifusdt", "jupusdt", "pythusdt",
    "avaxusdt", "galadusdt", "grtusdt", "avaxusdt", "stxusdt",
    "imxusdt", "rndrusdt", "gmtusdt", "apeusdt", "axsusdt",
    "sandusdt", "manausdt", "chzusdt", "etcusdt", "vetusdt"
]

# Çift kayıtları (varsa) temizlemek için akıllı filtreleme
ADAY_SYMBOLS = list(set(ADAY_SYMBOLS))

SYMBOLS = [] 
piyasa_verisi = {}
arbitraj_pozisyonlari = {}
SPOT_HASSASIYETLERI = {}
FUTURES_HASSASIYETLERI = {}

# 🔒 Veri Güvenlik Kilidi
data_lock = threading.Lock()

# --- 🛡️ DİNAMİK VE KONTROLLÜ COIN EKLEME MOTORU ---
def kontrollu_coin_ekle(coin_adi):
    coin_lower = coin_adi.lower().strip()
    coin_upper = coin_lower.upper()
    
    if coin_lower in SYMBOLS:
        return True
        
    try:
        # 1. Kontrol: Spot aktif mi?
        spot_info = client.get_symbol_info(coin_upper)
        if not spot_info or spot_info.get('status') != 'TRADING':
            return False
            
        # 2. Kontrol: Vadeli aktif mi?
        try:
            client.futures_change_leverage(symbol=coin_upper, leverage=KALDIRAC)
        except Exception:
            return False
            
        # 3. Kontrol: Likidite & Hacim (Son 24 saatlik hacim > 1,000,000 USDT)
        ticker_24h = client.get_ticker(symbol=coin_upper)
        hacim_usdt = float(ticker_24h.get('quoteVolume', 0))
        if hacim_usdt < 850000.0:
            print(f"⚠️ {coin_upper} Hacmi yetersiz ({hacim_usdt/1000:,.0f}K USDT). Güvenlik için elendi.")
            return False

        # Lot filtre hassasiyetini çek
        for f in spot_info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step_size_str = str(f['stepSize']).rstrip('0')
                precision = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
                SPOT_HASSASIYETLERI[coin_lower] = precision
                
        FUTURES_HASSASIYETLERI[coin_lower] = SPOT_HASSASIYETLERI[coin_lower]

        with data_lock:
            SYMBOLS.append(coin_lower)
            piyasa_verisi[coin_lower] = {"spot_price": None, "futures_price": None}
            arbitraj_pozisyonlari[coin_lower] = {
                "aktif": False, "giris_makas": 0.0, "spot_adet": 0.0, "futures_adet": 0.0,
                "giris_onay_sayac": 0, "cikis_onay_sayac": 0, "giris_spot_usdt": 0.0, "giris_futures_usdt": 0.0
            }
        return True
        
    except Exception:
        return False

def senkronize_et_mevcut_pozisyonlar():
    print("⏳ Binance Vadeli İşlemlerdeki aktif pozisyonlarınız taranıyor...")
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
                        "aktif": True, "giris_makas": GIRIS_MAKAS_YUZDE, "spot_adet": v_adet, "futures_adet": v_adet,
                        "giris_onay_sayac": 0, "cikis_onay_sayac": 0, "giris_spot_usdt": SPOT_BAKIYE, "giris_futures_usdt": FUTURES_BAKIYE
                    })
                    acik_sayac += 1
                    print(f"⚠️ AKTİF POZİSYON KİLİTLENDİ: {symbol_upper} ({notional_degeri:.2f} USDT büyüklüğünde).")
        print(f"✅ Filtreleme tamamlandı. Toplam {acik_sayac} gerçek pozisyon başarıyla koruma altına alındı.")
    except Exception as e:
        print(f"❌ Pozisyonlar senkronize edilirken hata: {e}. Bot boş hafızayla başlıyor.")

def tum_futures_hassasiyetlerini_yukle():
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
    except Exception as e: print(f"❌ Vadeli hassasiyet yükleme hatası: {e}")

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
    except Exception as e: sonuclar['spot_hata'] = str(e)

def _hizli_cikis_gonder_futures(coin_label, quantity, sonuclar):
    try: sonuclar['futures'] = client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=quantity)
    except Exception as e: sonuclar['futures_hata'] = str(e)

def execute_arbitrage_entry(symbol, spot_price, futures_price):
    coin_label = symbol.upper()
    try:
        spot_precision = SPOT_HASSASIYETLERI.get(symbol.lower(), 2)
        futures_precision = FUTURES_HASSASIYETLERI.get(symbol.lower(), 2)
        
        raw_spot_qty = SPOT_BAKIYE / spot_price
        raw_futures_qty = (FUTURES_BAKIYE / KALDIRAC) / futures_price
        
        tahmini_notional = raw_futures_qty * KALDIRAC * futures_price
        if tahmini_notional < 5.1: return False, 0, 0, 0, 0
        
        spot_quantity = float(int(raw_spot_qty * (10 ** spot_precision))) / (10 ** spot_precision) if spot_precision > 0 else int(raw_spot_qty)
        futures_quantity = float(int(raw_futures_qty * (10 ** futures_precision))) / (10 ** futures_precision) if futures_precision > 0 else int(raw_futures_qty)
        
        if spot_precision == 0: spot_quantity = int(spot_quantity)
        if futures_precision == 0: futures_quantity = int(futures_quantity)
            
        emir_sonuclari = {}
        t1 = threading.Thread(target=_hizli_emir_gonder_spot, args=(coin_label, spot_quantity, emir_sonuclari))
        t2 = threading.Thread(target=_hizli_emir_gonder_futures, args=(coin_label, futures_quantity, emir_sonuclari))
        
        t1.start(); t2.start(); t1.join(); t2.join()
        
        if 'spot_hata' in emir_sonuclari or 'futures_hata' in emir_sonuclari:
            if 'futures' in emir_sonuclari and 'spot_hata' in emir_sonuclari:
                try: client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=futures_quantity)
                except Exception: pass
            if 'spot' in emir_sonuclari and 'futures_hata' in emir_sonuclari:
                try: client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=spot_quantity)
                except Exception: pass
            return False, 0, 0, 0, 0
            
        gercek_spot_usdt = float(emir_sonuclari['spot'].get('cummulativeQuoteQty', SPOT_BAKIYE))
        gercek_futures_usdt = futures_quantity * futures_price
        
        return True, spot_quantity, futures_quantity, gercek_spot_usdt, gercek_futures_usdt
    except Exception as e:
        print(f"❌ Giriş Hatası: {e}"); return False, 0, 0, 0, 0

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
        
        cikis_spot_usdt = float(emir_sonuclari['spot'].get('cummulativeQuoteQty', 0)) if 'spot' in emir_sonuclari else 0.0
        with data_lock:
            f_price = piyasa_verisi[symbol]["futures_price"] or 0
        cikis_futures_usdt = futures_qty * f_price
            
        return True, cikis_spot_usdt, cikis_futures_usdt
    except Exception as e:
        print(f"❌ Kritik Çıkış Hatası: {e}"); return False, 0, 0

# --- 🌐 WEBSOCKET SÜRÜCÜLERİ ---
def on_spot_message(ws, message):
    data = json.loads(message)
    stream_name = data.get("stream", "")
    symbol = stream_name.split("@")[0].lower()
    with data_lock:
        if symbol in piyasa_verisi: piyasa_verisi[symbol]["spot_price"] = float(data.get("data", {}).get("p", 0))

def on_futures_message(ws, message):
    data = json.loads(message)
    stream_name = data.get("stream", "")
    symbol = stream_name.split("@")[0].lower()
    with data_lock:
        if symbol in piyasa_verisi: piyasa_verisi[symbol]["futures_price"] = float(data.get("data", {}).get("p", 0))

def start_multi_spot_ws():
    streams = "/".join([f"{s}@trade" for s in SYMBOLS])
    WebSocketApp(f"wss://stream.binance.com:9443/stream?streams={streams}", on_message=on_spot_message).run_forever()

def start_multi_futures_ws():
    streams = "/".join([f"{s}@trade" for s in SYMBOLS])
    WebSocketApp(f"wss://fstream.binance.com/stream?streams={streams}", on_message=on_futures_message).run_forever()

# --- 🎯 ARBİTRAJ MOTORU ---
def arbitraj_tarama_dongusu():
    global arbitraj_pozisyonlari
    while True:
        try:
            aktif_firsatlar = []
            with data_lock:
                for symbol in SYMBOLS:
                    spot_fiyat = piyasa_verisi[symbol]["spot_price"]
                    futures_fiyat = piyasa_verisi[symbol]["futures_price"]
                    
                    if not spot_fiyat or not futures_fiyat: continue
                    anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
                    coin_label = symbol.upper()
                    pos = arbitraj_pozisyonlari[symbol]
                    
                    if not pos["aktif"]:
                        pos["cikis_onay_sayac"] = 0
                        aktif_firsatlar.append({"symbol": coin_label, "makas": anlik_makas, "sp": spot_fiyat, "fu": futures_fiyat, "onay_durum": pos["giris_onay_sayac"]})
                        
                        if anlik_makas >= GIRIS_MAKAS_YUZDE:
                            pos["giris_onay_sayac"] += 1
                            if pos["giris_onay_sayac"] >= 2:
                                _, _, net = net_kar_hesapla(anlik_makas, CIKIS_MAKAS_YUZDE)
                                if net <= 0: {pos.update({"giris_onay_sayac": 0})}; continue
                                    
                                basarili, s_qty, f_qty, sp_usdt, fu_usdt = execute_arbitrage_entry(symbol, spot_fiyat, futures_fiyat)
                                if basarili:
                                    pos.update({"aktif": True, "giris_makas": anlik_makas, "spot_adet": s_qty, "futures_adet": f_qty, "giris_onay_sayac": 0, "giris_spot_usdt": sp_usdt, "giris_futures_usdt": fu_usdt})
                                    telegram_bildir(f"🤖 <b>{coin_label} İŞLEME GİRİLDİ (+)</b>\n⚡ Maliyet: {sp_usdt + fu_usdt:.2f} USDT")
                        else: pos["giris_onay_sayac"] = 0
                    else:
                        pos["giris_onay_sayac"] = 0
                        if anlik_makas <= CIKIS_MAKAS_YUZDE:
                            pos["cikis_onay_sayac"] += 1
                            if pos["cikis_onay_sayac"] >= 2:
                                basarili_cikis, cikis_sp_usdt, cikis_fu_usdt = execute_arbitrage_exit(symbol, pos["spot_adet"], pos["futures_adet"])
                                if basarili_cikis:
                                    spot_farki = cikis_sp_usdt - pos["giris_spot_usdt"]
                                    futures_farki = pos["giris_futures_usdt"] - cikis_fu_usdt
                                    gercek_net_kar = spot_farki + futures_farki
                                    
                                    durum = f"🟢 <b>KÂR:</b> +{gercek_net_kar:.4f} USDT" if gercek_net_kar > 0 else f"🔴 <b>ZARAR:</b> {gercek_net_kar:.4f} USDT"
                                    telegram_bildir(f"🤝 <b>{coin_label} İşlemi Kapandı</b>\n{durum}")
                                    pos["aktif"] = False; pos["cikis_onay_sayac"] = 0
                        else: pos["cikis_onay_sayac"] = 0
            
            if aktif_firsatlar:
                aktif_firsatlar.sort(key=lambda x: x["makas"], reverse=True)
                print("\n🔥 --- EN YÜKSEK MAKASLI İLK 3 PARİTE ---")
                for f in aktif_firsatlar[:3]:
                    onay_notu = f" [Teyit: {f['onay_durum']}/2]" if f['onay_durum'] > 0 else ""
                    print(f"📊 [İZLEME] {f['symbol']} Makas: +%{f['makas']:.3f} | Sp: {f['sp']} | Fu: {f['fu']}{onay_notu}")
                print("---------------------------------------------------------")
                            
        except Exception as e: print(f"❌ Döngü hatası: {e}")
        time.sleep(1.0) 

if __name__ == "__main__":
    print("⏳ 50 Aday coin havuzu filtreleniyor...")
    for coin in ADAY_SYMBOLS:
        kontrollu_coin_ekle(coin)
        
    print(f"🚀 Filtreleme Bitti! Toplam {len(SYMBOLS)} adet güvenli pariteyle motor kuruldu.")
    
    tum_futures_hassasiyetlerini_yukle()
    senkronize_et_mevcut_pozisyonlar()
    
    print("⏳ WebSocket hatlarına bağlanılıyor...")
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    
    time.sleep(4.0) 
    telegram_bildir(f"🎯 <b>50 Parite Kapasiteli Bot Başlatıldı! Aktif Koin Sayısı: {len(SYMBOLS)}</b>")
    arbitraj_tarama_dongusu()
