"""Employee claim page shows sensitive findings only as 'under finance review' (same masking as Q&A)."""
import re

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web

from conftest import install_test_users, login_as

DUP_CLAIM = "CLM-2026-0104"  # IT-6 is duplicate_suspected
SENSITIVE_TEXT = ["possible duplicate", "duplicate", "dup_suspected", "receipt_amount_mismatch",
                  "receipt amount mismatch", "multiple_receipts", "multiple receipts", "receipt_shared"]


@pytest.fixture
def client(monkeypatch):
    web.reset_all()
    install_test_users()
    yield TestClient(web.app)
    web.reset_all()


def _visible(html: str) -> str:
    html = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html).lower()


@pytest.mark.parametrize("flag", ["0", "1"])
def test_employee_page_masks_duplicate(client, monkeypatch, flag):
    monkeypatch.setenv("EMPLOYEE_QA", flag)
    login_as(client, "EMP-003", f"/employee/claims/{DUP_CLAIM}")  # the claim's owner
    r = client.get(f"/employee/claims/{DUP_CLAIM}")
    assert r.status_code == 200
    text = _visible(r.text)
    for s in SENSITIVE_TEXT:
        assert s not in text, s
    assert "Sedang ditinjau finance" in r.text and "Under finance review" in r.text


def test_masking_uses_qa_source_of_truth():
    from reimburse_web import upload_flow
    from reimburse_agent.qa import tools
    assert upload_flow.is_sensitive_item is tools.is_sensitive_item


def test_admin_page_still_shows_finding(client):
    login_as(client, "EMP-102")
    text = _visible(client.get(f"/admin/claims/{DUP_CLAIM}").text)
    assert "duplikat" in text
