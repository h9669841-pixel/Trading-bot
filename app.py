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
exchange_binance = ccxt.binance({'apiKey': BINANCE_API, 'secret': BINANCE_SECRET, 'enableRateLimit': True, **ccxt_proxy_config})
exchange_bybit = ccxt.bybit({'apiKey': BYBIT_API, 'secret': BYBIT_SECRET, 'enableRateLimit': True, **ccxt_proxy_config})
exchange_okx = ccxt.okx({'apiKey': OKX_API, 'secret': OKX_SECRET, 'password': OKX_PASSWORD, 'enableRateLimit': True, **ccxt_proxy_config})

# --- 📊 GENİŞLETİLMİŞ ARBİTRAJ AYARLARI ---
GIRIS_MAKAS_YUZDE = 0.55  # Vadeli piyasalarda komisyon ve fonlama riski için eşiği hafifçe yükselttik

# CCXT Kuralları: 
# '/' içeriyorsa SPOT piyasadır.
# '/' ve sonrasında ':USDT' içeriyorsa VADELİ (Futures/Perpetual) piyasadır.
TARANACAK_COINLER = [
    'BTC/USDT', 'BTC/USDT:USDT',
    'ETH/USDT', 'ETH/USDT:USDT',
    'SOL/USDT', 'SOL/USDT:USDT',
    'XRP/USDT', 'XRP/USDT:USDT',
    'ADA/USDT', 'ADA/USDT:USDT',
    'DOT/USDT', 'DOT/USDT:USDT',
    'AVAX/USDT', 'AVAX/USDT:USDT',
    'LINK/USDT', 'LINK/USDT:USDT',
    'DOGE/USDT', 'DOGE/USDT:USDT'
]

fiyat_havuzu = {
    'Binance': {},
    'Bybit': {},
    'OKX': {}
}

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"📢 [Telegram] -> {mesaj}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        print(f"❌ Telegram hatası: {e}")

# --- ⚡ ASENKRON FİYAT TOPLAMA MOTORLARI ---
def fiyat_cek_binance():
    while True:
        try:
            tickers = exchange_binance.fetch_tickers(TARANACAK_COINLER)
            for symbol in TARANACAK_COINLER:
                if symbol in tickers and tickers[symbol]['last']:
                    fiyat_havuzu['Binance'][symbol] = float(tickers[symbol]['last'])
        except Exception as e:
            pass
        time.sleep(0.5)

def fiyat_cek_bybit():
    while True:
        try:
            tickers = exchange_bybit.fetch_tickers(TARANACAK_COINLER)
            for symbol in TARANACAK_COINLER:
                if symbol in tickers and tickers[symbol]['last']:
                    fiyat_havuzu['Bybit'][symbol] = float(tickers[symbol]['last'])
        except Exception as e:
            pass
        time.sleep(0.5)

def fiyat_cek_okx():
    while True:
        try:
            tickers = exchange_okx.fetch_tickers(TARANACAK_COINLER)
            for symbol in TARANACAK_COINLER:
                if symbol in tickers and tickers[symbol]['last']:
                    fiyat_havuzu['OKX'][symbol] = float(tickers[symbol]['last'])
        except Exception as e:
            pass
        time.sleep(0.5)

# --- 🎯 AGRESİF ARBİTRAJ MATEMATİK MOTORU ---
def uc_borsa_arbitraj_motoru():
    print("🚀 3'lü Agresif Canavar (Spot + Vadeli) Pusuda Bekliyor...")
    telegram_bildir("🤖 <b>3'lü Agresif Arbitraj Canavarı (Spot + Vadeli) Başlatıldı!</b>")
    
    while True:
        try:
            fırsat_listesi = []
            
            for symbol in TARANACAK_COINLER:
                fiyatlar = {}
                piyasa_turu = "⚠️ (Vadeli)" if ":" in symbol else "🟢 (Spot)"
                temiz_isim = symbol.split(":")[0] # Görsel temizlik için
                
                # Fiyat kontrolü
                if symbol in fiyat_havuzu['Binance']: fiyatlar[f'Binance {piyasa_turu}'] = fiyat_havuzu['Binance'][symbol]
                if symbol in fiyat_havuzu['Bybit']: fiyatlar[f'Bybit {piyasa_turu}'] = fiyat_havuzu['Bybit'][symbol]
                if symbol in fiyat_havuzu['OKX']: fiyatlar[f'OKX {piyasa_turu}'] = fiyat_havuzu['OKX'][symbol]
                
                if len(fiyatlar) < 3: 
                    continue
                
                en_ucuz_piyasa = min(fiyatlar, key=fiyatlar.get)
                en_pahali_piyasa = max(fiyatlar, key=fiyatlar.get)
                
                ucuz_fiyat = fiyatlar[en_ucuz_piyasa]
                pahali_fiyat = fiyatlar[en_pahali_piyasa]
                
                makas = ((pahali_fiyat - ucuz_fiyat) / ucuz_fiyat) * 100
                fırsat_listesi.append((temiz_isim, makas, en_ucuz_piyasa, ucuz_fiyat, en_pahali_piyasa, pahali_fiyat))
                
                if makas >= GIRIS_MAKAS_YUZDE:
                    mesaj = (f"🔥 <b>AGRESİF ARBİTRAJ SİNYALİ</b>\n\n"
                             f"📊 <b>Koin:</b> {temiz_isim}\n"
                             f"⚡ <b>Makas:</b> +%{makas:.4f}\n"
                             f"📥 <b>Al (Ucuz):</b> {en_ucuz_piyasa} -> {ucuz_fiyat}\n"
                             f"📤 <b>Sat (Pahalı):</b> {en_pahali_piyasa} -> {pahali_fiyat}")
                    print(mesaj)
                    telegram_bildir(mesaj)
            
            if fırsat_listesi:
                fırsat_listesi.sort(key=lambda x: x[1], reverse=True)
                print("\n💵 --- 3 BORSA (SPOT+VADELİ) EN YÜKSEK 3 MAKAS ---")
                for i, item in enumerate(fırsat_listesi[:3]):
                    print(f"{i+1}. [{item[0]}] +%{item[1]:.4f} |\n    Ucuz: {item[2]} ({item[3]})\n    Pahalı: {item[4]} ({item[5]})")
                    
        except Exception as e:
            print(f"❌ Motor hatası: {e}")
            traceback.print_exc()
            
        time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=fiyat_cek_binance, daemon=True).start()
    threading.Thread(target=fiyat_cek_bybit, daemon=True).start()
    threading.Thread(target=fiyat_cek_okx, daemon=True).start()
    
    time.sleep(4) # Verilerin dolması için biraz daha süre tanıyalım
    uc_borsa_arbitraj_motoru()
