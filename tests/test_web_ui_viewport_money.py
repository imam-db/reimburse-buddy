"""UI spec tests 1 and 7: every main page is mobile-ready, and every Rp on finance/employee pages comes from
the rule engine or the claim data (the same invented-Rp check the eval and the Q&A guard use)."""
import html
import json
import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import plans  # noqa: F401  # puts agent/ on sys.path
from reimburse_agent.money import invented_rp  # noqa: E402
from reimburse_buddy.decision import draft_decision  # noqa: E402
from reimburse_buddy.models import rupiah, to_jsonable  # noqa: E402

VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1">'


@pytest.fixture
def client(tmp_path, monkeypatch):
    from reimburse_agent.audit import JsonlAuditLog
    from reimburse_agent.office.store import FilePlanStore

    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    web.reset_all()
    plans.STATE.update(store=FilePlanStore(tmp_path / "plans"), log=JsonlAuditLog())
    yield TestClient(web.app)
    plans.STATE.clear()
    web.reset_all()


def _main_pages(client) -> dict[str, str]:
    urls = ["/", "/admin", "/admin/plans", "/employee", "/employee/claims", "/employee/new",
            "/employee/claims/CLM-2026-0110", "/admin/claims/CLM-2026-0110/audit"]
    urls += [f"/admin/claims/{c['id']}" for c in web.store.claims]
    return {u: client.get(u).text for u in urls}


def test_every_main_page_has_the_viewport_tag(client):
    for url, markup in _main_pages(client).items():
        assert VIEWPORT in markup, url


def test_confirmation_page_has_viewport_tag(client):
    from datetime import datetime, timezone

    from reimburse_agent.office import plan as P
    from reimburse_agent.office import rules as R

    intents = P.read_request("Tolong kirim notulen ke Pak Budi ya.", [])
    plan, _ = P.build_plan(run_id="vp1", version=1, request="req", requester="EMP-101", intents=intents,
                           resolved={"Pak Budi": "EMP-202"}, now=datetime(2026, 10, 26, 2, tzinfo=timezone.utc),
                           directory=plans.directory(), rules=R.default_rules(), notices=[])
    plans.store().save_plan(plan)
    markup = client.get("/admin/plans/vp1").text
    assert "Belum ada yang dikirim" in markup
    assert re.search(r"<meta name=.?viewport.? content=.width=device-width, initial-scale=1.>", markup)


def _visible(markup: str) -> str:
    markup = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def _engine_texts() -> list[str]:
    out = []
    for c in web.store.claims:
        chk = web.run_check(c["id"])
        out += [json.dumps(to_jsonable(chk)), draft_decision(web.store, chk, language="both"), json.dumps(c)]
    # The triage page also shows the engine's own sum of held money across the exceptions.
    held = sum((web.run_check(x["claim_id"]).total_pending
                for x in web.triage_claims(web.store, web.BATCH)["exceptions"]), Decimal("0"))
    out.append(rupiah(held))
    return out


def test_every_rp_on_pages_comes_from_the_engine(client):
    allowed = _engine_texts()
    for url, markup in _main_pages(client).items():
        text = _visible(markup)
        assert invented_rp(text, allowed) == [], url


def test_invented_rp_check_catches_a_made_up_amount(client):
    text = _visible(client.get("/admin").text) + " Rp9.876.543"
    assert invented_rp(text, _engine_texts()) == [9876543]
