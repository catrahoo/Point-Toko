"""
Aplikasi Poin Toko - Program Loyalitas Pembelian Rokok
Aturan: 1 nota = min 1 pack, max 1 slop (10 pack). Poin = Rp100 per pack.
Jalankan: streamlit run app.py
"""
import hashlib
import sqlite3
from datetime import datetime, date

import pandas as pd
import streamlit as st

# ================== KONFIGURASI ==================
DB_PATH = "poin_toko.db"
POIN_PER_PACK = 100
MIN_PACK = 1
MAX_PACK = 10  # 1 slop

# Password sales: atur di .streamlit/secrets.toml -> SALES_PASSWORD = "..."
try:
    SALES_PASSWORD = st.secrets["SALES_PASSWORD"]
except Exception:
    SALES_PASSWORD = "sales123"  # GANTI sebelum dipakai!

st.set_page_config(page_title="Poin Toko", page_icon="🏪", layout="centered")


# ================== DATABASE ==================
@st.cache_resource
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS toko (
            kode_toko TEXT PRIMARY KEY,
            nama_toko TEXT NOT NULL,
            pemilik TEXT,
            no_hp TEXT,
            alamat TEXT,
            pin_hash TEXT NOT NULL,
            dibuat TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transaksi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kode_toko TEXT NOT NULL REFERENCES toko(kode_toko),
            no_nota TEXT NOT NULL UNIQUE,
            tanggal TEXT NOT NULL,
            jumlah_pack INTEGER NOT NULL,
            poin INTEGER NOT NULL,
            foto BLOB,
            sales TEXT,
            dibuat TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS penukaran (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kode_toko TEXT NOT NULL REFERENCES toko(kode_toko),
            tanggal TEXT NOT NULL,
            poin INTEGER NOT NULL,
            keterangan TEXT,
            dibuat TEXT NOT NULL
        );
        """
    )
    return conn


def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


def query_df(sql, params=()):
    return pd.read_sql_query(sql, get_conn(), params=params)


def saldo_toko(kode):
    conn = get_conn()
    masuk = conn.execute(
        "SELECT COALESCE(SUM(poin),0) FROM transaksi WHERE kode_toko=?", (kode,)
    ).fetchone()[0]
    keluar = conn.execute(
        "SELECT COALESCE(SUM(poin),0) FROM penukaran WHERE kode_toko=?", (kode,)
    ).fetchone()[0]
    return masuk, keluar, masuk - keluar


def list_toko():
    return query_df("SELECT kode_toko, nama_toko FROM toko ORDER BY nama_toko")


def rp(x):
    return f"Rp{int(x):,}".replace(",", ".")


# ================== LOGIN ==================
def halaman_login():
    st.title("🏪 Poin Toko")
    st.caption(f"Setiap 1 pack = {rp(POIN_PER_PACK)} poin")
    tab_toko, tab_sales = st.tabs(["Masuk sebagai Toko", "Masuk sebagai Sales"])

    with tab_toko:
        with st.form("login_toko"):
            kode = st.text_input("Kode Toko").strip().upper()
            pin = st.text_input("PIN", type="password")
            if st.form_submit_button("Masuk", use_container_width=True):
                row = get_conn().execute(
                    "SELECT nama_toko, pin_hash FROM toko WHERE kode_toko=?", (kode,)
                ).fetchone()
                if row and row[1] == hash_pin(pin):
                    st.session_state.update(role="toko", kode_toko=kode, nama=row[0])
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


def tombol_keluar():
    with st.sidebar:
        st.write(f"👤 **{st.session_state.nama}**")
        if st.button("Keluar", use_container_width=True):
            st.session_state.clear()
            st.rerun()


# ================== HALAMAN SALES ==================
def sales_input_nota():
    st.subheader("📝 Input Nota Pembelian")
    toko = list_toko()
    if toko.empty:
        st.info("Belum ada toko. Daftarkan dulu di menu **Daftar Toko**.")
        return

    opsi = {f"{r.nama_toko} ({r.kode_toko})": r.kode_toko for r in toko.itertuples()}
    pilihan = st.selectbox("Toko", list(opsi.keys()))
    kode = opsi[pilihan]

    col1, col2 = st.columns(2)
    no_nota = col1.text_input("No. Nota").strip().upper()
    tgl = col2.date_input("Tanggal", value=date.today(), max_value=date.today())
    jumlah = st.number_input(
        f"Jumlah pack (min {MIN_PACK}, max {MAX_PACK} = 1 slop)",
        min_value=MIN_PACK, max_value=MAX_PACK, value=1, step=1,
    )
    poin = int(jumlah) * POIN_PER_PACK
    st.metric("Poin yang didapat", rp(poin))

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
        try:
            get_conn().execute(
                """INSERT INTO transaksi
                   (kode_toko,no_nota,tanggal,jumlah_pack,poin,foto,sales,dibuat)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (kode, no_nota, tgl.isoformat(), int(jumlah), poin,
                 foto.getvalue(), st.session_state.nama, datetime.now().isoformat()),
            )
            get_conn().commit()
            st.success(f"Tersimpan! {pilihan} mendapat {rp(poin)} poin.")
            st.balloons()
        except sqlite3.IntegrityError:
            st.error(f"No. nota **{no_nota}** sudah pernah diinput. Cek lagi notanya.")


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
            else:
                try:
                    get_conn().execute(
                        "INSERT INTO toko VALUES (?,?,?,?,?,?,?)",
                        (kode, nama, pemilik, hp, alamat, hash_pin(pin),
                         datetime.now().isoformat()),
                    )
                    get_conn().commit()
                    st.success(f"Toko {nama} terdaftar. Kode: {kode}")
                except sqlite3.IntegrityError:
                    st.error("Kode toko sudah dipakai.")

    st.divider()
    st.write("**Toko terdaftar**")
    st.dataframe(
        query_df("SELECT kode_toko AS Kode, nama_toko AS Toko, pemilik AS Pemilik, "
                 "no_hp AS HP, alamat AS Alamat FROM toko ORDER BY nama_toko"),
        hide_index=True, use_container_width=True,
    )

    with st.expander("🔑 Reset PIN toko"):
        toko = list_toko()
        if not toko.empty:
            k = st.selectbox("Pilih toko", toko.kode_toko, key="reset_kode")
            pin_baru = st.text_input("PIN baru", type="password", key="pin_baru")
            if st.button("Reset PIN") and len(pin_baru) >= 4:
                get_conn().execute("UPDATE toko SET pin_hash=? WHERE kode_toko=?",
                                   (hash_pin(pin_baru), k))
                get_conn().commit()
                st.success("PIN diperbarui.")


def sales_rekap():
    st.subheader("📊 Rekap Poin Semua Toko")
    df = query_df(
        """
        SELECT t.kode_toko AS Kode, t.nama_toko AS Toko,
          COALESCE((SELECT COUNT(*) FROM transaksi x WHERE x.kode_toko=t.kode_toko),0) AS Nota,
          COALESCE((SELECT SUM(jumlah_pack) FROM transaksi x WHERE x.kode_toko=t.kode_toko),0) AS Pack,
          COALESCE((SELECT SUM(poin) FROM transaksi x WHERE x.kode_toko=t.kode_toko),0) AS PoinMasuk,
          COALESCE((SELECT SUM(poin) FROM penukaran p WHERE p.kode_toko=t.kode_toko),0) AS PoinDitukar
        FROM toko t ORDER BY PoinMasuk DESC
        """
    )
    if df.empty:
        st.info("Belum ada data.")
        return
    df["SaldoPoin"] = df.PoinMasuk - df.PoinDitukar

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Toko", len(df))
    c2.metric("Total Pack", f"{int(df.Pack.sum()):,}".replace(",", "."))
    c3.metric("Total Saldo Poin", rp(df.SaldoPoin.sum()))
    st.dataframe(df, hide_index=True, use_container_width=True)
    st.download_button("⬇️ Download CSV", df.to_csv(index=False).encode(),
                       "rekap_poin_toko.csv", "text/csv")

    st.divider()
    st.write("**Riwayat Nota**")
    trx = query_df(
        """SELECT x.id, x.tanggal AS Tanggal, t.nama_toko AS Toko, x.no_nota AS Nota,
                  x.jumlah_pack AS Pack, x.poin AS Poin, x.sales AS Sales
           FROM transaksi x JOIN toko t USING(kode_toko)
           ORDER BY x.tanggal DESC, x.id DESC"""
    )
    st.dataframe(trx.drop(columns="id"), hide_index=True, use_container_width=True)
    if not trx.empty:
        lihat_foto_nota(trx, key="sales_foto", boleh_hapus=True)


def sales_penukaran():
    st.subheader("🎁 Penukaran Poin")
    toko = list_toko()
    if toko.empty:
        st.info("Belum ada toko.")
        return
    opsi = {f"{r.nama_toko} ({r.kode_toko})": r.kode_toko for r in toko.itertuples()}
    pilihan = st.selectbox("Toko", list(opsi.keys()), key="tukar_toko")
    kode = opsi[pilihan]
    _, _, saldo = saldo_toko(kode)
    st.metric("Saldo poin saat ini", rp(saldo))

    with st.form("form_tukar", clear_on_submit=True):
        jml = st.number_input("Poin yang ditukar", min_value=0, step=100)
        ket = st.text_input("Keterangan (mis. potong tagihan, hadiah)")
        if st.form_submit_button("Proses Penukaran", use_container_width=True):
            if jml <= 0:
                st.error("Isi jumlah poin.")
            elif jml > saldo:
                st.error("Poin melebihi saldo.")
            else:
                get_conn().execute(
                    "INSERT INTO penukaran (kode_toko,tanggal,poin,keterangan,dibuat) "
                    "VALUES (?,?,?,?,?)",
                    (kode, date.today().isoformat(), int(jml), ket,
                     datetime.now().isoformat()),
                )
                get_conn().commit()
                st.success(f"Penukaran {rp(jml)} poin berhasil.")
                st.rerun()


# ================== HALAMAN TOKO ==================
def halaman_toko():
    kode = st.session_state.kode_toko
    st.title(f"🏪 {st.session_state.nama}")
    masuk, keluar, saldo = saldo_toko(kode)

    st.metric("💰 Saldo Poin Anda", rp(saldo))
    c1, c2 = st.columns(2)
    c1.metric("Total poin terkumpul", rp(masuk))
    c2.metric("Sudah ditukar", rp(keluar))

    trx = query_df(
        "SELECT id, tanggal AS Tanggal, no_nota AS Nota, jumlah_pack AS Pack, poin AS Poin "
        "FROM transaksi WHERE kode_toko=? ORDER BY tanggal DESC, id DESC",
        (kode,),
    )
    st.divider()
    st.subheader("Riwayat Pembelian")
    if trx.empty:
        st.info("Belum ada pembelian tercatat.")
    else:
        trx["Bulan"] = pd.to_datetime(trx.Tanggal).dt.strftime("%Y-%m")
        per_bulan = trx.groupby("Bulan")["Poin"].sum()
        st.bar_chart(per_bulan)
        st.dataframe(trx.drop(columns=["id", "Bulan"]), hide_index=True,
                     use_container_width=True)
        lihat_foto_nota(trx, key="toko_foto")

    tukar = query_df(
        "SELECT tanggal AS Tanggal, poin AS Poin, keterangan AS Keterangan "
        "FROM penukaran WHERE kode_toko=? ORDER BY tanggal DESC", (kode,)
    )
    if not tukar.empty:
        st.subheader("Riwayat Penukaran")
        st.dataframe(tukar, hide_index=True, use_container_width=True)


def lihat_foto_nota(trx, key, boleh_hapus=False):
    with st.expander("🧾 Lihat foto nota"):
        label = {f"{r.Nota} • {r.Tanggal}": r.id for r in trx.itertuples()}
        pilih = st.selectbox("Pilih nota", list(label.keys()), key=key)
        tid = label[pilih]
        foto = get_conn().execute("SELECT foto FROM transaksi WHERE id=?", (tid,)).fetchone()
        if foto and foto[0]:
            st.image(foto[0], use_container_width=True)
        if boleh_hapus and st.button("🗑️ Hapus nota ini (salah input)", key=key + "_del"):
            get_conn().execute("DELETE FROM transaksi WHERE id=?", (tid,))
            get_conn().commit()
            st.rerun()


# ================== ROUTER ==================
def main():
    role = st.session_state.get("role")
    if not role:
        halaman_login()
        return
    tombol_keluar()
    if role == "toko":
        halaman_toko()
    else:
        menu = st.sidebar.radio(
            "Menu", ["Input Nota", "Rekap Poin", "Penukaran Poin", "Daftar Toko"]
        )
        {
            "Input Nota": sales_input_nota,
            "Rekap Poin": sales_rekap,
            "Penukaran Poin": sales_penukaran,
            "Daftar Toko": sales_daftar_toko,
        }[menu]()


main()
