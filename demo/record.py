"""Perekam video demo reimburse-buddy.

    uv run --group demo python demo/record.py --scene main|ch1..ch8|all [--base http://127.0.0.1:8090]
        [--manage-server --env-file <path>] [--out recordings] [--headed] [--slow-mo 250] [--dry-run]

Playwright baru diimpor di dalam runner, jadi modul ini (dan tesnya) tidak butuh Playwright.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from demo import scenes  # noqa: E402
from demo.srt import build_srt  # noqa: E402,F401  (diekspor untuk tes)

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
PROD_VALUES = ("prod", "production")
VIEWPORT = {"width": 1920, "height": 1080}
HEALTH_TIMEOUT = 120
STEP_TIMEOUT = 20  # detik, bila adegan tidak menyebut batas sendiri

CAPTION_JS = r"""
(() => {
  const KEY = '__demo_caption';
  function ensure() {
    let el = document.getElementById('__demo_caption');
    if (!el) {
      el = document.createElement('div');
      el.id = '__demo_caption';
      el.setAttribute('aria-hidden', 'true');
      el.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);max-width:72vw;' +
        'padding:14px 28px;border-radius:14px;background:rgba(10,14,24,.84);color:#fff;' +
        'font:600 28px/1.35 system-ui,Segoe UI,sans-serif;text-align:center;z-index:2147483647;' +
        'pointer-events:none;box-shadow:0 6px 28px rgba(0,0,0,.35);display:none';
      document.documentElement.appendChild(el);
    }
    return el;
  }
  function render() {
    if (!document.documentElement) return;
    const t = sessionStorage.getItem(KEY) || '';
    const el = ensure();
    el.textContent = t;
    el.style.bottom = document.getElementById('draft-bar') ? '96px' : '28px';  // jangan tutup bar total
    el.style.display = t ? 'block' : 'none';
  }
  window.__setCaption = (t) => { sessionStorage.setItem(KEY, t); render(); };
  document.addEventListener('DOMContentLoaded', render);
  new MutationObserver((_, ob) => { if (document.body) { render(); ob.disconnect(); } })
    .observe(document, { childList: true, subtree: true });
})();
"""


class Refused(Exception):
    """Perekam menolak berjalan (keamanan)."""


class SceneFailure(Exception):
    def __init__(self, step: str, message: str):
        super().__init__(f"[{step}] {message}")
        self.step = step
        self.message = message


# ---------------------------------------------------------------------------------------------------------
# Keamanan
# ---------------------------------------------------------------------------------------------------------

def validate_base(base: str) -> str:
    """Hanya 127.0.0.1 atau localhost. Mengembalikan base tanpa garis miring akhir."""
    u = urlparse(base)
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in ALLOWED_HOSTS:
        raise Refused(f"--base harus 127.0.0.1 atau localhost, bukan '{u.hostname}'. Perekam hanya untuk server lokal.")
    return base.rstrip("/")


def read_app_env(path: Path) -> str | None:
    """Nilai APP_ENV dari file env (hanya kunci itu yang dibaca; nilai lain tidak disentuh)."""
    value = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\s*(?:export\s+)?APP_ENV\s*=\s*(.*)$", line)
        if m:
            value = m.group(1).split(" #")[0].strip().strip("'\"")
    return value


def check_env_file(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_file():
        raise Refused(f"--env-file tidak ditemukan: {p}")
    if (read_app_env(p) or "").lower() in PROD_VALUES:
        raise Refused("--env-file menyetel APP_ENV ke production. Perekam menolak menjalankan server produksi.")
    return p


# ---------------------------------------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------------------------------------

def healthy(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=3) as resp:
            return resp.read().decode().strip() == "ok"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_healthy(base: str, timeout: float = HEALTH_TIMEOUT) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if healthy(base):
            return
        time.sleep(1)
    raise SceneFailure("server", f"/healthz belum 'ok' setelah {timeout:g} detik")


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


class ManagedServer:
    """uvicorn sebagai subprocess dari root repo; stop mematikan seluruh pohon proses."""

    def __init__(self, env_file: Path, base: str, log_path: Path):
        self.env_file, self.base = env_file, base
        self.port = urlparse(base).port or 80
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None

    def command(self) -> list[str]:
        return ["uv", "run", "--group", "rapidocr", "--env-file", str(self.env_file), "uvicorn",
                "--app-dir", "web", "reimburse_web.app:app", "--port", str(self.port)]

    def start(self) -> None:
        if port_in_use(self.port):
            raise SceneFailure("server", f"port {self.port} sudah dipakai; hentikan server lain dulu")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "ab")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen(self.command(), cwd=REPO, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        wait_healthy(self.base)

    def stop(self) -> None:
        if self.proc is None:
            return
        pid = self.proc.pid
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        end = time.monotonic() + 30
        while port_in_use(self.port) and time.monotonic() < end:
            time.sleep(0.5)

    def restart(self) -> None:
        self.stop()
        self.start()


# ---------------------------------------------------------------------------------------------------------
# Runner di browser
# ---------------------------------------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class LiveRunner:
    """API yang sama dengan scenes.DryRunner, dijalankan lewat Playwright `page`."""

    def __init__(self, page, base: str, *, quiet: bool = False, state: dict | None = None,
                 server: ManagedServer | None = None, t0: float | None = None):
        self.page, self.base, self.quiet = page, base, quiet
        self.state = state if state is not None else {}
        self.server = server
        self.t0 = t0 if t0 is not None else time.monotonic()
        self.events: list[tuple[float, str]] = []
        self.agent_seconds: float | None = None
        self._step = ""
        self._pending: str | None = None

    # -- util ------------------------------------------------------------------------------------------
    def _loc(self, selector: str):
        sel = selector if ">>" in selector else f"{selector} >> visible=true"
        return self.page.locator(sel).first

    def _fail(self, msg: str) -> SceneFailure:
        return SceneFailure(self._step, msg)

    def _wait_fn(self, js: str, arg, timeout: float, what: str) -> None:
        from playwright.sync_api import TimeoutError as PWTimeout
        try:
            self.page.wait_for_function(js, arg=arg, timeout=timeout * 1000)
        except PWTimeout:
            raise self._fail(f"tidak muncul dalam {timeout:g}s: {what}") from None

    # -- API -------------------------------------------------------------------------------------------
    def step(self, name: str) -> None:
        self._step = name
        if not self.quiet:
            print(f"    - {name}", flush=True)

    def caption(self, text: str) -> None:
        if self.quiet:
            return
        self.events.append((time.monotonic() - self.t0, text))
        self._pending = text
        self._apply_caption()

    def _apply_caption(self) -> None:
        """Pasang caption yang tertunda; di halaman kosong (belum ada navigasi) ditunda sampai goto berikutnya."""
        if self._pending is None:
            return
        try:
            self.page.evaluate("t => window.__setCaption && window.__setCaption(t)", self._pending)
            self._pending = None
        except Exception:  # noqa: BLE001 - about:blank: sessionStorage belum bisa dipakai
            pass

    def hold(self, seconds: float | None = None) -> None:
        if self.quiet:
            return
        self.page.wait_for_timeout(1000 * (scenes.PROOF_HOLD if seconds is None else seconds))

    def proof(self, text: str, seconds: float | None = None) -> None:
        self.caption(text)
        self.hold(seconds)

    def goto(self, path: str) -> None:
        self.page.goto(self.base + path, wait_until="domcontentloaded")
        self._apply_caption()

    def click(self, selector: str) -> None:
        try:
            self._loc(selector).click(timeout=15000, no_wait_after=True)
        except Exception as exc:  # noqa: BLE001
            raise self._fail(f"klik gagal: {selector} ({type(exc).__name__})") from exc

    def fill(self, selector: str, value: str) -> None:
        try:
            loc = self._loc(selector)
            if self.quiet or "date" in selector or len(value) > 90:
                loc.fill(value, timeout=15000)
            else:
                loc.fill("", timeout=15000)
                loc.press_sequentially(value, delay=45)
        except Exception as exc:  # noqa: BLE001
            raise self._fail(f"isi gagal: {selector} ({type(exc).__name__})") from exc

    def select(self, selector: str, value: str = "", label: str = "") -> None:
        try:
            loc = self._loc(selector)
            loc.select_option(label=label) if label else loc.select_option(value=value)
        except Exception as exc:  # noqa: BLE001
            raise self._fail(f"pilih gagal: {selector} ({type(exc).__name__})") from exc

    def check(self, selector: str) -> None:
        try:
            self._loc(selector).check(timeout=15000)
        except Exception:  # noqa: BLE001
            try:
                self._loc(selector).check(force=True, timeout=5000)
            except Exception as exc:  # noqa: BLE001
                raise self._fail(f"centang gagal: {selector} ({type(exc).__name__})") from exc

    def upload(self, selector: str, files: list[str]) -> None:
        paths = [str(scenes.SAMPLES / f) for f in files]
        missing = [p for p in paths if not Path(p).is_file()]
        if missing:
            raise self._fail(f"berkas contoh tidak ada: {missing[0]}")
        self.page.set_input_files(selector, paths)

    def expect_text(self, text: str, timeout: float = STEP_TIMEOUT) -> None:
        js = ("t => { const s = [...document.body.children].map(e => e.innerText || '').join(' ')"
              ".replace(/\\s+/g, ' ').toLowerCase(); return s.includes(t); }")
        self._wait_fn(js, _norm(text).lower(), timeout, f"teks '{text}'")

    def expect_value(self, selector: str, text: str, timeout: float = STEP_TIMEOUT) -> None:
        """Nilai input (bukan teks halaman). Selector boleh memakai sintaks Playwright (:has-text)."""
        end = time.monotonic() + timeout
        js = "(els, t) => els.some(e => (e.value || '').toLowerCase().includes(t.toLowerCase()))"
        while True:
            try:
                if self.page.locator(selector).evaluate_all(js, text):
                    return
            except Exception:  # noqa: BLE001 - halaman sedang berpindah
                pass
            if time.monotonic() > end:
                raise self._fail(f"tidak muncul dalam {timeout:g}s: nilai '{text}' di {selector}")
            time.sleep(0.4)

    def expect_selector(self, selector: str, timeout: float = STEP_TIMEOUT) -> None:
        from playwright.sync_api import TimeoutError as PWTimeout
        try:
            self.page.wait_for_selector(selector, state="attached", timeout=timeout * 1000)
        except PWTimeout:
            raise self._fail(f"elemen tidak muncul dalam {timeout:g}s: {selector}") from None

    def login_as(self, emp: str) -> None:
        name = scenes.PROFILES.get(emp, emp)
        signed = self.page.locator("#signed-in")
        if signed.count() and name in (signed.first.inner_text() or ""):
            return
        if self.page.locator("#demo-profiles").count() == 0:
            if self.page.locator("#logout").count():
                self.page.locator("#logout").first.click()
            else:
                self.goto("/login")
            self.expect_selector("#demo-profiles")
        self._loc(f"#demo-{emp}").click(timeout=15000)
        self.expect_selector("#signed-in")
        self.expect_text(f"Masuk sebagai {name}")

    def open_same_tab(self, selector: str) -> None:
        if self.quiet:
            return  # penyiapan tanpa rekaman tidak perlu membuka file
        href = self._loc(selector).get_attribute("href")
        if not href:
            raise self._fail(f"tidak ada href: {selector}")
        self.page.goto(href if href.startswith("http") else self.base + href)

    def go_back(self) -> None:
        if self.quiet:
            return
        self.page.go_back(wait_until="domcontentloaded")

    def accept_next_dialog(self) -> None:
        self.page.once("dialog", lambda d: d.accept())

    def remember_url(self, key: str) -> None:
        self.state[key] = self.page.url

    def goto_remembered(self, key: str) -> None:
        if key not in self.state:
            raise self._fail(f"URL '{key}' belum tercatat")
        self.page.goto(self.state[key], wait_until="domcontentloaded")

    def agent_wait(self) -> None:
        start = time.monotonic()
        self.expect_text("Dasar persetujuan", timeout=scenes.AGENT_TIMEOUT)
        self.agent_seconds = time.monotonic() - start
        print(f"    [agent] gerbang persetujuan muncul setelah {self.agent_seconds:.0f} detik "
              f"(potong sekitar {self.agent_seconds:.0f} detik saat editing)", flush=True)

    def restart_server(self) -> None:
        if self.server is not None:
            self.server.restart()
        else:
            input("Restart the server now, then press Enter ")
            wait_healthy(self.base)


# ---------------------------------------------------------------------------------------------------------
# Satu adegan
# ---------------------------------------------------------------------------------------------------------

def _clean_start(pw, base: str, scene: scenes.Scene, server) -> dict:
    """Konteks tanpa rekaman: masuk sebagai finance, reset demo, lalu siapkan prasyarat. Mengembalikan state."""
    browser = pw.chromium.launch(headless=True)
    try:
        ctx = browser.new_context(viewport=VIEWPORT, base_url=base)
        r = ctx.request.post(base + "/login/demo", form={"employee_id": "EMP-102"}, max_redirects=0)
        if r.status >= 400:
            raise SceneFailure("clean-start", f"POST /login/demo -> {r.status}")
        r = ctx.request.post(base + "/admin/reset", max_redirects=0)
        if r.status >= 400:
            raise SceneFailure("clean-start", f"POST /admin/reset -> {r.status}")
        state: dict = {}
        if scene.prereq:
            print("  prasyarat (tidak direkam)...", flush=True)
            page = ctx.new_page()
            runner = LiveRunner(page, base, quiet=True, state=state, server=server)
            try:
                scene.prereq(runner)
            except Exception:
                state["_failed_page"] = page
                raise
        return state
    finally:
        browser.close()


def run_scene(pw, scene: scenes.Scene, *, base: str, out: Path, headed: bool, slow_mo: int,
              server: ManagedServer | None) -> dict:
    ts = datetime.now().strftime("%Y%m%d-%H%M")
    result = {"scene": scene.name, "status": "OK", "seconds": 0.0, "video": "", "note": ""}
    started = time.monotonic()
    print(f"[{scene.name}] {scene.title}", flush=True)
    out.mkdir(parents=True, exist_ok=True)
    try:
        state = _clean_start(pw, base, scene, server)
    except Exception as exc:  # noqa: BLE001
        result.update(status="FAILED", note=f"setup: {exc}", seconds=time.monotonic() - started)
        return result
    browser = pw.chromium.launch(headless=not headed, slow_mo=slow_mo)
    ctx = browser.new_context(viewport=VIEWPORT, record_video_dir=str(out / "_tmp"),
                              record_video_size=VIEWPORT, base_url=base, locale="id-ID")
    ctx.add_init_script(CAPTION_JS)
    ctx.set_default_timeout(15000)
    page = ctx.new_page()
    runner = LiveRunner(page, base, state=state, server=server)
    failure = None
    try:
        scene.run(runner)
    except Exception as exc:  # noqa: BLE001
        failure = exc
        step = re.sub(r"[^A-Za-z0-9]+", "-", getattr(exc, "step", runner._step) or "unknown").strip("-")[:40]
        shot = out / f"{scene.name}-FAILED-{step}.png"
        try:
            page.screenshot(path=str(shot))
        except Exception:  # noqa: BLE001
            pass
        result.update(status="FAILED", note=f"{exc} (screenshot: {shot.name})")
    duration = time.monotonic() - runner.t0
    video_tmp = page.video.path() if page.video else None
    ctx.close()
    browser.close()
    if video_tmp and Path(video_tmp).exists():
        final = out / f"{scene.name}-{ts}.webm"
        Path(video_tmp).replace(final)
        result["video"] = str(final)
        (out / f"{scene.name}-{ts}.srt").write_text(build_srt(runner.events, end=duration), encoding="utf-8")
    if runner.agent_seconds is not None:
        result["note"] = (result["note"] + f" agent_wait={runner.agent_seconds:.0f}s").strip()
    result["seconds"] = time.monotonic() - started
    return result


def print_summary(results: list[dict]) -> None:
    print("\nRingkasan")
    print(f"{'scene':<7}{'status':<9}{'detik':>7}  video / catatan")
    for r in results:
        print(f"{r['scene']:<7}{r['status']:<9}{r['seconds']:>7.0f}  {r['video'] or '-'}")
        if r["note"]:
            print(f"{'':<23}{r['note']}")


def print_dry(names: list[str]) -> None:
    for name in names:
        sc = scenes.SCENES[name]
        print(f"== {name}: {sc.title}")
        for i, s in enumerate(scenes.dry_run(name), 1):
            print(f"{i:>4}. {s.kind:<16} {s.detail}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rekam video demo reimburse-buddy dengan Playwright.")
    p.add_argument("--scene", required=True, help="main, ch1..ch8, atau all")
    p.add_argument("--base", default="http://127.0.0.1:8090")
    p.add_argument("--manage-server", action="store_true", help="jalankan dan hentikan uvicorn sendiri")
    p.add_argument("--env-file", help="file env untuk server (dengan --manage-server)")
    p.add_argument("--out", default="recordings")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--slow-mo", type=int, default=250)
    p.add_argument("--dry-run", action="store_true", help="cetak langkah tanpa browser dan server")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    names = list(scenes.SCENES) if args.scene == "all" else [args.scene]
    for n in names:
        if n not in scenes.SCENES:
            print(f"Adegan tidak dikenal: {n}. Pilih: {', '.join(scenes.SCENES)}, all", file=sys.stderr)
            return 2
    try:
        base = validate_base(args.base)
        env_file = None
        if args.manage_server:
            if not args.env_file:
                raise Refused("--manage-server butuh --env-file")
            env_file = check_env_file(args.env_file)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print_dry(names)
        return 0

    from playwright.sync_api import sync_playwright  # impor lambat: tes dan --dry-run tidak butuh Playwright

    out = Path(args.out)
    if not out.is_absolute():
        out = REPO / out
    server = None
    results: list[dict] = []
    try:
        if args.manage_server:
            server = ManagedServer(env_file, base, out / f"server-{datetime.now():%Y%m%d-%H%M}.log")
            print("Menjalankan server...", flush=True)
            server.start()
        else:
            if not healthy(base):
                print(f"Server di {base} belum sehat (/healthz). Jalankan dulu, atau pakai --manage-server.",
                      file=sys.stderr)
                return 2
        with sync_playwright() as pw:
            for n in names:
                try:
                    results.append(run_scene(pw, scenes.SCENES[n], base=base, out=out, headed=args.headed,
                                             slow_mo=args.slow_mo, server=server))
                except Exception as exc:  # noqa: BLE001
                    results.append({"scene": n, "status": "FAILED", "seconds": 0.0, "video": "", "note": str(exc)})
                if server is not None and not healthy(base):  # adegan gagal saat server mati: nyalakan lagi
                    try:
                        server.restart()
                    except Exception:  # noqa: BLE001
                        pass
    except KeyboardInterrupt:
        print("\nDibatalkan.", file=sys.stderr)
        return 130
    finally:
        if server is not None:
            server.stop()
        tmp = (out / "_tmp")
        if tmp.is_dir():
            try:
                tmp.rmdir()
            except OSError:
                pass
    print_summary(results)
    return 0 if results and all(r["status"] == "OK" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
