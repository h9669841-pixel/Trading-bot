import os
import time
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- 📊 ARBİTRAJ STRATEJİ AYARLARI ---
SYMBOL = "BTCUSDT"

# Eşiği yine Railway panelindeki GIRIS_MAKAS_YUZDE değişkeninden okur. Yoksa %0.05 kabul eder.
GIRIS_MAKAS_YUZDE = float(os.environ.get("GIRIS_MAKAS_YUZDE", 0.05))
CIKIS_MAKAS_YUZDE = 0.01  

# 🛑 IP ENGELİNİ AŞMAK İÇİN KRİTİK AYAR: 
# Sorgu aralığını 4 saniyeye çıkararak Binance'in radarına takılmayı önlüyoruz.
LOOP_INTERVAL = 4         
# -------------------------------------

# 🌐 IP Engeline Karşı Alternatif Ağ Yedekli API Adresleri (api3 kullanımı daha stabildir)
SPOT_URL = "https://api3.binance.com/api/v3/ticker/price"
FUTURES_URL = "https://fapi.binance.com/fapi/v1/ticker/price"

arbitraj_pozisyon = {
    "aktif": False,
    "yon": None, 
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
    
    # İstek başlıklarına (Headers) bot olmadığımızı belirten standart bir tarayıcı kimliği ekliyoruz
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        r_spot = requests.get(SPOT_URL, params={"symbol": SYMBOL.upper()}, headers=headers, timeout=5)
        if r_spot.status_code == 200:
            spot_price = float(r_spot.json().get("price", 0))
        elif r_spot.status_code == 429:
            print("🚨 Spot API: Binance çok sık istek attığımızı söylüyor (Rate Limit)!")
    except Exception as e:
        print(f"Spot fiyat hatası: {e}")
        
    try:
        r_fut = requests.get(FUTURES_URL, params={"symbol": SYMBOL.upper()}, headers=headers, timeout=5)
        if r_fut.status_code == 200:
            futures_price = float(r_fut.json().get("price", 0))
        elif r_fut.status_code == 429:
            print("🚨 Futures API: Binance çok sık istek attığımızı söylüyor (Rate Limit)!")
    except Exception as e:
        print(f"Futures fiyat hatası: {e}")
        
    return spot_price, futures_price

def arbitraj_tarama():
    global arbitraj_pozisyon
    
    spot_fiyat, futures_fiyat = get_live_prices()
    
    if not spot_fiyat or not futures_fiyat:
        print("Fiyatlar çekilemedi, havuzun rahatlaması için bir sonraki döngü beklenecek...")
        return

    anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
    
    print(f"⏱️ Spot: {spot_fiyat:.2f} | Futures: {futures_fiyat:.2f} | Makas: %{anlik_makas:.4f} (Hedef Eşik: %{GIRIS_MAKAS_YUZDE:.3f})")

    if not arbitraj_pozisyon["aktif"]:
        if anlik_makas >= GIRIS_MAKAS_YUZDE:
            arbitraj_pozisyon.update({
                "aktif": True,
                "yon": "ARTI",
                "giris_makas": anlik_makas
            })
            mesaj = (
                f"🚀 <b>💥 POZİTİF ARBİTRAJ FIRSATI!</b>\n\n"
                f"📊 <b>Parite:</b> {SYMBOL}\n"
                f"🟢 <b>Spot Fiyat:</b> {spot_fiyat:.2f} USDT\n"
                f"🔴 <b>Futures Fiyat:</b> {futures_fiyat:.2f} USDT\n"
                f"⚡ <b>Anlık Makas:</b> +%{anlik_makas:.4f}\n\n"
                f"💡 <i>Öneri: Spot AL, Vadeli SHORT aç!</i>"
            )
            telegram_bildir(mesaj)
            
        elif anlik_makas <= -GIRIS_MAKAS_YUZDE:
            arbitraj_pozisyon.update({
                "aktif": True,
                "yon": "EKSI",
                "giris_makas": anlik_makas
            })
            mesaj = (
                f"📉 <b>💥 NEGATİF ARBİTRAJ FIRSATI!</b>\n\n"
                f"📊 <b>Parite:</b> {SYMBOL}\n"
                f"🟢 <b>Spot Fiyat:</b> {spot_fiyat:.2f} USDT\n"
                f"🔴 <b>Futures Fiyat:</b> {futures_fiyat:.2f} USDT\n"
                f"⚡ <b>Anlık Makas:</b> %{anlik_makas:.4f}\n\n"
                f"💡 <i>Öneri: Spot SAT, Vadeli LONG aç!</i>"
            )
            telegram_bildir(mesaj)
            
    else:
        if arbitraj_pozisyon["yon"] == "ARTI" and anlik_makas <= CIKIS_MAKAS_YUZDE:
            kar = arbitraj_pozisyon["giris_makas"] - anlik_makas
            mesaj = f"🤝 <b>🔒 POZİTİF ARBİTRAJ KAPANDI</b>\n📊 {SYMBOL}\n📉 Makas daraldı: %{anlik_makas:.4f}\n💰 Tahmini Kazanç: %{kar:.3f}"
            telegram_bildir(mesaj)
            arbitraj_pozisyon["aktif"] = False
            
        elif arbitraj_pozisyon["yon"] == "EKSI" and anlik_makas >= -CIKIS_MAKAS_YUZDE:
            kar = abs(arbitraj_pozisyon["giris_makas"]) - abs(anlik_makas)
            mesaj = f"🤝 <b>🔒 NEGATİF ARBİTRAJ KAPANDI</b>\n📊 {SYMBOL}\n📈 Makas normale döndü: %{anlik_makas:.4f}\n💰 Tahmini Kazanç: %{kar:.3f}"
            telegram_bildir(mesaj)
            arbitraj_pozisyon["aktif"] = False

if __name__ == "__main__":
    print("Binance Güvenli Çift Yönlü Arbitraj Gözlemcisi Başlatıldı...")
    telegram_bildir(
        f"🛰️ <b>Yedekli Filtreli Arbitraj Botu Devrede!</b>\n"
        f"Tarama Sıklığı: {LOOP_INTERVAL} saniyede bire çekildi.\n"
        f"Eşik Değeri: ±%{GIRIS_MAKAS_YUZDE}\n"
        f"Binance ban koruması ve tarayıcı kimlik maskesi (User-Agent) aktif edildi."
    )
    
    while True:
        try:
            arbitraj_tarama()
        except Exception as e:
            print(f"Sistem döngü hatası: {e}")
        time.sleep(LOOP_INTERVAL)
