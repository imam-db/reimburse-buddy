"""Paged listing of one employee's reimburse@ mailbox."""


def message(mid, owner_id, date, subject="Synthetic message"):
    return {
        "id": mid,
        "owner_id": owner_id,
        "from": "sender@example.com",
        "date": date,
        "subject": subject,
        "body": f"Body of {mid}",
        "attachments": [{"name": f"{mid}.pdf", "type": "receipt"}],
        "mailbox": "reimburse@nusantara-niaga.example",
        "forwarded_by": "employee@nusantara-niaga.example",
    }


def test_paged_messages_oldest_first(make_store):
    store = make_store(inbox=[
        message("MSG-9001", "EMP-9001", "2026-01-01", "first"),
        message("MSG-9003", "EMP-9001", "2026-01-03", "third"),
        message("MSG-9002", "EMP-9001", "2026-01-02", "second"),
    ])

    first = store.list_messages("EMP-9001", limit=2)
    assert first["total"] == 3
    assert first["offset"] == 0
    assert first["limit"] == 2
    assert first["next_offset"] == 2
    assert [m["id"] for m in first["messages"]] == ["MSG-9001", "MSG-9002"]
    assert set(first["messages"][0]) == {"id", "date", "from", "subject", "snippet", "attachments"}
    assert "score" not in first["messages"][0]

    last = store.list_messages("EMP-9001", offset=2, limit=2)
    assert last["total"] == 3
    assert last["offset"] == 2
    assert last["next_offset"] is None
    assert [m["id"] for m in last["messages"]] == ["MSG-9003"]


def test_other_employees_messages_are_excluded(make_store):
    store = make_store(inbox=[
        message("MSG-9101", "EMP-9001", "2026-02-01"),
        message("MSG-9102", "EMP-9002", "2026-02-02"),
        message("MSG-9103", "EMP-9001", "2026-02-03"),
    ])

    result = store.list_messages("EMP-9001")
    assert result["total"] == 2
    assert [m["id"] for m in result["messages"]] == ["MSG-9101", "MSG-9103"]
