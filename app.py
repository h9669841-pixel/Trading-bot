import os
import json
import time
import requests
import threading
from websocket import WebSocketApp

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- 📊 ARBİTRAJ STRATEJİ VE HESAP AYARLARI ---
GIRIS_MAKAS_YUZDE = 0.99  # Sinyal tetiklenecek brüt makas eşiği
CIKIS_MAKAS_YUZDE = 0.02  # Pozisyon kapandı sayılacak çıkış eşiği

# 💰 BAKİYE VE KOMİSYON AYARLARI (100$ + 100$)
SPOT_BAKIYE = 100.0       
FUTURES_BAKIYE = 100.0    

SPOT_FEE_RATE = 0.0750 / 100     # %0.0750 Taker komisyonu
FUTURES_FEE_RATE = 0.0450 / 100  # %0.0450 Taker komisyonu
# ----------------------------------------------

# Dinamik koin listesi ve fiyat hafızası
SYMBOLS = []
piyasa_verisi = {}
arbitraj_pozisyonlari = {}

def get_all_futures_symbols():
    """Binance Vadeli İşlemler piyasasındaki tüm USDT paritelerini otomatik çeker"""
    try:
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            symbols = []
            for market in data.get("symbols", []):
                # Sadece aktif ve USDT çifti olan koinleri filtrele (Örn: BTCUSDT)
                if market.get("quoteAsset") == "USDT" and market.get("status") == "TRADING":
                    symbols.append(market.get("symbol").lower())
            return symbols
    except Exception as e:
        print(f"Koin listesi çekilirken hata oluştu: {e}")
    return ["btcusdt", "ethusdt", "solusdt", "xrpusdt"] # Hata durumunda koruma listesi

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

# --- 🌐 TÜM PİYASAYI DİNLEYEN WEBSOCKET SİSTEMİ ---

def start_multi_spot_ws():
    def on_message(ws, message):
        data = json.loads(message)
        stream_name = data.get("stream", "")
        event_data = data.get("data", {})
        symbol = stream_name.split("@")[0]
        if symbol in piyasa_verisi:
            piyasa_verisi[symbol]["spot_price"] = float(event_data.get("p", 0))

    def on_error(ws, error): print(f"Global Spot WS Hatası: {error}")
    def on_close(ws, c_code, c_msg): time.sleep(5); start_multi_spot_ws()

    # Binance'in bağlantı başına maksimum 200 stream limitine takılmamak için 
    # İlk aşamada en aktif 150 pariteyi tünel içerisine alıyoruz
    streams = "/".join([f"{symbol}@trade" for symbol in SYMBOLS[:150]])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever()

def start_multi_futures_ws():
    def on_message(ws, message):
        data = json.loads(message)
        stream_name = data.get("stream", "")
        event_data = data.get("data", {})
        symbol = stream_name.split("@")[0]
        if symbol in piyasa_verisi:
            piyasa_verisi[symbol]["futures_price"] = float(event_data.get("p", 0))

    def on_error(ws, error): print(f"Global Futures WS Hatası: {error}")
    def on_close(ws, c_code, c_msg): time.sleep(5); start_multi_futures_ws()

    streams = "/".join([f"{symbol}@trade" for symbol in SYMBOLS[:150]])
    url = f"wss://fstream.binance.com/stream?streams={streams}"
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever()

# --- 🧠 300+ KOİNİ AYNI ANDA KIYASLAYAN ANALİZ MOTORU ---

def arbitraj_tarama_dongusu():
    global arbitraj_pozisyonlari
    print("Market Tarayıcı arka planda en yüksek makasları süzüyor...")
    
    while True:
        try:
            en_yuksek_makaslar = []

            for symbol in SYMBOLS[:150]:
                spot_fiyat = piyasa_verisi[symbol]["spot_price"]
                futures_fiyat = piyasa_verisi[symbol]["futures_price"]
                
                if not spot_fiyat or not futures_fiyat:
                    continue

                anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
                coin_label = symbol.upper().replace("USDT", "")
                
                # Anlık tarama listesini doldur (Konsolda ilk 3'ü göstermek için)
                en_yuksek_makaslar.append((coin_label, anlik_makas, spot_fiyat, futures_fiyat))

                pos = arbitraj_pozisyonlari[symbol]

                if not pos["aktif"]:
                    # 🟢 TÜM PİYASADA GİRİŞ KONTROLÜ
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
                            f"💥 <b>PİYASADA MAKSİMUM MAKAS YAKALANDI!</b>\n\n"
                            f"📊 <b>Koin:</b> {coin_label}/USDT\n"
                            f"⚡ <b>Anlık Makas Oranı:</b> %{anlik_makas:.4f}\n"
                            f"💰 <b>İşlem Büyüklüğü:</b> {SPOT_BAKIYE}$ + {FUTURES_BAKIYE}$\n\n"
                            f"💵 <b>Tahmini Brüt Kazanç:</b> {brut:.4f} USDT\n"
                            f"✂️ <b>Toplam Komisyon:</b> {kesinti:.4f} USDT\n"
                            f"🎉 <b>NET CEBE KALACAK:</b> <b>{net:.4f} USDT</b>\n\n"
                            f"💡 <i>{oneri}</i>"
                        )
                        telegram_bildir(mesaj)
                        
                else:
                    # 🔴 ÇIŞIŞ KONTROLÜ
                    pozisyon_kapandi = False
                    if pos["yon"] == "ARTI" and anlik_makas <= CIKIS_MAKAS_YUZDE:
                        pozisyon_kapandi = True
                    elif pos["yon"] == "EKSI" and anlik_makas >= -CIKIS_MAKAS_YUZDE:
                        pozisyon_kapandi = True
                        
                    if pozisyon_kapandi:
                        brut, kesinti, net = net_kar_hesapla(pos["giris_makas"], anlik_makas)
                        mesaj = (
                            f"🤝 <b>🔒 {coin_label} POZİSYONU BAŞARIYLA KAPANDI</b>\n\n"
                            f"📉 <b>Kapanış Makası:</b> %{anlik_makas:.4f}\n"
                            f"💰 <b>Brüt Kâr:</b> {brut:.4f} USDT\n"
                            f"🎉 <b>NET TEMİZ KÂR:</b> <b>{net:.4f} USDT</b>"
                        )
                        telegram_bildir(mesaj)
                        pos["aktif"] = False

            # Konsol Ekranında o saniye piyasada en çok açılan ilk 3 makası gösterir (Ekranı yormaz)
            if en_yuksek_makaslar:
                en_yuksek_makaslar.sort(key=lambda x: abs(x[1]), reverse=True)
                print("\n🔥 --- PİYASADA ANLIK EN YÜKSEK 3 MAKAS ---")
                for i, item in enumerate(en_yuksek_makaslar[:3]):
                    print(f"{i+1}. [{item[0]}] Makas: %{item[1]:.4f} | Spot: {item[2]:.2f} | Fut: {item[3]:.2f}")

        except Exception as e:
            print(f"Scanner döngü hatası: {e}")
            
        time.sleep(2)

if __name__ == "__main__":
    print("🔄 Binance API'den tüm aktif vadeli koin listesi taranıyor...")
    SYMBOLS = get_all_futures_symbols()
    print(f"✅ Toplam {len(SYMBOLS)} aktif parite tespit edildi. Altyapı hazırlanıyor...")
    
    # Hafıza sözlüklerini dinamik doldur
    piyasa_verisi = {symbol: {"spot_price": None, "futures_price": None} for symbol in SYMBOLS}
    arbitraj_pozisyonlari = {symbol: {"aktif": False, "yon": None, "giris_makas": 0.0, "spot_giris_fiyat": 0.0, "futures_giris_fiyat": 0.0} for symbol in SYMBOLS}
    
    telegram_bildir(
        f"🕵️‍♂️ <b>Canlı Piyasa Tarayıcı Arbitraj Botu Başlatıldı!</b>\n\n"
        f"Binance üzerindeki tüm aktif pariteler dinamik olarak radara alındı.\n"
        f"🎯 <b>Giriş Eşiği:</b> ±%{GIRIS_MAKAS_YUZDE}\n"
        f"💰 <b>Kasa:</b> 100$ + 100$\n"
        f"Bot o an piyasada en çok ayrışan koinleri ayıklayıp sinyal atacaktır!"
    )
    
    # Websocket iplerini başlat
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    
    arbitraj_tarama_dongusu()
