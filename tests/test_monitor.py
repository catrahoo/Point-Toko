"""Monitoring: error di aplikasi harus memicu notif WA, dan tidak spam."""
import pytest

pytest.importorskip("streamlit")
import monitor  # noqa: E402


def test_tanpa_secrets_tidak_mengirim_apa_apa():
    assert monitor.kirim_wa("tes") is False


def test_nomor_08_diubah_jadi_62(monkeypatch):
    monkeypatch.setattr(monitor.st, "secrets", {"callmebot": {"phone": "0857 1234-5678", "apikey": "1"}})
    assert monitor._config() == ("+6285712345678", "1")


def test_error_aplikasi_dikirim_ke_wa_sekali_saja(monkeypatch):
    from streamlit.testing.v1 import AppTest

    terkirim = []
    monkeypatch.setattr(monitor, "kirim_wa", terkirim.append)
    monitor._terakhir.clear()

    def app():
        import monitor
        monitor.pasang("Tes")
        raise ValueError("contoh error")

    at = AppTest.from_function(app)
    at.run()
    assert at.exception
    at.run()   # error yang sama terulang -> tidak dikirim lagi
    assert len(terkirim) == 1
    assert "contoh error" in terkirim[0]
