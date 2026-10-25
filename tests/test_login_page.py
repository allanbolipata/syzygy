"""Tests for the static login page (static/login.html)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGIN_HTML = REPO_ROOT / "static" / "login.html"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _html():
    assert LOGIN_HTML.is_file(), "static/login.html is missing"
    return LOGIN_HTML.read_text(encoding="utf-8")


def test_login_page_exists():
    _html()


def test_login_page_links_style_css_like_index():
    html = _html()
    assert '<link rel="stylesheet" href="style.css">' in html


def test_login_page_content():
    html = _html()
    assert "<title>Syzygy login</title>" in html
    assert "Sign in with your Keycloak account" in html
    assert "Sign in with Keycloak" in html
    assert "/auth/redirect" in html


def test_login_page_checks_session_on_load():
    html = _html()
    assert 'fetch("/v1/auth/me")' in html
    assert '"/ui"' in html


def test_login_page_has_no_external_assets():
    html = _html()
    assert "http://" not in html and "https://" not in html
    srcs = re.findall(r"<script[^>]*\bsrc=\"([^\"]+)\"", html)
    for src in srcs:
        assert not src.startswith(("//", "http://", "https://"))
        assert (REPO_ROOT / "static" / src).is_file()


def test_login_page_inline_script_is_valid_js():
    html = _html()
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, flags=re.S)
    assert inline, "expected at least one inline script"
    js = "\n".join(inline)
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available for --check")
    proc = subprocess.run(
        [node, "--check"], input=js.encode(), capture_output=True, timeout=30
    )
    assert proc.returncode == 0, f"node --check failed: {proc.stderr.decode()}"
