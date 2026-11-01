# Poin Toko

Aplikasi Streamlit untuk program loyalitas toko. Toko mengumpulkan poin dari nota pembelian (per pack) dan misi bonus, lalu menukarnya dengan hadiah. Sales menyetujui transaksi, dan promotor mengantar hadiah dengan bukti foto + lokasi. Database memakai Google Sheets. Foto disimpan di Google Drive lewat Apps Script. Nota dibaca dengan AI (Gemini).

## Struktur
Semua kode ada di `app.py` dan dibagi dengan penanda `# ================== NAMA BAGIAN ==================`:
- `KONFIGURASI`: aturan poin (`POIN_PER_PACK`, `MIN_PACK`, `MAX_PACK`), folder foto, fungsi `secret()`.
- `COLS`: daftar kolom tiap sheet (Toko, Transaksi, Misi, Penukaran, Program, Hadiah, Klaim). Ini adalah skema database.
- `AUTH & CLIENTS`: koneksi Google Sheets dan Drive.
- `DATABASE: GOOGLE SHEETS`: `tabel`, `tambah`, `ubah`, `hapus`. Semua baca/tulis data lewat fungsi-fungsi ini.
- `AI PEMBACA NOTA`: `baca_nota_ai` (Gemini).
- `FOTO LANGSUNG (anti-curang)`: foto wajib diambil langsung dari kamera.

## Aturan
- Ubah hanya bagian yang perlu. Jangan menulis ulang `app.py` dari nol, karena file ini besar (±2.500 baris).
- Lihat dulu bagian kode yang akan diubah. Setelah mengubah, jalankan `pytest`.
- Menambah kolom: tambahkan di **akhir** list `COLS` sheet terkait. Jangan mengubah urutan atau nama kolom yang sudah ada, karena data lama di sheet akan bergeser.
- Baca/tulis data hanya lewat `tabel`/`tambah`/`ubah`/`hapus`. Jangan memanggil gspread langsung dari tampilan.
- Kunci, token, dan password hanya lewat `secret("NAMA")` (contoh di `.streamlit/secrets.toml.example`). Jangan menulis kunci atau password baru di kode, dan jangan commit `.streamlit/secrets.toml`.
- Waktu memakai WIB (`now_wib`, `today_wib`).
- Teks di tampilan memakai Bahasa Indonesia.

## Test
```
pip install -r requirements.txt pytest
pytest
```
GitHub Actions menjalankan test yang sama setiap ada push.
