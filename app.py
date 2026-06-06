import os
import time
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- 📊 ARBİTRAJ STRATEJİ AYARLARI ---
SYMBOL = "BTCUSDT"

# 🛠️ GİRİŞ EŞİĞİ (Hem + hem - yön için mutlak değer olarak çalışır)
# Örneğin 0.05 girerseniz, makas hem +0.05 olduğunda hem de -0.05 olduğunda sinyal gelir.
GIRIS_MAKAS_YUZDE = float(os.environ.get("GIRIS_MAKAS_YUZDE", 0.05))

# Çıkış için makasın sıfıra yaklaşma eşiği (%0.01 veya altına inince pozisyon biter)
CIKIS_MAKAS_YUZDE = 0.01  
LOOP_INTERVAL = 2         
# -------------------------------------

SPOT_URL = "https://api.binance.com/api/v3/ticker/price"
FUTURES_URL = "https://fapi.binance.com/fapi/v1/ticker/price"

# Çift yönlü takip için geliştirilmiş hafıza mekanizması
arbitraj_pozisyon = {
    "aktif": False,
    "yon": None, # "ARTI" veya "EKSI"
    "giris_makas": 0.0
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

def get_live_prices():
    spot_price = None
    futures_price = None
    try:
        r_spot = requests.get(SPOT_URL, params={"symbol": SYMBOL.upper()}, timeout=5)
        if r_spot.status_code == 200:
            spot_price = float(r_spot.json().get("price", 0))
    except Exception as e:
        print(f"Spot fiyat hatası: {e}")
        
    try:
        r_fut = requests.get(FUTURES_URL, params={"symbol": SYMBOL.upper()}, timeout=5)
        if r_fut.status_code == 200:
            futures_price = float(r_fut.json().get("price", 0))
    except Exception as e:
        print(f"Futures fiyat hatası: {e}")
        
    return spot_price, futures_price

def arbitraj_tarama():
    global arbitraj_pozisyon
    
    spot_fiyat, futures_fiyat = get_live_prices()
    
    if not spot_fiyat or not futures_fiyat:
        print("Fiyatlar çekilemedi, bekleniyor...")
        return

    # Makas hesabı
    anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
    
    print(f"⏱️ Spot: {spot_fiyat:.2f} | Futures: {futures_fiyat:.2f} | Makas: %{anlik_makas:.4f} (Hedef Eşik: %{GIRIS_MAKAS_YUZDE:.3f})")

    if not arbitraj_pozisyon["aktif"]:
        # 🟢 GİRİŞ KONTROLLERİ
        
        # Durum A: Vadeli piyasa pahalı (Pozitif Makas)
        if anlik_makas >= GIRIS_MAKAS_YUZDE:
            arbitraj_pozisyon.update({
                "aktif": True,
                "yon": "ARTI",
                "giris_makas": anlik_makas
            })
            mesaj = (
                f"🚀 <b>💥 POZİTİF ARBİTRAJ FIRSATI! (Vadeli Pahalı)</b>\n\n"
                f"📊 <b>Parite:</b> {SYMBOL}\n"
                f"🟢 <b>Spot Fiyat:</b> {spot_fiyat:.2f} USDT\n"
                f"🔴 <b>Futures Fiyat:</b> {futures_fiyat:.2f} USDT\n"
                f"⚡ <b>Anlık Makas:</b> +%{anlik_makas:.4f}\n\n"
                f"💡 <i>Manuel Önerisi: Spot piyasadan AL, Vadeli piyasada SHORT aç!</i>"
            )
            telegram_bildir(mesaj)
            
        # Durum B: Vadeli piyasa ucuz (Negatif Makas - Senin durumun!)
        elif anlik_makas <= -GIRIS_MAKAS_YUZDE:
            arbitraj_pozisyon.update({
                "aktif": True,
                "yon": "EKSI",
                "giris_makas": anlik_makas
            })
            mesaj = (
                f"📉 <b>💥 NEGATİF ARBİTRAJ FIRSATI! (Vadeli Ucuz)</b>\n\n"
                f"📊 <b>Parite:</b> {SYMBOL}\n"
                f"🟢 <b>Spot Fiyat:</b> {spot_fiyat:.2f} USDT\n"
                f"🔴 <b>Futures Fiyat:</b> {futures_fiyat:.2f} USDT\n"
                f"⚡ <b>Anlık Makas:</b> %{anlik_makas:.4f}\n\n"
                f"💡 <i>Manuel Önerisi: Spot malları SAT, Vadeli piyasada LONG aç!</i>"
            )
            telegram_bildir(mesaj)
            
    else:
        # 🔴 ÇIŞIŞ KONTROLLERİ (Makasın normalleşmesi/kapanması durumu)
        
        # Pozitif pozisyondan çıkış (Makas sıfıra doğru daraldı mı?)
        if arbitraj_pozisyon["yon"] == "ARTI" and anlik_makas <= CIKIS_MAKAS_YUZDE:
            kar = arbitraj_pozisyon["giris_makas"] - anlik_makas
            mesaj = f"🤝 <b>🔒 POZİTİF ARBİTRAJ KAPANDI</b>\n📊 {SYMBOL}\n📉 Makas daraldı: %{anlik_makas:.4f}\n💰 Tahmini Kazanç: %{kar:.3f}"
            telegram_bildir(mesaj)
            arbitraj_pozisyon["aktif"] = False
            
        # Negatif pozisyondan çıkış (Eksi makas sıfıra doğru yukarı tırmandı mı?)
        elif arbitraj_pozisyon["yon"] == "EKSI" and anlik_makas >= -CIKIS_MAKAS_YUZDE:
            # Eksiden girdiğimiz için kâr hesabı tam tersidir
            kar = abs(arbitraj_pozisyon["giris_makas"]) - abs(anlik_makas)
            mesaj = f"🤝 <b>🔒 NEGATİF ARBİTRAJ KAPANDI</b>\n📊 {SYMBOL}\n📈 Makas normale döndü: %{anlik_makas:.4f}\n💰 Tahmini Kazanç: %{kar:.3f}"
            telegram_bildir(mesaj)
            arbitraj_pozisyon["aktif"] = False

if __name__ == "__main__":
    print("Binance Çift Yönlü Arbitraj Gözlemcisi Başlatıldı...")
    telegram_bildir(
        f"🛰️ <b>Binance Çift Yönlü Arbitraj Botu Yayında!</b>\n"
        f"Piyasa: {SYMBOL}\n"
        f"Hassasiyet Eşiği: ±%{GIRIS_MAKAS_YUZDE}\n"
        f"Artık makas eksiye de gitse artıya da gitse botunuz tetikte!"
    )
    
    while True:
        try:
            arbitraj_tarama()
        except Exception as e:
            print(f"Sistem döngü hatası: {e}")
        time.sleep(LOOP_INTERVAL)
