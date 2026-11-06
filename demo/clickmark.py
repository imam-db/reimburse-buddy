"""Penanda klik untuk perekam demo (demo/record.py).

Playwright merekam video tanpa kursor, jadi klik tidak terlihat di hasil rekaman. Modul ini menggambar penanda
sebelum klik: kotak sorot di sekitar elemen, titik, dan cincin. Penanda mengikuti elemen (requestAnimationFrame),
jadi tetap menempel saat halaman bergulir. Bila caption menutupi elemen, elemen digulir ke tengah dulu.

Hanya konteks rekaman yang memakainya: JS-nya berhenti kalau `window.__setCaption` tidak ada (konteks penyiapan
tidak memasang skrip caption). `LiveRunner` juga tidak memanggilnya saat `quiet`. Matikan dengan `--no-click-marks`.
"""

from __future__ import annotations

BEFORE_MS = 700  # penanda terlihat sebelum klik (brief: sekitar 0,6-0,8 detik)
AFTER_MS = 450   # tahan sebentar setelah klik supaya cincin terbaca

MARK_JS = r"""
(el) => {
  // Hanya konteks rekaman: halaman prereq (quiet) tidak memakai init script caption.
  if (!window.__setCaption) return false;
  if (!el || !el.isConnected) return false;

  const root = document.documentElement;
  const capEl = document.getElementById('__demo_caption');
  const capOn = !!(capEl && capEl.style.display !== 'none' && capEl.textContent);

  // Caption tidak boleh menutupi tombol yang diklik: kalau tumpang tindih, gulir elemen ke tengah dulu.
  const r0 = el.getBoundingClientRect();
  if (capOn && r0.height > 0) {
    const c0 = capEl.getBoundingClientRect();
    const over = r0.bottom > c0.top && r0.top < c0.bottom && r0.right > c0.left && r0.left < c0.right;
    if (over) el.scrollIntoView({block: 'center', inline: 'nearest'});
  }

  const r = el.getBoundingClientRect();
  if (!r || r.width < 2 || r.height < 2) return false;

  if (!document.getElementById('__demo_click_css')) {
    const st = document.createElement('style');
    st.id = '__demo_click_css';
    st.textContent = [
      '@keyframes __dmRing { 0% { transform: scale(.35); opacity: .95 } 100% { transform: scale(1.9); opacity: 0 } }',
      '@keyframes __dmDot { 0% { transform: scale(.45); opacity: 1 } 55% { transform: scale(1); opacity: 1 }',
      '  100% { transform: scale(1); opacity: .12 } }',
      '@keyframes __dmBox { 0% { opacity: 0 } 8% { opacity: 1 } 80% { opacity: 1 } 100% { opacity: 0 } }'
    ].join('\n');
    root.appendChild(st);
  }

  // Bila elemen masih bertumpuk dengan caption (mis. kartu tinggi), penanda dinaikkan di atas caption.
  let zTop = 2147483646;
  if (capOn) {
    const c2 = capEl.getBoundingClientRect();
    if (r.bottom > c2.top && r.top < c2.bottom) zTop = 2147483647;
  }

  const wrap = document.createElement('div');
  wrap.className = '__demo_click';
  wrap.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;pointer-events:none;z-index:' + zTop;

  const ringA = document.createElement('div');
  ringA.style.cssText = 'position:absolute;left:-44px;top:-44px;width:88px;height:88px;border-radius:50%;' +
    'border:6px solid #ff2d55;box-sizing:border-box;animation:__dmRing 1.05s ease-out forwards';
  const ringB = document.createElement('div');
  ringB.style.cssText = 'position:absolute;left:-44px;top:-44px;width:88px;height:88px;border-radius:50%;' +
    'border:4px solid #ff2d55;box-sizing:border-box;opacity:.6;animation:__dmRing 1.05s ease-out .18s forwards';
  const dot = document.createElement('div');
  dot.style.cssText = 'position:absolute;left:-13px;top:-13px;width:26px;height:26px;border-radius:50%;' +
    'background:#ff2d55;box-shadow:0 0 0 4px rgba(255,255,255,.92),0 3px 12px rgba(0,0,0,.5);' +
    'animation:__dmDot 1.05s ease-out forwards';
  wrap.append(ringA, ringB, dot);

  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;box-sizing:border-box;' +
    'border:3px solid #ff2d55;border-radius:10px;pointer-events:none;z-index:' + zTop + ';' +
    'box-shadow:0 0 0 5px rgba(255,45,85,.16);animation:__dmBox 1.25s ease-out forwards';

  root.append(wrap, box);

  // Ikuti elemen: halaman bisa bergulir saat Playwright mengklik, penanda harus tetap menempel.
  const started = performance.now();
  function place() {
    if (!el.isConnected || performance.now() - started > 1500) {
      wrap.remove();
      box.remove();
      return;
    }
    const q = el.getBoundingClientRect();
    const zz = parseFloat(getComputedStyle(root).zoom) || 1;
    const inv = String(1 / zz);
    wrap.style.zoom = inv;
    wrap.style.left = (q.left + q.width / 2) + 'px';
    wrap.style.top = (q.top + q.height / 2) + 'px';
    box.style.zoom = inv;
    box.style.left = q.left + 'px';
    box.style.top = q.top + 'px';
    box.style.width = q.width + 'px';
    box.style.height = q.height + 'px';
    requestAnimationFrame(place);
  }
  place();
  return true;
}
"""


def enabled(*, quiet: bool, click_marks: bool) -> bool:
    """Penanda hanya untuk konteks yang direkam (bukan penyiapan) dan bila tidak dimatikan."""
    return click_marks and not quiet


def show(locator) -> bool:
    """Gambar penanda pada `locator` dan beri waktu terlihat. Tidak pernah menggagalkan adegan."""
    page = None
    try:
        try:
            locator.scroll_into_view_if_needed(timeout=2000)
        except Exception:  # noqa: BLE001
            pass
        handle = locator.element_handle(timeout=2500)
        if handle is None or not bool(handle.evaluate(MARK_JS)):
            return False
        page = locator.page
        page.wait_for_timeout(BEFORE_MS)
        return True
    except Exception:  # noqa: BLE001 - penanda tidak boleh menggagalkan adegan
        return False


def settle(locator) -> None:
    """Jeda singkat setelah klik, hanya kalau penanda tadi tampil."""
    try:
        locator.page.wait_for_timeout(AFTER_MS)
    except Exception:  # noqa: BLE001
        pass
