import os
import time
import requests
import numpy as np

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Strateji parametreleri
BB_LEN = 30
BB_MULT = 4.0
RSI_LEN = 14
RSI_OB = 61
RSI_OS = 27
VOL_LOOKBACK = 15
VOL_ADV = 20.0
SYMBOL = "BTCUSDT"
INTERVAL = "1h"

def telegram_bildir(mesaj):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mesaj,
        "parse_mode": "HTML"
    })

def get_candles():
    url = f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={INTERVAL}&limit=100"
    r = requests.get(url)
    data = r.json()
    closes = [float(d[4]) for d in data]
    opens  = [float(d[1]) for d in data]
    vols   = [float(d[5]) for d in data]
    return closes, opens, vols

def sma(data, period):
    return np.mean(data[-period:])

def stdev(data, period):
    return np.std(data[-period:])

def rsi(closes, period):
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def analiz():
    closes, opens, vols = get_candles()

    # Bollinger
    basis = sma(closes, BB_LEN)
    dev = stdev(closes, BB_LEN)
    upper_mult = BB_MULT * 0.85
    lower_mult = BB_MULT * 0.85
    bb_upper = basis + dev * upper_mult
    bb_lower = basis - dev * lower_mult

    # RSI
    rsi_val = rsi(closes, RSI_LEN)

    # Son fiyat
    close = closes[-1]
    open_ = opens[-1]
    prev_close = closes[-2]

    # Sinyaller
    buy_signal = (prev_close <= bb_lower) and (close > bb_lower) and (rsi_val < RSI_OS)
    sell_signal = (prev_close >= bb_upper) and (close < bb_upper) and (rsi_val > RSI_OB)

    # Volume dominance
    buy_vols  = [vols[i] if closes[i] > opens[i] else 0 for i in range(-VOL_LOOKBACK, 0)]
    sell_vols = [vols[i] if closes[i] < opens[i] else 0 for i in range(-VOL_LOOKBACK, 0)]
    avg_buy  = np.mean(buy_vols)
    avg_sell = np.mean(sell_vols)
    multiplier = 1.0 + VOL_ADV / 100.0

    bull_dom = buy_signal and avg_buy > avg_sell * multiplier
    bear_dom = sell_signal and avg_sell > avg_buy * multiplier

    print(f"Fiyat: {close:.2f} | RSI: {rsi_val:.1f} | BB_U: {bb_upper:.2f} | BB_L: {bb_lower:.2f}")

    if buy_signal:
        if bull_dom:
            mesaj = f"""🟢⭐ <b>GÜÇLÜ BUY SİNYALİ</b>
📊 {SYMBOL}
💰 Fiyat: {close:.2f}
📈 RSI: {rsi_val:.1f}
🔥 Volume Dominance: BULL ★"""
        else:
            mesaj = f"""🟢 <b>BUY SİNYALİ</b>
📊 {SYMBOL}
💰 Fiyat: {close:.2f}
📈 RSI: {rsi_val:.1f}"""
        telegram_bildir(mesaj)

    elif sell_signal:
        if bear_dom:
            mesaj = f"""🔴⭐ <b>GÜÇLÜ SELL SİNYALİ</b>
📊 {SYMBOL}
💰 Fiyat: {close:.2f}
📈 RSI: {rsi_val:.1f}
🔥 Volume Dominance: BEAR ★"""
        else:
            mesaj = f"""🔴 <b>SELL SİNYALİ</b>
📊 {SYMBOL}
💰 Fiyat: {close:.2f}
📈 RSI: {rsi_val:.1f}"""
        telegram_bildir(mesaj)

if __name__ == "__main__":
    print("Bot başladı...")
    telegram_bildir("🤖 <b>Bot başladı!</b> Sinyaller izleniyor...")
    while True:
        try:
            analiz()
        except Exception as e:
            print(f"Hata: {e}")
        time.sleep(3600)  # 1 saatte bir kontrol
