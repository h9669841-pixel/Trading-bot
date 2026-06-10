ebsocket import WebSocketApp

# --- 🌐 GLOBAL SOCKS5 ENJEKSİYONU (ÇEKİRDEK SEVİYESİNDE) ---
PROXY_URL = os.environ.get("PROXY_URL") 

if PROXY_URL:
    try:
        import socks
        from urllib.parse import urlparse, unquote
        
        parsed_proxy = urlparse(PROXY_URL)
        proxy_host = parsed_proxy.hostname
        proxy_port = parsed_proxy.port
        proxy_user = unquote(parsed_proxy.username) if parsed_proxy.username else None
        proxy_pass = unquote(parsed_proxy.password) if parsed_proxy.password else None

        print(f"🌐 SOCKS5 Protokolü Çekirdeğe Enjekte Ediliyor: {proxy_host}:{proxy_port}")
        
        # Python'ın tüm soket trafiğini (REST ve WS) global olarak SOCKS5 proxy'sine gömüyoruz.
        socks.set_default_proxy(
            socks.SOCKS5, 
            addr=proxy_host, 
            port=proxy_port, 
            username=proxy_user, 
            password=proxy_pass,
            rdns=True # DNS sorgularını da proxy üzerinden güvenli çözümler
        )
        socket.socket = socks.socksocket # Tüm sistemi SOCKS5 soketine yamala
        
    except ImportError:
        print("❌ HATA: SOCKS5 aktif edilemedi. Lütfen terminalde 'pip install PySocks' çalıştırın.")

# --- 🔑  BINANCE_SECRET_KEY)

# --- 📊 ARBİTRAJ STRATEJİ VE HESAP AYARLARI ---
GIRIS_MAKAS_YUZDE = 41  # 🎯 41 olan değer %0.41 olarak düzeltildi
CIKIS_MAKAS_YUZDE = 0.02  

SPOT_BAKIYE = 15.0       # 🎯 Binance minimum emir limitine (MIN_NOTIONAL) takılmamak için 15 USDT yapıldı
FUTURES_BAKIYE = 15.0    

SPOT_FEE_RATE = 0.0750 / 100     
FUTURES_FEE_RATE = 0.0450 / 100  
# ----------------------------------------------

SYMBOLS = []
piyasa_verisi = {}
arbitraj_pozisyonlari = {}

# ⚡ OPTİMİZASYON: LOT_SIZE basamak hassasiyetlerini RAM'de BinanceAPIException için global sözlük
SEMBOL_HASSASIYETLERI = {}

def get_all_futures_symbols():
    try:
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            symbols = []
            for market in data.get("symbols", []):
                if market.get("quotet") == "USDT" and market.get("status") == "TRADING":
                    symbols.appedef et_all_leverages():
    print("⏳ Tüm sembollerin kaldıracı 1x olarak ayarlanıyor...")
    for symbol in SYMBOLS[:150]:
        try:
            client.futures_change_leverage(symbol=symbol.upper(), leverage=1)
            time.sleep(0.1) # Rate limit koruması
        except BinanceAPIException as b_err:
            print(f"⚠️ {symbol.upper()} kaldıraç değiştirilemedi: {b_err.message}")
        except Exception as e:
            print(f"❌ Kaldıraç ayar hatası ({symbol.upper()}): {e}")

# ⚡ OPTİMİZASYON: Bot başlarken tüm borsa hassasiyet limitlerini tek seferde RAM'e yükler
def tum_hassasiyetleri_yukle():
    print("⏳ Tüm sembollerin lot hassasiyetleri borsadan çekiliyor ve önbelleğe alınıyor...")
    try:
        exchange_info = client.get_exchange_info()
        for market in exchange_info['symbols']:
            sym = market['symbol'].lower()
            if sym in SYMBOLS:
                for f in market['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        precision = 0 if step_size >= 1.0 else len(str(step_size).split('.')[1].rstrip('0'))
                        SEMBOL_HASSASIYETLERI[sym] = precision
        print("✅ Tüm sembol hassasiyetleri RAM belleğe başarıyla kaydedildi.")
    except Exception as e:
        print(f"❌ Hassasiyet haritası çıkarılırken hata oluştu (Varsayılanlar kullanılacak): {e}")

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"📢 [Telegram Değişkenleri Eksik] -> {mesaj}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        print(f"❌ Telegram hatası: {e}")

def net_kar_hesapla(giris_makas, kapanis_makas):
    brut_oran_farki = giris_makas - kapanis_makas
    brut_kazanc_usdt = SPOT_BAKIYE * (brut_oran_farki / 100)
    spot_toplam_komisyon = (SPOT_BAKIYE * SPOT_FEE_RATE) * 2
    futures_toplam_komisyon = (FUTURES_BAKIYE * FUTURES_FEE_RATE) * 2
    toplam_kesinti_usdt = spot_toplam_komisyon + futures_toplam_komisyon
    net_kazanc_usdt = brut_kazanc_usdt - toplam_kesinti_usdt
    return brut_kazanc_usdt, toplam_kesinti_usdt, net_kazanc_usdt

# ⚡ OPTİMİZASYON: Alt fonksiyonlar paralel thread'ler tarafından çağrılarak emirleri asenkron iletir
def _hizli_emir_gonder_spot(coin_label, quantity, sonuclar):
    try:
        sonuclar['spot'] = client.create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=quantity)
    except Exception as e:
        sonuclar['spot_hata'] = e

def _hizli_emir_gonder_futures(coin_label, quantity, sonuclar):
    try:
        sonuclar['futures'] = client.futures_change_leverage(symbol=coin_label, leverage=1) # Güvenlik kilidi
        sonuclar['futures'] = client.futures_create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=quantity)
    except Exception as e:
        sonuclar['futures_hata'] = e

def _hizli_cikis_gonder_spot(coin_label, quantity, sonuclar):
    try:
        sonuclar['spot'] = client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=quantity)
    except Exception as e:
        sonuclar['spot_hata'] = e

def _hizli_cikis_gonder_futures(coin_label, quantity, sonuclar):
    try:
        sonuclar['futures'] = client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=quantity)
    except Exception as e:
        sonuclar['futures_hata'] = e

# --- 🎯 DİNAMİK LOT SIZE HESAPLAMA FONKSİYONU ---
def get_lot_size_precision(symbol):
    """Mevcut uyumluluğu korumak amacıyla RAM'deki sözlükten okuma yapar (0 ms gecikme)"""
    return SEMBOL_HASSASIYETLERI.get(symbol.lower(), 2)

# --- 🎯 AKTİF EMİR YÖNETİM MOTORU ---
def execute_arbitrage_entry(symbol, spot_price, futures_price):
    coin_label = symbol.upper()
    try:
        # 🎯 OPTİMİZASYON: Borsa API'sine istek atmak yerine direkt RAM sözlüğünden veri çekiliyor (~200ms kazanç)
        precision = get_lot_size_precision(symbol)
        
        raw_spot_qty = SPOT_BAKIYE / spot_price
        raw_futures_qty = FUTURES_BAKIYE / futures_price
        
        # 🎯 Matematiksel Kırpma (Truncate) -> round() yerine güvenli aşağı yuvarlama
        factor = 10 ** precision
        spot_quantity = int(raw_spot_qty * factor) / factor if precision > 0 else int(raw_spot_qty)
        futures_quantity = int(raw_futures_qty * factor) / factor if precision > 0 else int(raw_futures_qty)

        print(f"⚙️ {coin_label} Hassasiyet: {precision} | Emir Adetleri -> Spot: {spot_quantity}, Futures: {futures_quantity}")

        # 🎯 OPTİMİZASYON: Sıralı istek göndermek yerine Spot ve Vadeli emirleri aynı anda (Paralel) fırlatılıyor
        emir_sonuclari = {}
        t1 = threading.Thread(target=_hizli_emir_gonder_spot, args=(coin_label, spot_quantity, emir_sonuclari))
        t2 = threading.Thread(target=_hizli_emir_gonder_futures, args=(coin_label, futures_quantity, emir_sonuclari))
        
        t1.start()
        t2.start()
        
        t1.join()
        t2.join()

        # Hata kontrolleri
        if 'spot_hata' in emir_sonuclari:
            raise emir_sonuclari['spot_hata']
        if 'futures_hata' in emir_sonuclari:
            raise emir_sonuclari['futures_hata']

        return True, spot_quantity, futures_quantity
    except BinanceAPIException as e:
        err_msg = f"❌ <b>{coin_label} BORSASAL GİRİŞ HATASI:</b>\nKod: {e.code}\nMesaj: {e.message}"
        print(err_msg)
        traceback.print_exc()
        telegram_bildir(err_msg)
        return False, 0, 0
    except Exception as e:
        err_msg = f"❌ {coin_label} SİSTEMSEL GİRİŞ HATASI: {e}"
        print(err_msg)
        traceback.print_exc()
        telegram_bildir(err_msg)
        return False, 0, 0

def execute_arbitrage_exit(symbol, spot_qty, futures_qty):
    coin_label = symbol.upper()
    try:
        # 🎯 OPTİMİZASYON: Pozisyondan çıkarken de bacakların açık kalmaması için paralel emir yapısı uygulandı
        emir_sonuclari = {}
        t1 = threading.Thread(target=_hizli_cikis_gonder_spot, args=(coin_label, spot_qty, emir_sonuclari))
        t2 = threading.Thread(target=_hizli_cikis_gonder_futures, args=(coin_label, futures_qty, emir_sonuclari))
        
        t1.start()
        t2.start()
        
        t1.join()
        t2.join()

        if 'spot_hata' in emir_sonuclari:
            raise emir_sonuclari['spot_hata']
        if 'futures_hata' in emir_sonuclari:
            raise emir_sonuclari['futures_hata']

        return True
    except BinanceAPIException as e:
        err_msg = f"❌ <b>{coin_label} BORSASAL ÇIKIŞ HATASI (BACAK AÇIK KALDI!):</b>\nKod: {e.code}\nMesaj: {e.message}"
        print(err_msg)
        traceback.print_exc()
        telegram_bildir(err_msg)
        return False
    except Exception as e:
        err_msg = f"❌ {coin_label} SİSTEMSEL ÇIKIŞ HATASI: {e}"
        print(err_msg)
        traceback.print_exc()
        telegram_bildir(err_msg)
        return False

# --- 🌐 GLOBAL 3/stream?streams={streams}"
    
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever()

def start_multi_futures_ws():
    def on_message(ws, message):
        data = json.loads(message)
        symbol = data.get("stream", "").split("@")[0]
        if symbol in piyasa_verisi:
            piyasa_verisi[symbol]["futures_price"] = float(data.get("data", {}).get("p", 0))

    def on_error(ws, error): 
        print(f"❌ Global Futures WS Hatası: {error}")
        traceback.print_exc()
        
    def on_close(ws, c_code, c_msg): 
        print(f"🔄 Futures WS kapandı. 5 saniye sonra yeniden bağlanıyor...")
        time.sleep(5)
        start_multi_futures_ws()

    streams = "/".join([f"{symbol}@trade" for symbol in SYMBOLS[:150]])
    url = f"wss://fstream.binance.com/stream?streams={streams}"
    
    WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close).run_forever()

def arbitraj_tarama_dongusu():
    global arbitraj_pozisyonlari
    while True:
        try:
            en_yuksek_makaslar = []
            for symbol in SYMBOLS[:150]:
                spot_fiyat = piyasa_verisi[symbol]["spot_price"]
                futures_fiyat = piyasa_verisi[symbol]["futures_price"]
                
                if not spot_fiyat or not futures_fiyat: continue

                anlik_makas = ((futures_fiyat - spot_fiyat) / spot_fiyat) * 100
                coin_label = symbol.upper()
                en_yuksek_makaslar.append((coin_label, anlik_makas, spot_fiyat, futures_fiyat))

                pos = arbitraj_pozisyonlari[symbol]

                if not pos["aktif"]:
                    if anlik_makas >= GIRIS_MAKAS_YUZDE:
                        brut, kesinti, net = net_kar_hesapla(anlik_makas, CIKIS_MAKAS_YUZDE)
                        if net <= 0: continue
                        
                        basarili, s_qty, f_qty = execute_arbitrage_entry(symbol, spot_fiyat, futures_fiyat)
                        if basarili:
                            pos.update({"aktif": True, "giris_makas": anlik_makas, "spot_adet": s_qty, "futures_adet": f_qty})
                            telegram_bildir(f"🤖 <b>İŞLEME GİRİLDİ</b>\n\n📊 <b>Koin:</b> {coin_label}\n⚡ <b>Makas:</b> +%{anlik_makas:.4f}\n💵 <b>Tahmini Net Kâr:</b> {net:.4f} USDT")
                else:
                    if anlik_makas <= CIKIS_MAKAS_YUZDE:
                        if execute_arbitrage_exit(symbol, pos["spot_adet"], pos["futures_adet"]):
                            brut, kesinti, net = net_kar_hesapla(pos["giris_makas"], anlik_makas)
                            telegram_bildir(f"🤝 <b>🔒 POZİSYON KAPATILDI</b>\n\n🎉 <b>NET REALİZE KÂR:</b> {net:.4f} USDT")
                            pos["aktif"] = False

            if en_yuksek_makaslar:
                en_yuksek_makaslar.sort(key=lambda x: x[1], reverse=True)
                print("\n💵 --- EN YÜKSEK 3 MAKAS ---")
                for i, item in enumerate(en_yuksek_makaslar[:3]):
                    print(f"{i+1}. [{item[0]}] +%{item[1]:.4f} | Sp: {item[2]} | Fu: {item[3]}")
        except Exception as e:
            print(f"❌ Döngü hatası: {e}")
            traceback.print_exc()
        
        # ⚡ OPTİMİZASYON: Tarama döngüsü bekleme süresi, canlı piyasayı kaçırmamak için 2 saniyeden 0.2 saniyeye düşürüldü.
        time.sleep(0.2)

if __name__ == "__main__":
    SYMBOLS = get_all_futures_symbols()
    piyasa_verisi = {symbol: {"spot_price": None, "futures_price": None} for symbol in SYMBOLS}
    arbitraj_pozisyonlari = {symbol: {"aktif": False, "giris_makas": 0.0, "spot_adet": 0.0, "futures_adet": 0.0} for symbol in SYMBOLS}
    
    # Kaldıraç ayarları başlangıçta bir kez yapılır
    set_all_leverages()
    
    # ⚡ OPTİMİZASYON: Bot döngüye girmeden hemen önce tüm limit kuralları RAM belleğe alınır.
    tum_hassasiyetleri_yukle()
    
    telegram_bildir(f"🤖 <b>SOCKS5 Enjeksiyonlu Robot Başlatıldı!</b>\n🎯 <b>Giriş Eşiği:</b> +%{GIRIS_MAKAS_YUZDE}")
    
    threading.Thread(target=start_multi_spot_ws, daemon=True).start()
    threading.Thread(target=start_multi_futures_ws, daemon=True).start()
    arbitraj_tarama_dongusu()
