import os
import sys  # Sistemi kapatmak için eklendi

print("🔴 BOT KULLANICI TARAFINDAN DURDURULDU!")

# Railway üzerinde botun sürekli döngüye girip tekrar başlamasını engellemek,
# ve servisi tamamen çökertip durdurmak için sys.exit() komutunu tetikliyoruz.
sys.exit("Bot kapatıldı.")

# NOT: Botu tekrar çalıştırmak istediğinde bu dosyanın eski (çalışan) sürümlerinden birini
# tekrar GitHub'a yüklemen (Push etmen) yeterlidir.

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
BYBIT_API_KEY = os.environ.get("BINANCE_API_KEY")  
BYBIT_SECRET = os.environ.get("BINANCE_SECRET")

BB_LEN = 14            
BB_MULT = 1.3          
RSI_LEN = 7            
RSI_OB = 50            
RSI_OS = 50            
INTERVAL = 1           

SYMBOL = "XBTUSD"        
BYBIT_SYMBOL = "BTCUSDT"  
QUANTITY = "0.01"
TESTNET_URL = "https://api-testnet.bybit.com"

TP_YUZDE = 1.0         
SL_YUZDE = 2.0         
BREAKEVEN_YUZDE = 0.3  

pozisyon = {
    "var": False,
    "yon": None,
    "giris": None,
    "tp": None,
    "sl": None,
    "breakeven": False
}
