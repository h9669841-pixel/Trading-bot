import os
import json
import time
import requests
import threading
from websocket import WebSocketApp

# --- 🔑 TELEGRAM ALARM AYARLARI ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- 📊 ARBİTRAJ TAKİP AYARLARI ---
GIRIS_MAKAS_YUZDE = 1.50       
TEYIT_ESIGI = 2  # Sinyal üretilmeden önce makasın kaç döngü boyunca eşikte kalması gerektiği

# 🎯 Takip Edilecek En Yüksek Hacimli Arbitraj Koinleri
ADAY_SYMBOLS = [
    "dydxusdt", "opusdt", "arbusdt", "ldousdt", "tiausdt", 
    "solusdt", "avaxusdt", "linkusdt", "suiusdt", "ethusdt", 
    "bnbusdt", "xrpusdt", "adausdt", "dotusdt", "maticusdt",
    "btcusdt", "dogeusdt", "shibusdt", "nearusdt", "ftmusdt",
    "atomusdt", "ltcusdt", "uniusdt", "aptusdt", "filusdt",
    "injusdt", "seiusdt", "fetusdt", "renderusdt", "flokusdt",
    "pepeusdt", "bonkusdt", "wifusdt", "jupusdt", "pythusdt",
    "grtusdt", "stxusdt", "imxusdt", "gmtusdt", "apeusdt", 
    "axsusdt", "sandusdt", "manausdt", "chzusdt", "etcusdt", "vetusdt"
]

# Çift kayıtları temizle ve küçük harfe sabitle
SYMBOLS = list(set([s.lower().strip() for s in ADAY_SYMBOLS]))

# Bellek Yapıları
piyasa_verisi = {s: {"spot_price": None, "futures_price": None} for s in SYMBOLS}
sinyal_durumu = {s: {"onay_sayac": 0, "son_sinyal_zamani": 0} for s in SYMBOLS}

# 🔒 Veri Güvenlik Kilidi
data_lock = threading.Lock()

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: 
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try: 
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, timeout=5)
    except Exception: 
        pass

# --- 🌐 WEBSOCKET SÜRÜCÜLERİ ---
def on_spot_message(ws, message):
    data = json.loads(message)
    stream_name = data.get("stream", "")
    symbol = stream_name.split("@")[0].lower()
    with data_lock:
        if symbol in piyasa_verisi: 
            piyasa_verisi[symbol]["spot_price"] = float(data.get("data", {}).get("p", 0))

def on_futures_message(ws, message):
    data = json.loads(message)
    stream_name = data.get("stream", "")
    symbol = stream_name.split("@")[0].lower()
    with data_lock:
        if symbol in piyasa_verisi: 
            piyasa_verisi[symbol]["futures_price"] = float(data.get("data", {}).get("p", 0))

def start_multi_spot_ws():
    streams = "/".join([f"{s}@trade" for s in SYMBOLS])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    while True:
        try:
            ws = WebSocketApp(url, on_message=on_spot_message)
            ws.run_forever()
        except Exception:
            time.sleep(5)

def start_multi_futures_ws():
    streams = "/".join([f"{s}@trade" for s in SYMBOLS])
    url = f"wss://fstream.binance.com/stream?streams={streams}"
    while True:
        try:
            ws = WebSocketApp(url, on_message=on_futures_message)
            ws.run_forever()
        except Exception:
            time.sleep(5)

# --- 🎯 SİNYAL VE İZLEME MOTORU ---
def arbitraj_tarama_dongusu():
    while True:
        try:
            aktif_firsatlar = []
            su_an = time.time()
            
            with data_lock:
                for symbol in SYMBOLS:
                    spot_fiyat = piyasa_verisi[symbol]["spot_price"]
                    futures_fiyat = piyasa_verisi[symbol]["futures_price"]
                    
                    if not spot_fiyat or not futures_fiyat: 
                        continue
                        
                    # Makas Hesaplama (Vadeli Fiyat - Spot Fiyat) / Spot Fiyat
                    anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
                    coin_label = symbol.upper()
                    
                    aktif_firsatlar.append({
                        "symbol": coin_label, 
                        "makas": anlik_makas, 
                        "sp": spot_fiyat, 
                        "fu": futures_fiyat, 
                        "onay": sinyal_durumu[symbol]["onay_sayac"]
                    })
                    
                    # Sinyal Kontrolü
                    if anlik_makas >= GIRIS_MAKAS_YUZDE:
                        sinyal_durumu[symbol]["onay_sayac"] += 1
                        
                        # Belirlenen teyit eşiğine ulaşıldı mı ve son 1 dakika içinde sinyal atılmadıysa alarm ver
                        if sinyal_durumu[symbol]["onay_sayac"] >= TEYIT_ESIGI:
                            if su_an - sinyal_durumu[symbol]["son_sinyal_zamani"] > 60: 
                                mesaj = (
                                    f"🚨 <b>ARBİTRAJ SİNYALİ ({coin_label})</b>\n"
                                    f"📈 Makas: <b>+%{anlik_makas:.3f}</b>\n"
                                    f"🟢 Spot Fiyat: {spot_fiyat}\n"
                                    f"🔴 Vadeli Fiyat: {futures_fiyat}"
                                )
                                telegram_bildir(mesaj)
                                sinyal_durumu[symbol]["son_sinyal_zamani"] = su_an
                    else:
                        sinyal_durumu[symbol]["onay_sayac"] = 0
            
            # Konsol Çıktısı: En yüksek makaslı ilk 3 pariteyi göster
            if aktif_firsatlar:
                aktif_firsatlar.sort(key=lambda x: x["makas"], reverse=True)
                print("\n🔥 --- EN YÜKSEK MAKASLI İLK 3 PARİTE ---")
                for f in aktif_firsatlar[:3]:
                    onay_notu = f" [Teyit: {f['onay']}/{TEYIT_ESIGI}]" if f['onay'] > 0 else ""
                    print(f"📊 [İZLEME] {f['symbol']} Makas: +%{f['makas']:.3f} | Sp: {f['sp']} | Fu: {f['fu']}{onay_notu}")
                print("---------------------------------------------------------")
                            
        except Exception as e: 
            print(f"❌ Döngü hatası: {e}")
            
        time.sleep(1.0) 

if __name__ == "__main__":
    print(f"🚀 {len(SYMBOLS)} parite için fiyatsal takip motoru kuruldu (Doğrudan Bağlantı).")
    
    print("⏳ WebSocket hatlarına bağlanılıyor...")
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    
    time.sleep(4.0)  # Verilerin akmaya başlaması için kısa bir bekleme
    telegram_bildir(f"🎯 <b>Arbitraj İzleme Botu Başlatıldı! Takip Edilen Parite: {len(SYMBOLS)}</b>")
    
    arbitraj_tarama_dongusu()
