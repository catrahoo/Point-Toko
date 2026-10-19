"""
Aplikasi Poin Toko - Program Loyalitas Pembelian Rokok
Database: Google Sheets (data) + Google Drive (foto), langsung via Service Account.
Aturan: 1 nota = min 1 pack, max 1 slop (10 pack). 1 pack = 100 poin.
Jalankan: streamlit run app.py
"""
import base64
import hashlib
import hmac
import time
import io
import json
import re
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
FOLDER_MAP = {"Transaksi": "Nota Pembelian", "Misi": "Misi Bonus", "Profil": "Foto Profil",
              "Hadiah": "Foto Hadiah"}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def secret(nama, default=None):
    """Ambil secret. Juga dicari di dalam [gcp_json], karena di TOML semua baris
    yang ditulis DI BAWAH [gcp_json] otomatis ikut masuk ke tabel itu."""
    try:
        if nama in st.secrets:
            return st.secrets[nama]
        g = st.secrets.get("gcp_json")
        if hasattr(g, "get") and nama in g:
            return g[nama]
    except Exception:
        pass
    return default


def _ambil_sheet_id(teks):
    """Terima ID polos atau URL lengkap spreadsheet."""
    teks = str(teks or "").strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", teks)
    return m.group(1) if m else teks


# Akun login sales (dikunci di kode)
SALES_AKUN = {"spv-sby": "sales123"}
SHEET_URL_DEFAULT = ("https://docs.google.com/spreadsheets/d/"
                     "1g5cHM2J00thNe-PpfeycufhHLPkpIXV8msewhm_Pf8I/edit")
SHEET_URL = str(secret("GSHEET_URL", "") or "").strip() or SHEET_URL_DEFAULT
SHEET_ID = _ambil_sheet_id(secret("GSHEET_ID", "")) or _ambil_sheet_id(SHEET_URL)
if not SHEET_URL and SHEET_ID:
    SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
GEMINI_API_KEY = secret("GEMINI_API_KEY")
GEMINI_MODEL = secret("GEMINI_MODEL", "gemini-2.0-flash")
# Foto disimpan ke Drive pribadi lewat Apps Script (lihat FotoAPI.gs)
FOTO_SCRIPT_URL = str(secret("FOTO_SCRIPT_URL", "") or "").strip()
FOTO_TOKEN = str(secret("FOTO_TOKEN", "") or "").strip()
# Master customer (sama dengan app Cek Koordinat), hanya dibaca
CUSTOMER_SHEET_ID = _ambil_sheet_id(
    secret("CUSTOMER_SHEET_ID", "113E5fKvZ0wWloSbQ9IQo8QiCOz5C3zlJr28012PbXz4"))

# Program poin: diatur di menu Sales > Atur Program (disimpan di sheet "Program").
# Daftar ini hanya isi awal saat sheet Program masih kosong.
PROGRAM_DEFAULT = [
    {"kode": "pembelian", "nama": "Pembelian Nota", "ikon": "🧾", "jenis": "pembelian",
     "poin": 100, "aktif": "ya", "deskripsi": "Poin per pack dari nota pembelian ke sales"},
]
PROGRAM_DIHAPUS = {"eceran", "sunblind", "stiker"}  # misi contoh lama, dibuang dari sheet

# Batas klaim misi per toko
FREKUENSI = {
    "harian": "1x per hari",
    "mingguan": "1x per minggu (Senin–Minggu)",
    "bulanan": "1x per bulan",
    "sekali": "1x selama program",
}

STATUS_LABEL = {
    "pending":  "⏳ Menunggu approval",
    "approved": "✅ Disetujui",
    "rejected": "❌ Ditolak",
    "diserahkan": "📦 Sudah diserahkan",
}

# Kolom tiap sheet
COLS = {
    "Toko": ["kode_toko", "nama_toko", "pemilik", "no_hp", "alamat", "pin_hash", "dibuat",
             "username", "zona", "rayon", "foto_profil_id"],
    "Transaksi": ["id", "kode_toko", "nama_toko", "no_nota", "tanggal", "jumlah_pack", "poin",
                  "status", "sumber", "deskripsi", "catatan", "sales", "diproses_oleh",
                  "diproses_pada", "dibuat", "foto_id", "foto_link", "ai_pack", "ai_baca",
                  "foto_mode", "foto_hash"],
    "Misi": ["id", "kode_toko", "nama_toko", "jenis", "minggu", "keterangan", "poin", "status",
             "catatan", "diproses_oleh", "diproses_pada", "dibuat", "foto_id", "foto_link",
             "foto_mode", "foto_hash"],
    "Penukaran": ["id", "kode_toko", "nama_toko", "tanggal", "poin", "keterangan", "dibuat"],
    "Program": ["kode", "nama", "ikon", "jenis", "poin", "aktif", "mulai", "selesai",
                "keterangan", "deskripsi", "diubah_oleh", "diubah_pada", "frekuensi"],
    "Hadiah": ["id", "nama", "poin", "stok", "deskripsi", "aktif", "foto_id", "foto_link",
               "dibuat", "diubah_oleh", "diubah_pada"],
    "Klaim": ["id", "kode_toko", "nama_toko", "hadiah_id", "nama_hadiah", "poin", "status",
              "catatan", "dibuat", "diproses_oleh", "diproses_pada", "diserahkan_oleh",
              "diserahkan_pada"],
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
    raw = st.secrets["gcp_json"]
    # Streamlit bisa membaca gcp_json sebagai dict (TOML table) atau string
    if isinstance(raw, str):
        info = json.loads(raw)
    else:
        # AttrDict / dict dari TOML table — konversi ke dict biasa,
        # buang key aplikasi (huruf besar) yang mungkin ikut masuk ke tabel
        info = {k: v for k, v in dict(raw).items() if not k.isupper()}
    if "private_key" in info:
        info["private_key"] = info["private_key"].replace("\\n", "\n")
    return Credentials.from_service_account_info(info, scopes=SCOPES)


@st.cache_resource
def _gc():
    return gspread.authorize(_creds())


@st.cache_resource
def _drive():
    return build("drive", "v3", credentials=_creds())


@st.cache_resource
def _wb():
    return _gc().open_by_key(SHEET_ID)


@st.cache_resource
def _header_sudah_dicek():
    return set()


# ================== MASTER CUSTOMER ==================
def _with_prefix(val, prefix):
    v = str(val).strip()
    if not v or v.lower() in ("nan", "none", "-"):
        return ""
    return v.upper() if v.upper().startswith(prefix) else f"{prefix} {v.upper()}"


def _cari_kolom(cols, *kandidat, exact=()):
    """Cari kolom: dulu yang namanya persis sama, lalu yang mengandung kata kunci."""
    norm = {c: c.lower().replace(" ", "").replace("_", "") for c in cols}
    for e in exact:
        for c, n in norm.items():
            if n == e:
                return c
    for k in kandidat:
        for c, n in norm.items():
            if k in n:
                return c
    return None


@st.cache_data(ttl=600, show_spinner="Memuat master customer...")
def load_customers():
    """Customer aktif dari master sheet: kode, nama, alamat, zona, rayon, label."""
    ws = _gc().open_by_key(CUSTOMER_SHEET_ID).sheet1
    rows = ws.get_all_values()
    if not rows:
        return pd.DataFrame(columns=["kode", "nama", "alamat", "zona", "rayon", "label"])
    headers = [str(h).strip() for h in rows[0]]
    df = pd.DataFrame(rows[1:], columns=headers)
    cols = df.columns.tolist()
    code_col = _cari_kolom(cols, "customerno", "kode", exact=("customerno", "kode")) or cols[0]
    name_col = _cari_kolom(cols, "nama", "toko", exact=("customer", "namatoko", "nama")) or cols[0]
    addr_col = _cari_kolom(cols, "alamat", "address")
    group_col = _cari_kolom(cols, "salesgroup")
    district_col = _cari_kolom(cols, "salesdistrict")
    status_col = _cari_kolom(cols, exact=("status",))

    if status_col:
        df = df[df[status_col].str.strip().str.lower() == "aktif"]

    out = pd.DataFrame({
        "kode": df[code_col].astype(str).str.strip(),
        "nama": df[name_col].astype(str).str.strip(),
        "alamat": df[addr_col].astype(str).str.strip() if addr_col else "",
        "zona": df[district_col].map(lambda v: _with_prefix(v, "ZONA")) if district_col else "",
        "rayon": df[group_col].map(lambda v: _with_prefix(v, "RAYON")) if group_col else "",
    })
    out = out[out.kode != ""].drop_duplicates("kode")

    def label(r):
        s = f"{r.nama} ({r.kode})"
        if r.alamat:
            s += f" — {r.alamat[:40]}"
        extra = " - ".join(x for x in (r.zona, r.rayon) if x)
        return s + (f" -- {extra}" if extra else "")

    out["label"] = out.apply(label, axis=1)
    return out.sort_values("nama").reset_index(drop=True)


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


def _foto_api(action, **kw):
    """Panggil Foto API (Apps Script) yang menyimpan foto di Drive pribadi pemilik."""
    if not (FOTO_SCRIPT_URL and FOTO_TOKEN):
        raise RuntimeError("FOTO_SCRIPT_URL / FOTO_TOKEN belum diisi di Secrets")
    r = requests.post(FOTO_SCRIPT_URL, json={"token": FOTO_TOKEN, "action": action, **kw},
                      timeout=90)
    try:
        hasil = r.json()
    except ValueError:
        raise RuntimeError("Balasan Foto API tidak terbaca. Cek URL /exec dan akses "
                           "'Siapa saja' pada deployment Apps Script.")
    if not hasil.get("ok"):
        raise RuntimeError(hasil.get("error", "Foto API gagal"))
    return hasil


def _upload_foto(data: bytes, filename: str, sheet_nama: str) -> tuple[str, str]:
    """Upload foto ke Drive pribadi lewat Apps Script. Returns (file_id, link)."""
    h = _foto_api("upload", b64=base64.b64encode(data).decode(), mime="image/jpeg",
                  name=filename, folder=FOLDER_MAP.get(sheet_nama, sheet_nama))
    return h["id"], h.get("url", "")


# ================== DATABASE: GOOGLE SHEETS ==================

@st.cache_data(ttl=180, show_spinner="Memuat data...")
def _muat_semua():
    wb = _wb()
    result = {}
    ada = {ws.title for ws in wb.worksheets()}
    for nama, cols in COLS.items():
        if nama not in ada:
            wb.add_worksheet(nama, rows=1000, cols=len(cols)).update([cols], "A1")
    # semua sheet dibaca sekaligus dalam 1 permintaan ke Google (jauh lebih cepat)
    hasil = wb.values_batch_get([f"'{n}'" for n in COLS]).get("valueRanges", [])
    for nama, vr in zip(COLS, hasil):
        cols = COLS[nama]
        rows = vr.get("values", [])
        if len(rows) < 2:
            # Pastikan header ada
            if not rows:
                wb.worksheet(nama).update([cols], "A1")
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
    # Pastikan header row benar (cukup dicek sekali per sheet)
    cek = _header_sudah_dicek()
    if nama not in cek:
        if ws.row_values(1) != COLS[nama]:
            ws.update([COLS[nama]], "A1")
        cek.add(nama)
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
                # jangan simpan data tanpa foto; pesan tetap tampil (tidak hilang oleh rerun)
                st.error(f"❌ Foto gagal disimpan ke Drive, data BELUM terkirim. Detail: {e}")
                st.stop()
        baris = []
        for c in cols:
            v = row.get(c, "")
            if c in KOLOM_ANGKA and v != "":
                try:
                    v = int(float(v))
                except Exception:
                    v = ""
            baris.append(v if v is not None else "")
        ws.append_row(baris, value_input_option="RAW")  # RAW: angka 0 di depan (no HP, kode) tidak hilang
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
                    _foto_api("delete", id=fid)
                except Exception:
                    pass
        ws.delete_rows(r)
    segarkan()


@st.cache_data(ttl=3600, show_spinner="Memuat foto...")
def ambil_foto(foto_id: str):
    if not foto_id:
        return None
    return base64.b64decode(_foto_api("get", id=foto_id)["b64"])


# Folder foto di Drive dibagikan "Siapa saja yang punya link", jadi browser bisa memuat
# foto langsung dari Drive (cepat). Set False jika folder dibuat terbatas (lebih lambat).
FOTO_LINK_LANGSUNG = True


def url_foto(foto_id, lebar=1000):
    return f"https://drive.google.com/thumbnail?id={foto_id}&sz=w{lebar}"


def tampil_foto(foto_id):
    if not foto_id:
        st.caption("📷 (tidak ada foto tersimpan)")
        return
    if FOTO_LINK_LANGSUNG:
        st.markdown(
            f'<img src="{url_foto(foto_id)}" loading="lazy" '
            f'style="width:100%;border-radius:12px;background:rgba(255,255,255,.08);min-height:80px">'
            f'<div style="font-size:.8rem;margin-top:4px"><a href="https://drive.google.com/file/d/'
            f'{foto_id}/view" target="_blank" style="color:#dbe9ff">🔗 Buka foto ukuran penuh</a>'
            f'</div>', unsafe_allow_html=True)
        return
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


# ================== FOTO LANGSUNG (anti-curang) ==================
_BROWSER_DALAM_APP = ("FBAN", "FBAV", "Instagram", "Line/", "WhatsApp", "; wv)",
                      "MicroMessenger", "TikTok", "musical_ly")


def _browser_dalam_app():
    try:
        ua = st.context.headers.get("User-Agent", "") or ""
    except Exception:
        return False
    return any(k in ua for k in _BROWSER_DALAM_APP)


def foto_sudah_dipakai(h):
    return bool(h) and any((tabel(n).foto_hash == h).any() for n in ("Transaksi", "Misi"))


def panduan_kamera():
    with st.expander("❓ Kamera tidak muncul / minta izin terus?"):
        st.markdown(
            "1. Saat muncul pertanyaan izin kamera, pilih **Izinkan / Allow**.\n"
            "2. Pernah menolak? Ketuk ikon 🔒 di samping alamat web → **Izin / Permissions** → "
            "**Kamera** → **Izinkan**, lalu muat ulang halaman.\n"
            "3. Buka link di **Google Chrome** (Android) atau **Safari** (iPhone), jangan dari "
            "dalam WhatsApp / Instagram / Facebook.\n"
            "4. Tutup aplikasi lain yang sedang memakai kamera, lalu muat ulang."
        )


def ambil_foto_langsung(key, label):
    """Foto real-time lewat kamera langsung. Return dict {bytes, mode, hash} atau None."""
    if _browser_dalam_app():
        st.warning("⚠️ Anda membuka aplikasi dari dalam WhatsApp/Instagram/Facebook. Kamera "
                   "sering tidak bisa dibuka di sini. Ketuk ⋮ lalu pilih **Buka di Chrome / "
                   "Open in browser**.")
    f = st.camera_input(label, key=f"{key}_cam")
    panduan_kamera()
    if f is None:
        return None
    raw = f.getvalue()
    return {"bytes": raw, "mode": "kamera langsung", "hash": hashlib.md5(raw).hexdigest()}


# ================== PROGRAM POIN ==================
def program_df():
    """Semua program dari sheet Program (diisi default kalau masih kosong)."""
    df = tabel("Program")
    if df.empty:
        ws = _ws_ensure("Program")
        ws.append_rows([[r.get(c, "") for c in COLS["Program"]] for r in PROGRAM_DEFAULT],
                       value_input_option="RAW")
        segarkan()
        df = tabel("Program")
    if df.kode.isin(PROGRAM_DIHAPUS).any():
        simpan_program(df[~df.kode.isin(PROGRAM_DIHAPUS)][COLS["Program"]].values.tolist())
        df = tabel("Program")
    df = df[df.kode.str.strip() != ""].drop_duplicates("kode").copy()
    df["aktif"] = df.aktif.str.strip().str.lower()
    df["frekuensi"] = df.frekuensi.str.strip().str.lower().where(
        df.frekuensi.str.strip().str.lower().isin(FREKUENSI), "mingguan")
    return df.reset_index(drop=True)


def simpan_program(rows):
    """Tulis ulang seluruh sheet Program."""
    ws = _ws_ensure("Program")
    ws.clear()
    bersih = [[v.item() if hasattr(v, "item") else v for v in r] for r in rows]  # numpy -> python
    ws.update([COLS["Program"]] + bersih, "A1", value_input_option="RAW")
    segarkan()


def periode_program(prog):
    m, s_ = prog.get("mulai", ""), prog.get("selesai", "")
    if m and s_:
        return f"📅 {tgl_id(m)} – {tgl_id(s_)}"
    if s_:
        return f"📅 s/d {tgl_id(s_)}"
    if m:
        return f"📅 Mulai {tgl_id(m)}"
    return "📅 Tanpa batas waktu"


def judul_program(judul, prog, level=3):
    """Judul di kiri, periode program di kanan atas."""
    st.markdown(
        f"""<div style="display:flex;justify-content:space-between;align-items:flex-start;
        gap:10px;flex-wrap:wrap"><div style="font-size:{1.6 if level == 3 else 2}rem;
        font-weight:900;line-height:1.2">{judul}</div>
        <div style="background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.5);
        border-radius:999px;padding:4px 12px;font-size:.85rem;white-space:nowrap">
        {periode_program(prog)}</div></div>""", unsafe_allow_html=True)


def _status_program(r):
    hari = today_wib().isoformat()
    if r["aktif"] != "ya":
        return "⏸️ Nonaktif"
    if r["mulai"] and hari < r["mulai"][:10]:
        return "🕒 Belum mulai"
    if r["selesai"] and hari > r["selesai"][:10]:
        return "⛔ Berakhir"
    return "🟢 Berjalan"


def program_berjalan(kode):
    df = program_df()
    r = df[df.kode == kode]
    return None if r.empty or _status_program(r.iloc[0]) != "🟢 Berjalan" else r.iloc[0].to_dict()


def misi_berjalan():
    df = program_df()
    return [r.to_dict() for _, r in df[df.jenis == "misi"].iterrows()
            if _status_program(r) == "🟢 Berjalan"]


def poin_per_pack():
    df = program_df()
    r = df[df.kode == "pembelian"]
    return int(r.poin.iloc[0]) if len(r) and r.poin.iloc[0] > 0 else POIN_PER_PACK


def label_misi(kode):
    df = program_df()
    r = df[df.kode == kode]
    if len(r):
        return f"{r.ikon.iloc[0]} {r.nama.iloc[0]}"
    lama = {"eceran": "🚬 Sedia Eceran", "sunblind": "⛱️ POSM Sunblind", "stiker": "🏷️ POSM Stiker"}
    return lama.get(kode, f"🎯 {kode}")


def tgl_id(iso):
    try:
        return datetime.fromisoformat(str(iso)[:10]).strftime("%d-%m-%Y")
    except ValueError:
        return ""


def papan_peringkat():
    """Peringkat semua toko berdasarkan total poin terkumpul sepanjang waktu."""
    toko = tabel("Toko")[["kode_toko", "nama_toko"]].copy()
    t, m = tabel("Transaksi"), tabel("Misi")
    total = pd.concat([t[t.status == "approved"][["kode_toko", "poin"]],
                       m[m.status == "approved"][["kode_toko", "poin"]]])
    toko["total"] = toko.kode_toko.map(total.groupby("kode_toko").poin.sum()).fillna(0).astype(int)
    toko = toko.sort_values("total", ascending=False).reset_index(drop=True)
    toko["peringkat"] = toko.total.rank(method="min", ascending=False).astype(int)
    return toko


def tampil_leaderboard(kode_saya):
    st.markdown("### 🏆 Papan Juara Poin")
    lb = papan_peringkat()
    juara = lb[lb.total > 0].head(3)
    if juara.empty:
        st.info("Belum ada juara. Kumpulkan poin dan jadilah toko pertama di puncak! 💪")
        return
    medali = ["🥇", "🥈", "🥉"]
    kolom = st.columns(3)
    for i, r in enumerate(juara.itertuples()):
        saya = " (Anda)" if r.kode_toko == kode_saya else ""
        kolom[i].markdown(
            f"""<div style="text-align:center;border:1.5px solid rgba(255,255,255,.7);
            border-radius:18px;padding:14px 6px;background:linear-gradient(180deg,
            rgba(74,155,240,.45),rgba(13,47,134,.6));">
            <div style="font-size:2rem;line-height:1">{medali[i]}</div>
            <div style="display:flex;justify-content:center;margin-top:6px">
            {avatar_html(foto_profil_toko(r.kode_toko), r.nama_toko, 64)}</div>
            <div style="font-weight:900;margin-top:6px">{r.nama_toko}{saya}</div>
            <div style="color:#dbe9ff">{poin(r.total)}</div></div>""",
            unsafe_allow_html=True)
    me = lb[lb.kode_toko == kode_saya]
    if me.empty:
        return
    rank, total = int(me.peringkat.iloc[0]), int(me.total.iloc[0])
    atas = lb[lb.total > total]
    if atas.empty:
        if total > 0:
            st.success(f"👑 Toko Anda **juara {rank}** dari {len(lb)} toko! Pertahankan!")
        return
    di_atas = atas.iloc[-1]  # toko dengan poin terdekat di atas kita
    selisih = int(di_atas.total) - total
    teks = (f"Toko Anda di peringkat **{rank}** dari {len(lb)} toko. Selisih "
            f"**{poin(selisih)}** dengan peringkat {int(di_atas.peringkat)} "
            f"(**{di_atas.nama_toko}**). Ayo kejar! 🚀")
    (st.success if total > 0 and rank <= 3 else st.info)(teks)


# ================== FOTO PROFIL ==================
def potong_persegi(raw, ukuran=640):
    """Potong tengah jadi persegi & kecilkan (seperti foto profil WhatsApp)."""
    img = Image.open(io.BytesIO(raw))
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    img = img.convert("RGB")
    sisi = min(img.size)
    kiri, atas = (img.width - sisi) // 2, (img.height - sisi) // 2
    img = img.crop((kiri, atas, kiri + sisi, atas + sisi)).resize((ukuran, ukuran), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def avatar_html(foto_id, nama, ukuran=56):
    """Lingkaran foto profil; inisial nama jika belum ada foto."""
    gaya = (f"width:{ukuran}px;height:{ukuran}px;border-radius:50%;object-fit:cover;"
            f"border:2px solid #fff;flex-shrink:0")
    if foto_id and FOTO_LINK_LANGSUNG:
        return f'<img src="{url_foto(foto_id, 300)}" loading="lazy" style="{gaya}">'
    b = None
    if foto_id:
        try:
            b = ambil_foto(foto_id)
        except Exception:
            b = None
    if b:
        return (f'<img src="data:image/jpeg;base64,{base64.b64encode(b).decode()}" '
                f'style="{gaya}">')
    inisial = "".join(w[0] for w in str(nama).split()[:2]).upper() or "?"
    return (f'<div style="{gaya};display:flex;align-items:center;justify-content:center;'
            f'background:#4a9bf0;font-weight:900;font-size:{ukuran * 0.4:.0f}px">{inisial}</div>')


def foto_profil_toko(kode):
    t = tabel("Toko")
    r = t[t.kode_toko == kode]
    return r.foto_profil_id.iloc[0] if len(r) else ""


def toko_profil():
    kode = st.session_state.kode_toko
    st.subheader("👤 Profil Toko")
    fid = foto_profil_toko(kode)
    st.markdown(
        f'<div style="display:flex;justify-content:center;margin:10px 0 6px">'
        f'{avatar_html(fid, st.session_state.nama, 160)}</div>'
        f'<div style="text-align:center;font-weight:900;font-size:1.3rem">'
        f'{st.session_state.nama}</div>', unsafe_allow_html=True)

    st.markdown("**Ganti foto profil**")
    v = st.session_state.setdefault("profil_ver", 0)
    cara = st.radio("Ambil dari", ["🖼️ Galeri", "📷 Kamera"], horizontal=True,
                    key=f"profil_cara{v}")
    f = (st.file_uploader("Pilih foto", type=["jpg", "jpeg", "png"], key=f"profil_up{v}")
         if cara.startswith("🖼️") else st.camera_input("Foto profil", key=f"profil_cam{v}"))
    if f is not None:
        try:
            data = potong_persegi(f.getvalue())
        except Exception:
            st.error("File ini bukan gambar yang bisa dibaca. Pilih foto lain.")
            return
        st.markdown(
            f'<div style="display:flex;justify-content:center">'
            f'<img src="data:image/jpeg;base64,{base64.b64encode(data).decode()}" '
            f'style="width:160px;height:160px;border-radius:50%;object-fit:cover;'
            f'border:2px solid #fff"></div>', unsafe_allow_html=True)
        st.caption("Pratinjau: foto dipotong bulat seperti foto profil WhatsApp.")
        if st.button("💾 Simpan foto profil", type="primary", use_container_width=True):
            with st.spinner("Menyimpan..."):
                try:
                    baru, _ = _upload_foto(data, f"{kode}_profil_{now_wib():%Y%m%d_%H%M%S}.jpg",
                                           "Profil")
                except Exception as e:
                    st.error(f"❌ Foto profil gagal disimpan: {e}")
                    return
                ubah("Toko", "kode_toko", kode, {"foto_profil_id": baru})
                if fid:
                    try:
                        _foto_api("delete", id=fid)
                    except Exception:
                        pass
            st.session_state.profil_ver = v + 1
            st.toast("Foto profil diperbarui!")
            st.rerun()


# ================== HADIAH ==================
KLAIM_TERPAKAI = ["pending", "approved", "diserahkan"]  # status klaim yang memakai poin


def stok_tersisa(h):
    """None = tanpa batas; selain itu stok dikurangi klaim yang masih menunggu."""
    stok = str(h["stok"]).strip()
    if stok == "":
        return None
    k = tabel("Klaim")
    menunggu = len(k[(k.hadiah_id == str(h["id"])) & (k.status == "pending")])
    return max(int(float(stok)) - menunggu, 0)


def kartu_hadiah_html(h, sisa):
    foto = (f'<img src="{url_foto(h["foto_id"], 600)}" loading="lazy" style="width:100%;'
            f'aspect-ratio:1/1;object-fit:cover;border-radius:14px">' if h["foto_id"] else
            '<div style="aspect-ratio:1/1;display:flex;align-items:center;justify-content:center;'
            'font-size:3rem;background:rgba(255,255,255,.1);border-radius:14px">🎁</div>')
    stok = "Stok tanpa batas" if sisa is None else ("❌ Habis" if sisa == 0 else f"Sisa {sisa}")
    return (f'<div style="border:1.5px solid rgba(255,255,255,.7);border-radius:18px;'
            f'padding:10px;background:linear-gradient(180deg,rgba(74,155,240,.35),'
            f'rgba(13,47,134,.55))">{foto}<div style="font-weight:900;margin-top:8px">'
            f'{h["nama"]}</div><div style="font-size:1.15rem;font-weight:800">'
            f'🪙 {poin(h["poin"])}</div><div style="font-size:.8rem;color:#dbe9ff">{stok}</div></div>')


def toko_hadiah():
    kode = st.session_state.kode_toko
    st.subheader("🎁 Tukar Poin dengan Hadiah")
    if msg := st.session_state.pop("hadiah_ok", None):
        st.success(msg)
    _, _, saldo = saldo_toko(kode)
    st.metric("💰 Saldo poin Anda", poin(saldo))

    hd = tabel("Hadiah")
    hd = hd[hd.aktif.str.lower() == "ya"].sort_values("poin")
    if hd.empty:
        st.info("Belum ada hadiah yang tersedia. Pantau terus ya! 🙏")
    kolom = st.columns(2)
    for i, (_, h) in enumerate(hd.iterrows()):
        with kolom[i % 2]:
            sisa = stok_tersisa(h)
            st.markdown(kartu_hadiah_html(h, sisa), unsafe_allow_html=True)
            if h["deskripsi"]:
                st.caption(h["deskripsi"])
            kurang = int(h["poin"]) - saldo
            if sisa == 0:
                st.button("Stok habis", key=f"th_{h['id']}", disabled=True,
                          use_container_width=True)
            elif kurang > 0:
                st.button(f"Kurang {poin(kurang)}", key=f"th_{h['id']}", disabled=True,
                          use_container_width=True)
            elif st.session_state.get("hadiah_konfirmasi") == h["id"]:
                st.warning(f"Tukar {poin(h['poin'])} dengan **{h['nama']}**?")
                b1, b2 = st.columns(2)
                if b1.button("✅ Ya, tukar", key=f"ya_{h['id']}", type="primary",
                             use_container_width=True):
                    segarkan()  # cek ulang saldo & stok terbaru
                    _, _, saldo_baru = saldo_toko(kode)
                    if saldo_baru < int(h["poin"]) or stok_tersisa(h) == 0:
                        st.error("Saldo atau stok sudah berubah. Coba lagi.")
                    else:
                        tambah("Klaim", {
                            "kode_toko": kode, "nama_toko": st.session_state.nama,
                            "hadiah_id": str(h["id"]), "nama_hadiah": h["nama"],
                            "poin": int(h["poin"]), "status": "pending",
                            "dibuat": now_wib().isoformat(timespec="seconds"),
                        })
                        st.session_state.pop("hadiah_konfirmasi", None)
                        st.session_state.hadiah_ok = (f"✅ Pengajuan {h['nama']} terkirim. "
                                                      "Poin ditahan sampai SPV menyetujui.")
                        st.rerun()
                if b2.button("Batal", key=f"no_{h['id']}", use_container_width=True):
                    st.session_state.pop("hadiah_konfirmasi", None)
                    st.rerun()
            elif st.button("🎁 Tukar", key=f"th_{h['id']}", type="primary",
                           use_container_width=True):
                st.session_state.hadiah_konfirmasi = h["id"]
                st.rerun()

    k = tabel("Klaim")
    riwayat = k[k.kode_toko == kode].sort_values("dibuat", ascending=False)
    if not riwayat.empty:
        st.divider()
        st.subheader("Riwayat Tukar Hadiah")
        st.dataframe(pd.DataFrame({
            "Tanggal": riwayat.dibuat.str[:10], "Hadiah": riwayat.nama_hadiah,
            "Poin": riwayat.poin, "Status": riwayat.status.map(STATUS_LABEL),
            "Catatan": riwayat.catatan}), hide_index=True, use_container_width=True)
        st.caption("Jika pengajuan ditolak, poin otomatis kembali ke saldo Anda.")


def sales_approval_hadiah():
    k = tabel("Klaim")
    pend = k[k.status == "pending"].sort_values("dibuat")
    if pend.empty:
        st.success("Tidak ada pengajuan hadiah yang menunggu.")
    hd = tabel("Hadiah")
    for _, r in pend.iterrows():
        with st.container(border=True):
            st.markdown(f"**{r.nama_toko}** ({r.kode_toko}) · diajukan {waktu(r.dibuat)}")
            st.write(f"🎁 **{r.nama_hadiah}** · {poin(r.poin)}")
            h = hd[hd.id == r.hadiah_id]
            if len(h) and h.foto_id.iloc[0]:
                st.markdown(f'<img src="{url_foto(h.foto_id.iloc[0], 400)}" style="width:160px;'
                            f'border-radius:12px">', unsafe_allow_html=True)
            catatan = st.text_input("Catatan / alasan ditolak", key=f"hct{r.id}").strip()
            b1, b2 = st.columns(2)
            if b1.button("✅ Setujui", key=f"hok{r.id}", type="primary", use_container_width=True):
                ubah("Klaim", "id", r.id, {
                    "status": "approved", "catatan": catatan,
                    "diproses_oleh": st.session_state.nama,
                    "diproses_pada": now_wib().isoformat(timespec="seconds")})
                if len(h) and str(h.stok.iloc[0]).strip() != "":
                    ubah("Hadiah", "id", r.hadiah_id,
                         {"stok": str(max(int(float(h.stok.iloc[0])) - 1, 0))})
                st.toast(f"Disetujui: {r.nama_hadiah} untuk {r.nama_toko}")
                st.rerun()
            if b2.button("❌ Tolak", key=f"hno{r.id}", use_container_width=True):
                if not catatan:
                    st.error("Isi alasan penolakan dulu supaya toko tahu.")
                else:
                    ubah("Klaim", "id", r.id, {
                        "status": "rejected", "catatan": catatan,
                        "diproses_oleh": st.session_state.nama,
                        "diproses_pada": now_wib().isoformat(timespec="seconds")})
                    st.toast(f"Ditolak, {poin(r.poin)} kembali ke {r.nama_toko}")
                    st.rerun()

    siap = k[k.status == "approved"].sort_values("diproses_pada")
    if not siap.empty:
        st.markdown("### 📦 Menunggu diserahkan ke toko")
        for _, r in siap.iterrows():
            c1, c2 = st.columns([3, 1])
            c1.write(f"**{r.nama_hadiah}** → {r.nama_toko} ({r.kode_toko}) · disetujui "
                     f"{waktu(r.diproses_pada)}")
            if c2.button("📦 Sudah diserahkan", key=f"hsr{r.id}", use_container_width=True):
                ubah("Klaim", "id", r.id, {
                    "status": "diserahkan", "diserahkan_oleh": st.session_state.nama,
                    "diserahkan_pada": now_wib().isoformat(timespec="seconds")})
                st.rerun()


def sales_hadiah():
    st.subheader("🎁 Atur Hadiah")
    st.caption("Hadiah aktif tampil di menu toko. Stok kosong = tanpa batas. Stok berkurang "
               "otomatis saat pengajuan disetujui.")
    if msg := st.session_state.pop("hadiah_admin_ok", None):
        st.success(msg)
    hd = tabel("Hadiah")

    if not hd.empty:
        kolom = st.columns(3)
        for i, (_, h) in enumerate(hd.iterrows()):
            with kolom[i % 3]:
                st.markdown(kartu_hadiah_html(h, stok_tersisa(h)), unsafe_allow_html=True)
                if h["aktif"].lower() != "ya":
                    st.caption("⏸️ Nonaktif (tidak tampil di toko)")

        st.markdown("### ✏️ Edit hadiah")
        opsi = {f"{r.nama} · {poin(r.poin)}": r.id for r in hd.itertuples()}
        hid = opsi[st.selectbox("Pilih hadiah", list(opsi.keys()), key="edit_hadiah_pilih")]
        h = hd[hd.id == hid].iloc[0]
        with st.container(border=True):
            nama = st.text_input("Nama hadiah *", value=h["nama"], key=f"h_nama_{hid}").strip()
            c1, c2 = st.columns(2)
            harga = c1.number_input("Harga (poin) *", min_value=1, value=max(int(h["poin"]), 1),
                                    step=100, key=f"h_poin_{hid}")
            aktif = c2.toggle("Tampil di toko", value=h["aktif"].lower() == "ya",
                              key=f"h_aktif_{hid}")
            tanpa_stok = st.checkbox("Stok tanpa batas", value=str(h["stok"]).strip() == "",
                                     key=f"h_ts_{hid}")
            stok = None if tanpa_stok else st.number_input(
                "Stok", min_value=0, value=int(float(h["stok"] or 0)), step=1, key=f"h_stok_{hid}")
            desk = st.text_area("Deskripsi", value=h["deskripsi"], key=f"h_desk_{hid}").strip()
            foto = st.file_uploader("Ganti foto (opsional)", type=["jpg", "jpeg", "png"],
                                    key=f"h_foto_{hid}")
            b1, b2 = st.columns(2)
            if b1.button("💾 Simpan perubahan", type="primary", use_container_width=True,
                         key=f"h_simpan_{hid}"):
                if not nama:
                    st.error("Nama hadiah wajib diisi.")
                else:
                    fields = {"nama": nama, "poin": int(harga), "aktif": "ya" if aktif else "tidak",
                              "stok": "" if stok is None else str(int(stok)), "deskripsi": desk,
                              "diubah_oleh": st.session_state.nama,
                              "diubah_pada": now_wib().isoformat(timespec="seconds")}
                    if foto is not None:
                        with st.spinner("Mengunggah foto..."):
                            fid, furl = _upload_foto(compress_image(foto),
                                                     f"hadiah_{hid}_{now_wib():%Y%m%d_%H%M%S}.jpg",
                                                     "Hadiah")
                        fields.update(foto_id=fid, foto_link=furl)
                    ubah("Hadiah", "id", hid, fields)
                    st.session_state.hadiah_admin_ok = f"✅ Hadiah {nama} tersimpan."
                    st.rerun()
            if b2.button("🗑️ Hapus hadiah", use_container_width=True, key=f"h_hapus_{hid}"):
                hapus("Hadiah", "id", hid)
                st.session_state.hadiah_admin_ok = f"Hadiah {h['nama']} dihapus."
                st.rerun()

    with st.expander("➕ Tambah hadiah baru", expanded=hd.empty):
        vb = st.session_state.setdefault("hadiah_baru_ver", 0)
        nama = st.text_input("Nama hadiah *", placeholder="mis. Payung Wismilak",
                             key=f"hb_nama_{vb}").strip()
        c1, c2 = st.columns(2)
        harga = c1.number_input("Harga (poin) *", min_value=1, value=5000, step=100,
                                key=f"hb_poin_{vb}")
        tanpa_stok = c2.checkbox("Stok tanpa batas", value=False, key=f"hb_ts_{vb}")
        stok = None if tanpa_stok else c2.number_input("Stok", min_value=0, value=10, step=1,
                                                        key=f"hb_stok_{vb}")
        desk = st.text_area("Deskripsi", placeholder="mis. Payung lipat ukuran besar",
                            key=f"hb_desk_{vb}").strip()
        cara = st.radio("Foto hadiah", ["🖼️ Galeri", "📷 Kamera"], horizontal=True,
                        key=f"hb_cara_{vb}")
        foto = (st.file_uploader("Pilih foto", type=["jpg", "jpeg", "png"], key=f"hb_up_{vb}")
                if cara.startswith("🖼️") else st.camera_input("Foto hadiah", key=f"hb_cam_{vb}"))
        if st.button("➕ Tambah Hadiah", type="primary", use_container_width=True,
                     key=f"hb_simpan_{vb}"):
            if not nama:
                st.error("Nama hadiah wajib diisi.")
            elif foto is None:
                st.error("Tambahkan foto hadiah supaya toko tahu bentuknya.")
            else:
                now = now_wib()
                tambah("Hadiah", {
                    "nama": nama, "poin": int(harga),
                    "stok": "" if stok is None else str(int(stok)), "deskripsi": desk,
                    "aktif": "ya", "dibuat": now.isoformat(timespec="seconds"),
                    "diubah_oleh": st.session_state.nama,
                    "diubah_pada": now.isoformat(timespec="seconds"),
                }, foto=foto, nama_foto=f"hadiah_{now:%Y%m%d_%H%M%S}")
                st.session_state.hadiah_baru_ver = vb + 1
                st.session_state.hadiah_admin_ok = f"✅ Hadiah {nama} ditambahkan."
                st.rerun()


# ================== PILIHAN IKON ==================
IKON_PILIHAN = [
    # rokok & toko
    "🚬", "🏪", "🛒", "🧾", "📦", "🏬", "🏠", "🛍️", "💳", "🏷️", "🪧", "📋",
    # promosi / POSM
    "📌", "📍", "📣", "📢", "🎪", "⛱️", "🪟", "🖼️", "🎏", "🎌", "🚩", "🏁",
    # hadiah & poin
    "🎁", "🎉", "🎊", "🏆", "🥇", "🏅", "🎖️", "👑", "💎", "💰", "💵", "🪙",
    # semangat
    "⭐", "🌟", "✨", "🔥", "⚡", "🚀", "💪", "👍", "🎯", "✅", "💯", "❤️",
    # foto & kegiatan
    "📷", "📸", "🤳", "👀", "🔍", "📅", "🗓️", "⏰", "🔔", "💡", "🤝", "🙏",
    # lain-lain
    "☕", "🥤", "🍜", "🛵", "🚚", "🏍️", "🌙", "☀️", "🌈", "🍀", "🎲", "🎮",
]


def pilih_ikon(key, awal="📌"):
    """Pilih ikon dari daftar emoji (klik), atau ketik emoji sendiri."""
    opsi = IKON_PILIHAN if awal in IKON_PILIHAN else [awal] + IKON_PILIHAN
    if hasattr(st, "pills"):
        pilih = st.pills("Ikon program (klik salah satu)", opsi, default=awal, key=f"{key}_pills")
    else:
        pilih = st.selectbox("Ikon program", opsi, index=opsi.index(awal), key=f"{key}_sel")
    lain = st.text_input("…atau ketik/tempel emoji lain (opsional)", key=f"{key}_lain",
                         max_chars=8).strip()
    return lain or pilih or awal


# ================== HELPER ==================
def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


def saldo_toko(kode):
    t, m, p = tabel("Transaksi"), tabel("Misi"), tabel("Penukaran")
    beli = t[(t.kode_toko == kode) & (t.status == "approved")].poin.sum()
    misi = m[(m.kode_toko == kode) & (m.status == "approved")].poin.sum()
    k = tabel("Klaim")
    keluar = (p[p.kode_toko == kode].poin.sum()
              + k[(k.kode_toko == kode) & k.status.isin(KLAIM_TERPAKAI)].poin.sum())
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


def periode_klaim(frek):
    """(kunci periode, teks kapan dibuka lagi) sesuai batas klaim program."""
    hari = today_wib()
    if frek == "harian":
        return hari.isoformat(), f"besok, {hari + timedelta(days=1):%d-%m-%Y}"
    if frek == "bulanan":
        awal_depan = (hari.replace(day=28) + timedelta(days=4)).replace(day=1)
        return f"{hari:%Y-%m}", f"tanggal {awal_depan:%d-%m-%Y}"
    if frek == "sekali":
        return "SEKALI", ""
    kunci, senin_depan = minggu_ini()
    return kunci, f"Senin, {senin_depan:%d-%m-%Y}"


def status_misi_periode(kode, prog):
    """Status klaim toko untuk periode berjalan (None = masih bisa klaim)."""
    kunci, _ = periode_klaim(prog["frekuensi"])
    m = tabel("Misi")
    m = m[(m.kode_toko == kode) & (m.jenis == prog["kode"])
          & m.status.isin(["pending", "approved"])]
    if prog["frekuensi"] == "sekali":
        mulai = str(prog.get("mulai", ""))[:10]
        if mulai:
            m = m[m.dibuat.str[:10] >= mulai]
    else:
        m = m[m.minggu == kunci]
    if m.empty:
        return None
    return "approved" if (m.status == "approved").any() else "pending"


def angka(x):
    return f"{int(x or 0):,}".replace(",", ".")


def poin(x):
    return f"{angka(x)} poin"


def waktu(s):
    return str(s)[:16].replace("T", " ")


# ================== SESI LOGIN (tetap login walau di-refresh) ==================
SESI_HARI = 30  # lama login tersimpan


def _kunci_sesi():
    return str(secret("SESSION_SECRET", "") or (FOTO_TOKEN + SHEET_ID + "poin-toko")).encode()


def _tanda(teks):
    return hmac.new(_kunci_sesi(), teks.encode(), hashlib.sha256).hexdigest()[:32]


def buat_sesi(role, ident):
    """Simpan token login bertanda tangan di URL (?s=...), jadi refresh tidak logout."""
    isi = f"{role}|{ident}|{int(time.time()) + SESI_HARI * 86400}"
    st.query_params["s"] = (base64.urlsafe_b64encode(isi.encode()).decode().rstrip("=")
                            + "." + _tanda(isi))


def pulihkan_sesi():
    """Kalau halaman di-refresh, login dipulihkan dari token di URL."""
    if st.session_state.get("role"):
        return
    token = st.query_params.get("s", "")
    try:
        bagian, tanda = token.rsplit(".", 1)
        isi = base64.urlsafe_b64decode(bagian + "=" * (-len(bagian) % 4)).decode()
        role, ident, exp = isi.split("|")
        if not hmac.compare_digest(tanda, _tanda(isi)) or int(exp) < time.time():
            raise ValueError
    except Exception:
        if token:
            st.query_params.pop("s", None)
        return
    if role == "sales" and ident in SALES_AKUN:
        st.session_state.update(role="sales", nama=ident)
    elif role == "toko":
        t = tabel("Toko")
        r = t[t.kode_toko == ident]
        if len(r):
            st.session_state.update(role="toko", kode_toko=ident, nama=r.nama_toko.iloc[0])


def pilih_menu(daftar):
    """Menu sidebar yang juga diingat di URL (?m=...)."""
    awal = st.query_params.get("m", daftar[0])
    menu = st.sidebar.radio("Menu", daftar,
                            index=daftar.index(awal) if awal in daftar else 0)
    if st.query_params.get("m") != menu:
        st.query_params["m"] = menu
    return menu


# ================== LOGIN ==================
def halaman_login():
    st.markdown(
        f"""<div class="logo-wrap">
              <div class="logo"><b>POIN</b><i>toko</i></div>
              <div class="logo-sub">Kumpulkan poin & jadi juara toko 🏆</div>
            </div>""",
        unsafe_allow_html=True,
    )
    tab_toko, tab_sales = st.tabs(["🏪 Masuk sebagai Toko", "🧑‍💼 Masuk sebagai Sales"])

    with tab_toko:
        with st.form("login_toko"):
            user = st.text_input("Username").strip().lower()
            pw = st.text_input("Password (Customer No.)", type="password").strip().upper()
            if st.form_submit_button("Masuk", use_container_width=True):
                segarkan()
                t = tabel("Toko")
                r = t[(t.username.str.strip().str.lower() == user)
                      & (t.kode_toko.str.strip().str.upper() == pw)]
                if user and pw and len(r):
                    st.session_state.update(role="toko", kode_toko=r.kode_toko.iloc[0],
                                            nama=r.nama_toko.iloc[0])
                    buat_sesi("toko", r.kode_toko.iloc[0])
                    st.rerun()
                else:
                    st.error("Username atau password salah.")

    with tab_sales:
        with st.form("login_sales"):
            nama = st.text_input("Username").strip().lower()
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Masuk", use_container_width=True):
                if nama in SALES_AKUN and pw == SALES_AKUN[nama]:
                    st.session_state.update(role="sales", nama=nama)
                    buat_sesi("sales", nama)
                    st.rerun()
                else:
                    st.error("Username atau password salah.")

    st.markdown(
        '<div class="footer-note">Kumpulkan poin dari setiap pembelian, '
        'eceran, dan POSM.<br>Tukarkan poin Anda ke sales kami.</div>',
        unsafe_allow_html=True,
    )


def tombol_keluar():
    with st.sidebar:
        if st.session_state.get("role") == "toko":
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">'
                f'{avatar_html(foto_profil_toko(st.session_state.kode_toko), st.session_state.nama, 52)}'
                f'<b>{st.session_state.nama}</b></div>', unsafe_allow_html=True)
        else:
            st.write(f"👤 **{st.session_state.nama}**")
        if st.button("🔄 Muat ulang data", use_container_width=True):
            segarkan()
            st.rerun()
        if st.button("Keluar", use_container_width=True):
            st.session_state.clear()
            st.query_params.clear()
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
    n_hadiah = jumlah_pending(sheet="Klaim")
    tab_nota, tab_misi, tab_hadiah = st.tabs([f"🧾 Nota Pembelian ({n_nota})",
                                              f"🎯 Misi Bonus ({n_misi})",
                                              f"🎁 Hadiah ({n_hadiah})"])
    with tab_nota:
        sales_approval_nota()
    with tab_misi:
        sales_approval_misi()
    with tab_hadiah:
        sales_approval_hadiah()


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
            tarif = (r.poin // r.jumlah_pack) if r.jumlah_pack else poin_per_pack()
            st.write(f"**Diajukan:** {r.jumlah_pack} pack × {tarif} = {poin(r.poin)}")

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
                              "poin": int(pack) * tarif, "catatan": catatan,
                              "diproses_oleh": st.session_state.nama,
                              "diproses_pada": now_wib().isoformat(timespec="seconds")}
                    if no_nota:
                        fields["no_nota"] = no_nota
                    ubah("Transaksi", "id", r.id, fields)
                    st.toast(f"Disetujui: {r.nama_toko} +{poin(int(pack) * tarif)}")
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
        info = dict(zip(("ikon", "nama"), label_misi(r.jenis).split(" ", 1)))
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
    dapat = int(jumlah) * poin_per_pack()
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
    st.caption("Pilih customer dari master. Login toko: username buatan Anda + "
               "password = Customer No.")
    if msg := st.session_state.pop("daftar_ok", None):
        st.success(msg)

    try:
        cust = load_customers()
    except Exception as e:
        st.error(f"Master customer tidak bisa dibaca: {e}")
        st.caption("Pastikan Customer Sheet di-share (Viewer) ke client_email service account.")
        return

    t = daftar_toko()
    terdaftar = set(t.kode_toko.str.strip().str.upper())
    belum = cust[~cust.kode.str.upper().isin(terdaftar)]
    st.caption(f"{len(cust):,} customer aktif · {len(belum):,} belum terdaftar".replace(",", "."))

    pilih = st.selectbox(
        "🔍 Cari Customer * (ketik kode customer atau nama toko)",
        ["— Pilih —"] + belum.label.tolist(), key="dt_cust",
    )
    if pilih != "— Pilih —":
        c = belum[belum.label == pilih].iloc[0]
        st.info(f"**{c.nama}** ({c.kode})"
                + (f"\n\n{c.alamat}" if c.alamat else "")
                + (f"\n\n{' - '.join(x for x in (c.zona, c.rayon) if x)}"
                   if (c.zona or c.rayon) else ""))
        with st.form("form_toko", clear_on_submit=True):
            username = st.text_input(
                "Username *", placeholder="mis. no. HP, gmail, atau nama singkat"
            ).strip().lower()
            pemilik = st.text_input("Nama Pemilik").strip()
            hp = st.text_input("No. HP Aktif *", placeholder="mis. 081234567890").strip()
            st.caption(f"Password toko otomatis = Customer No. **{c.kode}**")
            if st.form_submit_button("Daftarkan", use_container_width=True):
                hp_bersih = re.sub(r"\D", "", hp)
                if hp_bersih.startswith("62"):
                    hp_bersih = "0" + hp_bersih[2:]
                if not username or " " in username:
                    st.error("Username wajib diisi dan tanpa spasi.")
                elif not re.fullmatch(r"08\d{8,11}", hp_bersih):
                    st.error("No. HP aktif wajib diisi, format 08xxxxxxxxxx (10-13 digit).")
                elif (t.username.str.strip().str.lower() == username).any():
                    st.error("Username sudah dipakai toko lain.")
                else:
                    tambah("Toko", {
                        "kode_toko": c.kode.upper(), "nama_toko": c.nama, "pemilik": pemilik,
                        "no_hp": hp_bersih, "alamat": c.alamat, "pin_hash": hash_pin(c.kode.upper()),
                        "dibuat": now_wib().isoformat(timespec="seconds"),
                        "username": username, "zona": c.zona, "rayon": c.rayon,
                    })
                    st.session_state.daftar_ok = (f"Toko {c.nama} terdaftar. Username: "
                                                  f"{username} · Password: {c.kode}")
                    st.session_state.pop("dt_cust", None)
                    st.rerun()

    st.divider()
    st.write("**Toko terdaftar**")
    st.dataframe(
        t[["kode_toko", "nama_toko", "username", "zona", "rayon", "no_hp", "alamat"]].rename(
            columns={"kode_toko": "Customer No", "nama_toko": "Toko", "username": "Username",
                     "zona": "Zona", "rayon": "Rayon", "no_hp": "HP", "alamat": "Alamat"}),
        hide_index=True, use_container_width=True,
    )

    with st.expander("✏️ Ubah username toko"):
        if not t.empty:
            opsi = {f"{r.nama_toko} ({r.kode_toko}) · {r.username or '-'}": r.kode_toko
                    for r in t.itertuples()}
            k = opsi[st.selectbox("Pilih toko", list(opsi.keys()), key="ubah_user_kode")]
            baru = st.text_input("Username baru", key="user_baru").strip().lower()
            if st.button("Simpan username"):
                if not baru or " " in baru:
                    st.error("Username wajib diisi dan tanpa spasi.")
                elif ((t.username.str.strip().str.lower() == baru) & (t.kode_toko != k)).any():
                    st.error("Username sudah dipakai toko lain.")
                else:
                    ubah("Toko", "kode_toko", k, {"username": baru})
                    st.success("Username diperbarui.")


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
    kl = tabel("Klaim")
    kl = kl[kl.status.isin(KLAIM_TERPAKAI)][["kode_toko", "poin"]]
    keluar = pd.concat([p[["kode_toko", "poin"]], kl]).groupby("kode_toko").poin.sum()
    df["PoinDitukar"] = df.Kode.map(keluar).fillna(0).astype(int)
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
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:14px;margin:6px 0 14px">'
        f'{avatar_html(foto_profil_toko(kode), st.session_state.nama, 72)}'
        f'<div style="font-size:2rem;font-weight:900;line-height:1.1">'
        f'{st.session_state.nama}</div></div>', unsafe_allow_html=True)
    masuk, keluar, saldo = saldo_toko(kode)

    st.metric("💰 Saldo Poin Anda", poin(saldo))
    c1, c2 = st.columns(2)
    c1.metric("Total poin terkumpul", poin(masuk))
    c2.metric("Sudah ditukar", poin(keluar))

    st.divider()
    tampil_leaderboard(kode)

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
                    label_misi),
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
    prog = program_berjalan("pembelian")
    if not prog:
        st.subheader("➕ Tambahkan Poin dari Pembelian ke Sales")
        st.info("Program poin pembelian sedang tidak berjalan. Pantau terus info dari sales kami ya! 🙏")
        return
    judul_program("➕ Tambahkan Poin dari Pembelian ke Sales", prog)
    pp = int(prog["poin"])
    st.caption(
        f"1 pack = {pp} poin · min {MIN_PACK} pack, max {MAX_PACK} pack (1 slop) per nota. "
        "Poin masuk setelah disetujui sales."
    )

    if st.session_state.pop("toko_kirim_ok", None):
        st.success("Nota terkirim! Tunggu approval sales, poin akan muncul di Saldo Poin.")

    v = st.session_state.setdefault("form_ver", 0)

    foto = ambil_foto_langsung(f"nota{v}", "📷 Foto nota")

    hasil_ai = None
    if foto is not None and GEMINI_API_KEY:
        img = foto["bytes"]
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
        st.metric("Poin yang akan didapat", poin(int(pack) * pp))
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
        segarkan()
        if foto_sudah_dipakai(foto["hash"]):
            st.error("❌ Foto ini sudah pernah dikirim sebelumnya. Ambil foto baru.")
            return
        now = now_wib()
        tambah("Transaksi", {
            "kode_toko": kode, "nama_toko": st.session_state.nama, "no_nota": "",
            "tanggal": now.date().isoformat(), "jumlah_pack": int(pack),
            "poin": int(pack) * pp, "status": "pending", "sumber": "toko",
            "deskripsi": desk.strip(), "dibuat": now.isoformat(timespec="seconds"),
            "ai_pack": hasil_ai["total_pack"] if hasil_ai else "",
            "ai_baca": ringkas_ai(hasil_ai) if hasil_ai else "",
            "foto_mode": foto["mode"], "foto_hash": foto["hash"],
        }, foto=foto["bytes"], nama_foto=f"{kode}_nota_{now:%Y%m%d_%H%M%S}")
        st.session_state.form_ver = v + 1
        st.session_state.toko_kirim_ok = True
        st.rerun()


def kartu_misi(kode, prog):
    """Satu kartu misi. Kamera hanya dibuka untuk 1 misi sekaligus."""
    jenis = prog["kode"]
    frek = prog["frekuensi"]
    _, buka_lagi = periode_klaim(frek)
    kata = {"harian": "hari ini", "mingguan": "minggu ini", "bulanan": "bulan ini",
            "sekali": "untuk program ini"}[frek]
    with st.container(border=True):
        judul_program(f"{prog['ikon']} {prog['nama']} · +{poin(prog['poin'])}", prog)
        st.caption(f"🔁 Bisa diklaim {FREKUENSI[frek]}"
                   + (f" · {prog['deskripsi']}" if prog["deskripsi"] else ""))
        status = status_misi_periode(kode, prog)
        if status == "pending":
            st.info(f"⏳ Sudah dikirim {kata}, menunggu konfirmasi sales.")
            return
        if status == "approved":
            st.success(f"✅ Poin {kata} sudah didapat."
                       + (f" Bisa diklaim lagi {buka_lagi}." if buka_lagi else ""))
            return

        if st.session_state.get("misi_buka") != jenis:
            if st.button("📷 Ambil foto", key=f"buka_{jenis}", use_container_width=True):
                st.session_state.misi_buka = jenis
                st.rerun()
            return

        v = st.session_state.setdefault(f"misi_ver_{jenis}", 0)
        foto = ambil_foto_langsung(f"misi_{jenis}{v}", f"📷 Foto {prog['nama']}")
        ket = ""
        if prog["keterangan"]:
            ket = st.text_input(f"{prog['keterangan']} *", key=f"mket_{jenis}{v}").strip()

        b1, b2 = st.columns(2)
        if b2.button("✖ Batal", key=f"batal_{jenis}", use_container_width=True):
            st.session_state.pop("misi_buka", None)
            st.rerun()
        if b1.button("📤 Kirim", key=f"mkirim_{jenis}{v}", type="primary",
                     use_container_width=True):
            if foto is None:
                st.error("Ambil foto dulu dengan kamera.")
                return
            if prog["keterangan"] and not ket:
                st.error(f"Isi {prog['keterangan'].lower()}.")
                return
            segarkan()
            if status_misi_periode(kode, prog) or not program_berjalan(jenis):
                st.session_state.pop("misi_buka", None)
                st.rerun()
            if foto_sudah_dipakai(foto["hash"]):
                st.error("❌ Foto ini sudah pernah dikirim sebelumnya. Ambil foto baru.")
                return
            mg, _ = periode_klaim(frek)
            now = now_wib()
            tambah("Misi", {
                "kode_toko": kode, "nama_toko": st.session_state.nama, "jenis": jenis,
                "minggu": mg, "keterangan": ket, "poin": int(prog["poin"]),
                "status": "pending", "dibuat": now.isoformat(timespec="seconds"),
                "foto_mode": foto["mode"], "foto_hash": foto["hash"],
            }, foto=foto["bytes"], nama_foto=f"{kode}_{jenis}_{now:%Y%m%d_%H%M%S}")
            st.session_state[f"misi_ver_{jenis}"] = v + 1
            st.session_state.pop("misi_buka", None)
            st.toast(f"{prog['nama']} terkirim! Tunggu konfirmasi sales.")
            st.rerun()


def toko_misi():
    kode = st.session_state.kode_toko
    st.subheader("🎯 Misi Bonus Poin")
    st.info(
        "**Jangan khawatir!** Jika mau tambahan poin, Bapak/Ibu cukup sediakan "
        "**Eceran** dan **POSM** kami. 🙏"
    )
    st.caption("Setiap misi punya batas klaim sendiri (lihat 🔁 di tiap kartu). Poin masuk "
               "setelah dikonfirmasi sales.")
    daftar = misi_berjalan()
    if not daftar:
        st.info("Belum ada misi yang berjalan saat ini. Pantau terus ya! 🙏")
        return
    if st.session_state.get("misi_buka") not in {p["kode"] for p in daftar}:
        st.session_state.pop("misi_buka", None)
    for prog in daftar:
        kartu_misi(kode, prog)


# ================== ADMIN: ATUR PROGRAM ==================
def sales_program():
    st.subheader("⚙️ Atur Program Poin")
    st.caption("Tanggal kosong = tanpa batas. Perubahan poin hanya berlaku untuk pengajuan "
               "baru; poin yang sudah tercatat tidak berubah.")
    if msg := st.session_state.pop("prog_ok", None):
        st.success(msg)
    df = program_df()

    # Ringkasan semua program (hanya tampilan)
    st.dataframe(
        pd.DataFrame({
            "Program": df.ikon + " " + df.nama,
            "Jenis": df.jenis.map({"pembelian": "Pembelian", "misi": "Misi foto"}).fillna(df.jenis),
            "Poin": df.poin.astype(str) + df.jenis.map({"pembelian": " /pack"}).fillna(" /klaim"),
            "Periode": [periode_program(r).replace("📅 ", "") for _, r in df.iterrows()],
            "Batas klaim": [FREKUENSI[r["frekuensi"]] if r["jenis"] == "misi" else "Per nota"
                            for _, r in df.iterrows()],
            "Status": [_status_program(r) for _, r in df.iterrows()],
        }),
        hide_index=True, use_container_width=True,
    )

    # Edit satu program lewat form
    st.markdown("### ✏️ Edit program")
    opsi = {f"{r.ikon} {r.nama}": r.kode for r in df.itertuples()}
    kode_p = opsi[st.selectbox("Pilih program yang mau diedit", list(opsi.keys()),
                               key="edit_prog_pilih")]
    p = df[df.kode == kode_p].iloc[0]
    misi = p["jenis"] == "misi"

    def ke_tgl(x):
        try:
            return datetime.fromisoformat(str(x)[:10]).date()
        except ValueError:
            return None

    k = kode_p  # akhiran key widget, supaya isian reset saat ganti program
    with st.container(border=True):
        nama = st.text_input("Nama program *", value=p["nama"], key=f"e_nama_{k}").strip()
        ikon = pilih_ikon(f"ikon_edit_{kode_p}", p["ikon"] or "📌")
        c3, c4 = st.columns(2)
        jml = c3.number_input("Poin per klaim *" if misi else "Poin per pack *",
                              min_value=1, value=max(int(p["poin"]), 1), step=50 if misi else 10,
                              key=f"e_poin_{k}")
        aktif = c4.toggle("Program aktif", value=p["aktif"] == "ya", key=f"e_aktif_{k}")
        frek = p["frekuensi"]
        if misi:
            frek = st.selectbox("Batas klaim per toko", list(FREKUENSI),
                                index=list(FREKUENSI).index(p["frekuensi"]),
                                format_func=FREKUENSI.get, key=f"e_frek_{k}")
        c5, c6 = st.columns(2)
        # Centang "tanpa batas" -> kolom tanggal disembunyikan
        tanpa_mulai = c5.checkbox("Tanpa tanggal mulai (langsung berlaku)",
                                  value=not p["mulai"], key=f"e_tm_{k}")
        mulai = None if tanpa_mulai else c5.date_input(
            "Tanggal mulai", value=ke_tgl(p["mulai"]) or today_wib(), format="DD-MM-YYYY",
            key=f"e_mulai_{k}")
        tanpa_selesai = c6.checkbox("Tanpa tanggal selesai (berlaku terus)",
                                    value=not p["selesai"], key=f"e_ts_{k}")
        selesai = None if tanpa_selesai else c6.date_input(
            "Tanggal selesai", value=ke_tgl(p["selesai"]) or today_wib(), format="DD-MM-YYYY",
            key=f"e_selesai_{k}")
        ket = st.text_input("Isian wajib untuk toko (kosongkan jika cukup foto)",
                            value=p["keterangan"], key=f"e_ket_{k}",
                            placeholder="mis. Brand rokok yang diecer").strip() if misi else ""
        desk = st.text_area("Petunjuk / deskripsi", value=p["deskripsi"],
                            key=f"e_desk_{k}").strip()
        st.caption(f"Terakhir diubah: {p['diubah_oleh'] or '-'} · {waktu(p['diubah_pada']) or '-'}")
        if st.button("💾 Simpan perubahan", type="primary", use_container_width=True,
                     key=f"e_simpan_{k}"):
            if not nama:
                st.error("Nama program wajib diisi.")
            elif mulai and selesai and mulai > selesai:
                st.error("Tanggal mulai tidak boleh setelah tanggal selesai.")
            else:
                now = now_wib().isoformat(timespec="seconds")
                rows = []
                for _, r in df.iterrows():
                    if r["kode"] == kode_p:
                        rows.append([kode_p, nama, ikon or "📌", r["jenis"], int(jml),
                                     "ya" if aktif else "tidak",
                                     mulai.isoformat() if mulai else "",
                                     selesai.isoformat() if selesai else "",
                                     ket, desk, st.session_state.nama, now, frek])
                    else:
                        rows.append([r[c] for c in COLS["Program"]])
                with st.spinner("Menyimpan..."):
                    simpan_program(rows)
                st.session_state.prog_ok = f"✅ Program {nama} tersimpan."
                st.rerun()

    misi_ada = df[df.jenis == "misi"]
    if not misi_ada.empty:
        with st.expander("🗑️ Hapus program misi"):
            opsi = {f"{r.ikon} {r.nama}": r.kode for r in misi_ada.itertuples()}
            pilih = st.selectbox("Pilih program", list(opsi.keys()), key="hapus_prog")
            st.caption("Riwayat poin toko dari program ini tetap tersimpan.")
            if st.button("Hapus program ini", key="btn_hapus_prog"):
                with st.spinner("Menghapus..."):
                    simpan_program(df[df.kode != opsi[pilih]][COLS["Program"]].values.tolist())
                st.success(f"Program {pilih} dihapus.")
                st.rerun()

    with st.expander("➕ Tambah misi foto baru"):
        if msg := st.session_state.pop("misi_baru_ok", None):
            st.success(msg)
        vb = st.session_state.setdefault("misi_baru_ver", 0)  # naik -> isian kosong lagi
        with st.container(border=True):
            nama = st.text_input("Nama misi *", placeholder="mis. POSM Poster",
                                 key=f"b_nama_{vb}").strip()
            ikon = pilih_ikon(f"ikon_baru_{vb}", "📌")
            jml = st.number_input("Poin per klaim *", min_value=1, value=500, step=50,
                                  key=f"b_poin_{vb}")
            frek_baru = st.selectbox("Batas klaim per toko", list(FREKUENSI), index=1,
                                     format_func=FREKUENSI.get, key=f"b_frek_{vb}")
            c1, c2 = st.columns(2)
            tanpa_mulai = c1.checkbox("Tanpa tanggal mulai (langsung berlaku)", value=False,
                                      key=f"b_tm_{vb}")
            mulai = None if tanpa_mulai else c1.date_input(
                "Tanggal mulai", value=today_wib(), format="DD-MM-YYYY", key=f"b_mulai_{vb}")
            tanpa_selesai = c2.checkbox("Tanpa tanggal selesai (berlaku terus)", value=True,
                                        key=f"b_ts_{vb}")
            selesai = None if tanpa_selesai else c2.date_input(
                "Tanggal selesai", value=today_wib(), format="DD-MM-YYYY", key=f"b_selesai_{vb}")
            ket = st.text_input("Isian wajib (opsional)", placeholder="mis. Brand rokok yang diecer",
                                key=f"b_ket_{vb}").strip()
            desk = st.text_input("Petunjuk foto", placeholder="mis. Foto poster kami yang terpasang",
                                 key=f"b_desk_{vb}").strip()
            if st.button("Tambah Misi", use_container_width=True, key=f"b_simpan_{vb}"):
                kode_baru = re.sub(r"[^a-z0-9]+", "_", nama.lower()).strip("_")
                if not kode_baru:
                    st.error("Nama misi wajib diisi.")
                elif kode_baru in set(df.kode):
                    st.error("Misi dengan nama itu sudah ada.")
                elif mulai and selesai and selesai < mulai:
                    st.error("Tanggal selesai harus setelah tanggal mulai.")
                else:
                    tambah("Program", {
                        "kode": kode_baru, "nama": nama, "ikon": ikon or "📌", "jenis": "misi",
                        "poin": int(jml), "aktif": "ya",
                        "mulai": mulai.isoformat() if mulai else "",
                        "selesai": selesai.isoformat() if selesai else "",
                        "keterangan": ket, "deskripsi": desk, "frekuensi": frek_baru,
                        "diubah_oleh": st.session_state.nama,
                        "diubah_pada": now_wib().isoformat(timespec="seconds"),
                    })
                    st.session_state.misi_baru_ver = vb + 1
                    st.session_state.misi_baru_ok = f"✅ Misi {nama} ditambahkan."
                    st.rerun()


# ================== ROUTER ==================
def halaman_belum_setup():
    st.error("Secrets belum dikonfigurasi: GSHEET_ID / GSHEET_URL tidak ditemukan.")
    try:
        atas = sorted(st.secrets.keys())
        g = st.secrets.get("gcp_json")
        dalam = sorted(g.keys()) if hasattr(g, "keys") else []
    except Exception:
        atas, dalam = [], []
    st.write(f"**Secret yang terbaca:** {', '.join(atas) or '(kosong)'}")
    if dalam:
        st.write(f"**Isi [gcp_json]:** {', '.join(dalam)}")
    st.info("Tulis GSHEET_ID, GSHEET_URL, GEMINI_API_KEY di PALING ATAS, "
            "sebelum baris [gcp_json].")
    st.markdown(
        "Isi **Settings → Secrets** di Streamlit Cloud dengan:\n\n"
        "```toml\n"
        'GSHEET_ID = "spreadsheet-id-dari-url"\n'
        'GSHEET_URL = "https://docs.google.com/spreadsheets/d/.../edit"\n'
        'GEMINI_API_KEY = "AIza..."  # opsional\n'
        'FOTO_SCRIPT_URL = "https://script.google.com/macros/s/.../exec"\n'
        'FOTO_TOKEN = "token-yang-sama-dengan-di-FotoAPI.gs"\n\n'
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
    except KeyError as e:
        st.error(f"Secret tidak ditemukan: {e}")
        st.caption("Pastikan nama secret sudah benar di Streamlit Cloud → Settings → Secrets.")
        return
    except Exception as e:
        st.error(f"Gagal membaca credentials: {e}")
        st.caption("Cek format gcp_json di Secrets — harus berupa JSON service account yang valid.")
        return

    pulihkan_sesi()
    role = st.session_state.get("role")
    if not role:
        halaman_login()
        return
    tombol_keluar()
    if role == "toko":
        menu = pilih_menu(["Saldo Poin", "Tambah Poin", "Misi Bonus", "Tukar Hadiah", "Profil"])
        {"Saldo Poin": toko_saldo, "Tambah Poin": toko_tambah_poin,
         "Misi Bonus": toko_misi, "Tukar Hadiah": toko_hadiah, "Profil": toko_profil}[menu]()
    else:
        if not (FOTO_SCRIPT_URL and FOTO_TOKEN):
            st.sidebar.error("📷 Penyimpanan foto belum diatur (FOTO_SCRIPT_URL / FOTO_TOKEN).")
        n = jumlah_pending() + jumlah_pending(sheet="Misi") + jumlah_pending(sheet="Klaim")
        if n:
            st.sidebar.warning(f"🔔 {n} pengajuan toko menunggu approval")
        menu = pilih_menu(["Approval", "Input Nota", "Rekap Poin", "Penukaran Poin",
                           "Daftar Toko", "Atur Program", "Atur Hadiah"])
        {
            "Approval": sales_approval,
            "Input Nota": sales_input_nota,
            "Rekap Poin": sales_rekap,
            "Penukaran Poin": sales_penukaran,
            "Daftar Toko": sales_daftar_toko,
            "Atur Program": sales_program,
            "Atur Hadiah": sales_hadiah,
        }[menu]()


try:
    main()
except Exception as e:
    st.error(f"Terjadi kesalahan: {e}")
    if st.button("Coba lagi"):
        segarkan()
        st.rerun()
