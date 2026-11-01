"""Monitoring: kirim notifikasi WhatsApp (CallMeBot) setiap aplikasi error.

Pakai di app.py, cukup sekali setelah import:
    import monitor
    monitor.pasang("Nama Aplikasi")

Isi di Streamlit → Settings → Secrets:
    [callmebot]
    phone = "+628xxxxxxxxxx"
    apikey = "123456"

Kalau secrets belum diisi, monitoring diam saja (aplikasi tetap jalan normal).
"""
import hashlib
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import streamlit as st

JEDA_DETIK = 600          # error yang sama maksimal dikirim 1x per 10 menit
_terakhir: dict = {}      # tanda tangan error -> waktu terakhir dikirim
_terpasang = False


def _config():
    try:
        sec = st.secrets.get("callmebot", {})
        phone = (sec.get("phone") if hasattr(sec, "get") else None) or st.secrets.get("WA_PHONE")
        apikey = (sec.get("apikey") if hasattr(sec, "get") else None) or st.secrets.get("WA_APIKEY")
    except Exception:
        return None
    if not phone or not apikey:
        return None
    phone = str(phone).strip().replace(" ", "").replace("-", "")
    if phone.startswith("0"):
        phone = "+62" + phone[1:]
    return phone, str(apikey).strip()


def kirim_wa(teks: str) -> bool:
    """Kirim pesan WA ke nomor di secrets. Berjalan di background supaya app tidak tertahan."""
    cfg = _config()
    if not cfg:
        return False
    phone, apikey = cfg
    url = ("https://api.callmebot.com/whatsapp.php?"
           + urllib.parse.urlencode({"phone": phone, "text": teks[:1500], "apikey": apikey}))

    def _kirim():
        try:
            urllib.request.urlopen(url, timeout=15).read()
        except Exception:
            pass

    threading.Thread(target=_kirim, daemon=True).start()
    return True


def lapor_error(ex: BaseException, nama_app: str) -> None:
    tb = traceback.extract_tb(ex.__traceback__)
    lokasi = f"{tb[-1].filename.rsplit('/', 1)[-1]}:{tb[-1].lineno}" if tb else "-"
    tanda = hashlib.md5(f"{type(ex).__name__}|{lokasi}".encode()).hexdigest()
    sekarang = time.time()
    if sekarang - _terakhir.get(tanda, 0) < JEDA_DETIK:
        return
    _terakhir[tanda] = sekarang

    jam = datetime.now(timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M WIB")
    detail = "".join(traceback.format_exception(type(ex), ex, ex.__traceback__)[-3:])[-700:]
    kirim_wa(f"🚨 *{nama_app}* error\n{jam}\n\n*{type(ex).__name__}*: {str(ex)[:300]}\n"
             f"Lokasi: {lokasi}\n\n```{detail}```")


def pasang(nama_app: str) -> None:
    """Sambungkan ke penanganan error Streamlit. Aman dipanggil berulang tiap rerun."""
    global _terpasang
    if _terpasang:
        return
    try:
        from streamlit.runtime.scriptrunner import exec_code
    except Exception:
        return  # versi Streamlit berbeda: monitoring nonaktif, aplikasi tetap jalan

    def bungkus(asli):
        def dengan_notif(ex, *args, **kwargs):
            try:
                lapor_error(ex, nama_app)
            except Exception:
                pass
            return asli(ex, *args, **kwargs)
        return dengan_notif

    # Streamlit >= 1.5x: handle_user_script_exception; versi lama: handle_uncaught_app_exception
    for nama in ("handle_user_script_exception", "handle_uncaught_app_exception"):
        if hasattr(exec_code, nama):
            setattr(exec_code, nama, bungkus(getattr(exec_code, nama)))
            _terpasang = True
            return
