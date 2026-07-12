import os
import json
import time
import requests
import threading
import traceback
import math
from datetime import datetime
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException
from websocket import WebSocketApp

# --- 🔑 GÜVENLİK VE API AYARLARI ---
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
PROXY_URL = os.environ.get("PROXY_URL")

requests_proxies = None
if PROXY_URL:
    print(f"🌐 Statik IP tüneli hazırlandı. Sadece alım/satım işlemlerinde tetiklenecek.")
    requests_proxies = {"http": PROXY_URL, "https": PROXY_URL}

client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
if requests_proxies:
    client.session.proxies = requests_proxies

class TrendBotConfig:
    def __init__(self):
        self.TIMEFRAME = Client.KLINE_INTERVAL_15MINUTE  
        self.ISLEM_MARJIN = 1.0        
        self.KALDIRAC = 20             
        self.MAX_ACIK_POZISYON = 10     
        self.TAHMINI_TP_YUZDE = 0.010   # Fiyat yönümüzde net %1 giderse kapatır.
        self.BOT_CALISIYOR = True
        self.COOLDOWN_SURESI = 300     # ⏱️ Zararla kapanan veya yeni işlem sonrası giriş engeli (5 dk)
        
        # === Pine Script Strateji Parametreleri ===
        self.BB_LEN = 20
        self.BB_MULT = 2.0
        self.KC_MULT = 1.5
        self.BARS_CHECK = 2            
        
        self.USE_ATR_FILTER = True
        self.ATR_LEN = 14
        self.MAX_EXT_LONG_ATR = 0.8
        self.MAX_EXT_SHORT_ATR = 1.6
        
        self.USE_RSI_FILTER = True
        self.RSI_LEN = 14
        self.RSI_OB = 70
        self.RSI_OS = 30

config = TrendBotConfig()

SYMBOLS = [] 
piyasa_verisi = {}
aktif_pozisyonlar = {}
FUTURES_HASSASIYETLERI = {}
son_islem_zamanlari = {}        # Sadece YENİ GİRİŞLERİ engellemek için cooldown takibi
pozisyon_acilis_zamanlari = {}  # Hayalet kapanışları engellemek için milisaniyelik takip

data_lock = threading.Lock()
listen_key = None

# --- 🛠️ MATEMATİKSEL İNDİKATÖR MOTORU (PINE SCRIPT UYUMLU) ---

def sma(seri, periyod):
    if len(seri) < periyod: return [0.0] * len(seri)
    res = []
    current_sum = sum(seri[:periyod])
    res.append(current_sum / periyod)
    for i in range(periyod, len(seri)):
        current_sum += seri[i] - seri[i - periyod]
        res.append(current_sum / periyod)
    return [0.0] * (periyod - 1) + res

def stdev(seri, periyod):
    if len(seri) < periyod: return [0.0] * len(seri)
    res = [0.0] * (periyod - 1)
    for i in range(periyod, len(seri) + 1):
        pencere = seri[i - periyod:i]
        ort = sum(pencere) / periyod
        varyans = sum((x - ort) ** 2 for x in pencere) / periyod
        res.append(math.sqrt(varyans))
    return res

def rsi_hesapla(kapanislar, periyod=14):
    if len(kapanislar) < periyod + 1: return 50.0
    kazanclar, kayiplar = [], []
    for i in range(1, len(kapanislar)):
        fark = kapanislar[i] - kapanislar[i-1]
        if fark > 0: kazanclar.append(fark); kayiplar.append(0)
        else: kazanclar.append(0); kayiplar.append(abs(fark))
    ort_kazanc = sum(kazanclar[:periyod]) / periyod
    ort_kayip = sum(kayiplar[:periyod]) / periyod
    for i in range(periyod, len(kazanclar)):
        ort_kazanc = (ort_kazanc * (periyod - 1) + kazanclar[i]) / periyod
        ort_kayip = (ort_kayip * (periyod - 1) + kayiplar[i]) / periyod
    if ort_kayip <= 0.00000001: return 100.0  
    return 100.0 - (100.0 / (1.0 + (ort_kazanc / ort_kayip)))

def atr_hesapla(yuksekler, dusukler, kapanislar, periyod=14):
    if len(kapanislar) < 2: return [0.0] * len(kapanislar)
    tr = [yuksekler[0] - dusukler[0]]
    for i in range(1, len(kapanislar)):
        hl = yuksekler[i] - dusukler[i]
        hc = abs(yuksekler[i] - kapanislar[i-1])
        lc = abs(dusukler[i] - kapanislar[i-1])
        tr.append(max(hl, hc, lc))
    
    atr = [sum(tr[:periyod]) / periyod]
    for i in range(periyod, len(tr)):
        atr.append((atr[-1] * (periyod - 1) + tr[i]) / periyod)
    return [0.0] * (periyod - 1) + atr

def strateji_sinyal_uret(v, anlik_fiyat):
    kapanislar = v["kapanislar"] + [anlik_fiyat]
    yuksekler = v["yuksekler"] + [v["yuksekler"][-1]] 
    dusukler = v["dusukler"] + [v["dusukler"][-1]]

    L = len(kapanislar)
    gerekli_uzunluk = max(config.BB_LEN, config.ATR_LEN, config.RSI_LEN) + config.BARS_CHECK + 3
    if L < gerekli_uzunluk:
        return "HOLD"

    basis = sma(kapanislar, config.BB_LEN)
    dev = stdev(kapanislar, config.BB_LEN)
    upper_bb = [basis[i] + (config.BB_MULT * dev[i]) for i in range(L)]
    lower_bb = [basis[i] - (config.BB_MULT * dev[i]) for i in range(L)]

    kc_atr = atr_hesapla(yuksekler, dusukler, kapanislar, config.BB_LEN)
    upper_kc = [basis[i] + (kc_atr[i] * config.KC_MULT) for i in range(L)]
    lower_kc = [basis[i] - (kc_atr[i] * config.KC_MULT) for i in range(L)]

    squeeze_on = [(upper_bb[i] < upper_kc[i]) and (lower_bb[i] > lower_kc[i]) for i in range(L)]
    target_idx = -(config.BARS_CHECK + 2)

    if squeeze_on[target_idx]:
        dilim_yuksekler = yuksekler[-(config.BARS_CHECK + 1):-1]
        high_avg_prev_n = sum(dilim_yuksekler) / len(dilim_yuksekler)

        current_close = kapanislar[-1]
        rsi_val = rsi_hesapla(kapanislar, config.RSI_LEN)
        atr_serisi = atr_hesapla(yuksekler, dusukler, kapanislar, config.ATR_LEN)
        atr_val = atr_serisi[-1]

        ext_up = max(0.0, current_close - high_avg_prev_n)
        ext_down = max(0.0, high_avg_prev_n - current_close)

        long_ok = current_close > high_avg_prev_n
        short_ok = current_close < high_avg_prev_n

        if config.USE_ATR_FILTER:
            long_ok = long_ok and (ext_up <= config.MAX_EXT_LONG_ATR * atr_val)
            short_ok = short_ok and (ext_down <= config.MAX_EXT_SHORT_ATR * atr_val)

        if config.USE_RSI_FILTER:
            long_ok = long_ok and (rsi_val > config.RSI_OS)
            short_ok = short_ok and (rsi_val < config.RSI_OB)

        if long_ok:
            return "BUY"
        elif short_ok:
            return "SELL"

    return "HOLD"

# --- 🌐 TEMEL ALTYAPI FONKSİYONLARI ---

def ilk_100_hacimli_coin_bul():
    try:
        ticker_url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        response = requests.get(ticker_url, timeout=15).json()
        usdt_pairs = [x for x in response if x["symbol"].endswith("USDT")]
        sorted_by_volume = sorted(usdt_pairs, key=lambda k: float(k.get("quoteVolume", 0)), reverse=True)
        return [x["symbol"].lower() for x in sorted_by_volume[:100]]
    except Exception as e:
        print(f"❌ Hacim listesi alınamadı: {e}")
        return []

def kontrollu_coin_ekle(coin_adi):
    coin_lower = coin_adi.lower().strip()
    coin_upper = coin_lower.upper()
    if coin_lower in SYMBOLS: return True
    try:
        f_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(f_url, timeout=10).json()
        market_info = next((m for m in r.get("symbols", []) if m["symbol"] == coin_upper), None)
        if not market_info or market_info.get('status') != 'TRADING': return False
        
        client.futures_change_leverage(symbol=coin_upper, leverage=config.KALDIRAC)
        try: client.futures_change_margin_type(symbol=coin_upper, marginType="ISOLATED")
        except BinanceAPIException as e:
            if "No need to change" not in e.message: pass

        for f in market_info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step_size_str = str(f['stepSize']).rstrip('0')
                precision = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
                FUTURES_HASSASIYETLERI[coin_lower] = precision

        with data_lock:
            SYMBOLS.append(coin_lower)
            piyasa_verisi[coin_lower] = {"anlik_fiyat": None, "kapanislar": [], "yuksekler": [], "dusukler": []}
            aktif_pozisyonlar[coin_lower] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0}
            son_islem_zamanlari[coin_lower] = 0.0  
            pozisyon_acilis_zamanlari[coin_lower] = 0.0
        return True
    except Exception: return False

def acik_pozisyonlari_binanceden_guncelle():
    try:
        pozisyonlar = client.futures_position_information()
        with data_lock:
            for s in SYMBOLS:
                aktif_pozisyonlar[s] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0}
            for p in pozisyonlar:
                sym = p.get("symbol", "").lower()
                if sym in aktif_pozisyonlar:
                    amt = float(p.get("positionAmt", 0))
                    entry_price = float(p.get("entryPrice", 0))
                    if amt != 0:
                        aktif_pozisyonlar[sym]["aktif"] = True
                        aktif_pozisyonlar[sym]["yon"] = "LONG" if amt > 0 else "SHORT"
                        aktif_pozisyonlar[sym]["adet"] = abs(amt)
                        aktif_pozisyonlar[sym]["giris_fiyati"] = entry_price
        print("🔄 [API Senkronizasyonu] Mevcut açık pozisyonlar başarıyla güncellendi.")
    except Exception as e:
        print(f"❌ Pozisyon senkronizasyon hatası: {e}")

def tüm_gecmis_verileri_guncelle():
    for s in SYMBOLS:
        try:
            url = f"https://fapi.binance.com/fapi/v1/klines?symbol={s.upper()}&interval={config.TIMEFRAME}&limit=60"
            k = requests.get(url, timeout=10).json()
            with data_lock:
                piyasa_verisi[s]["kapanislar"] = [float(x[4]) for x in k]
                piyasa_verisi[s]["yuksekler"] = [float(x[2]) for x in k]
                piyasa_verisi[s]["dusukler"] = [float(x[3]) for x in k]
                if k and len(k) > 0 and piyasa_verisi[s]["anlik_fiyat"] is None:
                    piyasa_verisi[s]["anlik_fiyat"] = float(k[-1][4])
        except Exception: pass

# --- 🎛️ TELEGRAM YÖNETİMİ ---
def telegram_bildir(mesaj, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}
        if reply_markup: data["reply_markup"] = reply_markup
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=data, timeout=5)
    except Exception: pass

def ana_menu_olustur():
    return {
        "keyboard": [[{"text": "📊 Bot Durumu"}], [{"text": "▶️ Botu Başlat"}, {"text": "⏸️ Botu Durdur"}]],
        "resize_keyboard": True, "one_time_keyboard": False
    }

def telegram_canli_rapor_uret():
    acik_pozisyonlari_binanceden_guncelle()
    with data_lock:
        acik_pozlar = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])
        durum_str = "🟢 Squeeze Mod Aktif" if config.BOT_CALISIYOR else "🔴 Sistem Durduruldu"
        poz_buyuklugu = config.ISLEM_MARJIN * config.KALDIRAC

        rapor = (
            f"⚙️ <b>Squeeze + N Bars Avcı Botu</b>\n"
            f"• Sistem: {durum_str}\n"
            f"• Marjin: {config.ISLEM_MARJIN:.1f} USDT\n"
            f"• Kaldıraç: {config.KALDIRAC}x (İZOLE)\n"
            f"• Poz Büyüklüğü: {poz_buyuklugu:.1f} USDT\n"
            f"• Risk Limiti: {acik_pozlar}/{config.MAX_ACIK_POZISYON} Pozisyon\n"
            f"• TP Hedefi: %{config.TAHMINI_TP_YUZDE * 100:.1f}\n\n"
            f"⚡ <b>Açık İşlemler:</b>\n"
        )

        if acik_pozlar == 0:
            rapor += "Açık izole pozisyon bulunmuyor."
        else:
            for s in SYMBOLS:
                if aktif_pozisyonlar[s]["aktif"]:
                    p = aktif_pozisyonlar[s]
                    rapor += f"• {s.upper()} | {p['yon']} | Giriş: {p['giris_fiyati']}\n"
    return rapor

def telegram_gelen_mesaj_dinleyici():
    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"timeout": 10, "offset": offset}
            response = requests.get(url, params=params, timeout=15).json()
            if response.get("ok") and response.get("result"):
                for update in response["result"]:
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if not message or str(message.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID): continue
                    text = message.get("text", "")
                    
                    if text == "/start":
                        telegram_bildir("🤖 <b>Bot Kontrol Paneli Aktif!</b>", reply_markup=ana_menu_olustur())
                    elif text == "📊 Bot Durumu":
                        telegram_bildir(telegram_canli_rapor_uret(), reply_markup=ana_menu_olustur())
                    elif text == "▶️ Botu Başlat":
                        config.BOT_CALISIYOR = True
                        telegram_bildir("🚀 Bot tarama döngüsü <b>aktif.</b>", reply_markup=ana_menu_olustur())
                    elif text == "⏸️ Botu Durdur":
                        config.BOT_CALISIYOR = False
                        telegram_bildir("⏸️ Bot tarama döngüsü <b>durduruldu.</b>", reply_markup=ana_menu_olustur())
        except Exception: time.sleep(5)

# --- 🔐 WEBSOCKET BAĞLANTILARI ---
def on_user_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("e") == "ACCOUNT_UPDATE":
            positions = data.get("a", {}).get("P", [])
            for p in positions:
                sym = p.get("s", "").lower()
                if sym in aktif_pozisyonlar:
                    pa = float(p.get("pa", 0))
                    ep = float(p.get("ep", 0))
                    with data_lock:
                        if pa == 0:
                            if aktif_pozisyonlar[sym]["aktif"]:
                                aktif_pozisyonlar[sym]["aktif"] = False
                                pozisyon_acilis_zamanlari[sym] = 0.0
                        else:
                            if not aktif_pozisyonlar[sym]["aktif"]:
                                pozisyon_acilis_zamanlari[sym] = time.time()
                            aktif_pozisyonlar[sym]["aktif"] = True
                            aktif_pozisyonlar[sym]["yon"] = "LONG" if pa > 0 else "SHORT"
                            aktif_pozisyonlar[sym]["adet"] = abs(pa)
                            aktif_pozisyonlar[sym]["giris_fiyati"] = ep
    except Exception: pass

def start_user_data_ws():
    global listen_key
    try:
        listen_key = client.futures_stream_get_listen_key()
        threading.Thread(target=lambda: (time.sleep(1800), client.futures_stream_keepalive(listenKey=listen_key)), daemon=True).start()
        ws = WebSocketApp(f"wss://fstream.binance.com/ws/{listen_key}", on_message=on_user_message, on_close=lambda ws,c,m: time.sleep(5))
        ws.run_forever(reconnect=5)
    except Exception: pass

def on_market_data_message(ws, message):
    try:
        data = json.loads(message)
        if isinstance(data, list):
            with data_lock:
                for ticker in data:
                    sym = ticker.get("s", "").lower()
                    if sym in piyasa_verisi:
                        piyasa_verisi[sym]["anlik_fiyat"] = float(ticker.get("c", 0))
    except Exception: pass

def start_market_data_ws():
    try:
        ws = WebSocketApp("wss://fstream.binance.com/ws/!ticker@arr", 
                          on_message=on_market_data_message, 
                          on_close=lambda ws,c,m: time.sleep(2))
        ws.run_forever(reconnect=5)
    except Exception: pass

# --- 🛰️ ASENKRON CANLI RADAR EK MOTORU (15s DÖNGÜSÜ) ---
def canlı_radar_dongusu():
    while True:
        try:
            time.sleep(15.0)
            if not config.BOT_CALISIYOR: continue

            radar_adaylari = []
            su_an_ts = time.time()

            with data_lock:
                for symbol in SYMBOLS:
                    v = piyasa_verisi[symbol]
                    pos = aktif_pozisyonlar[symbol]
                    
                    if len(v["kapanislar"]) < 40 or not v["anlik_fiyat"] or v["anlik_fiyat"] <= 0: continue
                    if su_an_ts - son_islem_zamanlari[symbol] < config.COOLDOWN_SURESI: continue
                    if pos["aktif"]: continue 

                    sinyal_durumu = strateji_sinyal_uret(v, v["anlik_fiyat"])
                    if sinyal_durumu != "HOLD":
                        radar_adaylari.append({
                            "symbol": symbol.upper(),
                            "yon": "LONG Yönü" if sinyal_durumu == "BUY" else "SHORT Yönü",
                            "fiyat": v["anlik_fiyat"],
                            "sinyal": sinyal_durumu
                        })

            if radar_adaylari:
                zaman_str = datetime.now().strftime("%H:%M:%S")
                print(f"\n🎯 [CANLI RADAR - {zaman_str}] Squeeze + N Bars Kırılım Adayları:")
                print("-----------------------------------------------------------------")
                for i, coin in enumerate(radar_adaylari[:5], 1):
                    print(f"{i}. {coin['symbol']:<10} | {coin['yon']:<11} | Sinyal Fiyatı: {coin['fiyat']:<10}")
                print("-----------------------------------------------------------------")

        except Exception as e:
            print(f"❌ Radar hatası: {e}")

# --- 🎯 1 SANİYELİK KOTA DOSTU SQUEEZE MOTORU ---
def hibrit_tarama_dongusu():
    last_kline_sync = 0
    threading.Thread(target=canlı_radar_dongusu, daemon=True).start()

    while True:
        try:
            if not config.BOT_CALISIYOR:
                time.sleep(1.0); continue
                
            su_an_ts = time.time()
            if su_an_ts - last_kline_sync > 300:
                tüm_gecmis_verileri_guncelle()
                acik_pozisyonlari_binanceden_guncelle()  
                last_kline_sync = su_an_ts

            with data_lock:
                for symbol in SYMBOLS:
                    v = piyasa_verisi[symbol]
                    pos = aktif_pozisyonlar[symbol]
                    
                    if len(v["kapanislar"]) < 40 or not v["anlik_fiyat"] or v["anlik_fiyat"] <= 0: continue
                    anlik_fiyat = v["anlik_fiyat"]

                    # ==========================================
                    # 🎯 ÇIKIŞ MANTIĞI (% TP Kontrolü)
                    # ==========================================
                    if pos["aktif"]:
                        maliyet = pos["giris_fiyati"]
                        if maliyet <= 0: continue
                        
                        # 🛑 HAYALET KAPANMA ENGELİ: Pozisyon açılalı 10 saniye geçmeden TP kontrolü yapma
                        if pozisyon_acilis_zamanlari.get(symbol, 0) == 0 or (su_an_ts - pozisyon_acilis_zamanlari[symbol] < 10):
                            continue

                        # Fiyatın saf yüzde değişimi
                        fiyat_degisim_yuzde = (anlik_fiyat - maliyet) / maliyet
                        
                        # LONG KAPATMA
                        if pos["yon"] == "LONG" and fiyat_degisim_yuzde >= config.TAHMINI_TP_YUZDE:
                            try:
                                qty_to_close = pos["adet"]
                                if qty_to_close > 0:
                                    client.futures_create_order(
                                        symbol=symbol.upper(), 
                                        side=SIDE_SELL, 
                                        type=ORDER_TYPE_MARKET, 
                                        quantity=qty_to_close,
                                        reduceOnly=True
                                    )
                                    son_islem_zamanlari[symbol] = su_an_ts  # Kapanış sonrası cooldown başlasın
                                    pozisyon_acilis_zamanlari[symbol] = 0.0
                                    pos["aktif"] = False
                                    telegram_bildir(f"💰 <b>{symbol.upper()} LONG Kar Alındı!</b>\nFiyat Hareketi: %{fiyat_degisim_yuzde * 100:.2f}\nTahmini ROE: %{fiyat_degisim_yuzde * config.KALDIRAC * 100:.2f}")
                            except Exception as e:
                                print(f"❌ Long kapatma hatası ({symbol}): {e}")
                                    
                        # SHORT KAPATMA
                        elif pos["yon"] == "SHORT" and fiyat_degisim_yuzde <= -config.TAHMINI_TP_YUZDE:
                            try:
                                qty_to_close = pos["adet"]
                                if qty_to_close > 0:
                                    client.futures_create_order(
                                        symbol=symbol.upper(), 
                                        side=SIDE_BUY, 
                                        type=ORDER_TYPE_MARKET, 
                                        quantity=qty_to_close,
                                        reduceOnly=True
                                    )
                                    son_islem_zamanlari[symbol] = su_an_ts  # Kapanış sonrası cooldown başlasın
                                    pozisyon_acilis_zamanlari[symbol] = 0.0
                                    pos["aktif"] = False
                                    telegram_bildir(f"💰 <b>{symbol.upper()} SHORT Kar Alındı!</b>\nFiyat Hareketi: %{-fiyat_degisim_yuzde * 100:.2f}\nTahmini ROE: %{-fiyat_degisim_yuzde * config.KALDIRAC * 100:.2f}")
                            except Exception as e:
                                print(f"❌ Short kapatma hatası ({symbol}): {e}")
                    
                    # ==========================================
                    # 📈 GİRİŞ MANTIĞI (SQUEEZE + N BARS)
                    # ==========================================
                    if not pos["aktif"]:
                        
                        # COOLDOWN KONTROLÜ: Sadece yeni girişi engeller, çıkışı etkilemez!
                        if su_an_ts - son_islem_zamanlari[symbol] < config.COOLDOWN_SURESI: 
                            continue

                        guncel_acik_pozisyon_sayisi = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])
                        if guncel_acik_pozisyon_sayisi >= config.MAX_ACIK_POZISYON:
                            continue 

                        sinyal = strateji_sinyal_uret(v, anlik_fiyat)

                        if sinyal != "HOLD":
                            precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                            qty = (config.ISLEM_MARJIN * config.KALDIRAC) / anlik_fiyat
                            qty = float(int(qty * (10 ** precision))) / (10 ** precision) if precision > 0 else int(qty)
                            if qty <= 0: continue

                            # LONG EMİR
                            if sinyal == "BUY" and pos["yon"] != "SHORT":
                                try:
                                    client.futures_create_order(symbol=symbol.upper(), side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
                                    
                                    # Hafıza Senkronizasyonu
                                    pos["aktif"] = True
                                    pos["yon"] = "LONG"
                                    pos["adet"] = qty
                                    pos["giris_fiyati"] = anlik_fiyat
                                    pozisyon_acilis_zamanlari[symbol] = su_an_ts
                                    
                                    telegram_bildir(f"🚀 <b>{symbol.upper()} LONG Pozisyonu Açıldı!</b>\nTetikleyici: Squeeze + N Bars Kırılımı\nFiyat: {anlik_fiyat}")
                                except Exception as e:
                                    print(f"❌ Long emir hatası ({symbol}): {e}")
                                        
                            # SHORT EMİR
                            elif sinyal == "SELL" and pos["yon"] != "LONG":
                                try:
                                    client.futures_create_order(symbol=symbol.upper(), side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=qty)
                                    
                                    # Hafıza Senkronizasyonu
                                    pos["aktif"] = True
                                    pos["yon"] = "SHORT"
                                    pos["adet"] = qty
                                    pos["giris_fiyati"] = anlik_fiyat
                                    pozisyon_acilis_zamanlari[symbol] = su_an_ts
                                    
                                    telegram_bildir(f"🚀 <b>{symbol.upper()} SHORT Pozisyonu Açıldı!</b>\nTetikleyici: Squeeze + N Bars Kırılımı\nFiyat: {anlik_fiyat}")
                                except Exception as e:
                                    print(f"❌ Short emir hatası ({symbol}): {e}")
            time.sleep(1.0)
        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            time.sleep(1.0)

# --- 🚀 ANA ÇALIŞTIRICI SİSTEM ---
if __name__ == "__main__":
    print("🎬 Squeeze + N Bars Avcı Botu Başlatılıyor...")
    
    hacimli_coinler = ilk_100_hacimli_coin_bul()
    print(f"📋 İlk etapta {len(hacimli_coinler)} adet hacimli coin tespit edildi.")
    
    eklenen_sayac = 0
    for c in hacimli_coinler:
        if kontrollu_coin_ekle(c):
            eklenen_sayac += 1
            
    print(f"✅ Filtreleri geçen {eklenen_sayac} coin tarama listesine eklendi.")
    
    tüm_gecmis_verileri_guncelle()
    acik_pozisyonlari_binanceden_guncelle()
    
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=telegram_gelen_mesaj_dinleyici, daemon=True).start()
        telegram_bildir("🤖 <b>Squeeze + N Bars Kırılımı Botu Başlatıldı!</b>")
    
    threading.Thread(target=start_user_data_ws, daemon=True).start()
    threading.Thread(target=start_market_data_ws, daemon=True).start()
    
    print("⚡ Tüm sistemler aktif. Squeeze tarama motoru ve asenkron Canlı Radar başlatıldı.")
    hibrit_tarama_dongusu()
