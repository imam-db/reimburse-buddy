"""Tes perekam demo (demo/). Berjalan tanpa browser dan tanpa server."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo import record, scenes  # noqa: E402
from demo.srt import build_srt, format_ts  # noqa: E402


def test_format_ts():
    assert format_ts(0) == "00:00:00,000"
    assert format_ts(61.5) == "00:01:01,500"
    assert format_ts(3661.0074) == "01:01:01,007"


def test_build_srt_cues_run_until_next_change():
    srt = build_srt([(0.0, "Satu"), (2.5, "Dua"), (5.0, "")], end=9.0)
    assert srt == ("1\n00:00:00,000 --> 00:00:02,500\nSatu\n\n"
                   "2\n00:00:02,500 --> 00:00:05,000\nDua\n")


def test_build_srt_last_cue_uses_end_or_default():
    assert "00:00:04,000 --> 00:00:10,000" in build_srt([(4.0, "A")], end=10.0)
    assert "00:00:04,000 --> 00:00:07,000" in build_srt([(4.0, "A")])


def test_build_srt_sorts_and_skips_empty():
    srt = build_srt([(3.0, "B"), (0.0, "A"), (1.0, "  ")], end=4.0)
    assert srt.count("-->") == 2
    assert srt.index("A") < srt.index("B")
    assert build_srt([]) == ""


@pytest.mark.parametrize("base", ["http://example.com:8090", "https://reimburse.example.com", "http://10.0.0.5:8090",
                                  "http://127.0.0.1.evil.com:8090"])
def test_refuses_non_localhost_base(base, capsys):
    assert record.main(["--scene", "ch1", "--base", base, "--dry-run"]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_accepts_localhost_bases():
    assert record.validate_base("http://127.0.0.1:8090/") == "http://127.0.0.1:8090"
    assert record.validate_base("http://localhost:8090") == "http://localhost:8090"


@pytest.mark.parametrize("value", ["prod", "production", "PRODUCTION", '"production"'])
def test_refuses_prod_env_file(tmp_path, capsys, value):
    env = tmp_path / ".env.prod"
    env.write_text(f"FOO=bar\nAPP_ENV={value}\n", encoding="utf-8")
    code = record.main(["--scene", "ch1", "--manage-server", "--env-file", str(env), "--dry-run"])
    err = capsys.readouterr().err
    assert code == 2 and "REFUSED" in err and "bar" not in err


def test_accepts_non_prod_env_file(tmp_path):
    env = tmp_path / ".env.demo"
    env.write_text("APP_ENV=\nOTHER=1\n", encoding="utf-8")
    assert record.check_env_file(env) == env
    env.write_text("APP_ENV=dev\n", encoding="utf-8")
    assert record.check_env_file(env) == env


def test_manage_server_needs_env_file(capsys):
    assert record.main(["--scene", "ch1", "--manage-server", "--dry-run"]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_import_does_not_need_playwright():
    import importlib
    src = Path(record.__file__).read_text(encoding="utf-8")
    top = src.split("class Refused")[0]
    assert "import playwright" not in top and "from playwright" not in top
    importlib.import_module("demo.scenes")


def test_registry_has_all_scenes():
    assert list(scenes.SCENES) == ["main", "ch1", "ch2", "ch3", "ch4", "ch5", "ch6", "ch7", "ch8"]


@pytest.mark.parametrize("name", list(scenes.SCENES))
def test_dry_run_returns_steps(name):
    steps = scenes.dry_run(name)
    assert steps and any(s.kind == "caption" for s in steps)
    assert any(s.kind in ("expect_text", "expect_selector") for s in steps)


def _captions(name: str) -> str:
    return "\n".join(s.detail for s in scenes.dry_run(name) if s.kind == "caption")


def _all(name: str) -> str:
    return "\n".join(s.detail for s in scenes.dry_run(name))


def test_main_has_the_proof_moments():
    caps = _captions("main")
    for key in ("Bukti 1", "Bukti 1b", "Bukti 2", "Bukti 3", "Bukti 4", "Bukti 5", "Bukti 6", "Pasal 4"):
        assert key in caps, key
    steps = scenes.dry_run("main")
    assert any(s.kind == "restart_server" for s in steps)
    assert any(s.kind == "agent_wait" for s in steps)
    assert "Agent sedang menyelidiki… (dipercepat saat editing)" in caps
    uploads = [s.detail for s in steps if s.kind == "upload"]
    assert uploads and ".pdf" not in uploads[0] and uploads[0].count(".") >= 4
    assert "1.120.000" in _all("main") and "Rumah Makan Sari Rasa" in _all("main")


def test_chapter_key_texts():
    assert "Pasal 4" in _all("ch2")
    assert "Permintaan persetujuan dikirim ke atasan (APR-" in _all("ch3")  # any number: ids never repeat (PR #62)
    assert "10 klaim, 7 bersih, 3 pengecualian" in _all("ch4")
    assert "Rp2.400.000" in _all("ch5") and "CLM-2026-0120" in _all("ch5")
    assert "Dasar persetujuan" in _all("ch6")
    assert "Rencana selesai" in _all("ch7")
    assert "EMP-100" in _all("ch7") and "EMP-202" in _all("ch7")
    assert any(s.kind == "restart_server" for s in scenes.dry_run("ch8")) and "Rencana selesai" in _all("ch8")


def test_dry_run_cli_prints_steps(capsys):
    assert record.main(["--scene", "all", "--dry-run"]) == 0
    out = capsys.readouterr().out
    for key in ("== main", "== ch8", "Bukti 2", "Pasal 4", "Rencana selesai"):
        assert key in out


def test_ch7_requester_is_dewi_not_hendra():
    steps = scenes.dry_run("ch7")
    sel = [s.detail for s in steps if s.kind == "select" and "requester" in s.detail]
    assert sel and "EMP-100" in sel[0]
    logins = [s.detail for s in steps if s.kind == "login_as"]
    assert logins[0].startswith("EMP-102") and logins[-1].startswith("EMP-101")


def test_proof_clears_caption_after_hold_in_dry_run():
    r = scenes.DryRunner()
    r.proof("Bukti X", 2)
    assert [(s.kind, s.detail) for s in r.steps] == [("caption", "Bukti X"), ("hold", "2s"), ("caption", "")]


def test_login_as_clears_caption_first():
    r = scenes.DryRunner()
    r.login_as("EMP-101")
    assert (r.steps[0].kind, r.steps[0].detail) == ("caption", "")


def test_srt_ends_cue_at_empty_caption():
    srt = build_srt([(1.0, "Rencana"), (4.0, ""), (9.0, "Lain")], end=12.0)
    assert "00:00:01,000 --> 00:00:04,000\nRencana" in srt
    assert "00:00:09,000 --> 00:00:12,000\nLain" in srt


def test_caption_js_zoom_and_sticky_bars():
    js = record.CAPTION_JS
    assert "__PAGE_ZOOM__" in js and "sticky" in js and "fixed" in js and "scroll" in js
    assert record.PAGE_ZOOM > 1
