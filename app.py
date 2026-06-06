import os
import time
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- 📊 ARBİTRAJ STRATEJİ AYARLARI ---
SYMBOL = "BTCUSDT"

# 🛠️ GİRİŞ EŞİĞİNİ ELLE DEĞİŞTİRİLEBİLİR YAPTIK:
# Railway panelinde GIRIS_MAKAS_YUZDE değişkenini ne yaparsan kod onu okur. 
# Eğer panelde tanımlanmamışsa otomatik olarak %0.08 kabul edilir.
GIRIS_MAKAS_YUZDE = float(os.environ.get("GIRIS_MAKAS_YUZDE", 0.08))

CIKIS_MAKAS_YUZDE = 0.01  # Makas %0.01'e düştüğünde çıkış sinyali verir
LOOP_INTERVAL = 2         # Piyasayı 2 saniyede bir tarar
# -------------------------------------

# Binance API Adresleri
SPOT_URL = "https://api.binance.com/api/v3/ticker/price"
FUTURES_URL = "https://fapi.binance.com/fapi/v1/ticker/price"

# Sanal arbitraj pozisyon takip hafızası
arbitraj_pozisyon = {
    "aktif": False,
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

def get_live_prices():
    """Hem Spot hem de Vadeli piyasadan anlık fiyatları çeker"""
    spot_price = None
    futures_price = None
    
    # 1. Spot Fiyatını Çek
    try:
        r_spot = requests.get(SPOT_URL, params={"symbol": SYMBOL.upper()}, timeout=5)
        if r_spot.status_code == 200:
            spot_price = float(r_spot.json().get("price", 0))
    except Exception as e:
        print(f"Spot fiyat çekme hatası: {e}")
        
    # 2. Vadeli (Futures) Fiyatını Çek
    try:
        r_fut = requests.get(FUTURES_URL, params={"symbol": SYMBOL.upper()}, timeout=5)
        if r_fut.status_code == 200:
            futures_price = float(r_fut.json().get("price", 0))
    except Exception as e:
        print(f"Futures fiyat çekme hatası: {e}")
        
    return spot_price, futures_price

def arbitraj_tarama():
    global arbitraj_pozisyon
    
    spot_fiyat, futures_fiyat = get_live_prices()
    
    if not spot_fiyat or not futures_fiyat:
        print("Fiyatlar çekilemedi, bir sonraki saniye tekrar denenecek...")
        return

    # Vadeli işlem ile Spot arasındaki makas yüzdesini hesapla
    anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
    
    print(f"⏱️ Spot: {spot_fiyat:.2f} | Futures: {futures_fiyat:.2f} | Makas: %{anlik_makas:.4f} (Hedef: %{GIRIS_MAKAS_YUZDE:.2f})")

    if not arbitraj_pozisyon["aktif"]:
        # 🟢 GİRİŞ KOŞULU KONTROLÜ
        if anlik_makas >= GIRIS_MAKAS_YUZDE:
            arbitraj_pozisyon.update({
                "aktif": True,
                "giris_makas": anlik_makas,
                "spot_giris_fiyat": spot_fiyat,
                "futures_giris_fiyat": futures_fiyat
            })
            
            mesaj = (
                f"🚀 <b>💥 ARBİTRAJ FIRSATI YAKALANDI!</b>\n\n"
                f"📊 <b>Parite:</b> {SYMBOL}\n"
                f"🟢 <b>Spot Fiyat:</b> {spot_fiyat:.2f} USDT\n"
                f"🔴 <b>Futures Fiyat:</b> {futures_fiyat:.2f} USDT\n"
                f"⚡ <b>Anlık Makas (Spread):</b> %{anlik_makas:.3f}\n"
                f"🎯 <b>Tetiklenen Eşik:</b> %{GIRIS_MAKAS_YUZDE:.2f}\n\n"
                f"💡 <i>Manuel İşlem Önerisi: Spot piyasadan AL, Vadeli piyasada aynı miktarda SHORT aç!</i>"
            )
            telegram_bildir(mesaj)
            
    else:
        # 🔴 ÇIŞIŞ KOŞULU KONTROLÜ (Makas daraldı mı?)
        if anlik_makas <= CIKIS_MAKAS_YUZDE:
            kar_orani = arbitraj_pozisyon["giris_makas"] - anlik_makas
            
            mesaj = (
                f"🤝 <b>🔒 ARBİTRAJ POZİSYONU KAPANDI</b>\n\n"
                f"📊 <b>Parite:</b> {SYMBOL}\n"
                f"📉 <b>Makas Daraldı:</b> %{anlik_makas:.3f}'e düştü.\n"
                f"💰 <b>Tahmini Brüt Kazanç:</b> %{kar_orani:.3f}\n\n"
                f"💡 <i>Manuel İşlem Önerisi: Spot malları SAT, Vadeli SHORT pozisyonunu KAPAT!</i>"
            )
            telegram_bildir(mesaj)
            
            # Hafızayı sıfırla
            arbitraj_pozisyon["aktif"] = False

if __name__ == "__main__":
    # İlk başlangıçta küçük bir imza düzeltmesi: 
    # Kodun başındaki "İmport os" ifadesi Python'da hata verir, doğrusu "import os" olmalıdır.
    print("Binance Spot-Futures Arbitraj Gözlemcisi Başlatıldı...")
    telegram_bildir(
        f"🛰️ <b>Binance Dinamik Arbitraj Botu Yayında!</b>\n"
        f"Piyasa: {SYMBOL}\n"
        f"Güncel Giriş Eşiği: %{GIRIS_MAKAS_YUZDE}\n"
        f"Çıkış Eşiği: %{CIKIS_MAKAS_YUZDE}\n"
        f"Eşiği değiştirmek için Railway panelinden <code>GIRIS_MAKAS_YUZDE</code> değişkenini güncelleyebilirsiniz."
    )
    
    while True:
        try:
            arbitraj_tarama()
        except Exception as e:
            print(f"Sistem döngü hatası: {e}")
        time.sleep(LOOP_INTERVAL)
