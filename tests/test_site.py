"""The static Vercel demo: correct results, and nothing private or executable. No network."""
import json
import re
import sys

import pytest

from clearance.config import ROOT

sys.path.insert(0, str(ROOT / "scripts"))
import build_site  # noqa: E402


@pytest.fixture(scope="module")
def mails():
    return build_site.build_data()


def test_site_matches_the_expected_decisions(mails):
    outcomes = [m["outcome"] for m in mails]
    assert len(mails) == 12 and outcomes.count("APPROVE") == 2 and outcomes.count("ESCALATE") == 2 and outcomes.count("HOLD") == 8
    assert all(m["chain"] for m in mails)


def test_site_masks_every_email_address(mails):
    for m in mails:
        blob = json.dumps({k: m[k] for k in ("sender", "text", "findings", "callback", "drafts", "case")})
        assert not re.search(r"[A-Za-z0-9._%+-]+@", blob)


def test_mail_text_cannot_break_out_of_the_page():
    page = build_site.render_page([{"text": "</script><script>alert(1)</script>"}])
    data = page.split('type="application/json">')[1].split("</script>")[0]
    assert "<script>" not in data and json.loads(data)[0]["text"].startswith("</script>")


def test_page_never_renders_mail_as_html():
    assert "innerHTML" not in build_site.PAGE and "document.write" not in build_site.PAGE


def test_page_has_no_outside_requests():
    assert not re.search(r"https?://|fetch\(|XMLHttpRequest|<script src|<link", build_site.PAGE)


def test_published_page_is_current():
    published = (ROOT / "site" / "index.html").read_text()
    assert "Static demo" in published and "application/json" in published


def test_vercel_only_serves_the_static_site():
    config = json.loads((ROOT / "vercel.json").read_text())
    assert config["builds"] == [{"src": "site/**", "use": "@vercel/static"}]
    ignored = (ROOT / ".vercelignore").read_text()
    assert "streamlit_app.py" in ignored and "fixtures/" in ignored
