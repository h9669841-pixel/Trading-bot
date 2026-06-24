                    # STAGE 2: GİRİŞ EMİRLERİ VERİLDİYSE -> ZAMAN AŞIMI GÜVENLİK KORUMASI (100 SANİYE)
                    elif pos["durum"] == "GIRIS_BEKLIYOR":
                        s_status = client.get_order(symbol=coin_label, orderId=pos["spot_entry_id"]).get("status")
                        f_status = client.futures_get_order(symbol=coin_label, orderId=pos["futures_entry_id"]).get("status")
                        
                        if s_status == "FILLED" and f_status == "FILLED":
                            hedef_spot_cikis = pos["giris_spot_fiyat"] * (1 + (CIKIS_MAKAS_YUZDE / 2 / 100))
                            hedef_futures_cikis = pos["giris_futures_fiyat"] * (1 - (CIKIS_MAKAS_YUZDE / 2 / 100))
                            
                            basarili_hedef, s_cl_id, f_cl_id = execute_limit_arbitrage_exit_targets(
                                symbol, pos["spot_adet"], pos["futures_adet"], hedef_spot_cikis, hedef_futures_cikis
                            )
                            if basarili_hedef:
                                pos.update({
                                    "durum": "CIKIS_BEKLIYOR",
                                    "spot_exit_id": s_cl_id,
                                    "futures_exit_id": f_cl_id,
                                    "hedef_spot_cikis": hedef_spot_cikis,
                                    "hedef_futures_cikis": hedef_futures_cikis
                                })
                                telegram_bildir(
                                    f"🚀 <b>{coin_label} GİRİŞLER DOLDU!</b>\n"
                                    f"🔒 Kâr Limitleri Tahtaya Asıldı:\n"
                                    f"📈 Spot Satış: {hedef_spot_cikis:.4f}\n"
                                    f"📉 Vadeli Alış: {hedef_futures_cikis:.4f}"
                                )
                        
                        # 🛡️ Acil Durum Yönetimi: Artık 10 saniye değil, 100 saniye boyunca limit emirler tahtada dolmazsa temizle
                        elif time.time() - pos.get("emir_giris_zamani", time.time()) > 100.0:
                            print(f"🚨 100sn Süre Aşımı: {coin_label} emirleri iptal ediliyor...")
                            
                            if s_status != "FILLED":
                                try: client.cancel_order(symbol=coin_label, orderId=pos["spot_entry_id"])
                                except Exception: pass
                            if f_status != "FILLED":
                                try: client.futures_cancel_order(symbol=coin_label, orderId=pos["futures_entry_id"])
                                except Exception: pass
                                
                            # Yarım bacak koruma kontrolü (Piyasa emriyle eşitleme)
                            if s_status == "FILLED" and f_status != "FILLED":
                                client.create_order(symbol=coin_label, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=pos["spot_adet"])
                                telegram_bildir(f"🛡️ <b>BACAK RİSKİ ÖNLENDİ:</b> 100sn içinde vadeli dolmadığı için spotlar piyasadan geri satıldı.")
                                
                            elif f_status == "FILLED" and s_status != "FILLED":
                                client.futures_create_order(symbol=coin_label, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=pos["futures_adet"])
                                telegram_bildir(f"🛡️ <b>BACAK RİSKİ ÖNLENDİ:</b> 100sn içinde spot dolmadığı için vadeli short piyasadan kapatıldı.")
                            else:
                                print(f"🧹 {coin_label}: Eşleşmeyen pasif limit emirler 100sn sonunda temizlendi. Risk yok.")
                                
                            pos.update({"durum": "BOS", "spot_entry_id": None, "futures_entry_id": None})
