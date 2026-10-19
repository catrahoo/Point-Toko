
"""
Aplikasi Poin Toko - Program Loyalitas Pembelian Rokok
Aturan: 1 nota = min 1 pack, max 1 slop (10 pack). 1 pack = 100 poin.
Alur toko: toko foto nota (kamera) -> sales approve -> poin masuk.
Jalankan: streamlit run app.py
"""
import hashlib
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

WIB = ZoneInfo("Asia/Jakarta")


def now_wib():
    """Waktu sekarang WIB, tanpa info zona (server Streamlit Cloud pakai UTC)."""
    return datetime.now(WIB).replace(tzinfo=None)


def today_wib():
    return now_wib().date()

# ================== KONFIGURASI ==================
DB_PATH = "poin_toko.db"
POIN_PER_PACK = 100
MIN_PACK = 1
MAX_PACK = 10  # 1 slop

# Password sales: atur di Settings > Secrets -> SALES_PASSWORD = "..."
try:
    SALES_PASSWORD = st.secrets["SALES_PASSWORD"]
except Exception:
    SALES_PASSWORD = "sales123"  # GANTI sebelum dipakai!

# Misi bonus: poin per jenis, bisa diklaim 1x per minggu (Senin-Minggu) per jenis
MISI = {
    "eceran": {"nama": "Sedia Eceran", "ikon": "🚬", "poin": 1000},
    "sunblind": {"nama": "POSM Sunblind", "ikon": "⛱️", "poin": 1000},
    "stiker": {"nama": "POSM Stiker", "ikon": "🏷️", "poin": 500},
}

STATUS_LABEL = {
    "pending": "⏳ Menunggu approval",
    "approved": "✅ Disetujui",
    "rejected": "❌ Ditolak",
}

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
        CREATE TABLE IF NOT EXISTS misi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kode_toko TEXT NOT NULL REFERENCES toko(kode_toko),
            jenis TEXT NOT NULL,
            minggu TEXT NOT NULL,
            foto BLOB,
            keterangan TEXT,
            poin INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            catatan TEXT,
            diproses_oleh TEXT,
            diproses_pada TEXT,
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
    # Migrasi: tambah kolom baru untuk alur approval (aman untuk database lama)
    kolom = {r[1] for r in conn.execute("PRAGMA table_info(transaksi)")}
    tambahan = [
        ("status", "TEXT NOT NULL DEFAULT 'approved'"),
        ("sumber", "TEXT NOT NULL DEFAULT 'sales'"),
        ("deskripsi", "TEXT"),
        ("catatan", "TEXT"),
        ("diproses_oleh", "TEXT"),
        ("diproses_pada", "TEXT"),
    ]
    for nama, ddl in tambahan:
        if nama not in kolom:
            conn.execute(f"ALTER TABLE transaksi ADD COLUMN {nama} {ddl}")
    conn.commit()
    return conn


def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


def query_df(sql, params=()):
    return pd.read_sql_query(sql, get_conn(), params=params)


def saldo_toko(kode):
    conn = get_conn()
    beli = conn.execute(
        "SELECT COALESCE(SUM(poin),0) FROM transaksi "
        "WHERE kode_toko=? AND status='approved'", (kode,)
    ).fetchone()[0]
    misi = conn.execute(
        "SELECT COALESCE(SUM(poin),0) FROM misi "
        "WHERE kode_toko=? AND status='approved'", (kode,)
    ).fetchone()[0]
    keluar = conn.execute(
        "SELECT COALESCE(SUM(poin),0) FROM penukaran WHERE kode_toko=?", (kode,)
    ).fetchone()[0]
    masuk = beli + misi
    return masuk, keluar, masuk - keluar


def jumlah_pending(kode=None, tabel="transaksi"):
    sql = f"SELECT COUNT(*) FROM {tabel} WHERE status='pending'"
    params = ()
    if kode:
        sql += " AND kode_toko=?"
        params = (kode,)
    return get_conn().execute(sql, params).fetchone()[0]


def minggu_ini():
    """Kode minggu ISO (Senin-Minggu), mis. 2026-W43, plus tanggal Senin depan."""
    hari = today_wib()
    th, mg, _ = hari.isocalendar()
    senin_depan = hari + timedelta(days=7 - hari.weekday())
    return f"{th}-W{mg:02d}", senin_depan


def status_misi_minggu_ini(kode, jenis):
    """Status klaim misi minggu ini (None jika belum ada / hanya yang ditolak)."""
    mg, _ = minggu_ini()
    row = get_conn().execute(
        "SELECT status FROM misi WHERE kode_toko=? AND jenis=? AND minggu=? "
        "AND status IN ('pending','approved') ORDER BY id DESC LIMIT 1",
        (kode, jenis, mg),
    ).fetchone()
    return row[0] if row else None


def list_toko():
    return query_df("SELECT kode_toko, nama_toko FROM toko ORDER BY nama_toko")


def angka(x):
    return f"{int(x or 0):,}".replace(",", ".")


def poin(x):
    return f"{angka(x)} poin"


def tampil_nota(no):
    return "-" if str(no).startswith("AUTO-") else no


# ================== LOGIN ==================
def halaman_login():
    st.title("🏪 Poin Toko")
    st.caption(f"Setiap 1 pack = {POIN_PER_PACK} poin")
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


def lihat_foto_nota(trx, key, boleh_hapus=False):
    with st.expander("🧾 Lihat foto nota"):
        label = {
            f"#{r.id} • {r.Tanggal} • {r.Pack} pack": r.id for r in trx.itertuples()
        }
        pilih = st.selectbox("Pilih nota", list(label.keys()), key=key)
        tid = label[pilih]
        row = get_conn().execute(
            "SELECT foto, deskripsi FROM transaksi WHERE id=?", (tid,)
        ).fetchone()
        if row and row[0]:
            st.image(row[0], use_container_width=True)
        if row and row[1]:
            st.caption(f"Deskripsi: {row[1]}")
        if boleh_hapus and st.button("🗑️ Hapus nota ini (salah input)", key=key + "_del"):
            get_conn().execute("DELETE FROM transaksi WHERE id=?", (tid,))
            get_conn().commit()
            st.rerun()


# ================== HALAMAN SALES ==================
def sales_approval():
    st.subheader("✅ Approval dari Toko")
    n_nota, n_misi = jumlah_pending(), jumlah_pending(tabel="misi")
    tab_nota, tab_misi = st.tabs([f"🧾 Nota Pembelian ({n_nota})",
                                  f"🎯 Misi Bonus ({n_misi})"])
    with tab_nota:
        sales_approval_nota()
    with tab_misi:
        sales_approval_misi()


def sales_approval_misi():
    pend = query_df(
        """SELECT m.id, m.kode_toko, t.nama_toko, m.jenis, m.minggu, m.keterangan,
                  m.poin, m.dibuat
           FROM misi m JOIN toko t USING(kode_toko)
           WHERE m.status='pending' ORDER BY m.dibuat"""
    )
    if pend.empty:
        st.success("Tidak ada misi yang menunggu approval.")
        return
    st.caption(f"{len(pend)} misi menunggu. Pastikan foto sesuai sebelum approve.")

    for r in pend.itertuples():
        info = MISI[r.jenis]
        with st.container(border=True):
            st.markdown(
                f"**{info['ikon']} {info['nama']}** · {r.nama_toko} ({r.kode_toko}) · "
                f"minggu {r.minggu} · diajukan {r.dibuat[:16].replace('T', ' ')}"
            )
            foto = get_conn().execute(
                "SELECT foto FROM misi WHERE id=?", (r.id,)
            ).fetchone()[0]
            if foto:
                st.image(foto, use_container_width=True)
            if r.keterangan:
                label = "Brand yang diecer" if r.jenis == "eceran" else "Keterangan"
                st.write(f"**{label}:** {r.keterangan}")
            st.write(f"**Poin:** {poin(r.poin)}")
            catatan = st.text_input("Catatan / alasan ditolak", key=f"mct{r.id}").strip()

            b1, b2 = st.columns(2)
            if b1.button("✅ Approve", key=f"mok{r.id}", type="primary",
                         use_container_width=True):
                get_conn().execute(
                    "UPDATE misi SET status='approved', catatan=?, diproses_oleh=?, "
                    "diproses_pada=? WHERE id=?",
                    (catatan or None, st.session_state.nama, now_wib().isoformat(), r.id),
                )
                get_conn().commit()
                st.toast(f"Disetujui: {r.nama_toko} +{poin(r.poin)}")
                st.rerun()
            if b2.button("❌ Tolak", key=f"mno{r.id}", use_container_width=True):
                if not catatan:
                    st.error("Isi alasan penolakan dulu supaya toko tahu.")
                else:
                    get_conn().execute(
                        "UPDATE misi SET status='rejected', catatan=?, diproses_oleh=?, "
                        "diproses_pada=? WHERE id=?",
                        (catatan, st.session_state.nama, now_wib().isoformat(), r.id),
                    )
                    get_conn().commit()
                    st.toast(f"Ditolak: {r.nama_toko}")
                    st.rerun()


def sales_approval_nota():
    pend = query_df(
        """SELECT x.id, x.kode_toko, t.nama_toko, x.tanggal, x.jumlah_pack,
                  x.deskripsi, x.dibuat
           FROM transaksi x JOIN toko t USING(kode_toko)
           WHERE x.status='pending' ORDER BY x.dibuat"""
    )
    if pend.empty:
        st.success("Tidak ada nota yang menunggu approval.")
        return
    st.caption(f"{len(pend)} nota menunggu. Cek foto nota sebelum approve.")

    for r in pend.itertuples():
        with st.container(border=True):
            st.markdown(
                f"**{r.nama_toko}** ({r.kode_toko}) · diajukan {r.dibuat[:16].replace('T', ' ')}"
            )
            foto = get_conn().execute(
                "SELECT foto FROM transaksi WHERE id=?", (r.id,)
            ).fetchone()[0]
            if foto:
                st.image(foto, use_container_width=True)
            st.write(f"**Deskripsi dari toko:** {r.deskripsi or '-'}")
            st.write(f"**Diajukan:** {r.jumlah_pack} pack = {poin(r.jumlah_pack * POIN_PER_PACK)}")

            c1, c2 = st.columns(2)
            pack = c1.number_input(
                "Jumlah pack disetujui (koreksi bila beda dengan nota)",
                min_value=MIN_PACK, max_value=MAX_PACK,
                value=int(r.jumlah_pack), step=1, key=f"pk{r.id}",
            )
            no_nota = c2.text_input("No. Nota (opsional)", key=f"nn{r.id}").strip().upper()
            catatan = st.text_input("Catatan / alasan ditolak", key=f"ct{r.id}").strip()

            b1, b2 = st.columns(2)
            if b1.button("✅ Approve", key=f"ok{r.id}", type="primary",
                         use_container_width=True):
                try:
                    sql = ("UPDATE transaksi SET status='approved', jumlah_pack=?, poin=?, "
                           "catatan=?, diproses_oleh=?, diproses_pada=?")
                    params = [int(pack), int(pack) * POIN_PER_PACK, catatan or None,
                              st.session_state.nama, now_wib().isoformat()]
                    if no_nota:
                        sql += ", no_nota=?"
                        params.append(no_nota)
                    sql += " WHERE id=?"
                    params.append(r.id)
                    get_conn().execute(sql, params)
                    get_conn().commit()
                    st.toast(f"Disetujui: {r.nama_toko} +{poin(int(pack) * POIN_PER_PACK)}")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error(f"No. nota {no_nota} sudah pernah dipakai. Kemungkinan nota dobel.")
            if b2.button("❌ Tolak", key=f"no{r.id}", use_container_width=True):
                if not catatan:
                    st.error("Isi alasan penolakan dulu supaya toko tahu.")
                else:
                    get_conn().execute(
                        "UPDATE transaksi SET status='rejected', catatan=?, "
                        "diproses_oleh=?, diproses_pada=? WHERE id=?",
                        (catatan, st.session_state.nama, now_wib().isoformat(), r.id),
                    )
                    get_conn().commit()
                    st.toast(f"Ditolak: {r.nama_toko}")
                    st.rerun()


def sales_input_nota():
    st.subheader("📝 Input Nota Pembelian (oleh Sales)")
    st.caption("Nota yang diinput sales langsung disetujui.")
    toko = list_toko()
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
        try:
            get_conn().execute(
                """INSERT INTO transaksi
                   (kode_toko,no_nota,tanggal,jumlah_pack,poin,foto,sales,dibuat,
                    status,sumber,diproses_oleh,diproses_pada)
                   VALUES (?,?,?,?,?,?,?,?,'approved','sales',?,?)""",
                (kode, no_nota, tgl.isoformat(), int(jumlah), dapat,
                 foto.getvalue(), st.session_state.nama, now_wib().isoformat(),
                 st.session_state.nama, now_wib().isoformat()),
            )
            get_conn().commit()
            st.success(f"Tersimpan! {pilihan} mendapat {poin(dapat)}.")
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
                         now_wib().isoformat()),
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
          (SELECT COUNT(*) FROM transaksi x
             WHERE x.kode_toko=t.kode_toko AND x.status='approved') AS Nota,
          COALESCE((SELECT SUM(jumlah_pack) FROM transaksi x
             WHERE x.kode_toko=t.kode_toko AND x.status='approved'),0) AS Pack,
          COALESCE((SELECT SUM(poin) FROM transaksi x
             WHERE x.kode_toko=t.kode_toko AND x.status='approved'),0) AS PoinBeli,
          COALESCE((SELECT SUM(poin) FROM misi m
             WHERE m.kode_toko=t.kode_toko AND m.status='approved'),0) AS PoinMisi,
          COALESCE((SELECT SUM(poin) FROM penukaran p
             WHERE p.kode_toko=t.kode_toko),0) AS PoinDitukar,
          (SELECT COUNT(*) FROM transaksi x
             WHERE x.kode_toko=t.kode_toko AND x.status='pending')
          + (SELECT COUNT(*) FROM misi m
             WHERE m.kode_toko=t.kode_toko AND m.status='pending') AS Pending
        FROM toko t
        """
    )
    if df.empty:
        st.info("Belum ada data.")
        return
    df["SaldoPoin"] = df.PoinBeli + df.PoinMisi - df.PoinDitukar
    df = df.sort_values("SaldoPoin", ascending=False)

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Toko", len(df))
    c2.metric("Total Pack", angka(df.Pack.sum()))
    c3.metric("Total Saldo Poin", angka(df.SaldoPoin.sum()))
    st.dataframe(df, hide_index=True, use_container_width=True)
    st.download_button("⬇️ Download CSV", df.to_csv(index=False).encode(),
                       "rekap_poin_toko.csv", "text/csv")

    st.divider()
    st.write("**Riwayat Semua Nota**")
    trx = query_df(
        """SELECT x.id, x.tanggal AS Tanggal, t.nama_toko AS Toko, x.no_nota AS Nota,
                  x.jumlah_pack AS Pack, x.poin AS Poin, x.status AS Status,
                  x.sumber AS Dari, x.diproses_oleh AS Diproses
           FROM transaksi x JOIN toko t USING(kode_toko)
           ORDER BY x.dibuat DESC"""
    )
    if trx.empty:
        st.info("Belum ada nota.")
        return
    tampil = trx.copy()
    tampil["Nota"] = tampil.Nota.map(tampil_nota)
    tampil["Status"] = tampil.Status.map(STATUS_LABEL)
    st.dataframe(tampil.drop(columns="id"), hide_index=True, use_container_width=True)
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
                get_conn().execute(
                    "INSERT INTO penukaran (kode_toko,tanggal,poin,keterangan,dibuat) "
                    "VALUES (?,?,?,?,?)",
                    (kode, today_wib().isoformat(), int(jml), ket,
                     now_wib().isoformat()),
                )
                get_conn().commit()
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

    n_pending = jumlah_pending(kode) + jumlah_pending(kode, tabel="misi")
    if n_pending:
        st.info(f"⏳ {n_pending} pengajuan sedang menunggu approval sales.")

    trx = query_df(
        "SELECT id, tanggal AS Tanggal, no_nota AS Nota, jumlah_pack AS Pack, "
        "poin AS Poin, status AS Status, catatan AS Catatan "
        "FROM transaksi WHERE kode_toko=? ORDER BY dibuat DESC",
        (kode,),
    )
    st.divider()
    st.subheader("Riwayat Pembelian")
    if trx.empty:
        st.info("Belum ada pembelian tercatat.")
    else:
        ok = trx[trx.Status == "approved"]
        if not ok.empty:
            per_bulan = (
                ok.assign(Bulan=pd.to_datetime(ok.Tanggal).dt.strftime("%Y-%m"))
                .groupby("Bulan")["Poin"].sum()
            )
            st.bar_chart(per_bulan, y_label="Poin")
        tampil = trx.copy()
        tampil["Nota"] = tampil.Nota.map(tampil_nota)
        tampil["Status"] = tampil.Status.map(STATUS_LABEL)
        tampil["Catatan"] = tampil.Catatan.fillna("")
        st.dataframe(tampil.drop(columns="id"), hide_index=True,
                     use_container_width=True)
        lihat_foto_nota(trx, key="toko_foto")

    misi = query_df(
        "SELECT dibuat, jenis, keterangan, poin, status, catatan "
        "FROM misi WHERE kode_toko=? ORDER BY dibuat DESC", (kode,)
    )
    if not misi.empty:
        st.subheader("Riwayat Misi Bonus")
        st.dataframe(
            pd.DataFrame({
                "Tanggal": misi.dibuat.str[:10],
                "Misi": misi.jenis.map(lambda j: f"{MISI[j]['ikon']} {MISI[j]['nama']}"),
                "Keterangan": misi.keterangan.fillna(""),
                "Poin": misi.poin,
                "Status": misi.status.map(STATUS_LABEL),
                "Catatan": misi.catatan.fillna(""),
            }),
            hide_index=True, use_container_width=True,
        )

    tukar = query_df(
        "SELECT tanggal AS Tanggal, poin AS Poin, keterangan AS Keterangan "
        "FROM penukaran WHERE kode_toko=? ORDER BY tanggal DESC", (kode,)
    )
    if not tukar.empty:
        st.subheader("Riwayat Penukaran")
        st.dataframe(tukar, hide_index=True, use_container_width=True)


def toko_tambah_poin():
    kode = st.session_state.kode_toko
    st.subheader("➕ Tambahkan Poin dari Pembelian ke Sales")
    st.caption(
        f"1 pack = {POIN_PER_PACK} poin · min {MIN_PACK} pack, max {MAX_PACK} pack (1 slop) per nota. "
        "Poin masuk setelah disetujui sales."
    )

    if st.session_state.pop("toko_kirim_ok", None):
        st.success("Nota terkirim! Tunggu approval sales, poin akan muncul di Saldo Poin.")

    v = st.session_state.setdefault("form_ver", 0)  # untuk reset form setelah kirim

    foto = st.camera_input("📷 Foto nota (langsung dari kamera)", key=f"cam{v}")
    pack = st.number_input(
        "Total pembelian (pack)",
        min_value=MIN_PACK, max_value=MAX_PACK, value=None, step=1,
        placeholder=f"Isi angka {MIN_PACK}-{MAX_PACK}", key=f"pack{v}",
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
        get_conn().execute(
            """INSERT INTO transaksi
               (kode_toko,no_nota,tanggal,jumlah_pack,poin,foto,dibuat,
                status,sumber,deskripsi)
               VALUES (?,?,?,?,?,?,?,'pending','toko',?)""",
            (kode, f"AUTO-{kode}-{now:%Y%m%d%H%M%S%f}", now.date().isoformat(),
             int(pack), int(pack) * POIN_PER_PACK, foto.getvalue(),
             now.isoformat(), desk.strip() or None),
        )
        get_conn().commit()
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
            # cek ulang supaya tidak dobel klik
            if status_misi_minggu_ini(kode, jenis):
                st.rerun()
            mg, _ = minggu_ini()
            get_conn().execute(
                "INSERT INTO misi (kode_toko,jenis,minggu,foto,keterangan,poin,status,dibuat) "
                "VALUES (?,?,?,?,?,?,'pending',?)",
                (kode, jenis, mg, foto.getvalue(), brand or None, info["poin"],
                 now_wib().isoformat()),
            )
            get_conn().commit()
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
def main():
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
        n = jumlah_pending() + jumlah_pending(tabel="misi")
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


main()
