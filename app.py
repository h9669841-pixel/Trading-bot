import os
import json
import time
import requests
import threading
from websocket import WebSocketApp

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- 📊 ARBİTRAJ STRATEJİ VE HESAP AYARLARI ---
# Takip etmek istediğimiz tüm koinleri küçük harfle listeye ekledik
SYMBOLS = ["btcusdt", "ethusdt", "xrpusdt", "arbusdt"]

GIRIS_MAKAS_YUZDE = 0.12  # Brüt hedef makas eşiği
CIKIS_MAKAS_YUZDE = 0.1  # Çıkış makas eşiği

# 💰 BAKİYE VE KOMİSYON AYARLARI (Görseldeki değerlere göre % bazında)
SPOT_BAKIYE = 1000.0       # Giriş yapılacak Spot bütçesi (USDT)
FUTURES_BAKIYE = 1000.0    # Giriş yapılacak Vadeli bütçesi (USDT)

SPOT_FEE_RATE = 0.0750 / 100     # %0.0750 Taker komisyon oranı
FUTURES_FEE_RATE = 0.0450 / 100  # %0.0450 Taker komisyon oranı
# ----------------------------------------------

# Tüm koinlerin anlık fiyat hafızası
piyasa_verisi = {symbol: {"spot_price": None, "futures_price": None} for symbol in SYMBOLS}

# Tüm koinlerin bağımsız arbitraj pozisyon hafızası
arbitraj_pozisyonlari = {
    symbol: {
        "aktif": False,
        "yon": None, 
        "giris_makas": 0.0,
        "spot_giris_fiyat": 0.0,
        "futures_giris_fiyat": 0.0
    } for symbol in SYMBOLS
}

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram değişkenleri eksik!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        print(f"Telegram hatası: {e}")

def net_kar_hesapla(giris_makas, cikis_makas):
    """Brüt kârdan, giriş ve çıkıştaki toplam komisyonu düşerek NET kazancı hesaplar"""
    brut_oran_farki = abs(giris_makas) - abs(cikis_makas)
    brut_kazanc_usdt = SPOT_BAKIYE * (brut_oran_farki / 100)
    
    spot_toplam_komisyon = (SPOT_BAKIYE * SPOT_FEE_RATE) * 2
    futures_toplam_komisyon = (FUTURES_BAKIYE * FUTURES_FEE_RATE) * 2
    toplam_kesinti_usdt = spot_toplam_komisyon + futures_toplam_komisyon
    
    net_kazanc_usdt = brut_kazanc_usdt - toplam_kesinti_usdt
    return brut_kazanc_usdt, toplam_kesinti_usdt, net_kazanc_usdt

# --- 🌐 ÇOKLU WEBSOCKET AKIŞLARI ---

def start_multi_spot_ws():
    """Tüm koinlerin Spot fiyatlarını tek tünelden çeker"""
    def on_message(ws, message):
        data = json.loads(message)
        # Çoklu akışlarda (Combined Stream) gelen veri yapısı farklıdır
        stream_name = data.get("stream", "")
        event_data = data.get("data", {})
        
        for symbol in SYMBOLS:
            if symbol in stream_name:
                piyasa_verisi[symbol]["spot_price"] = float(event_data.get("p", 0))

    def on_error(ws, error): print(f"Multi-Spot WS Hatası: {error}")
    def on_close(ws, c_code, c_msg):
        time.sleep(5); start_multi_spot_ws()

    # Tüm koinleri tek tünelde birleştiriyoruz
    streams = "/".join([f"{symbol}@trade" for symbol in SYMBOLS])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever()

def start_multi_futures_ws():
    """Tüm koinlerin Vadeli fiyatlarını tek tünelden çeker"""
    def on_message(ws, message):
        data = json.loads(message)
        stream_name = data.get("stream", "")
        event_data = data.get("data", {})
        
        for symbol in SYMBOLS:
            if symbol in stream_name:
                piyasa_verisi[symbol]["futures_price"] = float(event_data.get("p", 0))

    def on_error(ws, error): print(f"Multi-Futures WS Hatası: {error}")
    def on_close(ws, c_code, c_msg):
        time.sleep(5); start_multi_futures_ws()

    streams = "/".join([f"{symbol}@trade" for symbol in SYMBOLS])
    url = f"wss://fstream.binance.com/stream?streams={streams}"
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever()

# --- 🧠 ÇOKLU ANALİZ MOTORU ---

def arbitraj_tarama_dongusu():
    global arbitraj_pozisyonlari
    print(f"Arbitraj Analiz Motoru {len(SYMBOLS)} koin için başlatıldı...")
    
    while True:
        try:
            for symbol in SYMBOLS:
                spot_fiyat = piyasa_verisi[symbol]["spot_price"]
                futures_fiyat = piyasa_verisi[symbol]["futures_price"]
                
                # Koinlerden herhangi birinin verisi henüz gelmediyse o adımı atla
                if not spot_fiyat or not futures_fiyat:
                    continue

                anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
                coin_label = symbol.upper().replace("USDT", "")
                
                # Konsolda hangi koinin ne durumda olduğunu temizce listeler
                print(f"⏱️ [{coin_label}] Spot: {spot_fiyat:.4f} | Futures: {futures_fiyat:.4f} | Makas: %{anlik_makas:.4f}")

                pos = arbitraj_pozisyonlari[symbol]

                if not pos["aktif"]:
                    # 🟢 GİRİŞ KONTROLLERİ
                    if anlik_makas >= GIRIS_MAKAS_YUZDE or anlik_makas <= -GIRIS_MAKAS_YUZDE:
                        yon = "ARTI" if anlik_makas >= GIRIS_MAKAS_YUZDE else "EKSI"
                        pos.update({
                            "aktif": True,
                            "yon": yon,
                            "giris_makas": anlik_makas,
                            "spot_giris_fiyat": spot_fiyat,
                            "futures_giris_fiyat": futures_fiyat
                        })
                        
                        brut, kesinti, net = net_kar_hesapla(anlik_makas, CIKIS_MAKAS_YUZDE if yon == "ARTI" else -CIKIS_MAKAS_YUZDE)
                        
                        baslik = "🚀 POZİTİF ARBİTRAJ" if yon == "ARTI" else "📉 NEGATİF ARBİTRAJ"
                        oneri = f"{coin_label} Spot AL, Vadeli SHORT aç!" if yon == "ARTI" else f"{coin_label} Spot SAT, Vadeli LONG aç!"
                        
                        mesaj = (
                            f"💥 <b>{baslik} FIRSATI YAKALANDI!</b>\n\n"
                            f"📊 <b>Koin:</b> {coin_label}/USDT\n"
                            f"⚡ <b>Giriş Makası:</b> %{anlik_makas:.4f}\n"
                            f"💰 <b>İşlem Büyüklüğü:</b> {SPOT_BAKIYE}$ + {FUTURES_BAKIYE}$\n\n"
                            f"💵 <b>Tahmini Brüt Kazanç:</b> {brut:.2f} USDT\n"
                            f"✂️ <b>Toplam Komisyon:</b> {kesinti:.2f} USDT\n"
                            f"🎉 <b>NET CEBE KALACAK:</b> <b>{net:.2f} USDT</b>\n\n"
                            f"💡 <i>{oneri}</i>"
                        )
                        telegram_bildir(mesaj)
                        
                else:
                    # 🔴 ÇIŞIŞ KONTROLLERİ
                    pozisyon_kapandi = False
                    if pos["yon"] == "ARTI" and anlik_makas <= CIKIS_MAKAS_YUZDE:
                        pozisyon_kapandi = True
                    elif pos["yon"] == "EKSI" and anlik_makas >= -CIKIS_MAKAS_YUZDE:
                        pozisyon_kapandi = True
                        
                    if pozisyon_kapandi:
                        brut, kesinti, net = net_kar_hesapla(pos["giris_makas"], anlik_makas)
                        
                        mesaj = (
                            f"🤝 <b>🔒 {coin_label} ARBİTRAJ POZİSYONU KAPANDI</b>\n\n"
                            f"📊 <b>Koin:</b> {coin_label}/USDT\n"
                            f"📉 <b>Kapanış Makası:</b> %{anlik_makas:.4f}\n\n"
                            f"💰 <b>Brüt Kâr:</b> {brut:.2f} USDT\n"
                            f"✂️ <b>Komisyon Kesintisi:</b> {kesinti:.2f} USDT\n"
                            f"🎉 <b>NET TEMİZ KÂR:</b> <b>{net:.2f} USDT</b>"
                        )
                        telegram_bildir(mesaj)
                        pos["aktif"] = False

        except Exception as e:
            print(f"Analiz motoru çoklu tarama hatası: {e}")
            
        time.sleep(2)  # Log akış hızı ve işlemciyi yormamak için ideal süre

if __name__ == "__main__":
    print("Çoklu Koin Websocket Arbitraj Botu Başlatılıyor...")
    
    telegram_bildir(
        f"🛰️ <b>Çoklu Arbitraj Avcısı Aktif!</b>\n"
        f"📋 <b>Takip Listesi:</b> BTC, ETH, XRP, ARB\n"
        f"🎯 <b>Giriş Eşiği:</b> ±%{GIRIS_MAKAS_YUZDE}\n"
        f"Sistem tüm koinleri tek tünelden sıfır ban riskiyle tarıyor."
    )
    
    # Arka plan iş parçacıklarını (Thread) başlatıyoruz
    spot_thread = threading.Thread(target=start_multi_spot_ws, daemon=True)
    futures_thread = threading.Thread(target=start_multi_futures_ws, daemon=True)
    
    spot_thread.start()
    futures_thread.start()
    
    # Ana döngüyü başlat
    arbitraj_tarama_dongusu()
