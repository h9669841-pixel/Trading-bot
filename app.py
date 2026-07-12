import os
import json
import time
import requests
import threading
import traceback
import socket
import math
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

# --- ⚙️ TREND VE RİSK YÖNETİMİ AYARLARI ---
class TrendBotConfig:
    def __init__(self):
        self.TIMEFRAME = Client.KLINE_INTERVAL_15MINUTE  # ⏱️ 15 Dakikalık Mumlar
        self.ISLEM_MARJIN = 1.0        # 💎 Pozisyon başına bağlanan teminat: 1 Dolar
        self.KALDIRAC = 20             # ⚡ Kaldıraç: 20x İzole (Toplam pozisyon gücü = 20 Dolar)
        self.MAX_ACIK_POZISYON = 10     # 🛑 Aynı anda açık olabilecek maksimum pozisyon limiti
        self.RSI_PERIYOD = 14
        self.RSI_ASTR_SATIM = 32       
        self.RSI_ASTR_ALIM = 68        
        self.BOLLINGER_PERIYOD = 20
        self.BOLLINGER_STANDART_SAPMA = 2
        self.TAHMINI_TP_YUZDE = 0.010   # 🎯 Fiyat bazında %1.0 Kâr Al
        self.BOT_CALISIYOR = True

config = TrendBotConfig()

# 📊 50 Adet Yüksek Hacimli Koin Havuzu
ADAY_SYMBOLS = [
    "dydxusdt", "opusdt", "arbusdt", "ldousdt", "tiausdt", 
    "solusdt", "avaxusdt", "linkusdt", "suiusdt", "ethusdt", 
    "bnbusdt", "xrpusdt", "adausdt", "dotusdt", "maticusdt",
    "btcusdt", "dogeusdt", "shibusdt", "nearusdt", "ftmusdt",
    "atomusdt", "ltcusdt", "uniusdt", "aptusdt", "filusdt",
    "injusdt", "seiusdt", "fetusdt", "renderusdt", "flokusdt",
    "pepeusdt", "bonkusdt", "wifusdt", "jupusdt", "pythusdt",
    "galausdt", "grtusdt", "stxusdt", "imxusdt", "gmtusdt", 
    "apeusdt", "axsusdt", "sandusdt", "manausdt", "chzusdt", 
    "etcusdt", "vetusdt"
]
ADAY_SYMBOLS = list(set(ADAY_SYMBOLS))

SYMBOLS = [] 
piyasa_verisi = {}
aktif_pozisyonlar = {}
FUTURES_HASSASIYETLERI = {}

data_lock = threading.Lock()
ws_futures_client = None

# --- 🛠️ TEKNİK ANALİZ MOTORU ---
def rsi_hesapla(kapanislar, periyod=14):
    if len(kapanislar) < periyod + 1: return 50.0
    kazanclar, kayiplar = [], []
    for i in range(1, len(kapanislar)):
        fark = kapanislar[i] - kapanislar[i-1]
        if fark > 0:
            kazanclar.append(fark); kayiplar.append(0)
        else:
            kazanclar.append(0); kayiplar.append(abs(fark))
            
    ort_kazanc = sum(kazanclar[:periyod]) / periyod
    ort_kayip = sum(kayiplar[:periyod]) / periyod
    
    for i in range(periyod, len(kazanclar)):
        ort_kazanc = (ort_kazanc * (periyod - 1) + kazanclar[i]) / periyod
        ort_kayip = (ort_kayip * (periyod - 1) + kayiplar[i]) / periyod
        
    if ort_kayip <= 0.00000001: return 100.0  # 🛡️ Sıfıra bölünme koruması artırıldı
    return 100.0 - (100.0 / (1.0 + (ort_kazanc / ort_kayip)))

def bollinger_bands(kapanislar, periyod=20, standart_sapma=2):
    if len(kapanislar) < periyod: return 0.0, 0.0, 0.0
    veri = kapanislar[-periyod:]
    orta_bant = sum(veri) / periyod
    varyans = sum((x - orta_bant) ** 2 for x in veri) / periyod
    if varyans <= 0: varyans = 0.00000001  # 🛡️ Sıfır varyans koruması
    ust_bant = orta_bant + (standart_sapma * math.sqrt(varyans))
    alt_bant = orta_bant - (standart_sapma * math.sqrt(varyans))
    return ust_bant, orta_bant, alt_bant

def fibonacci_seviyelerini_hesapla(yuksekler, dusukler):
    if not yuksekler or not dusukler: return {}
    en_yuksek = max(yuksekler[-40:])
    en_dusuk = min(dusukler[-40:])
    fark = en_yuksek - en_dusuk
    if fark <= 0: fark = 0.00000001  # 🛡️ Sıfır yatay piyasa fark koruması
    return {
        "fib_618": en_yuksek - (0.618 * fark),
        "fib_786": en_yuksek - (0.786 * fark),
        "fib_236": en_yuksek - (0.236 * fark),
        "fib_382": en_yuksek - (0.382 * fark)
    }

# --- 🛡️ İZOLE MARJİN VE HAVUZ KONTROLÜ ---
def kontrollu_coin_ekle(coin_adi):
    coin_lower = coin_adi.lower().strip()
    coin_upper = coin_lower.upper()
    if coin_lower in SYMBOLS: return True
    try:
        f_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(f_url, timeout=10).json()
        market_info = next((m for m in r.get("symbols", []) if m["symbol"] == coin_upper), None)
        
        if not market_info or market_info.get('status') != 'TRADING': return False
        
        # ⚡ 20x Kaldıraç Set Edilir
        client.futures_change_leverage(symbol=coin_upper, leverage=config.KALDIRAC)
        
        # 🔐 Mod İZOLE (Isolated) olarak zorlanır
        try:
            client.futures_change_margin_type(symbol=coin_upper, marginType="ISOLATED")
        except BinanceAPIException as e:
            if "No need to change" not in e.message: pass
        
        ticker = client.futures_ticker(symbol=coin_upper)
        if float(ticker.get('quoteVolume', 0)) < 850000.0: return False

        for f in market_info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step_size_str = str(f['stepSize']).rstrip('0')
                precision = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
                FUTURES_HASSASIYETLERI[coin_lower] = precision

        with data_lock:
            SYMBOLS.append(coin_lower)
            piyasa_verisi[coin_lower] = {"anlik_fiyat": None, "kapanislar": [], "yuksekler": [], "dusukler": [], "klines_raw": []}
            aktif_pozisyonlar[coin_lower] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0}
        return True
    except Exception: return False

def gecmis_verileri_tazele(symbol):
    try:
        k = client.futures_klines(symbol=symbol.upper(), interval=config.TIMEFRAME, limit=60)
        with data_lock:
            piyasa_verisi[symbol]["kapanislar"] = [float(x[4]) for x in k[:-1]]
            piyasa_verisi[symbol]["yuksekler"] = [float(x[2]) for x in k[:-1]]
            piyasa_verisi[symbol]["dusukler"] = [float(x[3]) for x in k[:-1]]
            piyasa_verisi[symbol]["klines_raw"] = k[:-1]
            if k: piyasa_verisi[symbol]["anlik_fiyat"] = float(k[-1][4])
    except Exception: pass

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, timeout=5)
    except Exception: pass

def set_telegram_menu_commands():
    if not TELEGRAM_TOKEN: return
    commands_payload = {
        "commands": [
            {"command": "ayarlar", "description": "📊 Kaldıraç, marjin ve pozisyon raporu sunar"},
            {"command": "botu_durdur", "description": "🛑 Sistem taramasını duraklatır"},
            {"command": "botu_baslat", "description": "🚀 Taramayı uyandırır"}
        ]
    }
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands", json=commands_payload, timeout=5)
    except Exception: pass

# --- 💬 TELEGRAM KOMUT ARAYÜZÜ ---
def telegram_komut_dinleyici():
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    last_update_id = 0
    try:
        r = requests.get(url, params={"timeout": 1}, timeout=5).json()
        if r.get("result"): last_update_id = r["result"][-1]["update_id"]
    except Exception: pass

    while True:
        try:
            r = requests.get(url, params={"offset": last_update_id + 1, "timeout": 10}, timeout=15).json()
            for update in r.get("result", []):
                last_update_id = update["update_id"]
                message = update.get("message", {})
                text = message.get("text", "").strip()
                if str(message.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID): continue
                
                if text.startswith("/ayarlar"):
                    durum = "🟢 İzole Mod Aktif" if config.BOT_CALISIYOR else "🛑 Durduruldu"
                    acik_sayisi = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])
                    
                    rapor = f"⚙️ <b>İzole 20x Avcı Botu</b>\n• Sistem: <b>{durum}</b>\n• Marjin: {config.ISLEM_MARJIN} USDT\n• Kaldıraç: {config.KALDIRAC}x (İZOLE)\n• Poz Büyüklüğü: {config.ISLEM_MARJIN * config.KALDIRAC} USDT\n• Risk Limiti: {acik_sayisi}/{config.MAX_ACIK_POZISYON} Pozisyon\n• TP Hedefi: %1.0\n• SL Durumu: ❌ KAPALI (Liq Yönetimi)\n\n⚡ <b>Açık İşlemler:</b>\n"
                    acik_var = False
                    for s in SYMBOLS:
                        p = aktif_pozisyonlar[s]
                        if p["aktif"]:
                            acik_var = True
                            anlik = piyasa_verisi[s]["anlik_fiyat"] or p["giris_fiyati"]
                            pnl = (anlik - p["giris_fiyati"]) * p["adet"] if p["yon"] == "LONG" else (p["giris_fiyati"] - anlik) * p["adet"]
                            rapor += f"▪️ {s.upper()} | {p['yon']} | Marjin: 1$ | PnL: {pnl:.2f} USDT\n"
                    if not acik_var: rapor += "<i>Açık izole pozisyon bulunmuyor.</i>"
                    telegram_bildir(rapor)
                elif text.startswith("/botu_durdur"):
                    config.BOT_CALISIYOR = False; telegram_bildir("🛑 Bot durduruldu.")
                elif text.startswith("/botu_baslat"):
                    config.BOT_CALISIYOR = True; telegram_bildir("🚀 Bot yeniden aktif.")
        except Exception: time.sleep(2)

# --- 🚀 HIZLI EMİR MOTORU ---
def execute_order(symbol, side, quantity):
    try:
        order = client.futures_create_order(symbol=symbol.upper(), side=side, type=ORDER_TYPE_MARKET, quantity=quantity)
        return True, float(order.get("avgPrice", 0))
    except Exception as e:
        print(f"❌ Emir Reddedildi ({symbol}): {e}")
        return False, 0.0

# --- 🌐 WEBSOCKET SÜRÜCÜSÜ ---
def on_futures_message(ws, message):
    if not config.BOT_CALISIYOR: return
    data = json.loads(message)
    stream_name = data.get("stream", "")
    symbol = stream_name.split("@")[0].lower()
    
    if "@kline_15m" in stream_name:
        kline = data.get("data", {}).get("k", {})
        is_closed = kline.get("x", False)
        current_close = float(kline.get("c", 0))
        
        with data_lock:
            if symbol in piyasa_verisi:
                piyasa_verisi[symbol]["anlik_fiyat"] = current_close
                if is_closed:
                    piyasa_verisi[symbol]["kapanislar"].append(float(kline.get("c")))
                    piyasa_verisi[symbol]["yuksekler"].append(float(kline.get("h")))
                    piyasa_verisi[symbol]["dusukler"].append(float(kline.get("l")))
                    piyasa_verisi[symbol]["kapanislar"] = piyasa_verisi[symbol]["kapanislar"][-60:]
                    piyasa_verisi[symbol]["yuksekler"] = piyasa_verisi[symbol]["yuksekler"][-60:]
                    piyasa_verisi[symbol]["dusukler"] = piyasa_verisi[symbol]["dusukler"][-60:]
                    
                    raw_candle = [kline.get("t"), kline.get("o"), kline.get("h"), kline.get("l"), kline.get("c")]
                    piyasa_verisi[symbol]["klines_raw"].append(raw_candle)
                    piyasa_verisi[symbol]["klines_raw"] = piyasa_verisi[symbol]["klines_raw"][-60:]

def start_multi_futures_ws():
    global ws_futures_client
    streams = "/".join([f"{s}@kline_15m" for s in SYMBOLS])
    ws_futures_client = WebSocketApp(
        f"wss://fstream.binance.com/stream?streams={streams}", 
        on_message=on_futures_message,
        on_error=lambda ws, err: print(f"⚠️ WS Hatası: {err}"),
        on_close=lambda ws, c, m: time.sleep(5)
    )
    ws_futures_client.run_forever(reconnect=5)

# --- 🎯 TREND VE HASSAS KÂR MOTORU ---
def trend_tarama_dongusu():
    while True:
        try:
            if not config.BOT_CALISIYOR:
                time.sleep(2.0); continue
                
            with data_lock:
                guncel_acik_pozisyon_sayisi = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])

                for symbol in SYMBOLS:
                    v = piyasa_verisi[symbol]
                    pos = aktif_pozisyonlar[symbol]
                    
                    # 🛡️ Fiyat verisi yoksa veya sıfırsa anlık pas geç (Sıfıra bölünme koruması)
                    if len(v["kapanislar"]) < 20 or not v["anlik_fiyat"] or v["anlik_fiyat"] <= 0: 
                        continue
                    
                    anlik_fiyat = v["anlik_fiyat"]
                    
                    # 📈 1. GİRİŞ TAKİBİ
                    if not pos["aktif"]:
                        if guncel_acik_pozisyon_sayisi >= config.MAX_ACIK_POZISYON:
                            continue

                        ust_bant, _, alt_bant = bollinger_bands(v["kapanislar"])
                        rsi = rsi_hesapla(v["kapanislar"])
                        fib = fibonacci_seviyelerini_hesapla(v["yuksekler"], v["dusukler"])
                        
                        # 🛡️ Fibonacci verisi boş geldiyse döngü kırılmasın, sonraki koine geçsin
                        if not fib or "fib_618" not in fib:
                            continue

                        precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                        
                        qty = (config.ISLEM_MARJIN * config.KALDIRAC) / anlik_fiyat
                        qty = float(int(qty * (10 ** precision))) / (10 ** precision) if precision > 0 else int(qty)
                        if qty <= 0: continue

                        # LONG GİRİŞ
                        if anlik_fiyat <= alt_bant and rsi <= config.RSI_ASTR_SATIM:
                            # 🛡️ Payda koruması (anlik_fiyat bölmesi için ek kontrol)
                            if abs(anlik_fiyat - fib["fib_618"]) / anlik_fiyat < 0.006 or abs(anlik_fiyat - fib["fib_786"]) / anlik_fiyat < 0.006:
                                ok, giris_f = execute_order(symbol, SIDE_BUY, qty)
                                if ok:
                                    pos.update({"aktif": True, "yon": "LONG", "adet": qty, "giris_fiyati": giris_f})
                                    guncel_acik_pozisyon_sayisi += 1
                                    telegram_bildir(f"⚡ <b>{symbol.upper()} 20x İZOLE LONG</b>\n💰 Giriş: {giris_f}\n💵 Marjin: 1.00 USDT\n🛡️ Poz Büyüklüğü: 20.00 USDT\n📊 Pozisyon Havuzu: {guncel_acik_pozisyon_sayisi}/{config.MAX_ACIK_POZISYON}\n🎯 TP: %1.0 | 🚨 SL: Yok (1$ Liq)")
                                    
                        # SHORT GİRİŞ
                        elif anlik_fiyat >= ust_bant and rsi >= config.RSI_ASTR_ALIM:
                            # 🛡️ Payda koruması (anlik_fiyat bölmesi için ek kontrol)
                            if abs(anlik_fiyat - fib["fib_236"]) / anlik_fiyat < 0.006 or abs(anlik_fiyat - fib["fib_382"]) / anlik_fiyat < 0.006:
                                ok, giris_f = execute_order(symbol, SIDE_SELL, qty)
                                if ok:
                                    pos.update({"aktif": True, "yon": "SHORT", "adet": qty, "giris_fiyati": giris_f})
                                    guncel_acik_pozisyon_sayisi += 1
                                    telegram_bildir(f"⚡ <b>{symbol.upper()} 20x İZOLE SHORT</b>\n💰 Giriş: {giris_f}\n💵 Marjin: 1.00 USDT\n🛡️ Poz Büyüklüğü: 20.00 USDT\n📊 Pozisyon Havuzu: {guncel_acik_pozisyon_sayisi}/{config.MAX_ACIK_POZISYON}\n🎯 TP: %1.0 | 🚨 SL: Yok (1$ Liq)")
                    
                    # 🎯 2. KÂR AL (TP) TAKİBİ
                    else:
                        maliyet = pos["giris_fiyati"]
                        if maliyet <= 0: continue # 🛡️ Sıfır maliyet koruması
                        
                        fark_yuzde = (anlik_fiyat - maliyet) / maliyet
                        
                        # LONG ÇIKIŞ
                        if pos["yon"] == "LONG":
                            if fark_yuzde >= config.TAHMINI_TP_YUZDE:
                                ok, _ = execute_order(symbol, SIDE_SELL, pos["adet"])
                                if ok:
                                    telegram_bildir(f"🎯 <b>{symbol.upper()} LONG %1 TP ULAŞILDI!</b>\n💸 Net PnL: +0.20 USDT")
                                    pos["aktif"] = False
                                    guncel_acik_pozisyon_sayisi -= 1
                                    
                        # SHORT ÇIKIŞ
                        elif pos["yon"] == "SHORT":
                            if fark_yuzde <= -config.TAHMINI_TP_YUZDE:
                                ok, _ = execute_order(symbol, SIDE_BUY, pos["adet"])
                                if ok:
                                    telegram_bildir(f"🎯 <b>{symbol.upper()} SHORT %1 TP ULAŞILDI!</b>\n💸 Net PnL: +0.20 USDT")
                                    pos["aktif"] = False
                                    guncel_acik_pozisyon_sayisi -= 1

        except Exception as e:
            # 💡 Eğer başka beklenmedik bir hata olursa tam detayını (satır numarasını) konsola basar
            print("❌ Döngü kritik hatası detayları:")
            traceback.print_exc()
        time.sleep(2.0)

if __name__ == "__main__":
    set_telegram_menu_commands()
    
    print("⏳ 50 Aday koin havuzu İzole 20x için hazırlanıyor...")
    for coin in ADAY_SYMBOLS:
        kontrollu_coin_ekle(coin)
        
    print(f"🚀 Başarılı! Toplam {len(SYMBOLS)} parite veri hattına bağlandı.")
    for s in SYMBOLS: gecmis_verileri_tazele(s)
        
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    threading.Thread(target=telegram_komut_dinleyici, daemon=True).start()
    
    time.sleep(3.0)
    telegram_bildir(f"🤖 <b>Güvenli Filtreli İzole 20x Bot Canlıda!</b>\n💰 Poz Büyüklüğü: 20 USDT\n🛑 Maks Eşzamanlı Poz: {config.MAX_ACIK_POZISYON}\n🎯 Hedef: %1 Fiyat Hareketi (+0.20$ PnL)\n🚨 Risk: Maksimum 1$ Liq\nTarama döngüsü başlatıldı.")
    trend_tarama_dongusu()
