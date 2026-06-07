import os
import json
import time
import requests
import threading
from websocket import WebSocketApp

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- 📊 ARBİTRAJ STRATEJİ AYARLARI ---
SYMBOL = "btcusdt"        # Websocket için küçük harf olmalıdır
GIRIS_MAKAS_YUZDE = 0.06  # Vadeli fiyat, Spottan %0.30 veya daha fazla uzaklaşırsa (+ veya -) sinyal ver
CIKIS_MAKAS_YUZDE = 0.05  # Makas normale döndüğünde çıkış sinyali ver
# -------------------------------------

# Anlık fiyatları ve pozisyonu hafızada tutacak küresel sözlük (Websocket canlı güncelleyecek)
piyasa_verisi = {
    "spot_price": None,
    "futures_price": None
}

arbitraj_pozisyon = {
    "aktif": False,
    "yon": None, 
    "giris_makas": 0.0,
    "spot_giris_fiyat": 0.0,
    "futures_giris_fiyat": 0.0
}

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram değişkenleri eksik!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": mesaj,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        print(f"Telegram hatası: {e}")

# --- 🌐 WEBSOCKET BAĞLANTILARI ---

def start_spot_ws():
    """Spot piyasa anlık fiyat akışı"""
    def on_message(ws, message):
        data = json.loads(message)
        piyasa_verisi["spot_price"] = float(data.get("p", 0))

    def on_error(ws, error):
        print(f"Spot WS Hatası: {error}")

    def on_close(ws, close_status_code, close_msg):
        print("Spot WS Bağlantısı kapandı, 5 saniye sonra yeniden bağlanıyor...")
        time.sleep(5)
        start_spot_ws()

    # Binance Spot Mini-Ticker akışı
    url = f"wss://stream.binance.com:9443/ws/{SYMBOL}@trade"
    ws = WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever()

def start_futures_ws():
    """Vadeli (Futures) piyasa anlık fiyat akışı"""
    def on_message(ws, message):
        data = json.loads(message)
        piyasa_verisi["futures_price"] = float(data.get("p", 0))

    def on_error(ws, error):
        print(f"Futures WS Hatası: {error}")

    def on_close(ws, close_status_code, close_msg):
        print("Futures WS Bağlantısı kapandı, 5 saniye sonra yeniden bağlanıyor...")
        time.sleep(5)
        start_futures_ws()

    # Binance Vadeli (USD-M) Mark Price veya Trade akışı
    url = f"wss://fstream.binance.com/ws/{SYMBOL}@trade"
    ws = WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever()

# --- 🧠 ARBİTRAJ ANALİZ MOTORU ---

def arbitraj_tarama_dongusu():
    global arbitraj_pozisyon
    print("Arbitraj Analiz Motoru arka planda taramaya başladı...")
    
    while True:
        try:
            spot_fiyat = piyasa_verisi["spot_price"]
            futures_fiyat = piyasa_verisi["futures_price"]
            
            # Eğer iki piyasadan da henüz veri akışı başlamadıysa bekle
            if not spot_fiyat or not futures_fiyat:
                time.sleep(1)
                continue

            # Orijinal makas hesabı
            anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
            
            # Log ekranına her 2 saniyede bir canlı durumu basar
            print(f"⏱️ Spot: {spot_fiyat:.2f} | Futures: {futures_fiyat:.2f} | Makas: %{anlik_makas:.4f}")

            if not arbitraj_pozisyon["aktif"]:
                # 🟢 GİRİŞ KOŞULLARI KONTROLÜ
                if anlik_makas >= GIRIS_MAKAS_YUZDE:
                    arbitraj_pozisyon.update({
                        "aktif": True,
                        "yon": "ARTI",
                        "giris_makas": anlik_makas,
                        "spot_giris_fiyat": spot_fiyat,
                        "futures_giris_fiyat": futures_fiyat
                    })
                    mesaj = (
                        f"🚀 <b>💥 POZİTİF ARBİTRAJ FIRSATI YAKALANDI!</b>\n\n"
                        f"📊 <b>Parite:</b> {SYMBOL.upper()}\n"
                        f"🟢 <b>Spot Fiyat:</b> {spot_fiyat:.2f} USDT\n"
                        f"🔴 <b>Futures Fiyat:</b> {futures_fiyat:.2f} USDT\n"
                        f"⚡ <b>Anlık Makas (Spread):</b> +%{anlik_makas:.3f}\n\n"
                        f"💡 <i>Manuel İşlem Önerisi: Spot piyasadan AL, Vadeli piyasada aynı miktarda SHORT aç!</i>"
                    )
                    telegram_bildir(mesaj)
                    
                elif anlik_makas <= -GIRIS_MAKAS_YUZDE:
                    arbitraj_pozisyon.update({
                        "aktif": True,
                        "yon": "EKSI",
                        "giris_makas": anlik_makas,
                        "spot_giris_fiyat": spot_fiyat,
                        "futures_giris_fiyat": futures_fiyat
                    })
                    mesaj = (
                        f"📉 <b>💥 NEGATİF ARBİTRAJ FIRSATI YAKALANDI!</b>\n\n"
                        f"📊 <b>Parite:</b> {SYMBOL.upper()}\n"
                        f"🟢 <b>Spot Fiyat:</b> {spot_fiyat:.2f} USDT\n"
                        f"🔴 <b>Futures Fiyat:</b> {futures_fiyat:.2f} USDT\n"
                        f"⚡ <b>Anlık Makas (Spread):</b> %{anlik_makas:.3f}\n\n"
                        f"💡 <i>Manuel İşlem Önerisi: Spot malları SAT, Vadeli piyasada aynı miktarda LONG aç!</i>"
                    )
                    telegram_bildir(mesaj)
                    
            else:
                # 🔴 ÇIŞIŞ KOŞULLARI KONTROLÜ
                if arbitraj_pozisyon["yon"] == "ARTI" and anlik_makas <= CIKIS_MAKAS_YUZDE:
                    kar_orani = arbitraj_pozisyon["giris_makas"] - anlik_makas
                    mesaj = (
                        f"🤝 <b>🔒 POZİTİF ARBİTRAJ POZİSYONU KAPANDI</b>\n\n"
                        f"📊 <b>Parite:</b> {SYMBOL.upper()}\n"
                        f"📉 <b>Makas Daraldı:</b> %{anlik_makas:.3f}'e düştü.\n"
                        f"💰 <b>Tahmini Brüt Kazanç:</b> %{kar_orani:.3f}\n\n"
                        f"💡 <i>Manuel İşlem Önerisi: Spot malları SAT, Vadeli SHORT pozisyonunu KAPAT!</i>"
                    )
                    telegram_bildir(mesaj)
                    arbitraj_pozisyon["aktif"] = False
                    
                elif arbitraj_pozisyon["yon"] == "EKSI" and anlik_makas >= -CIKIS_MAKAS_YUZDE:
                    kar_orani = abs(arbitraj_pozisyon["giris_makas"]) - abs(anlik_makas)
                    mesaj = (
                        f"🤝 <b>🔒 NEGATİF ARBİTRAJ POZİSYONU KAPANDI</b>\n\n"
                        f"📊 <b>Parite:</b> {SYMBOL.upper()}\n"
                        f"📈 <b>Makas Normale Döndü:</b> %{anlik_makas:.3f}'e tırmandı.\n"
                        f"💰 <b>Tahmini Brüt Kazanç:</b> %{kar_orani:.3f}\n\n"
                        f"💡 <i>Manuel İşlem Önerisi: Spot piyasadan geri AL, Vadeli LONG pozisyonunu KAPAT!</i>"
                    )
                    telegram_bildir(mesaj)
                    arbitraj_pozisyon["aktif"] = False

        except Exception as e:
            print(f"Analiz motoru döngü hatası: {e}")
            
        time.sleep(2)  # Logların okunabilir akması ve sistemi yormamak için analizi 2 saniyede bir tetikliyoruz

if __name__ == "__main__":
    print("Websocket Tabanlı Kesintisiz Arbitraj Botu Başlatılıyor...")
    
    telegram_bildir(
        f"🛰️ <b>Sonsuz Websocket Arbitraj Botu Yayında!</b>\n"
        f"Piyasa: {SYMBOL.upper()}\n"
        f"Giriş Eşiği: ±%{GIRIS_MAKAS_YUZDE}\n"
        f"Durum: Canlı tünel bağlantısı kuruldu. Ban riski sıfırlandı!"
    )
    
    # Spot ve Vadeli akışları arka planda bağımsız iş parçacıkları (Thread) olarak başlatıyoruz
    spot_thread = threading.Thread(target=start_spot_ws, daemon=True)
    futures_thread = threading.Thread(target=start_futures_ws, daemon=True)
    
    spot_thread.start()
    futures_thread.start()
    
    # Ana döngüyü (Analiz motorunu) başlatıyoruz
    arbitraj_tarama_dongusu()
