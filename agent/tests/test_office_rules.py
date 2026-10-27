"""Directory + operating rules (pure functions) on the synthetic office data."""
from datetime import datetime, timedelta, timezone

from reimburse_agent import injection as inj
from reimburse_agent.office import plan as P
from reimburse_agent.office import rules as R
from reimburse_agent.office.directory import Directory

D = Directory.load()
RU = R.default_rules()
WIB = RU.tz("WIB")


def people(*ids):
    return [D.get(i) for i in ids]


def test_one_budi_vs_two():
    assert [c.id for c in D.find("Pak Budi")] == ["EMP-201", "EMP-202"]
    assert [c.id for c in D.find("Budi Santoso")] == ["EMP-201"]
    assert D.find("Pak Joko") == []


def test_two_budis_one_question_single_match_none():
    two = P.read_request("Tolong kirim notulen ke Pak Budi ya, terus jadwalin follow-up Jumat siang.")
    q = P.who_question(two, D, {})
    assert q and q["options"] == ["EMP-201", "EMP-202"] and "Budi" in q["id_text"] and "Which one" in q["en"]
    assert P.who_question(two, D, {q["name"]: "EMP-202"}) is None  # the answer covers both actions
    one = P.read_request("Kirim notulen ke Bu Sari")
    assert P.who_question(one, D, {}) is None
    assert P.resolve_names(one, D, {}) == {"Bu Sari": "EMP-003"}
    none = P.read_request("Kirim notulen ke Pak Joko")
    q0 = P.who_question(none, D, {})
    assert q0["options"] == [] and "Tidak ada" in q0["id_text"]


def test_answer_must_be_an_option():
    q = P.who_question(P.read_request("kirim ke Pak Budi"), D, {})
    assert P.valid_answer(q, "emp-201", D) == "EMP-201"
    assert P.valid_answer(q, "EMP-001", D) is None
    assert P.valid_answer(q, "budi@x.example", D) is None


def test_friday_prayer():
    noon = datetime(2026, 10, 30, 12, 0, tzinfo=WIB)
    v = R.check_slot(noon, 60, people("EMP-201"))
    assert any("Friday prayer" in x for x in v)
    assert R.check_slot(datetime(2026, 10, 30, 13, 0, tzinfo=WIB), 60, people("EMP-201")) == []
    assert R.check_slot(datetime(2026, 10, 29, 12, 0, tzinfo=WIB), 60, people("EMP-201")) == []  # Thursday
    nxt = R.next_allowed_slots(noon, 60, people("EMP-201"))
    assert nxt[0] == datetime(2026, 10, 30, 13, 0, tzinfo=WIB)


def test_wib_wit_overlap():
    p = people("EMP-201", "EMP-202")  # Jakarta WIB, Jayapura WIT (+2h)
    # 15:30 WIB = 17:30 WIT: outside WIT office hours
    v = R.check_slot(datetime(2026, 10, 28, 15, 30, tzinfo=WIB), 60, p)
    assert v and all("EMP-202" in x for x in v) and "outside office hours" in v[0]
    # 08:00 WIB = 10:00 WIT is fine for both
    assert R.check_slot(datetime(2026, 10, 28, 8, 0, tzinfo=WIB), 60, p) == []
    # next slots after Wed 15:00 WIB land on Thu 08:00 WIB (10:00 WIT), never after 15:00 WIB
    nxt = R.next_allowed_slots(datetime(2026, 10, 28, 15, 0, tzinfo=WIB), 60, p, n=3)
    assert nxt[0] == datetime(2026, 10, 29, 8, 0, tzinfo=WIB)
    for t in nxt:
        assert R.check_slot(t, 60, p) == []
    # Friday: WIT prayer 11:30-13:00 WIT = 09:30-11:00 WIB also blocks
    assert any("EMP-202" in x and "prayer" in x for x in R.check_slot(datetime(2026, 10, 30, 10, 0, tzinfo=WIB), 60, p))


def test_holiday():
    xmas = datetime(2026, 12, 25, 10, 0, tzinfo=WIB)
    v = R.check_slot(xmas, 60, people("EMP-201"))
    assert any("public holiday" in x for x in v)
    assert R.next_allowed_slots(xmas, 60, people("EMP-201"))[0].date() == datetime(2026, 12, 28).date()


def test_outside_hours_and_weekend():
    assert any("outside office hours" in x for x in R.check_slot(datetime(2026, 10, 28, 7, 0, tzinfo=WIB), 60, people("EMP-201")))
    assert any("outside office hours" in x for x in R.check_slot(datetime(2026, 10, 28, 16, 30, tzinfo=WIB), 60, people("EMP-201")))
    assert any("not a working day" in x for x in R.check_slot(datetime(2026, 10, 31, 10, 0, tzinfo=WIB), 60, people("EMP-201")))
    assert R.check_slot(datetime(2026, 10, 28, 10, 0), 60, people("EMP-201")) == ["start time has no time zone"]


def test_partial_time_proposes_nearest_allowed_slot_as_assumption():
    now = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)  # Mon 09:00 WIB
    hint = P.parse_when("jadwalin follow-up Jumat siang")
    s = P.propose_slot(hint, now, 60, people("EMP-101", "EMP-201"), "WIB", RU)
    assert s.start == datetime(2026, 10, 30, 13, 0, tzinfo=WIB)
    assert any("prayer" in a["en"] and "13:00 WIB" in a["en"] for a in s.assumptions)
    none = P.propose_slot(None, now, 60, people("EMP-101"), "WIB", RU)
    assert none.start == now + timedelta(minutes=30) and "No time was given" in none.assumptions[0]["en"]


def test_time_args_come_from_rules_tool_output():
    """Was: every time argument refused (PR #22 TODO). Now: allowed iff it is in the rules tool's output."""
    offered = {R.iso(t) for t in R.next_allowed_slots(datetime(2026, 10, 30, 12, 0, tzinfo=WIB), 60, people("EMP-201"))}
    good = sorted(offered)[0]
    args = {"attendees": ["EMP-201"], "title": "x", "start": good, "duration_min": 60}
    assert inj.arg_source_violations("create_calendar_event", args, directory_ids=D.ids(), operating_times=offered) == []
    bad = dict(args, start="2026-10-30T05:00:00Z")
    assert inj.arg_source_violations("create_calendar_event", bad, directory_ids=D.ids(), operating_times=offered)
    assert inj.arg_source_violations("create_calendar_event", args, directory_ids=D.ids())  # no rules output
    assert inj.arg_source_violations("send_email", {"to": ["budi@nusantara-niaga.example"]}, directory_ids=D.ids())
    assert inj.arg_source_violations("send_email", {"to": ["EMP-999"]}, directory_ids=D.ids())
