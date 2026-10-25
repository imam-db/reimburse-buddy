import pytest

from reimburse_buddy.data_store import DataStore
from reimburse_buddy.decision import draft_decision
from reimburse_buddy.rules import check_claim


@pytest.fixture
def check():
    s = DataStore()
    return s, check_claim(s, "CLM-2026-0103", [], {"IT-1": "approve_capped"})


def test_indonesian_note(check):
    s, c = check
    text = draft_decision(s, c, language="id")
    assert "Keputusan: DISETUJUI SEBAGIAN" in text and "Disetujui: Rp1.500.000" in text


def test_bilingual_note_has_both_and_same_numbers(check):
    s, c = check
    text = draft_decision(s, c, "capped", language="both")
    id_part, en_part = text.split("\n\n---\n\n")
    assert "DISETUJUI SEBAGIAN" in id_part and "PARTIALLY APPROVED" in en_part
    assert id_part.count("Rp1.500.000") == en_part.count("Rp1.500.000") == 1
    assert "Catatan reviewer: capped" in id_part


def test_invalid_language(check):
    s, c = check
    with pytest.raises(ValueError):
        draft_decision(s, c, language="fr")


def test_skill_mentions_every_gap_code():
    from pathlib import Path

    from reimburse_buddy import gaps
    skill = (Path(__file__).parents[1] / "skills/claim-auditor/SKILL.md").read_text()
    # B_ONLY codes are documented in the version B addendum until SKILL.md is unfrozen (then drop this).
    for code in set(gaps.ALL) - set(gaps.B_ONLY):
        assert f"`{code}`" in skill, code
    for tool in ("triage_claims", "update_claim_status", "request_approval", "send_message"):
        assert tool in skill
