"""Cek dasar: kode bisa dikompilasi, secrets tidak ikut ter-commit, aplikasi bisa start."""
import pathlib
import py_compile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY_FILES = [p for p in ROOT.rglob("*.py")
            if not any(part.startswith(".") or part in {"venv", "__pycache__"}
                       for part in p.relative_to(ROOT).parts)]


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_file_python_bisa_dikompilasi(path):
    py_compile.compile(str(path), doraise=True)


def test_secrets_asli_tidak_ikut_ke_repo():
    assert not (ROOT / ".streamlit" / "secrets.toml").exists(), \
        "secrets.toml berisi kunci asli, jangan di-commit. Isi lewat Streamlit → Settings → Secrets."


def test_aplikasi_bisa_start_tanpa_secrets():
    """Tanpa secrets, aplikasi harus menampilkan pesan/login, bukan crash."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not at.exception, [e.message for e in at.exception]
