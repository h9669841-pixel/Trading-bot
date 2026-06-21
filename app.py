import os
import time
import threading
import traceback
import requests
import ccxt

# --- 🔑 API VE HESAP AYARLARI ---
BINANCE_API = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET = os.environ.get("BINANCE_SECRET_KEY", "")

BYBIT_API = os.environ.get("BYBIT_API_KEY", "")
BYBIT_SECRET = os.environ.get("BYBIT_SECRET_KEY", "")

OKX_API = os.environ.get("OKX_API_KEY", "")
OKX_SECRET = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSWORD = os.environ.get("OKX_PASSWORD", "")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- 🌐 PROXY VE BAĞLANTI AYARLARI ---
PROXY_URL = os.environ.get("PROXY_URL")
ccxt_proxy_config = {}
if PROXY_URL:
    ccxt_proxy_config = {'socksProxy': PROXY_URL}

# --- 🏛️ BORSA BAĞLANTILARINI BAŞLATMA ---
exchange_binance = ccxt.binance({'enableRateLimit': True, **ccxt_proxy_config})
exchange_bybit = ccxt.bybit({'enableRateLimit': True, **ccxt_proxy_config})
exchange_okx = ccxt.okx({'enableRateLimit': True, **ccxt_proxy_config})

# --- 📊 ARBİTRAJ AYARLARI ---
GIRIS_MAKAS_YUZDE = 0.60  

# CCXT'nin 3 borsada da (Binance, Bybit, OKX) istisnasız %100 aynı isimle tanıdığı en likit çiftler
TARANACAK_COINLER = [
    'FIL/USDT', 'FIL/USDT:USDT',
    'LTC/USDT', 'LTC/USDT:USDT',
    'APT/USDT', 'APT/USDT:USDT',
    'OP/USDT', 'OP/USDT:USDT',
    'ARB/USDT', 'ARB/USDT:USDT',
    'NEAR/USDT', 'NEAR/USDT:USDT',
    'ATOM/USDT', 'ATOM/USDT:USDT',
    'GRT/USDT', 'GRT/USDT:USDT',
    'PEPE/USDT', 'PEPE/USDT:USDT',
    'WIF/USDT', 'WIF/USDT:USDT'
]

fiyat_havuzu = {
    'Binance': {},
    'Bybit': {},
    'OKX': {}
}

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, timeout=5)
    except Exception:
        pass

# --- ⚡ GÜVENLİ VE BAĞIMSIZ FİYAT TOPLAMA MOTORLARI ---
def fiyat_cek_binance():
    while True:
        for symbol in TARANACAK_COINLER:
            try:
                ticker = exchange_binance.fetch_ticker(symbol)
                if ticker and ticker['last']:
                    fiyat_havuzu['Binance'][symbol] = float(ticker['last'])
            except Exception:
                pass # Bir koin hata verirse botun çökmesini engelle, sıradakine geç
            time.sleep(0.2)
        time.sleep(1)

def fiyat_cek_bybit():
    while True:
        for symbol in TARANACAK_COINLER:
            try:
                ticker = exchange_bybit.fetch_ticker(symbol)
                if ticker and ticker['last']:
                    fiyat_havuzu['Bybit'][symbol] = float(ticker['last'])
            except Exception:
                pass
            time.sleep(0.2)
        time.sleep(1)

def fiyat_cek_okx():
    while True:
        for symbol in TARANACAK_COINLER:
            try:
                ticker = exchange_okx.fetch_ticker(symbol)
                if ticker and ticker['last']:
                    fiyat_havuzu['OKX'][symbol] = float(ticker['last'])
            except Exception:
                pass
            time.sleep(0.2)
        time.sleep(1)

# --- 🎯 ARBİTRAJ MATEMATİK MOTORU ---
def uc_borsa_arbitraj_motoru():
    print("🚀 3'lü Canavar İzleme Motoru Başarıyla Devreye Girdi.")
    telegram_bildir("🤖 3'lü Canavar Başlatıldı.")
    
    while True:
        try:
            fırsat_listesi = []
            
            # Canlı veri akışını takip edebilmemiz için durum çubuğu
            print(f"\n🔄 Havuz Veri Sayısı -> Binance: {len(fiyat_havuzu['Binance'])} | Bybit: {len(fiyat_havuzu['Bybit'])} | OKX: {len(fiyat_havuzu['OKX'])}")
            
            for symbol in TARANACAK_COINLER:
                fiyatlar = {}
                piyasa_turu = "(Vadeli)" if ":" in symbol else "(Spot)"
                temiz_isim = symbol.split(":")[0]
                
                if symbol in fiyat_havuzu['Binance']: fiyatlar[f'Binance {piyasa_turu}'] = fiyat_havuzu['Binance'][symbol]
                if symbol in fiyat_havuzu['Bybit']: fiyatlar[f'Bybit {piyasa_turu}'] = fiyat_havuzu['Bybit'][symbol]
                if symbol in fiyat_havuzu['OKX']: fiyatlar[f'OKX {piyasa_turu}'] = fiyat_havuzu['OKX'][symbol]
                
                # En az 2 borsadan fiyat geldiyse kıyaslama yap (Biri çökmüş olsa bile bot çalışır!)
                if len(fiyatlar) < 2: 
                    continue
                
                en_ucuz_piyasa = min(fiyatlar, key=fiyatlar.get)
                en_pahali_piyasa = max(fiyatlar, key=fiyatlar.get)
                
                ucuz_fiyat = fiyatlar[en_ucuz_piyasa]
                pahali_fiyat = fiyatlar[en_pahali_piyasa]
                
                makas = ((pahali_fiyat - ucuz_fiyat) / ucuz_fiyat) * 100
                fırsat_listesi.append((temiz_isim, makas, en_ucuz_piyasa, ucuz_fiyat, en_pahali_piyasa, pahali_fiyat))
                
                if makas >= GIRIS_MAKAS_YUZDE:
                    mesaj = (f"🔥 <b>ARBİTRAJ SİNYALİ</b>\n\n"
                             f"📊 <b>Koin:</b> {temiz_isim} {piyasa_turu}\n"
                             f"⚡ <b>Makas:</b> +%{makas:.4f}\n"
                             f"📥 <b>Al:</b> {en_ucuz_piyasa} -> {ucuz_fiyat}\n"
                             f"📤 <b>Sat:</b> {en_pahali_piyasa} -> {pahali_fiyat}")
                    print(mesaj)
                    telegram_bildir(mesaj)
            
            if fırsat_listesi:
                fırsat_listesi.sort(key=lambda x: x[1], reverse=True)
                print("💵 --- ANLİK EN YÜKSEK 3 MAKAS ---")
                for i, item in enumerate(fırsat_listesi[:3]):
                    print(f" {i+1}. [{item[0]}] +%{item[1]:.4f} | Ucuz: {item[2]} ({item[3]}) -> Pahalı: {item[4]} ({item[5]})")
            else:
                print("⏳ Borsalardan ilk fiyatların senkronize olması bekleniyor...")
                    
        except Exception as e:
            print(f"❌ Motor hatası: {e}")
            traceback.print_exc()
            
        time.sleep(3) # Logların aşırı hızlı akıp Railway'i şişirmemesi için 3 saniye idealdir

if __name__ == "__main__":
    threading.Thread(target=fiyat_cek_binance, daemon=True).start()
    threading.Thread(target=fiyat_cek_bybit, daemon=True).start()
    threading.Thread(target=fiyat_cek_okx, daemon=True).start()
    
    # İlk koin fiyatlarının havuzlara yazılması için 6 saniye avans verelim
    time.sleep(6) 
    uc_borsa_arbitraj_motoru()
