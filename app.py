import os
import json
import time
import requests
import threading
from websocket import WebSocketApp

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- 📊 ARBİTRAJ STRATEJİ VE HESAP AYARLARI ---
GIRIS_MAKAS_YUZDE = 0.30  # Sinyal tetiklenecek pozitif brüt makas eşiği
CIKIS_MAKAS_YUZDE = 0.02  # Pozisyon kapatılıp kâr alınacak KESİN çıkış eşiği

# 💰 BAKİYE VE KOMİSYON AYARLARI (100$ Spot Alım + 100$ Vadeli Short)
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
    """Binance Vadeli İşlemler piyasasındaki tüm aktif USDT paritelerini otomatik çeker"""
    try:
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            symbols = []
            for market in data.get("symbols", []):
                if market.get("quoteAsset") == "USDT" and market.get("status") == "TRADING":
                    symbols.append(market.get("symbol").lower())
            return symbols
    except Exception as e:
        print(f"Koin listesi çekilirken hata oluştu: {e}")
    return ["btcusdt", "ethusdt", "solusdt", "xrpusdt"] # Hata koruma listesi

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
    
    # Giriş + çıkış toplam komisyon sabit masrafını düşer (100+100 için ~0.24 USDT)
    spot_toplam_komisyon = (SPOT_BAKIYE * SPOT_FEE_RATE) * 2
    futures_toplam_komisyon = (FUTURES_BAKIYE * FUTURES_FEE_RATE) * 2
    toplam_kesinti_usdt = spot_toplam_komisyon + futures_toplam_komisyon
    
    net_kazanc_usdt = brut_kazanc_usdt - toplam_kesinti_usdt
    return brut_kazanc_usdt, toplam_kesinti_usdt, net_kazanc_usdt

# --- 🌐 GLOBAL WEBSOCKET AKIŞLARI ---

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

    # Binance stream limiti nedeniyle en aktif ilk 150 parite ana tünele alınır
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

# --- 🧠 POZİTİF ARBİTRAJ MOTORU ---

def arbitraj_tarama_dongusu():
    global arbitraj_pozisyonlari
    print("Market Tarayıcı sadece net kârlı pozitif (+) fırsatları süzüyor...")
    
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
                
                en_yuksek_makaslar.append((coin_label, anlik_makas, spot_fiyat, futures_fiyat))

                pos = arbitraj_pozisyonlari[symbol]

                if not pos["aktif"]:
                    # 🟢 SADECE POZİTİF (+) MAKAS GİRİŞ KONTROLÜ
                    if anlik_makas >= GIRIS_MAKAS_YUZDE:
                        brut, kesinti, net = net_kar_hesapla(anlik_makas, CIKIS_MAKAS_YUZDE)
                        
                        # Girişte net kâr eksi veya sıfırsa pozisyona hiç başlama
                        if net <= 0:
                            continue
                        
                        pos.update({
                            "aktif": True,
                            "yon": "ARTI",
                            "giris_makas": anlik_makas,
                            "spot_giris_fiyat": spot_fiyat,
                            "futures_giris_fiyat": futures_fiyat
                        })
                        
                        mesaj = (
                            f"🚀 <b>YÜKSEK KÂRLI ARBİTRAJ FIRSATI!</b>\n\n"
                            f"📊 <b>Koin:</b> {coin_label}/USDT\n"
                            f"⚡ <b>Anlık Makas:</b> +%{anlik_makas:.4f}\n"
                            f"💰 <b>Gerekli Nakit:</b> {SPOT_BAKIYE}$ Spot + {FUTURES_BAKIYE}$ Vadeli\n\n"
                            f"💵 <b>Tahmini Brüt Kazanç:</b> {brut:.4f} USDT\n"
                            f"✂️ <b>Toplam Komisyon Masrafı:</b> {kesinti:.4f} USDT\n"
                            f"🎉 <b>NET TEMİZ KÂR:</b> <b>{net:.4f} USDT</b>\n\n"
                            f"💡 <b>TALİMAT:</b> <u>{coin_label} Spot cüzdandan SATIN AL, Vadeli tarafta SHORT aç!</u>"
                        )
                        telegram_bildir(mesaj)
                        
                else:
                    # 🔴 KESİN ÇIKIŞ KONTROLÜ (İstediğin gibi eksideyken asla kapatmaz, sadece hedefe sadık kalır)
                    # Makas, bizim belirlediğimiz çıkış eşiğine (%0.02) eşit veya altına inene kadar bekler.
                    if anlik_makas <= CIKIS_MAKAS_YUZDE:
                        brut, kesinti, net = net_kar_hesapla(pos["giris_makas"], anlik_makas)
                        mesaj = (
                            f"🤝 <b>🔒 {coin_label} POZİSYONU BAŞARIYLA KAPANDI</b>\n\n"
                            f"📉 <b>Kapanış Makası:</b> %{anlik_makas:.4f}\n"
                            f"💰 <b>Brüt Kâr:</b> {brut:.4f} USDT\n"
                            f"🎉 <b>NET TEMİZ KÂR:</b> <b>{net:.4f} USDT</b>\n\n"
                            f"💡 <b>TALİMAT:</b> Spottaki malı sat, vadelideki shortu kapat ve tamamen nakit USDT'ye dön!"
                        )
                        telegram_bildir(mesaj)
                        pos["aktif"] = False

            # Konsolda piyasanın en yüksek ilk 3 pozitif makasını listeler
            if en_yuksek_makaslar:
                en_yuksek_makaslar.sort(key=lambda x: x[1], reverse=True)
                print("\n💵 --- PİYASADA ANLIK EN YÜKSEK 3 POZİTİF MAKAS ---")
                for i, item in enumerate(en_yuksek_makaslar[:3]):
                    print(f"{i+1}. [{item[0]}] Makas: +%{item[1]:.4f} | Spot: {item[2]:.2f} | Fut: {item[3]:.2f}")

        except Exception as e:
            print(f"Scanner döngü hatası: {e}")
            
        time.sleep(2)

if __name__ == "__main__":
    print("🔄 Binance API'den tüm aktif vadeli koin listesi taranıyor...")
    SYMBOLS = get_all_futures_symbols()
    print(f"✅ Toplam {len(SYMBOLS)} aktif parite radara alındı. Altyapı hazırlanıyor...")
    
    piyasa_verisi = {symbol: {"spot_price": None, "futures_price": None} for symbol in SYMBOLS}
    arbitraj_pozisyonlari = {symbol: {"aktif": False, "yon": None, "giris_makas": 0.0, "spot_giris_fiyat": 0.0, "futures_giris_fiyat": 0.0} for symbol in SYMBOLS}
    
    telegram_bildir(
        f"🕵️‍♂️ <b>Sabırlı Arbitraj Botu Başlatıldı!</b>\n\n"
        f"🎯 <b>Giriş Eşiği:</b> +%{GIRIS_MAKAS_YUZDE}\n"
        f"🔒 <b>Çıkış Eşiği:</b> %{CIKIS_MAKAS_YUZDE}\n"
        f"🛡️ <b>Kural:</b> Pozisyon açıldıktan sonra makas tersine genişlese dahi hedef seviyeye gelene kadar pozisyonlar sıkıca korunacaktır."
    )
    
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    
    arbitraj_tarama_dongusu()
