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
            filtered_symbols = symbols[:150]
            print(f"🎯 Binance Vadeli İşlemlerden ilk {len(filtered_symbols)} aktif sembol başarıyla çekildi.")
            return filtered_symbols
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
                
                if amt != 0 and notional_degeri >= 5.0:
                    v_adet = abs(amt)
                    
                    arbitraj_pozisyonlari[symbol_lower].update({
                        "aktif": True,
                        "giris_makas": GIRIS_MAKAS_YUZDE, 
                        "spot_adet": v_adet,             
                        "futures_adet": v_adet,
                        "spot_entry_order_id": None,      # Eski pozisyonlar için ID bulunamaz
                        "futures_entry_order_id": None
                    })
                    acik_sayac += 1
                    print(f"⚠️ AKTİF POZİSYON KİLİTLENDİ: {symbol_upper} ({notional_degeri:.2f} USDT büyüklüğünde).")
                elif amt != 0 and notional_degeri < 5.0:
                    print(f"🧹 KIRINTI ELENDİ: {symbol_upper} üzerinde {notional_degeri:.2f} USDT'lik ufak bir parça var, dahil edilmedi.")
        
        print(f"✅ Filtreleme tamamlandı. Toplam {acik_sayac} gerçek pozisyon başarıyla koruma altına alındı.")
    except Exception as e:
        print(f"❌ Pozisyonlar senkronize edilirken hata: {e}. Bot güvenlik amacıyla boş hafızayla başlıyor.")

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

    print(f"⏳ Kaldıraçlar {KALDIRAC}x olarak ayarlanıyor...")
    for symbol in SYMBOLS:
        try:
            client.futures_change_leverage(symbol=symbol.upper(), leverage=KALDIRAC)
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
    net_kazanc_usdt = brut_kazanc_usdt - toplam_kesinti_usdt
    return brut_kazanc_usdt, toplam_kesinti_usdt, net_kazanc_usdt

# 🛠️ YENİ FONKSİYON: Binance borsasından gerçek emir detaylarını okur
def get_order_real_data(symbol, order_id, is_futures=False):
    try:
        if is_futures:
            order = client.futures_get_order(symbol=symbol.upper(), orderId=order_id)
            avg_price = float(order.get("avgPrice", 0))
            cum_qty = float(order.get("executedQty", 0))
            # Basitçe vadeli komisyonunu USDT karşılığı hesaplıyoruz
            fee = (avg_price * cum_qty) * FUTURES_FEE_RATE
            return avg_price, cum_qty, fee
        else:
            order = client.get_order(symbol=symbol.upper(), orderId=order_id)
            # Spot tarafında gerçekleşen işlemleri çekiyoruz
            trades = client.get_my_trades(symbol=symbol.upper(), orderId=order_id)
            total_cost = 0.0
            total_qty = 0.0
            total_fee_usdt = 0.0
            for t in trades:
                p = float(t.get("price", 0))
                q = float(t.get("qty", 0))
                total_cost += (p * q)
                total_qty += q
                # Komisyon hesabı
                if t.get("commissionAsset") == "USDT":
                    total_fee_usdt += float(t.get("commission", 0))
                else:
                    total_fee_usdt += (p * q) * SPOT_FEE_RATE
            avg_price = total_cost / total_qty if total_qty > 0 else 0
            return avg_price, total_qty, total_fee_usdt
    except Exception:
        return 0, 0, 0

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
        if tahmini_notional < 5.1:
            return False, 0, 0, None, None
        
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
            return False, 0, 0, None, None
            
        s_id = emir_sonuclari['spot'].get('orderId')
        f_id = emir_sonuclari['futures'].get('orderId')
        return True, spot_quantity, futures_quantity, s_id, f_id
    except Exception as e:
        print(f"❌ Giriş Hatası: {e}"); return False, 0, 0, None, None

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
            if 'spot_hata' in emir_sonuclari:
                hata_mesaji += f"❌ <b>Spot Çıkış Hatası:</b> <code>{emir_sonuclari['spot_hata']}</code>\n"
            if 'futures_hata' in emir_sonuclari:
                hata_mesaji += f"❌ <b>Vadeli Çıkış Hatası:</b> <code>{emir_sonuclari['futures_hata']}</code>\n"
            hata_mesaji += "⚠️ Lütfen borsa hesabınızı manuel olarak kontrol edin!"
            print(hata_mesaji)
            telegram_bildir(hata_mesaji)
            return False, None, None
            
        s_close_id = emir_sonuclari['spot'].get('orderId')
        f_close_id = emir_sonuclari['futures'].get('orderId')
        return True, s_close_id, f_close_id
    except Exception as e:
        print(f"❌ Kritik Çıkış Hatası: {e}")
        return False, None, None

# --- 🌐 WEBSOCKET SÜRÜCÜLERİ ---
def on_spot_message(ws, message):
    data = json.loads(message)
    symbol = data.get("stream", "").split("@")[0]
    with data_lock:
        if symbol in piyasa_verisi: 
            piyasa_verisi[symbol]["spot_price"] = float(data.get("data", {}).get("p", 0))

def on_futures_message(ws, message):
    data = json.loads(message)
    symbol = data.get("stream", "").split("@")[0]
    with data_lock:
        if symbol in piyasa_verisi: 
            piyasa_verisi[symbol]["futures_price"] = float(data.get("data", {}).get("p", 0))

def start_multi_spot_ws():
    WebSocketApp(f"wss://stream.binance.com:9443/stream?streams={'/'.join([f'{s}@trade' for s in SYMBOLS])}", on_message=on_spot_message).run_forever()

def start_multi_futures_ws():
    WebSocketApp(f"wss://fstream.binance.com/stream?streams={'/'.join([f'{s}@trade' for s in SYMBOLS])}", on_message=on_futures_message).run_forever()

# --- 🎯 ARBİTRAJ MOTORU ---
def arbitraj_tarama_dongusu():
    global arbitraj_pozisyonlari
    while True:
        try:
            en_yuksek_makaslar = []
            
            with data_lock:
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
                            _, _, net = net_kar_hesapla(anlik_makas, CIKIS_MAKAS_YUZDE)
                            if net <= 0: continue
                                
                            basarili, s_qty, f_qty, s_id, f_id = execute_arbitrage_entry(symbol, spot_fiyat, futures_fiyat)
                            if basarili:
                                pos.update({
                                    "aktif": True, 
                                    "giris_makas": anlik_makas, 
                                    "spot_adet": s_qty, 
                                    "futures_adet": f_qty,
                                    "spot_entry_order_id": s_id,
                                    "futures_entry_order_id": f_id
                                })
                                # 🟢 Giriş sinyalinde yakalanan anlık fiyatlar ekleniyor
                                telegram_bildir(
                                    f"🤖 <b>İŞLEME GİRİLDİ (+)</b>\n\n"
                                    f"📊 <b>Koin:</b> {coin_label}\n"
                                    f"🟢 <b>Long (Spot) Fiyat:</b> {spot_fiyat}\n"
                                    f"🔴 <b>Short (Vadeli) Fiyat:</b> {futures_fiyat}\n"
                                    f"⚡ <b>Formül Makası:</b> +%{anlik_makas:.4f}\n"
                                    f"💵 <b>Tahmini Net Kâr:</b> {net:.4f} USDT"
                                )
                    else:
                        if anlik_makas <= CIKIS_MAKAS_YUZDE:
                            if pos["aktif"]:
                                basarili_cikis, s_close_id, f_close_id = execute_arbitrage_exit(symbol, pos["spot_adet"], pos["futures_adet"])
                                if basarili_cikis:
                                    pos["aktif"] = False
                                    
                                    # 🛠️ Binance'ten Gerçekleşen Gerçek Fiyatları Çekme Bölümü
                                    time.sleep(1.0) # Emirlerin borsada işlenmesi için 1 saniye bekle
                                    
                                    # Giriş fiyatları (Eğer bota sonradan senkronize olmadıysa)
                                    real_sp_in, _, fee_sp_in = get_order_real_data(symbol, pos["spot_entry_order_id"], False) if pos["spot_entry_order_id"] else (0, 0, 0)
                                    real_fu_in, _, fee_fu_in = get_order_real_data(symbol, pos["futures_entry_order_id"], True) if pos["futures_entry_order_id"] else (0, 0, 0)
                                    
                                    # Çıkış fiyatları
                                    real_sp_out, _, fee_sp_out = get_order_real_data(symbol, s_close_id, False) if s_close_id else (0, 0, 0)
                                    real_fu_out, _, fee_fu_out = get_order_real_data(symbol, f_close_id, True) if f_close_id else (0, 0, 0)
                                    
                                    # Eğer bot eski bir pozisyonu otomatik yakalayıp kapattıysa giriş fiyatları borsa verisinden tahmini atanır
                                    if real_sp_in == 0: real_sp_in = spot_fiyat
                                    if real_fu_in == 0: real_fu_in = futures_fiyat
                                    
                                    # Milimetrik borsa kâr hesaplaması:
                                    # Spot Kârı = (Satış Fiyatı - Alış Fiyatı) * Adet
                                    spot_pnl = (real_sp_out - real_sp_in) * pos["spot_adet"]
                                    # Vadeli Kârı = (Giriş Fiyatı - Kapanış Fiyatı) * Adet (Short olduğu için)
                                    futures_pnl = (real_fu_in - real_fu_out) * pos["futures_adet"]
                                    
                                    toplam_komisyon = fee_sp_in + fee_fu_in + fee_sp_out + fee_fu_out
                                    gercek_net_pnl = (spot_pnl + futures_pnl) - toplam_komisyon
                                    
                                    durum_etiketi = "🎉 <b>Net Realize Kâr:</b>" if gercek_net_pnl >= 0 else "❌ <b>Net Realize Zarar:</b>"
                                    
                                    # 🔴 Kapanış raporuna tüm gerçek fiyatlar ve net borsa verisi basılıyor
                                    telegram_bildir(
                                        f"🤝 <b>🔒 POZİSYON KAPATILDI</b>\n\n"
                                        f"📊 <b>Koin:</b> {coin_label}\n"
                                        f"📉 <b>Kapanış Spot Fiyatı:</b> {real_sp_out}\n"
                                        f"📈 <b>Kapanış Vadeli Fiyatı:</b> {real_fu_out}\n"
                                        f"🧮 <b>Gerçekleşen Spot PNL:</b> {spot_pnl:+.4f} USDT\n"
                                        f"🧮 <b>Gerçekleşen Vadeli PNL:</b> {futures_pnl:+.4f} USDT\n"
                                        f"⛽ <b>Toplam Borsa Komisyonu:</b> {toplam_komisyon:.4f} USDT\n\n"
                                        f"{durum_etiketi} <code>{gercek_net_pnl:.4f} USDT</code>"
                                    )
                            
            if en_yuksek_makaslar:
                en_yuksek_makaslar.sort(key=lambda x: x[1], reverse=True)
                print("\n💵 --- EN YÜKSEK 3 ARTI (+) MAKAS ---")
                for i, item in enumerate(en_yuksek_makaslar[:3]):
                    print(f"{i+1}. [{item[0]}] +%{item[1]:.4f} | Sp: {item[2]} | Fu: {item[3]}")
                    
        except Exception as e: 
            print(f"❌ Döngü hatası: {e}")
            traceback.print_exc()
        time.sleep(14.0) # 👈 4 saniyede bir güvenli tarama yapacak şekilde kalibre edildi.

if __name__ == "__main__":
    SYMBOLS = get_all_futures_symbols()
    piyasa_verisi = {symbol: {"spot_price": None, "futures_price": None} for symbol in SYMBOLS}
    arbitraj_pozisyonlari = {symbol: {"aktif": False, "giris_makas": 0.0, "spot_adet": 0.0, "futures_adet": 0.0, "spot_entry_order_id": None, "futures_entry_order_id": None} for symbol in SYMBOLS}
    
    set_all_leverages()
    tum_hassasiyetleri_yukle()
    
    senkronize_et_mevcut_pozisyonlar()
    
    telegram_bildir("🚀 <b>Fiyat ve Borsa Gerçek PNL Takipli Bot Yayında!</b>\nTaramalar 4 saniyede bir yapılıyor.")
    
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    arbitraj_tarama_dongusu()
