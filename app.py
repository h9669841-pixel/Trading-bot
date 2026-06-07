import os
import json
import time
import requests
import threading
from websocket import WebSocketApp

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- 📊 ARBİTRAJ STRATEJİ VE HESAP AYARLARI ---
SYMBOL = "btcusdt"
GIRIS_MAKAS_YUZDE = 0.06  # Brüt hedef makas eşiği
CIKIS_MAKAS_YUZDE = 0.05  # Çıkış makas eşiği

# 💰 BAKİYE VE KOMİSYON AYARLARI (Görseldeki değerlere göre % bazında)
SPOT_BAKIYE = 1000.0       # Giriş yapılacak Spot bütçesi (USDT)
FUTURES_BAKIYE = 1000.0    # Giriş yapılacak Vadeli bütçesi (USDT)

SPOT_FEE_RATE = 0.0750 / 100     # %0.0750 Taker komisyon oranı
FUTURES_FEE_RATE = 0.0450 / 100  # %0.0450 Taker komisyon oranı
# ----------------------------------------------

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
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        print(f"Telegram hatası: {e}")

def net_kar_hesapla(giris_makas, cikis_makas):
    """Brüt kârdan, giriş ve çıkıştaki toplam komisyonu düşerek NET kazancı hesaplar"""
    # 1. Toplam Brüt Kazanç Oranı (Yüzdesel fark)
    brut_oran_farki = abs(giris_makas) - abs(cikis_makas)
    
    # 2. Brüt dolar kazancı (Spot ve Vadeli taraftaki fiyat hareketinin toplam getirisi)
    # Arbitrajda iki bacak da aynı büyüklükte açıldığı için ana bakiye üzerinden brüt kâr:
    brut_kazanc_usdt = SPOT_BAKIYE * (brut_oran_farki / 100)
    
    # 3. Ödenecek Toplam Komisyonlar (Giriş + Çıkış)
    spot_toplam_komisyon = (SPOT_BAKIYE * SPOT_FEE_RATE) * 2
    futures_toplam_komisyon = (FUTURES_BAKIYE * FUTURES_FEE_RATE) * 2
    toplam_kesinti_usdt = spot_toplam_komisyon + futures_toplam_komisyon
    
    # 4. Net Kazanç
    net_kazanc_usdt = brut_kazanc_usdt - toplam_kesinti_usdt
    return brut_kazanc_usdt, toplam_kesinti_usdt, net_kazanc_usdt

# --- 🌐 WEBSOCKET AKIŞLARI ---
def start_spot_ws():
    def on_message(ws, message):
        data = json.loads(message)
        piyasa_verisi["spot_price"] = float(data.get("p", 0))
    def on_error(ws, error): print(f"Spot WS Hatası: {error}")
    def on_close(ws, c_code, c_msg):
        time.sleep(5); start_spot_ws()
    url = f"wss://stream.binance.com:9443/ws/{SYMBOL}@trade"
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever()

def start_futures_ws():
    def on_message(ws, message):
        data = json.loads(message)
        piyasa_verisi["futures_price"] = float(data.get("p", 0))
    def on_error(ws, error): print(f"Futures WS Hatası: {error}")
    def on_close(ws, c_code, c_msg):
        time.sleep(5); start_futures_ws()
    url = f"wss://fstream.binance.com/ws/{SYMBOL}@trade"
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever()

# --- 🧠 ANALİZ MOTORU ---
def arbitraj_tarama_dongusu():
    global arbitraj_pozisyon
    print("Arbitraj Analiz Motoru komisyon filtreleriyle çalışıyor...")
    
    while True:
        try:
            spot_fiyat = piyasa_verisi["spot_price"]
            futures_fiyat = piyasa_verisi["futures_price"]
            
            if not spot_fiyat or not futures_fiyat:
                time.sleep(1)
                continue

            anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
            print(f"⏱️ Spot: {spot_fiyat:.2f} | Futures: {futures_fiyat:.2f} | Makas: %{anlik_makas:.4f}")

            if not arbitraj_pozisyon["aktif"]:
                # 🟢 GİRİŞ KONTROLLERİ
                if anlik_makas >= GIRIS_MAKAS_YUZDE or anlik_makas <= -GIRIS_MAKAS_YUZDE:
                    yon = "ARTI" if anlik_makas >= GIRIS_MAKAS_YUZDE else "EKSI"
                    arbitraj_pozisyon.update({
                        "aktif": True,
                        "yon": yon,
                        "giris_makas": anlik_makas,
                        "spot_giris_fiyat": spot_fiyat,
                        "futures_gener_fiyat": futures_fiyat
                    })
                    
                    # Girişte tahmini hesaplama yapıyoruz (Pozisyon CIKIS_MAKAS_YUZDE'de kapanırsa ne kalacak?)
                    brut, kesinti, net = net_kar_hesapla(anlik_makas, CIKIS_MAKAS_YUZDE if yon == "ARTI" else -CIKIS_MAKAS_YUZDE)
                    
                    baslik = "🚀 POZİTİF ARBİTRAJ" if yon == "ARTI" else "📉 NEGATİF ARBİTRAJ"
                    oneri = "Spot AL, Vadeli SHORT aç!" if yon == "ARTI" else "Spot SAT, Vadeli LONG aç!"
                    
                    mesaj = (
                        f"💥 <b>{baslik} FIRSATI!</b>\n\n"
                        f"📊 <b>Parite:</b> {SYMBOL.upper()}\n"
                        f"⚡ <b>Giriş Makası:</b> %{anlik_makas:.4f}\n"
                        f"💰 <b>Hedef Büyüklüğü:</b> {SPOT_BAKIYE}$ Spot + {FUTURES_BAKIYE}$ Vadeli\n\n"
                        f"💵 <b>Tahmini Brüt Kazanç:</b> {brut:.2f} USDT\n"
                        f"✂️ <b>Toplam Komisyon Kesintisi:</b> {kesinti:.2f} USDT\n"
                        f"💵 <b>💵 NET CEBE KALACAK:</b> <b>{net:.2f} USDT</b>\n\n"
                        f"💡 <i>{oneri}</i>"
                    )
                    telegram_bildir(mesaj)
                    
            else:
                # 🔴 ÇIKIŞ KONTROLLERİ
                pozisyon_kapandi = False
                if arbitraj_pozisyon["yon"] == "ARTI" and anlik_makas <= CIKIS_MAKAS_YUZDE:
                    pozisyon_kapandi = True
                elif arbitraj_pozisyon["yon"] == "EKSI" and anlik_makas >= -CIKIS_MAKAS_YUZDE:
                    pozisyon_kapandi = True
                    
                if pozisyon_kapandi:
                    brut, kesinti, net = net_kar_hesapla(arbitraj_pozisyon["giris_makas"], anlik_makas)
                    
                    mesaj = (
                        f"🤝 <b>🔒 ARBİTRAJ POZİSYONU KAPANDI</b>\n\n"
                        f"📊 <b>Parite:</b> {SYMBOL.upper()}\n"
                        f"📉 <b>Kapanış Makası:</b> %{anlik_makas:.4f}\n\n"
                        f"💰 <b>Gerçekleşen Brüt Kâr:</b> {brut:.2f} USDT\n"
                        f"✂️ <b>Ödenen Toplam Komisyon:</b> {kesinti:.2f} USDT\n"
                        f"🎉 <b>NET TEMİZ KÂR:</b> <b>{net:.2f} USDT</b>"
                    )
                    telegram_bildir(mesaj)
                    arbitraj_pozisyon["aktif"] = False

        except Exception as e:
            print(f"Analiz motoru hatası: {e}")
        time.sleep(2)

if __name__ == "__main__":
    spot_thread = threading.Thread(target=start_spot_ws, daemon=True)
    futures_thread = threading.Thread(target=start_futures_ws, daemon=True)
    spot_thread.start()
    futures_thread.start()
    arbitraj_tarama_dongusu()
