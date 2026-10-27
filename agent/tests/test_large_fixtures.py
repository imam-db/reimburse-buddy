"""The large synthetic sets are frozen (regenerating gives identical files), sized 40-80 per employee, and
set A and set B share no paraphrase, so set B stays held out."""

import json

from eval import large_fixtures as L


def test_regenerating_reproduces_the_frozen_files(tmp_path):
    for frozen, s, seed in ((L.LARGE_A, 0, 8101), (L.LARGE_B, 1, 8202)):
        L.write(tmp_path / frozen.name, s, seed)
        for f in ("inbox", "calendar", "claims", "replies", "truth"):
            assert (tmp_path / frozen.name / f"{f}.json").read_text() == (frozen / f"{f}.json").read_text(), (frozen.name, f)


def test_mailbox_sizes_and_truth():
    for d in (L.LARGE_A, L.LARGE_B):
        inbox = json.loads((d / "inbox.json").read_text())
        truth = json.loads((d / "truth.json").read_text())
        ids = {m["id"] for m in inbox}
        for t in truth.values():
            n = sum(m["owner_id"] == t["employee_id"] for m in inbox)
            assert 40 <= n <= 80, (d.name, t["employee_id"], n)
            assert t["needed"] in ids or t["via_reply"]


def test_sets_use_disjoint_paraphrases():
    for name in dir(L):
        v = getattr(L, name)
        if isinstance(v, tuple) and len(v) == 2 and all(isinstance(x, list) for x in v) and name.isupper() and name not in ("CITIES", "CLIENTS"):
            assert not set(v[0]) & set(v[1]), name
