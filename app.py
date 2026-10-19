"""
Aplikasi Poin Toko - Program Loyalitas Pembelian Rokok
Database: Google Sheets (data) + Google Drive (foto), langsung via Service Account.
Aturan: 1 nota = min 1 pack, max 1 slop (10 pack). 1 pack = 100 poin.
Jalankan: streamlit run app.py
"""
import base64
import hashlib
import io
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import requests
import streamlit as st
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from PIL import Image

WIB = ZoneInfo("Asia/Jakarta")


def now_wib():
    """Waktu sekarang WIB, tanpa info zona (server Streamlit Cloud pakai UTC)."""
    return datetime.now(WIB).replace(tzinfo=None)


def today_wib():
    return now_wib().date()


# ================== KONFIGURASI ==================
POIN_PER_PACK = 100
MIN_PACK = 1
MAX_PACK = 10  # 1 slop

IMAGE_MAX_PX = 1920
IMAGE_QUALITY = 75

FOLDER_UTAMA = "Foto Poin Toko"
FOLDER_MAP = {"Transaksi": "Nota Pembelian", "Misi": "Misi Bonus"}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def secret(nama, default=None):
    try:
        return st.secrets[nama]
    except Exception:
        return default


SALES_PASSWORD = secret("SALES_PASSWORD", "sales123")
SHEET_ID = secret("GSHEET_ID", "")
SHEET_URL = secret("GSHEET_URL",
                   f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit" if SHEET_ID else "")
GEMINI_API_KEY = secret("GEMINI_API_KEY")
GEMINI_MODEL = secret("GEMINI_MODEL", "gemini-2.0-flash")

# Misi bonus: poin per jenis, bisa diklaim 1x per minggu (Senin-Minggu) per jenis
MISI = {
    "eceran":   {"nama": "Sedia Eceran",  "ikon": "🚬",  "poin": 1000},
    "sunblind": {"nama": "POSM Sunblind", "ikon": "⛱️", "poin": 1000},
    "stiker":   {"nama": "POSM Stiker",   "ikon": "🏷️", "poin": 500},
}

STATUS_LABEL = {
    "pending":  "⏳ Menunggu approval",
    "approved": "✅ Disetujui",
    "rejected": "❌ Ditolak",
}

# Kolom tiap sheet
COLS = {
    "Toko": ["kode_toko", "nama_toko", "pemilik", "no_hp", "alamat", "pin_hash", "dibuat"],
    "Transaksi": ["id", "kode_toko", "nama_toko", "no_nota", "tanggal", "jumlah_pack", "poin",
                  "status", "sumber", "deskripsi", "catatan", "sales", "diproses_oleh",
                  "diproses_pada", "dibuat", "foto_id", "foto_link", "ai_pack", "ai_baca"],
    "Misi": ["id", "kode_toko", "nama_toko", "jenis", "minggu", "keterangan", "poin", "status",
             "catatan", "diproses_oleh", "diproses_pada", "dibuat", "foto_id", "foto_link"],
    "Penukaran": ["id", "kode_toko", "nama_toko", "tanggal", "poin", "keterangan", "dibuat"],
}
KOLOM_ANGKA = {"jumlah_pack", "poin", "ai_pack"}

st.set_page_config(page_title="Poin Toko", page_icon="🏪", layout="centered")

# ================== TEMA BIRU ==================
TEMA_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Nunito+Sans:ital,wght@0,400;0,700;0,800;0,900;1,300&display=swap');

html, body, [class*="css"], .stApp, button, input, textarea {
    font-family: 'Nunito Sans', sans-serif !important;
}

/* Latar: biru tua bergradasi + cahaya di atas */
.stApp {
    background:
        radial-gradient(ellipse 80% 45% at 50% 12%, rgba(120,180,255,.45) 0%, rgba(120,180,255,0) 70%),
        linear-gradient(180deg, #1a4fb8 0%, #0d2f86 40%, #071a52 75%, #040f33 100%) !important;
    background-attachment: fixed !important;
    color: #fff;
}
[data-testid="stHeader"] { background: transparent !important; }

/* Sidebar */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0d2f86 0%, #061646 100%) !important;
    border-right: 1px solid rgba(255,255,255,.15);
}
[data-testid="stSidebar"] * { color: #fff !important; }

/* Teks */
h1, h2, h3, h4, p, label, span, li, .stMarkdown, [data-testid="stCaptionContainer"] {
    color: #fff !important;
}
[data-testid="stCaptionContainer"] { opacity: .85; }
h1, h2, h3 { font-weight: 900 !important; letter-spacing: .2px; }

/* Tombol: biru mengkilap, sudut membulat */
.stButton > button, .stDownloadButton > button,
[data-testid="stFormSubmitButton"] > button {
    background: linear-gradient(180deg, #4a9bf0 0%, #2367d1 45%, #123a8c 100%) !important;
    color: #fff !important;
    font-weight: 800 !important;
    font-size: 1.05rem !important;
    border: 1px solid rgba(255,255,255,.35) !important;
    border-radius: 16px !important;
    padding: .7rem 1rem !important;
    box-shadow: inset 0 1px 0 rgba(255,255,255,.35), 0 4px 12px rgba(0,0,0,.25) !important;
    transition: transform .08s ease, filter .15s ease;
}
.stButton > button:hover, .stDownloadButton > button:hover,
[data-testid="stFormSubmitButton"] > button:hover {
    filter: brightness(1.12); border-color: #fff !important;
}
.stButton > button:active, [data-testid="stFormSubmitButton"] > button:active {
    transform: scale(.98);
}
.stButton > button p, .stDownloadButton > button p,
[data-testid="stFormSubmitButton"] > button p { font-weight: 800 !important; }

/* Kartu / kotak bergaris & form */
[data-testid="stVerticalBlockBorderWrapper"], [data-testid="stForm"] {
    background: linear-gradient(180deg, rgba(74,155,240,.22) 0%, rgba(13,47,134,.35) 100%) !important;
    border: 1.5px solid rgba(255,255,255,.75) !important;
    border-radius: 18px !important;
}

/* Metric (saldo poin) */
[data-testid="stMetric"] {
    background: linear-gradient(180deg, #3d8de8 0%, #1d55b8 50%, #0f327f 100%);
    border: 1.5px solid rgba(255,255,255,.7);
    border-radius: 18px;
    padding: 14px 18px;
    box-shadow: inset 0 1px 0 rgba(255,255,255,.3), 0 4px 14px rgba(0,0,0,.25);
}
[data-testid="stMetricLabel"] * { color: #dbe9ff !important; font-weight: 700 !important; }
[data-testid="stMetricValue"], [data-testid="stMetricValue"] * {
    color: #fff !important; font-weight: 900 !important;
}

/* Input */
[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div,
[data-baseweb="base-input"] {
    background: #ffffff !important; border-radius: 12px !important;
}
[data-baseweb="input"] input, [data-baseweb="textarea"] textarea,
[data-baseweb="select"] * { color: #0b2a6f !important; }
[data-testid="stNumberInputContainer"] button { color: #0b2a6f !important; }

/* Tab: seperti tombol pil */
.stTabs [data-baseweb="tab-list"] { gap: 8px; }
.stTabs [data-baseweb="tab"] {
    background: rgba(255,255,255,.12); border-radius: 14px 14px 0 0;
    padding: 8px 16px; border: 1px solid rgba(255,255,255,.25);
}
.stTabs [aria-selected="true"] {
    background: linear-gradient(180deg, #4a9bf0 0%, #1d55b8 100%) !important;
}
.stTabs [data-baseweb="tab-highlight"] { background: #fff !important; }

/* Kotak info / sukses / error lebih kontras */
[data-testid="stAlert"] { border-radius: 14px; }

/* Kamera */
[data-testid="stCameraInput"] { border-radius: 16px; overflow: hidden; }

/* Logo teks di halaman login */
.logo-wrap { text-align: center; margin: 1.5rem 0 1.8rem; }
.logo { font-size: 3.2rem; line-height: 1; color: #fff;
        text-shadow: 0 2px 10px rgba(0,0,0,.25); }
.logo b { font-weight: 900; font-style: italic; letter-spacing: -1px; }
.logo i { font-weight: 300; }
.logo-sub { margin-top: .6rem; color: #dbe9ff; font-size: .95rem; }
.footer-note { text-align: center; color: #cfe0ff; font-size: .8rem;
               margin-top: 2.5rem; opacity: .85; }
</style>
"""
st.markdown(TEMA_CSS, unsafe_allow_html=True)


# ================== AUTH & CLIENTS ==================

@st.cache_resource
def _creds():
    info = json.loads(st.secrets["gcp_json"])
    return Credentials.from_service_account_info(info, scopes=SCOPES)


@st.cache_resource
def _gc():
    return gspread.authorize(_creds())


@st.cache_resource
def _drive():
    return build("drive", "v3", credentials=_creds())


def _wb():
    return _gc().open_by_key(SHEET_ID)


# ================== COMPRESS IMAGE ==================

def compress_image(uploaded_file) -> bytes:
    """Kompres gambar. Rotasi EXIF otomatis, resize max 1920px, JPEG. Auto-turun quality jika >1.5MB."""
    raw = uploaded_file.getvalue() if hasattr(uploaded_file, "getvalue") else uploaded_file
    img = Image.open(io.BytesIO(raw))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    try:
        import PIL.ExifTags as _et
        exif = img._getexif()
        if exif:
            ori_key = next((k for k, v in _et.TAGS.items() if v == "Orientation"), None)
            if ori_key and ori_key in exif:
                rotate_map = {3: 180, 6: 270, 8: 90}
                if exif[ori_key] in rotate_map:
                    img = img.rotate(rotate_map[exif[ori_key]], expand=True)
    except Exception:
        pass
    ratio = IMAGE_MAX_PX / max(img.size)
    if ratio < 1:
        img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
    quality = IMAGE_QUALITY
    for _ in range(3):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= 1_500_000 or quality <= 50:
            break
        quality -= 15
    return buf.getvalue()


# ================== DRIVE UTILS ==================

@st.cache_data(ttl=86400)
def _folder_id(subfolder: str) -> str:
    """Cari atau buat folder Drive. Returns folder ID."""
    drive = _drive()

    def _find_or_create(name, parent_id=None):
        q = (f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
             f"and trashed=false")
        if parent_id:
            q += f" and '{parent_id}' in parents"
        res = drive.files().list(q=q, fields="files(id)").execute()
        files = res.get("files", [])
        if files:
            return files[0]["id"]
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            meta["parents"] = [parent_id]
        return drive.files().create(body=meta, fields="id").execute()["id"]

    root_id = _find_or_create(FOLDER_UTAMA)
    return _find_or_create(subfolder, root_id)


def _upload_foto(data: bytes, filename: str, sheet_nama: str) -> tuple[str, str]:
    """Upload foto ke Drive. Returns (file_id, web_view_link)."""
    sub = FOLDER_MAP.get(sheet_nama, sheet_nama)
    fid = _folder_id(sub)
    drive = _drive()
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="image/jpeg", resumable=False)
    meta = {"name": filename, "parents": [fid]}
    f = drive.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()
    drive.permissions().create(
        fileId=f["id"],
        body={"role": "reader", "type": "anyone"},
    ).execute()
    return f["id"], f.get("webViewLink", "")


# ================== DATABASE: GOOGLE SHEETS ==================

@st.cache_data(ttl=60, show_spinner="Memuat data...")
def _muat_semua():
    wb = _wb()
    result = {}
    for nama, cols in COLS.items():
        try:
            ws = wb.worksheet(nama)
        except gspread.WorksheetNotFound:
            ws = wb.add_worksheet(nama, rows=1000, cols=len(cols))
            ws.update([cols], "A1")
            result[nama] = []
            continue
        rows = ws.get_all_values()
        if len(rows) < 2:
            # Pastikan header ada
            if not rows:
                ws.update([cols], "A1")
            result[nama] = []
            continue
        headers = rows[0]
        data = []
        for r in rows[1:]:
            if any(r):
                pad = r + [""] * max(0, len(headers) - len(r))
                data.append(dict(zip(headers, pad)))
        result[nama] = data
    return result


def tabel(nama):
    rows = _muat_semua().get(nama, [])
    df = pd.DataFrame(rows).reindex(columns=COLS[nama]).fillna("")
    for c in COLS[nama]:
        if c in KOLOM_ANGKA:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
        else:
            df[c] = df[c].astype(str)
    return df


def segarkan():
    _muat_semua.clear()


def _ws_ensure(nama) -> gspread.Worksheet:
    """Buka worksheet, buat kalau belum ada."""
    wb = _wb()
    try:
        ws = wb.worksheet(nama)
    except gspread.WorksheetNotFound:
        ws = wb.add_worksheet(nama, rows=1000, cols=len(COLS[nama]))
        ws.update([COLS[nama]], "A1")
        return ws
    # Pastikan header row benar
    header = ws.row_values(1)
    if header != COLS[nama]:
        ws.update([COLS[nama]], "A1")
    return ws


def _next_id(ws, cols) -> str:
    if "id" not in cols:
        return ""
    ki = cols.index("id") + 1
    vals = ws.col_values(ki)[1:]  # skip header
    ids = [int(v) for v in vals if v.isdigit()]
    return str(max(ids) + 1) if ids else "1"


def _cari_baris(ws, cols, key, value) -> int:
    """Nomor baris (1-indexed), atau -1."""
    ki = cols.index(key) + 1
    for i, v in enumerate(ws.col_values(ki)):
        if i == 0:
            continue
        if str(v) == str(value):
            return i + 1
    return -1


def tambah(sheet, row, foto=None, nama_foto="foto"):
    with st.spinner("Menyimpan..."):
        ws = _ws_ensure(sheet)
        cols = COLS[sheet]
        if "id" in cols and not row.get("id"):
            row["id"] = _next_id(ws, cols)
        if foto is not None:
            data = compress_image(foto)
            try:
                fid, furl = _upload_foto(data, f"{nama_foto}.jpg", sheet)
                row["foto_id"] = fid
                row["foto_link"] = furl
            except Exception as e:
                st.warning(f"Foto gagal diupload ke Drive: {e}")
        baris = []
        for c in cols:
            v = row.get(c, "")
            if c in KOLOM_ANGKA and v != "":
                try:
                    v = int(float(v))
                except Exception:
                    v = ""
            baris.append(v if v is not None else "")
        ws.append_row(baris, value_input_option="USER_ENTERED")
    segarkan()
    return {"id": row.get("id"), "foto_id": row.get("foto_id")}


def ubah(sheet, key, value, fields):
    with st.spinner("Menyimpan..."):
        ws = _ws_ensure(sheet)
        cols = COLS[sheet]
        r = _cari_baris(ws, cols, key, value)
        if r < 0:
            raise RuntimeError("Data tidak ditemukan")
        for k, v in fields.items():
            if k in cols:
                ci = cols.index(k) + 1
                val = int(v) if k in KOLOM_ANGKA and v != "" else str(v)
                ws.update_cell(r, ci, val)
    segarkan()


def hapus(sheet, key, value):
    with st.spinner("Menghapus..."):
        ws = _ws_ensure(sheet)
        cols = COLS[sheet]
        r = _cari_baris(ws, cols, key, value)
        if r < 0:
            raise RuntimeError("Data tidak ditemukan")
        if "foto_id" in cols:
            fi = cols.index("foto_id") + 1
            fid = ws.cell(r, fi).value
            if fid:
                try:
                    _drive().files().delete(fileId=fid).execute()
                except Exception:
                    pass
        ws.delete_rows(r)
    segarkan()


@st.cache_data(ttl=3600, show_spinner="Memuat foto...")
def ambil_foto(foto_id: str):
    if not foto_id:
        return None
    buf = io.BytesIO()
    request = _drive().files().get_media(fileId=foto_id)
    dl = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def tampil_foto(foto_id):
    try:
        b = ambil_foto(foto_id)
        if b:
            st.image(b, use_container_width=True)
    except Exception as e:
        st.warning(f"Foto tidak bisa dimuat: {e}")


# ================== AI PEMBACA NOTA (Gemini) ==================
PROMPT_NOTA = """Ini foto nota/faktur pembelian rokok dari sales ke toko (Indonesia).
Baca semua baris item rokok di nota. Untuk setiap item tulis nama produk dan jumlahnya
dalam PACK (bungkus). Aturan satuan: 1 slop = 10 pack, 1 bal = 10 slop = 100 pack.
Jika satuan tidak tertulis, anggap pack. Abaikan harga dan item yang bukan rokok.
Balas HANYA JSON dengan format:
{"terbaca": true, "tanggal": "YYYY-MM-DD atau kosong", "no_nota": "nomor nota atau kosong",
 "items": [{"nama": "nama produk", "pack": 0}], "total_pack": 0,
 "catatan": "sebutkan singkat bagian yang tidak jelas, atau kosong"}
Jika foto bukan nota atau tulisan sama sekali tidak terbaca, isi "terbaca": false."""


@st.cache_data(show_spinner=False, max_entries=200)
def baca_nota_ai(img_bytes):
    """Kirim foto nota ke Gemini, kembalikan dict hasil bacaan."""
    mime = "image/png" if img_bytes[:4] == b"\x89PNG" else "image/jpeg"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")
    body = {
        "contents": [{"parts": [
            {"text": PROMPT_NOTA},
            {"inline_data": {"mime_type": mime,
                             "data": base64.b64encode(img_bytes).decode()}},
        ]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    r = requests.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=60)
    if r.status_code == 429:
        raise RuntimeError("Kuota AI sedang penuh, coba lagi sebentar.")
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    teks = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    teks = teks.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    hasil = json.loads(teks)
    items = []
    for it in hasil.get("items") or []:
        try:
            items.append({"nama": str(it.get("nama", "")).strip(),
                          "pack": int(float(it.get("pack") or 0))})
        except (TypeError, ValueError):
            continue
    hasil["items"] = items
    hasil["total_pack"] = sum(i["pack"] for i in items) or int(
        float(hasil.get("total_pack") or 0))
    return hasil


def ringkas_ai(h):
    if not h.get("terbaca", True) or not h["items"]:
        return "AI: nota tidak terbaca"
    isi = "; ".join(f"{i['nama']} = {i['pack']} pack" for i in h["items"])
    teks = f"AI: {isi}. Total {h['total_pack']} pack."
    if h.get("catatan"):
        teks += f" (Catatan AI: {h['catatan']})"
    return teks


# ================== HELPER ==================
def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


def saldo_toko(kode):
    t, m, p = tabel("Transaksi"), tabel("Misi"), tabel("Penukaran")
    beli = t[(t.kode_toko == kode) & (t.status == "approved")].poin.sum()
    misi = m[(m.kode_toko == kode) & (m.status == "approved")].poin.sum()
    keluar = p[p.kode_toko == kode].poin.sum()
    return beli + misi, keluar, beli + misi - keluar


def jumlah_pending(kode=None, sheet="Transaksi"):
    df = tabel(sheet)
    df = df[df.status == "pending"]
    if kode:
        df = df[df.kode_toko == kode]
    return len(df)


def daftar_toko():
    return tabel("Toko").sort_values("nama_toko")


def nama_toko(kode):
    t = tabel("Toko")
    r = t[t.kode_toko == kode]
    return r.nama_toko.iloc[0] if len(r) else kode


def minggu_ini():
    """Kode minggu ISO (Senin-Minggu), mis. 2026-W43, plus tanggal Senin depan."""
    hari = today_wib()
    th, mg, _ = hari.isocalendar()
    senin_depan = hari + timedelta(days=7 - hari.weekday())
    return f"{th}-W{mg:02d}", senin_depan


def status_misi_minggu_ini(kode, jenis):
    mg, _ = minggu_ini()
    m = tabel("Misi")
    m = m[(m.kode_toko == kode) & (m.jenis == jenis) & (m.minggu == mg)
          & m.status.isin(["pending", "approved"])]
    if m.empty:
        return None
    return "approved" if (m.status == "approved").any() else "pending"


def angka(x):
    return f"{int(x or 0):,}".replace(",", ".")


def poin(x):
    return f"{angka(x)} poin"


def waktu(s):
    return str(s)[:16].replace("T", " ")


# ================== LOGIN ==================
def halaman_login():
    st.markdown(
        f"""<div class="logo-wrap">
              <div class="logo"><b>POIN</b><i>toko</i></div>
              <div class="logo-sub">Setiap 1 pack = {POIN_PER_PACK} poin 🎁</div>
            </div>""",
        unsafe_allow_html=True,
    )
    tab_toko, tab_sales = st.tabs(["🏪 Masuk sebagai Toko", "🧑‍💼 Masuk sebagai Sales"])

    with tab_toko:
        with st.form("login_toko"):
            kode = st.text_input("Kode Toko").strip().upper()
            pin = st.text_input("PIN", type="password")
            if st.form_submit_button("Masuk", use_container_width=True):
                segarkan()
                t = tabel("Toko")
                r = t[t.kode_toko == kode]
                if len(r) and r.pin_hash.iloc[0] == hash_pin(pin):
                    st.session_state.update(role="toko", kode_toko=kode,
                                            nama=r.nama_toko.iloc[0])
                    st.rerun()
                else:
                    st.error("Kode toko atau PIN salah.")

    with tab_sales:
        with st.form("login_sales"):
            nama = st.text_input("Nama Sales").strip()
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Masuk", use_container_width=True):
                if pw == SALES_PASSWORD and nama:
                    st.session_state.update(role="sales", nama=nama)
                    st.rerun()
                else:
                    st.error("Nama wajib diisi dan password harus benar.")

    st.markdown(
        '<div class="footer-note">Kumpulkan poin dari setiap pembelian, '
        'eceran, dan POSM.<br>Tukarkan poin Anda ke sales kami.</div>',
        unsafe_allow_html=True,
    )


def tombol_keluar():
    with st.sidebar:
        st.write(f"👤 **{st.session_state.nama}**")
        if st.button("🔄 Muat ulang data", use_container_width=True):
            segarkan()
            st.rerun()
        if st.button("Keluar", use_container_width=True):
            st.session_state.clear()
            st.rerun()


def lihat_foto_nota(trx, key, boleh_hapus=False):
    """trx: dataframe Transaksi (kolom asli)."""
    with st.expander("🧾 Lihat foto nota"):
        label = {
            f"#{r.id} • {r.tanggal} • {r.jumlah_pack} pack • {STATUS_LABEL.get(r.status, r.status)}": r.id
            for r in trx.itertuples()
        }
        pilih = st.selectbox("Pilih nota", list(label.keys()), key=key)
        row = trx[trx.id == label[pilih]].iloc[0]
        tampil_foto(row.foto_id)
        if row.deskripsi:
            st.caption(f"Deskripsi: {row.deskripsi}")
        if boleh_hapus and st.button("🗑️ Hapus nota ini (salah input)", key=key + "_del"):
            hapus("Transaksi", "id", row.id)
            st.rerun()


# ================== HALAMAN SALES ==================
def sales_approval():
    st.subheader("✅ Approval dari Toko")
    n_nota, n_misi = jumlah_pending(), jumlah_pending(sheet="Misi")
    tab_nota, tab_misi = st.tabs([f"🧾 Nota Pembelian ({n_nota})",
                                  f"🎯 Misi Bonus ({n_misi})"])
    with tab_nota:
        sales_approval_nota()
    with tab_misi:
        sales_approval_misi()


def sales_approval_nota():
    t = tabel("Transaksi")
    pend = t[t.status == "pending"].sort_values("dibuat")
    if pend.empty:
        st.success("Tidak ada nota yang menunggu approval.")
        return
    st.caption(f"{len(pend)} nota menunggu. Cek foto nota sebelum approve.")

    for r in pend.itertuples():
        with st.container(border=True):
            st.markdown(f"**{r.nama_toko}** ({r.kode_toko}) · diajukan {waktu(r.dibuat)}")
            tampil_foto(r.foto_id)
            st.write(f"**Deskripsi dari toko:** {r.deskripsi or '-'}")
            if r.ai_baca:
                st.write(f"**🤖 Bacaan AI saat foto diambil:** {r.ai_baca}")
                if r.ai_pack and r.ai_pack != r.jumlah_pack:
                    st.warning(f"⚠️ Toko mengajukan {r.jumlah_pack} pack, tapi AI membaca "
                               f"{r.ai_pack} pack di nota. Cek foto dengan teliti.")
            st.write(f"**Diajukan:** {r.jumlah_pack} pack = {poin(r.jumlah_pack * POIN_PER_PACK)}")

            c1, c2 = st.columns(2)
            pack = c1.number_input(
                "Jumlah pack disetujui (koreksi bila beda dengan nota)",
                min_value=MIN_PACK, max_value=MAX_PACK,
                value=min(max(int(r.jumlah_pack), MIN_PACK), MAX_PACK), step=1,
                key=f"pk{r.id}",
            )
            no_nota = c2.text_input("No. Nota (opsional)", key=f"nn{r.id}").strip().upper()
            catatan = st.text_input("Catatan / alasan ditolak", key=f"ct{r.id}").strip()

            b1, b2 = st.columns(2)
            if b1.button("✅ Approve", key=f"ok{r.id}", type="primary",
                         use_container_width=True):
                if no_nota and ((t.no_nota == no_nota) & (t.id != r.id)).any():
                    st.error(f"No. nota {no_nota} sudah pernah dipakai. Kemungkinan nota dobel.")
                else:
                    fields = {"status": "approved", "jumlah_pack": int(pack),
                              "poin": int(pack) * POIN_PER_PACK, "catatan": catatan,
                              "diproses_oleh": st.session_state.nama,
                              "diproses_pada": now_wib().isoformat(timespec="seconds")}
                    if no_nota:
                        fields["no_nota"] = no_nota
                    ubah("Transaksi", "id", r.id, fields)
                    st.toast(f"Disetujui: {r.nama_toko} +{poin(int(pack) * POIN_PER_PACK)}")
                    st.rerun()
            if b2.button("❌ Tolak", key=f"no{r.id}", use_container_width=True):
                if not catatan:
                    st.error("Isi alasan penolakan dulu supaya toko tahu.")
                else:
                    ubah("Transaksi", "id", r.id, {
                        "status": "rejected", "catatan": catatan,
                        "diproses_oleh": st.session_state.nama,
                        "diproses_pada": now_wib().isoformat(timespec="seconds")})
                    st.toast(f"Ditolak: {r.nama_toko}")
                    st.rerun()


def sales_approval_misi():
    m = tabel("Misi")
    pend = m[m.status == "pending"].sort_values("dibuat")
    if pend.empty:
        st.success("Tidak ada misi yang menunggu approval.")
        return
    st.caption(f"{len(pend)} misi menunggu. Pastikan foto sesuai sebelum approve.")

    for r in pend.itertuples():
        info = MISI.get(r.jenis, {"nama": r.jenis, "ikon": "🎯"})
        with st.container(border=True):
            st.markdown(
                f"**{info['ikon']} {info['nama']}** · {r.nama_toko} ({r.kode_toko}) · "
                f"minggu {r.minggu} · diajukan {waktu(r.dibuat)}"
            )
            tampil_foto(r.foto_id)
            if r.keterangan:
                label = "Brand yang diecer" if r.jenis == "eceran" else "Keterangan"
                st.write(f"**{label}:** {r.keterangan}")
            st.write(f"**Poin:** {poin(r.poin)}")
            catatan = st.text_input("Catatan / alasan ditolak", key=f"mct{r.id}").strip()

            b1, b2 = st.columns(2)
            if b1.button("✅ Approve", key=f"mok{r.id}", type="primary",
                         use_container_width=True):
                ubah("Misi", "id", r.id, {
                    "status": "approved", "catatan": catatan,
                    "diproses_oleh": st.session_state.nama,
                    "diproses_pada": now_wib().isoformat(timespec="seconds")})
                st.toast(f"Disetujui: {r.nama_toko} +{poin(r.poin)}")
                st.rerun()
            if b2.button("❌ Tolak", key=f"mno{r.id}", use_container_width=True):
                if not catatan:
                    st.error("Isi alasan penolakan dulu supaya toko tahu.")
                else:
                    ubah("Misi", "id", r.id, {
                        "status": "rejected", "catatan": catatan,
                        "diproses_oleh": st.session_state.nama,
                        "diproses_pada": now_wib().isoformat(timespec="seconds")})
                    st.toast(f"Ditolak: {r.nama_toko}")
                    st.rerun()


def sales_input_nota():
    st.subheader("📝 Input Nota Pembelian (oleh Sales)")
    st.caption("Nota yang diinput sales langsung disetujui.")
    toko = daftar_toko()
    if toko.empty:
        st.info("Belum ada toko. Daftarkan dulu di menu **Daftar Toko**.")
        return

    opsi = {f"{r.nama_toko} ({r.kode_toko})": r.kode_toko for r in toko.itertuples()}
    pilihan = st.selectbox("Toko", list(opsi.keys()))
    kode = opsi[pilihan]

    col1, col2 = st.columns(2)
    no_nota = col1.text_input("No. Nota").strip().upper()
    tgl = col2.date_input("Tanggal", value=today_wib(), max_value=today_wib())
    jumlah = st.number_input(
        f"Jumlah pack (min {MIN_PACK}, max {MAX_PACK} = 1 slop)",
        min_value=MIN_PACK, max_value=MAX_PACK, value=1, step=1,
    )
    dapat = int(jumlah) * POIN_PER_PACK
    st.metric("Poin yang didapat", poin(dapat))

    sumber = st.radio("Foto nota", ["Kamera", "Upload file"], horizontal=True)
    foto = (
        st.camera_input("Foto nota")
        if sumber == "Kamera"
        else st.file_uploader("Upload foto nota", type=["jpg", "jpeg", "png"])
    )

    if st.button("💾 Simpan Nota", type="primary", use_container_width=True):
        if not no_nota:
            st.error("No. nota wajib diisi.")
            return
        if foto is None:
            st.error("Foto nota wajib dilampirkan.")
            return
        if (tabel("Transaksi").no_nota == no_nota).any():
            st.error(f"No. nota **{no_nota}** sudah pernah diinput. Cek lagi notanya.")
            return
        now = now_wib().isoformat(timespec="seconds")
        tambah("Transaksi", {
            "kode_toko": kode, "nama_toko": nama_toko(kode), "no_nota": no_nota,
            "tanggal": tgl.isoformat(), "jumlah_pack": int(jumlah), "poin": dapat,
            "status": "approved", "sumber": "sales", "sales": st.session_state.nama,
            "diproses_oleh": st.session_state.nama, "diproses_pada": now, "dibuat": now,
        }, foto=foto, nama_foto=f"{kode}_nota_{no_nota}")
        st.success(f"Tersimpan! {pilihan} mendapat {poin(dapat)}.")
        st.balloons()


def sales_daftar_toko():
    st.subheader("🏬 Daftar Toko Baru")
    with st.form("form_toko", clear_on_submit=True):
        kode = st.text_input("Kode Toko (unik, mis. TK001)").strip().upper()
        nama = st.text_input("Nama Toko").strip()
        pemilik = st.text_input("Nama Pemilik").strip()
        hp = st.text_input("No. HP").strip()
        alamat = st.text_area("Alamat").strip()
        pin = st.text_input("PIN untuk login toko (min 4 angka)", type="password")
        if st.form_submit_button("Daftarkan", use_container_width=True):
            if not (kode and nama and len(pin) >= 4):
                st.error("Kode, nama toko, dan PIN (min 4 karakter) wajib diisi.")
            elif (tabel("Toko").kode_toko == kode).any():
                st.error("Kode toko sudah dipakai.")
            else:
                tambah("Toko", {
                    "kode_toko": kode, "nama_toko": nama, "pemilik": pemilik,
                    "no_hp": hp, "alamat": alamat, "pin_hash": hash_pin(pin),
                    "dibuat": now_wib().isoformat(timespec="seconds"),
                })
                st.success(f"Toko {nama} terdaftar. Kode: {kode}")

    st.divider()
    st.write("**Toko terdaftar**")
    t = daftar_toko()
    st.dataframe(
        t[["kode_toko", "nama_toko", "pemilik", "no_hp", "alamat"]].rename(columns={
            "kode_toko": "Kode", "nama_toko": "Toko", "pemilik": "Pemilik",
            "no_hp": "HP", "alamat": "Alamat"}),
        hide_index=True, use_container_width=True,
    )

    with st.expander("🔑 Reset PIN toko"):
        if not t.empty:
            k = st.selectbox("Pilih toko", t.kode_toko, key="reset_kode")
            pin_baru = st.text_input("PIN baru", type="password", key="pin_baru")
            if st.button("Reset PIN") and len(pin_baru) >= 4:
                ubah("Toko", "kode_toko", k, {"pin_hash": hash_pin(pin_baru)})
                st.success("PIN diperbarui.")


def sales_rekap():
    st.subheader("📊 Rekap Poin Semua Toko")
    if SHEET_URL:
        st.link_button("📗 Buka Google Spreadsheet", SHEET_URL, use_container_width=True)
    toko = daftar_toko()
    if toko.empty:
        st.info("Belum ada data.")
        return
    t, m, p = tabel("Transaksi"), tabel("Misi"), tabel("Penukaran")
    ta, ma = t[t.status == "approved"], m[m.status == "approved"]

    df = toko[["kode_toko", "nama_toko"]].rename(columns={"kode_toko": "Kode", "nama_toko": "Toko"})
    df["Pack"] = df.Kode.map(ta.groupby("kode_toko").jumlah_pack.sum()).fillna(0).astype(int)
    df["PoinBeli"] = df.Kode.map(ta.groupby("kode_toko").poin.sum()).fillna(0).astype(int)
    df["PoinMisi"] = df.Kode.map(ma.groupby("kode_toko").poin.sum()).fillna(0).astype(int)
    df["PoinDitukar"] = df.Kode.map(p.groupby("kode_toko").poin.sum()).fillna(0).astype(int)
    df["SaldoPoin"] = df.PoinBeli + df.PoinMisi - df.PoinDitukar
    pend = pd.concat([t[t.status == "pending"].kode_toko, m[m.status == "pending"].kode_toko])
    df["Pending"] = df.Kode.map(pend.value_counts()).fillna(0).astype(int)
    df = df.sort_values("SaldoPoin", ascending=False)

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Toko", len(df))
    c2.metric("Total Pack", angka(df.Pack.sum()))
    c3.metric("Total Saldo Poin", angka(df.SaldoPoin.sum()))
    st.dataframe(df, hide_index=True, use_container_width=True)

    st.divider()
    st.write("**Riwayat Semua Nota**")
    if t.empty:
        st.info("Belum ada nota.")
        return
    t = t.sort_values("dibuat", ascending=False)
    st.dataframe(
        pd.DataFrame({
            "Tanggal": t.tanggal, "Toko": t.nama_toko, "Nota": t.no_nota.replace("", "-"),
            "Pack": t.jumlah_pack, "Poin": t.poin, "Status": t.status.map(STATUS_LABEL),
            "Dari": t.sumber, "Diproses": t.diproses_oleh,
        }),
        hide_index=True, use_container_width=True,
    )
    lihat_foto_nota(t, key="sales_foto", boleh_hapus=True)


def sales_penukaran():
    st.subheader("🎁 Penukaran Poin")
    toko = daftar_toko()
    if toko.empty:
        st.info("Belum ada toko.")
        return
    opsi = {f"{r.nama_toko} ({r.kode_toko})": r.kode_toko for r in toko.itertuples()}
    pilihan = st.selectbox("Toko", list(opsi.keys()), key="tukar_toko")
    kode = opsi[pilihan]
    _, _, saldo = saldo_toko(kode)
    st.metric("Saldo poin saat ini", poin(saldo))

    with st.form("form_tukar", clear_on_submit=True):
        jml = st.number_input("Poin yang ditukar", min_value=0, step=100)
        ket = st.text_input("Keterangan (mis. potong tagihan, hadiah)")
        if st.form_submit_button("Proses Penukaran", use_container_width=True):
            if jml <= 0:
                st.error("Isi jumlah poin.")
            elif jml > saldo:
                st.error("Poin melebihi saldo.")
            else:
                tambah("Penukaran", {
                    "kode_toko": kode, "nama_toko": nama_toko(kode),
                    "tanggal": today_wib().isoformat(), "poin": int(jml),
                    "keterangan": ket, "dibuat": now_wib().isoformat(timespec="seconds"),
                })
                st.success(f"Penukaran {poin(jml)} berhasil.")
                st.rerun()


# ================== HALAMAN TOKO ==================
def toko_saldo():
    kode = st.session_state.kode_toko
    st.title(f"🏪 {st.session_state.nama}")
    masuk, keluar, saldo = saldo_toko(kode)

    st.metric("💰 Saldo Poin Anda", poin(saldo))
    c1, c2 = st.columns(2)
    c1.metric("Total poin terkumpul", poin(masuk))
    c2.metric("Sudah ditukar", poin(keluar))

    n_pending = jumlah_pending(kode) + jumlah_pending(kode, sheet="Misi")
    if n_pending:
        st.info(f"⏳ {n_pending} pengajuan sedang menunggu approval sales.")

    t = tabel("Transaksi")
    trx = t[t.kode_toko == kode].sort_values("dibuat", ascending=False)
    st.divider()
    st.subheader("Riwayat Pembelian")
    if trx.empty:
        st.info("Belum ada pembelian tercatat.")
    else:
        ok = trx[trx.status == "approved"]
        if not ok.empty:
            per_bulan = (
                ok.assign(Bulan=pd.to_datetime(ok.tanggal.str[:10], errors="coerce")
                          .dt.strftime("%Y-%m"))
                .groupby("Bulan")["poin"].sum()
            )
            st.bar_chart(per_bulan, y_label="Poin")
        st.dataframe(
            pd.DataFrame({
                "Tanggal": trx.tanggal, "Nota": trx.no_nota.replace("", "-"),
                "Pack": trx.jumlah_pack, "Poin": trx.poin,
                "Status": trx.status.map(STATUS_LABEL), "Catatan": trx.catatan,
            }),
            hide_index=True, use_container_width=True,
        )
        lihat_foto_nota(trx, key="toko_foto")

    m = tabel("Misi")
    misi = m[m.kode_toko == kode].sort_values("dibuat", ascending=False)
    if not misi.empty:
        st.subheader("Riwayat Misi Bonus")
        st.dataframe(
            pd.DataFrame({
                "Tanggal": misi.dibuat.str[:10],
                "Misi": misi.jenis.map(
                    lambda j: f"{MISI[j]['ikon']} {MISI[j]['nama']}" if j in MISI else j),
                "Keterangan": misi.keterangan,
                "Poin": misi.poin,
                "Status": misi.status.map(STATUS_LABEL),
                "Catatan": misi.catatan,
            }),
            hide_index=True, use_container_width=True,
        )

    p = tabel("Penukaran")
    tukar = p[p.kode_toko == kode].sort_values("dibuat", ascending=False)
    if not tukar.empty:
        st.subheader("Riwayat Penukaran")
        st.dataframe(
            tukar[["tanggal", "poin", "keterangan"]].rename(columns={
                "tanggal": "Tanggal", "poin": "Poin", "keterangan": "Keterangan"}),
            hide_index=True, use_container_width=True,
        )


def toko_tambah_poin():
    kode = st.session_state.kode_toko
    st.subheader("➕ Tambahkan Poin dari Pembelian ke Sales")
    st.caption(
        f"1 pack = {POIN_PER_PACK} poin · min {MIN_PACK} pack, max {MAX_PACK} pack (1 slop) per nota. "
        "Poin masuk setelah disetujui sales."
    )

    if st.session_state.pop("toko_kirim_ok", None):
        st.success("Nota terkirim! Tunggu approval sales, poin akan muncul di Saldo Poin.")

    v = st.session_state.setdefault("form_ver", 0)

    foto = st.camera_input("📷 Foto nota (langsung dari kamera)", key=f"cam{v}")

    hasil_ai = None
    if foto is not None and GEMINI_API_KEY:
        img = foto.getvalue()
        sidik = hashlib.md5(img).hexdigest()
        try:
            with st.spinner("🤖 AI sedang membaca nota..."):
                hasil_ai = baca_nota_ai(img)
        except Exception as e:
            st.warning(f"AI belum bisa membaca nota ({e}). Silakan isi manual.")
        if hasil_ai is not None:
            with st.container(border=True):
                st.markdown("**🤖 Hasil baca AI**")
                if hasil_ai.get("terbaca", True) and hasil_ai["items"]:
                    st.dataframe(
                        pd.DataFrame(hasil_ai["items"]).rename(
                            columns={"nama": "Item", "pack": "Pack"}),
                        hide_index=True, use_container_width=True,
                    )
                    st.write(f"**Total: {hasil_ai['total_pack']} pack**")
                    if hasil_ai["total_pack"] > MAX_PACK:
                        st.warning(f"Total lebih dari {MAX_PACK} pack. Maksimal 1 nota = "
                                   f"{MAX_PACK} pack (1 slop), jadi yang diklaim {MAX_PACK} pack.")
                    if hasil_ai.get("catatan"):
                        st.caption(f"Catatan AI: {hasil_ai['catatan']}")
                    st.caption("Cek lagi: jika AI salah baca, ubah angka pack di bawah.")
                else:
                    st.warning("AI tidak bisa membaca nota. Foto ulang lebih dekat dan "
                               "terang, atau isi manual.")
            if st.session_state.get(f"ai_isi{v}") != sidik:
                st.session_state[f"ai_isi{v}"] = sidik
                if hasil_ai["total_pack"] > 0:
                    st.session_state[f"pack{v}"] = min(max(hasil_ai["total_pack"], MIN_PACK),
                                                       MAX_PACK)
                st.session_state[f"desk{v}"] = ringkas_ai(hasil_ai)

    awal = {} if f"pack{v}" in st.session_state else {"value": None}
    pack = st.number_input(
        "Total pembelian (pack)",
        min_value=MIN_PACK, max_value=MAX_PACK, step=1,
        placeholder=f"Isi angka {MIN_PACK}-{MAX_PACK}", key=f"pack{v}", **awal,
    )
    if pack:
        st.metric("Poin yang akan didapat", poin(int(pack) * POIN_PER_PACK))
    desk = st.text_area(
        "Deskripsi (opsional)",
        placeholder="Tulis keterangan jika tulisan di nota kurang jelas, "
                    "mis. '5 pack, nota tgl 19 Okt, sales Budi'",
        key=f"desk{v}",
    )

    if st.button("📤 Kirim ke Sales", type="primary", use_container_width=True):
        if foto is None:
            st.error("Foto nota wajib diambil dengan kamera.")
            return
        if not pack:
            st.error("Isi total pembelian (pack).")
            return
        now = now_wib()
        tambah("Transaksi", {
            "kode_toko": kode, "nama_toko": st.session_state.nama, "no_nota": "",
            "tanggal": now.date().isoformat(), "jumlah_pack": int(pack),
            "poin": int(pack) * POIN_PER_PACK, "status": "pending", "sumber": "toko",
            "deskripsi": desk.strip(), "dibuat": now.isoformat(timespec="seconds"),
            "ai_pack": hasil_ai["total_pack"] if hasil_ai else "",
            "ai_baca": ringkas_ai(hasil_ai) if hasil_ai else "",
        }, foto=foto, nama_foto=f"{kode}_nota_{now:%Y%m%d_%H%M%S}")
        st.session_state.form_ver = v + 1
        st.session_state.toko_kirim_ok = True
        st.rerun()


def kartu_misi(kode, jenis, minta_brand=False):
    """Satu kartu misi: foto kamera (+ brand), kirim ke sales, 1x per minggu."""
    info = MISI[jenis]
    _, senin_depan = minggu_ini()
    with st.container(border=True):
        st.markdown(f"### {info['ikon']} {info['nama']} · +{poin(info['poin'])}")
        status = status_misi_minggu_ini(kode, jenis)
        if status == "pending":
            st.info("⏳ Sudah dikirim minggu ini, menunggu konfirmasi sales.")
            return
        if status == "approved":
            st.success(
                f"✅ Poin minggu ini sudah didapat. Misi dibuka lagi Senin, "
                f"{senin_depan:%d-%m-%Y}."
            )
            return

        v = st.session_state.setdefault(f"misi_ver_{jenis}", 0)
        if minta_brand:
            st.caption("Foto rak/etalase rokok eceran Anda, lalu tulis brand yang diecer.")
        else:
            st.caption(f"Foto {info['nama'].replace('POSM ', '').lower()} kami yang "
                       "terpasang di toko Anda.")
        foto = st.camera_input(f"📷 Foto {info['nama']}", key=f"mcam_{jenis}{v}")
        brand = ""
        if minta_brand:
            brand = st.text_input(
                "Brand rokok yang diecer",
                placeholder="mis. Surya 12, Gudang Garam Filter, Sampoerna Mild",
                key=f"mbrand_{jenis}{v}",
            ).strip()

        if st.button(f"📤 Kirim {info['nama']}", key=f"mkirim_{jenis}{v}",
                     type="primary", use_container_width=True):
            if foto is None:
                st.error("Ambil foto dulu dengan kamera.")
                return
            if minta_brand and not brand:
                st.error("Tulis brand rokok yang diecer.")
                return
            segarkan()
            if status_misi_minggu_ini(kode, jenis):
                st.rerun()
            mg, _ = minggu_ini()
            now = now_wib()
            tambah("Misi", {
                "kode_toko": kode, "nama_toko": st.session_state.nama, "jenis": jenis,
                "minggu": mg, "keterangan": brand, "poin": info["poin"],
                "status": "pending", "dibuat": now.isoformat(timespec="seconds"),
            }, foto=foto, nama_foto=f"{kode}_{jenis}_{now:%Y%m%d_%H%M%S}")
            st.session_state[f"misi_ver_{jenis}"] = v + 1
            st.toast(f"{info['nama']} terkirim! Tunggu konfirmasi sales.")
            st.rerun()


def toko_misi():
    kode = st.session_state.kode_toko
    st.subheader("🎯 Misi Bonus Poin")
    st.info(
        "**Jangan khawatir!** Jika mau tambahan poin, Bapak/Ibu cukup sediakan "
        "**Eceran** dan **POSM** kami. 🙏"
    )
    _, senin_depan = minggu_ini()
    st.caption(
        f"Setiap misi bisa diklaim 1x per minggu (Senin–Minggu) dan poin masuk setelah "
        f"dikonfirmasi sales. Minggu baru mulai Senin, {senin_depan:%d-%m-%Y}."
    )

    st.markdown("## 🚬 Sedia Eceran")
    kartu_misi(kode, "eceran", minta_brand=True)

    st.markdown("## 📌 Misi POSM")
    st.caption("Pasang POSM kami lalu foto. Sunblind dan stiker bisa diklaim dua-duanya.")
    kartu_misi(kode, "sunblind")
    kartu_misi(kode, "stiker")


# ================== ROUTER ==================
def halaman_belum_setup():
    st.error("Secrets belum dikonfigurasi.")
    st.markdown(
        "Isi **Settings → Secrets** di Streamlit Cloud dengan:\n\n"
        "```toml\n"
        'SALES_PASSWORD = "password-sales"\n'
        'GSHEET_ID = "spreadsheet-id-dari-url"\n'
        'GSHEET_URL = "https://docs.google.com/spreadsheets/d/.../edit"\n'
        'GEMINI_API_KEY = "AIza..."  # opsional\n\n'
        '[gcp_json]\n'
        '# Paste isi JSON service account di sini (lihat README)\n'
        "```"
    )


def main():
    if not SHEET_ID:
        halaman_belum_setup()
        return
    try:
        _creds()
    except Exception:
        halaman_belum_setup()
        return

    role = st.session_state.get("role")
    if not role:
        halaman_login()
        return
    tombol_keluar()
    if role == "toko":
        menu = st.sidebar.radio("Menu", ["Saldo Poin", "Tambah Poin", "Misi Bonus"])
        {"Saldo Poin": toko_saldo, "Tambah Poin": toko_tambah_poin,
         "Misi Bonus": toko_misi}[menu]()
    else:
        n = jumlah_pending() + jumlah_pending(sheet="Misi")
        if n:
            st.sidebar.warning(f"🔔 {n} pengajuan toko menunggu approval")
        menu = st.sidebar.radio(
            "Menu",
            ["Approval", "Input Nota", "Rekap Poin", "Penukaran Poin", "Daftar Toko"],
        )
        {
            "Approval": sales_approval,
            "Input Nota": sales_input_nota,
            "Rekap Poin": sales_rekap,
            "Penukaran Poin": sales_penukaran,
            "Daftar Toko": sales_daftar_toko,
        }[menu]()


try:
    main()
except Exception as e:
    st.error(f"Terjadi kesalahan: {e}")
    if st.button("Coba lagi"):
        segarkan()
        st.rerun()
