import os
import json
import time
import requests
import threading
import math
from datetime import datetime
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

# --- 🔑 GÜVENLİK VE API AYARLARI ---
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "").strip()
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
PROXY_URL = os.environ.get("PROXY_URL")

proxy_formatted = None
if PROXY_URL:
    proxy_formatted = PROXY_URL
    if PROXY_URL.startswith("socks5://"):
        proxy_formatted = PROXY_URL.replace("socks5://", "socks5h://")

# ⚡ BAĞLANTI HIZLANDIRICI SESSION KURUMLARI
session_tarama = requests.Session()
session_emir = requests.Session()

if proxy_formatted:
    print(f"🌐 Emir istemcisi için statik IP tüneli hazırlandı.")
    session_emir.proxies = {"http": proxy_formatted, "https": proxy_formatted}

# --- 🚀 GÜVENLİ BAĞLANTI ENJEKSİYONU ---
client = Client(
    api_key=BINANCE_API_KEY, 
    api_secret=BINANCE_SECRET_KEY,
    requests_params={"session": session_tarama}
)

order_client = Client(
    api_key=BINANCE_API_KEY, 
    api_secret=BINANCE_SECRET_KEY,
    requests_params={"session": session_emir}
)

class TrendBotConfig:
    def __init__(self):
        self.TIMEFRAME = Client.KLINE_INTERVAL_15MINUTE  
        self.ISLEM_MARJIN = 5.0        
        self.KALDIRAC = 10             
        self.MAX_ACIK_POZISYON = 10     
        self.BOT_CALISIYOR = True
        self.COOLDOWN_SURESI = 0     
        self.SABIT_DOLAR_TP = 0.15     
        
        # === 🛡️ ÇİFT KADEMELİ GÜVENLİK AYARLARI ===
        self.DCA1_TETIK_YUZDE = 3.0    
        self.DCA1_MARJIN = 5.0         
        
        self.DCA2_TETIK_YUZDE = 3.5    
        self.DCA2_EK_MARJIN = 2.0      
        
        # === 📊 RSI PARAMETRELERİ (GÜNCELLENDİ) ===
        self.RSI_LEN = 14
        self.RSI_OB = 75               # Aşırı Alım Sınırı (Yukarıdan aşağı dönüş aranır)
        self.RSI_OS = 25               # Aşırı Satım Sınırı (Aşağıdan yukarı dönüş aranır)
        
        self.API_DELAY = 0.3
        self.HIZLI_TAKIP_PERIYODU = 0.2 # 200ms ultra hızlı kontrol

config = TrendBotConfig()

SYMBOLS = [] 
piyasa_verisi = {}
aktif_pozisyonlar = {}
FUTURES_HASSASIYETLERI = {}
son_islem_zamanlari = {}        
emir_beklemede_durumu = {} 

data_lock = threading.Lock()

# --- 🛠️ MATEMATİKSEL İNDİKATÖR MOTORU ---
def rsi_hesapla(kapanislar, periyod=14):
    if len(kapanislar) < periyod + 1: return 50.0
    kazanclar, kayiplar = [], []
    for i in range(1, len(kapanislar)):
        fark = kapanislar[i] - kapanislar[i-1]
        if fark > 0: kazanclar.append(fark); kayiplar.append(0)
        else: kazanclar.append(0); kayiplar.append(abs(fark))
    ort_kazanc = sum(kazanclar[:periyod]) / periyod
    ort_kayip = sum(kayiplar[:periyod]) / periyod
    for i in range(periyod, len(kazanclar)):
        ort_kazanc = (ort_kazanc * (periyod - 1) + kazanclar[i]) / periyod
        ort_kayip = (ort_kayip * (periyod - 1) + kayiplar[i]) / periyod
    if ort_kayip <= 0.00000001: return 100.0  
    return 100.0 - (100.0 / (1.0 + (ort_kazanc / ort_kayip)))

# 🔄 YENİ SİNYAL MOTORU: RSI DÖNÜŞÜ (CROSSOVER / CROSSUNDER)
def strateji_sinyal_uret(v, anlik_fiyat):
    kapanislar = list(v["kapanislar"])
    if not kapanislar or anlik_fiyat <= 0: return "HOLD", 50.0

    kapanislar.append(anlik_fiyat)
    if len(kapanislar) < config.RSI_LEN + 5: return "HOLD", 50.0

    # Güncel ve bir önceki mumun RSI değerleri hesaplanıyor
    rsi_guncel = rsi_hesapla(kapanislar, config.RSI_LEN)
    rsi_onceki = rsi_hesapla(kapanislar[:-1], config.RSI_LEN)

    # RSI Dönüş Koşulları
    long_ok = (rsi_onceki < config.RSI_OS) and (rsi_guncel >= config.RSI_OS)   # 25'in altından yukarı kesti
    short_ok = (rsi_onceki > config.RSI_OB) and (rsi_guncel <= config.RSI_OB) # 75'in üstünden aşağı kesti

    if long_ok: return "BUY", rsi_guncel
    elif short_ok: return "SELL", rsi_guncel
    return "HOLD", rsi_guncel

# --- 🌐 REST API ALTYAPI FONKSİYONLARI ---
def ilk_100_hacimli_coin_bul():
    try:
        ticker_url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        response = session_tarama.get(ticker_url, timeout=10)
        if response.status_code != 200: return []
        data = response.json()
        usdt_pairs = [x for x in data if isinstance(x, dict) and x.get("symbol", "").endswith("USDT")]
        sorted_by_volume = sorted(usdt_pairs, key=lambda k: float(k.get("quoteVolume", 0)), reverse=True)
        return [x["symbol"].lower() for x in sorted_by_volume[:100]]
    except Exception as e:
        print(f"❌ Hacim listesi alınamadı: {e}")
        return []

def kontrollu_coin_ekle(coin_adi, eski_pozisyon_mu=False):
    coin_lower = coin_adi.lower().strip()
    coin_upper = coin_lower.upper()
    if coin_lower in SYMBOLS: return True
    try:
        f_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        response = session_tarama.get(f_url, timeout=10)
        if response.status_code != 200: return False
        r = response.json()

        market_info = next((m for m in r.get("symbols", []) if m["symbol"] == coin_upper), None)
        if not market_info or market_info.get('status') != 'TRADING': return False
        
        time.sleep(0.1) 
        
        if not eski_pozisyon_mu:
            try:
                order_client.futures_change_leverage(symbol=coin_upper, leverage=config.KALDIRAC)
                order_client.futures_change_margin_type(symbol=coin_upper, marginType="ISOLATED")
            except BinanceAPIException as e:
                if "No need to change" not in e.message: pass

        for f in market_info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step_size_str = str(f['stepSize']).rstrip('0')
                precision = 0 if '.' not in step_size_str else len(step_size_str.split('.')[1])
                FUTURES_HASSASIYETLERI[coin_lower] = precision

        with data_lock:
            if coin_lower not in SYMBOLS:
                SYMBOLS.append(coin_lower)
                piyasa_verisi[coin_lower] = {"anlik_fiyat": 0.0, "kapanislar": []}
                aktif_pozisyonlar[coin_lower] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "dca_kademe": 0}
                son_islem_zamanlari[coin_lower] = 0.0  
                emir_beklemede_durumu[coin_lower] = False
        return True
    except Exception: return False

def tek_coin_api_verisi_guncelle(s):
    try:
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={s.upper()}&interval={config.TIMEFRAME}&limit=60"
        response = session_tarama.get(url, timeout=4)
        if response.status_code != 200: return False
        k = response.json()
        if not k or len(k) == 0: return False
        
        kapanislar_yeni = [float(x[4]) for x in k]
        anlik_fiyat_yeni = kapanislar_yeni[-1]  
        
        with data_lock:
            piyasa_verisi[s]["kapanislar"] = kapanislar_yeni[:-1] 
            piyasa_verisi[s]["anlik_fiyat"] = anlik_fiyat_yeni
        return True
    except Exception: return False

def acik_pozisyonlari_binanceden_guncelle():
    try:
        pozisyonlar = order_client.futures_position_information()
        with data_lock:
            for s in SYMBOLS:
                if not emir_beklemede_durumu.get(s, False):
                    eski_kademe = aktif_pozisyonlar[s].get("dca_kademe", 0)
                    aktif_pozisyonlar[s] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "dca_kademe": eski_kademe}
            
            for p in pozisyonlar:
                sym = p.get("symbol", "").lower().strip()
                if sym in aktif_pozisyonlar:
                    if emir_beklemede_durumu.get(sym, False): continue
                    amt = float(p.get("positionAmt", 0))
                    entry_price = float(p.get("entryPrice", 0))
                    if amt != 0:
                        aktif_pozisyonlar[sym]["aktif"] = True
                        aktif_pozisyonlar[sym]["yon"] = "LONG" if amt > 0 else "SHORT"
                        aktif_pozisyonlar[sym]["adet"] = abs(amt)
                        aktif_pozisyonlar[sym]["giris_fiyati"] = entry_price
                    else:
                        aktif_pozisyonlar[sym]["dca_kademe"] = 0
    except Exception as e:
        print(f"❌ Pozisyon senkronizasyon hatası: {e}")

# --- 🎛️ TELEGRAM YÖNETİMİ ---
def telegram_bildir(mesaj, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}
        if reply_markup: data["reply_markup"] = reply_markup
        session_tarama.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=data, timeout=4)
    except Exception: pass

def ana_menu_olustur():
    return {
        "keyboard": [[{"text": "📊 Bot Durumu"}], [{"text": "▶️ Botu Başlat"}, {"text": "⏸️ Botu Durdur"}]],
        "resize_keyboard": True, "one_time_keyboard": False
    }

def telegram_canli_rapor_uret():
    acik_pozisyonlari_binanceden_guncelle()
    with data_lock:
        acik_pozlar = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])
        durum_str = "🟢 API Oturumu Aktif" if config.BOT_CALISIYOR else "🔴 Sistem Durduruldu"

        rapor = (
            f"⚙️ <b>RSI Dönüş (Rejection) Botu</b>\n"
            f"• Sistem: {durum_str}\n"
            f"• Risk Limiti: {acik_pozlar}/{config.MAX_ACIK_POZISYON} Pozisyon\n"
            f"• RSI Sınırları: 25 - 75 📊\n"
            f"• TP Hedefi: Net +{config.SABIT_DOLAR_TP} USD\n"
            f"• Kontrol Sıklığı: 200ms ⚡\n\n"
            f"⚡ <b>Açık İşlemler:</b>\n"
        )

        if acik_pozlar == 0:
            rapor += "Açık izole pozisyon bulunmuyor."
        else:
            for s in SYMBOLS:
                if aktif_pozisyonlar[s]["aktif"]:
                    p = aktif_pozisyonlar[s]
                    rapor += f"• {s.upper()} | {p['yon']} | Giriş: {p['giris_fiyati']} | Kademe: {p.get('dca_kademe', 0)}/2\n"
    return rapor

def telegram_gelen_mesaj_dinleyici():
    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"timeout": 10, "offset": offset}
            response = session_tarama.get(url, params=params, timeout=15).json()
            if response.get("ok") and response.get("result"):
                for update in response["result"]:
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if not message or str(message.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID): continue
                    text = message.get("text", "")
                    
                    if text == "/start":
                        telegram_bildir("🤖 <b>Bot Kontrol Paneli Aktif!</b>", reply_markup=ana_menu_olustur())
                    elif text == "📊 Bot Durumu":
                        telegram_bildir(telegram_canli_rapor_uret(), reply_markup=ana_menu_olustur())
                    elif text == "▶️ Botu Başlat":
                        config.BOT_CALISIYOR = True
                        telegram_bildir("🚀 Bot tarama döngüsü <b>aktif.</b>", reply_markup=ana_menu_olustur())
                    elif text == "⏸️ Botu Durdur":
                        config.BOT_CALISIYOR = False
                        telegram_bildir("⏸️ Bot tarama döngüsü <b>durduruldu.</b>", reply_markup=ana_menu_olustur())
        except Exception: time.sleep(5)

# --- 🚀 ULTRA HIZLI TAKIP DONGUSU ---
def hizli_acik_pozisyon_takip_dongusu():
    while True:
        try:
            if not config.BOT_CALISIYOR:
                time.sleep(0.5)
                continue

            try:
                pozisyonlar = order_client.futures_position_information()
            except Exception as ae:
                print(f"❌ Binance hızlı pozisyon bilgisi çekilemedi: {ae}")
                time.sleep(1.0)
                continue

            su_an_ts = time.time()

            for p in pozisyonlar:
                amt = float(p.get("positionAmt", 0))
                symbol = p.get("symbol", "").lower().strip()
                
                if amt == 0 or symbol not in aktif_pozisyonlar:
                    continue

                borsa_net_pnl = float(p.get("unrealizedProfit", 0.0))
                
                with data_lock:
                    pos = dict(aktif_pozisyonlar[symbol])
                    emir_beklemede = emir_beklemede_durumu.get(symbol, False)
                    maliyet = float(p.get("entryPrice", 0.0))
                    mark_fiyati = float(p.get("markPrice", 0.0))

                if emir_beklemede or maliyet <= 0:
                    continue

                if amt > 0: 
                    fiyat_sapma_yuzde = ((maliyet - mark_fiyati) / maliyet) * 100
                else: 
                    fiyat_sapma_yuzde = ((mark_fiyati - maliyet) / maliyet) * 100

                # 💰 TAKE PROFIT TRIGGER
                if borsa_net_pnl >= config.SABIT_DOLAR_TP:
                    with data_lock:
                        if emir_beklemede_durumu[symbol]: continue
                        emir_beklemede_durumu[symbol] = True

                    try:
                        precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                        faktor = 10 ** precision
                        adet_mutlak = abs(amt)
                        qty_to_close = math.floor(adet_mutlak * faktor) / faktor if precision > 0 else int(adet_mutlak)
                        side_to_close = SIDE_SELL if amt > 0 else SIDE_BUY
                        
                        if qty_to_close > 0:
                            with data_lock:
                                son_islem_zamanlari[symbol] = su_an_ts  
                                aktif_pozisyonlar[symbol] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "dca_kademe": 0}
                            
                            order_client.futures_create_order(
                                symbol=symbol.upper(), side=side_to_close, type=ORDER_TYPE_MARKET, 
                                quantity=qty_to_close, reduceOnly=True
                            )
                            threading.Thread(target=telegram_bildir, args=(f"💰 <b>{symbol.upper()}</b> Net Kâr: <b>{round(borsa_net_pnl, 3)}$</b> ile Kapatıldı!",)).start()
                    except Exception as e:
                        print(f"❌ Kapatma emri borsada başarısız oldu ({symbol}): {e}")
                    finally:
                        with data_lock: emir_beklemede_durumu[symbol] = False

                # 🛡️ DCA MOTORU
                else:
                    if fiyat_sapma_yuzde >= config.DCA1_TETIK_YUZDE and pos.get("dca_kademe", 0) == 0:
                        with data_lock:
                            if emir_beklemede_durumu[symbol]: continue
                            emir_beklemede_durumu[symbol] = True

                        try:
                            precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                            dca_qty = (config.DCA1_MARJIN * config.KALDIRAC) / mark_fiyati
                            dca_qty = float(int(dca_qty * (10 ** precision))) / (10 ** precision) if precision > 0 else int(dca_qty)

                            if dca_qty > 0:
                                dca_side = SIDE_BUY if amt > 0 else SIDE_SELL
                                with data_lock:
                                    aktif_pozisyonlar[symbol]["dca_kademe"] = 1
                                
                                order_client.futures_create_order(
                                    symbol=symbol.upper(), side=dca_side, type=ORDER_TYPE_MARKET, quantity=dca_qty
                                )
                                threading.Thread(target=telegram_bildir, args=(f"⚠️ <b>{symbol.upper()}</b> DCA 1 Alındı.",)).start()
                        except Exception as e:
                            with data_lock: aktif_pozisyonlar[symbol]["dca_kademe"] = 0
                            print(f"❌ DCA 1 Hatası: {e}")
                        finally:
                            with data_lock: emir_beklemede_durumu[symbol] = False

                    elif fiyat_sapma_yuzde >= config.DCA2_TETIK_YUZDE and pos.get("dca_kademe", 0) == 1:
                        with data_lock:
                            if emir_beklemede_durumu[symbol]: continue
                            emir_beklemede_durumu[symbol] = True

                        try:
                            with data_lock:
                                aktif_pozisyonlar[symbol]["dca_kademe"] = 2
                                
                            order_client.futures_change_position_margin(
                                symbol=symbol.upper(), amount=config.DCA2_EK_MARJIN, type=1  
                            )
                            threading.Thread(target=telegram_bildir, args=(f"🛡️ <b>{symbol.upper()}</b> Marjine {config.DCA2_EK_MARJIN} USDT eklendi.",)).start()
                        except Exception as e:
                            with data_lock: aktif_pozisyonlar[symbol]["dca_kademe"] = 1
                            print(f"❌ Marjin Ekleme Hatası: {e}")
                        finally:
                            with data_lock: emir_beklemede_durumu[symbol] = False

            time.sleep(config.HIZLI_TAKIP_PERIYODU)
        except Exception as e:
            print(f"❌ Hızlı takip hatası: {e}")
            time.sleep(1.0)

# --- 🎯 YAVAŞ TARAMA MOTORU ---
def pure_api_tarama_dongusu():
    while True:
        try:
            if not config.BOT_CALISIYOR:
                time.sleep(1.0)
                continue
                
            su_an_ts = time.time()
            with data_lock:
                kapali_olanlar = [s for s in SYMBOLS if not aktif_pozisyonlar[s]["aktif"]]

            for symbol in kapali_olanlar:
                if not config.BOT_CALISIYOR: break
                with data_lock:
                    if aktif_pozisyonlar[symbol]["aktif"]: continue

                if not tek_coin_api_verisi_guncelle(symbol):
                    time.sleep(config.API_DELAY)
                    continue

                with data_lock:
                    v = dict(piyasa_verisi[symbol])
                    pos = dict(aktif_pozisyonlar[symbol])
                    son_islem = son_islem_zamanlari[symbol]

                if len(v["kapanislar"]) < 20 or not v["anlik_fiyat"] or v["anlik_fiyat"] <= 0:
                    time.sleep(config.API_DELAY)
                    continue
                
                anlik_fiyat = v["anlik_fiyat"]

                if not pos["aktif"]:
                    if su_an_ts - son_islem < config.COOLDOWN_SURESI: 
                        time.sleep(config.API_DELAY)
                        continue
                    
                    with data_lock:
                        guncel_acik_pozisyon_sayisi = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])
                    if guncel_acik_pozisyon_sayisi >= config.MAX_ACIK_POZISYON: 
                        time.sleep(config.API_DELAY)
                        continue 

                    sinyal, guncel_rsi = strateji_sinyal_uret(v, anlik_fiyat)

                    if sinyal != "HOLD":
                        with data_lock:
                            guncel_acik_pozisyon_sayisi = sum(1 for s in SYMBOLS if aktif_pozisyonlar[s]["aktif"])
                            if guncel_acik_pozisyon_sayisi >= config.MAX_ACIK_POZISYON or emir_beklemede_durumu[symbol] or aktif_pozisyonlar[symbol]["aktif"]: 
                                time.sleep(config.API_DELAY)
                                continue
                            emir_beklemede_durumu[symbol] = True

                        try:
                            precision = FUTURES_HASSASIYETLERI.get(symbol, 2)
                            qty = (config.ISLEM_MARJIN * config.KALDIRAC) / anlik_fiyat
                            qty = float(int(qty * (10 ** precision))) / (10 ** precision) if precision > 0 else int(qty)
                            
                            if qty <= 0:
                                with data_lock: emir_beklemede_durumu[symbol] = False
                                continue

                            if qty > 0:
                                if sinyal == "BUY":
                                    with data_lock:
                                        aktif_pozisyonlar[symbol] = {"aktif": True, "yon": "LONG", "adet": qty, "giris_fiyati": anlik_fiyat, "dca_kademe": 0}
                                    order_client.futures_create_order(symbol=symbol.upper(), side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
                                    threading.Thread(target=telegram_bildir, args=(f"🚀 <b>{symbol.upper()} LONG Açıldı!</b>\nRSI Dönüşü: {round(guncel_rsi, 2)}",)).start()
                                        
                                elif sinyal == "SELL":
                                    with data_lock:
                                        aktif_pozisyonlar[symbol] = {"aktif": True, "yon": "SHORT", "adet": qty, "giris_fiyati": anlik_fiyat, "dca_kademe": 0}
                                    order_client.futures_create_order(symbol=symbol.upper(), side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=qty)
                                    threading.Thread(target=telegram_bildir, args=(f"🚀 <b>{symbol.upper()} SHORT Açıldı!</b>\nRSI Dönüşü: {round(guncel_rsi, 2)}",)).start()
                        except Exception as e:
                            with data_lock: 
                                aktif_pozisyonlar[symbol] = {"aktif": False, "yon": None, "adet": 0.0, "giris_fiyati": 0.0, "dca_kademe": 0}
                            print(f"❌ Emir hatası ({symbol}): {e}")
                        finally:
                            with data_lock: emir_beklemede_durumu[symbol] = False
                
                time.sleep(config.API_DELAY)
        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            time.sleep(2.0)

# --- 🚀 ANA ÇALIŞTIRICI ---
if __name__ == "__main__":
    print("🎬 Temiz RSI Dönüş Botu Başlatılıyor...")
    
    try:
        mevcut_pozisyonlar = order_client.futures_position_information()
        for p in mevcut_pozisyonlar:
            amt = float(p.get("positionAmt", 0))
            sym = p.get("symbol", "").lower().strip()
            if amt != 0:
                kontrollu_coin_ekle(sym, eski_pozisyon_mu=True)
    except Exception as e:
        print(f"❌ İlk pozisyon tarama hatası: {e}")
    
    hacimli_coinler = ilk_100_hacimli_coin_bul()
    for c in hacimli_coinler:
        kontrollu_coin_ekle(c, eski_pozisyon_mu=False)
            
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=telegram_gelen_mesaj_dinleyici, daemon=True).start()
        telegram_bildir("🤖 <b>Bot Saf RSI (25-75) Dönüş Modunda Başlatıldı!</b>")
    
    threading.Thread(target=hizli_acik_pozisyon_takip_dongusu, daemon=True).start()
    pure_api_tarama_dongusu()
