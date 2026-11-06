"""Subtitle (SRT) dari daftar perubahan caption. Fungsi murni, diuji tanpa browser."""

from __future__ import annotations

DEFAULT_LAST_CUE = 3.0


def format_ts(seconds: float) -> str:
    """Detik -> 'HH:MM:SS,mmm'."""
    ms = max(0, int(round(seconds * 1000)))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(events: list[tuple[float, str]], end: float | None = None) -> str:
    """events = [(detik_mulai, teks), ...]; teks kosong berarti caption dihapus.

    Setiap cue berlangsung sampai perubahan berikutnya; cue terakhir sampai `end` (atau 3 detik).
    """
    cues: list[tuple[float, float, str]] = []
    ordered = sorted(events, key=lambda ev: ev[0])
    for i, (start, text) in enumerate(ordered):
        if not text.strip():
            continue
        if i + 1 < len(ordered):
            stop = ordered[i + 1][0]
        else:
            stop = end if end is not None and end > start else start + DEFAULT_LAST_CUE
        if stop <= start:
            continue
        cues.append((start, stop, text.strip()))
    blocks = [f"{n}\n{format_ts(a)} --> {format_ts(b)}\n{t}\n" for n, (a, b, t) in enumerate(cues, 1)]
    return "\n".join(blocks)
