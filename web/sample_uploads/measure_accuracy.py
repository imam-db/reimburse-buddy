"""Measure extraction accuracy and latency on the synthetic samples.

uv run --group rapidocr python web/sample_uploads/measure_accuracy.py [ocr|rapidocr|dual ...]
(default: ocr). In dual mode a field counts as correct only if it is right; flagged fields are listed.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reimburse_web.extract import get_extractor  # noqa: E402

HERE = Path(__file__).parent


def measure(folder: Path = HERE):
    truth = json.loads((folder / "truth.json").read_text())
    ex, hit, total, misses, flagged, secs = get_extractor(), 0, 0, [], 0, 0.0
    for name, want in truth.items():
        t0 = time.perf_counter()
        d = ex.extract(folder / name)
        secs += time.perf_counter() - t0
        got = d.fields
        if d.disagreements:
            flagged += 1
            misses.append(f"{name}: flagged for review ({', '.join(d.disagreements)})")
        for k, v in want.items():
            total += 1
            g = got[k]
            if g.value == v:
                hit += 1
            else:
                misses.append(f"{name}:{k} want {v!r} got {g.value!r} (conf {g.confidence:.2f})")
    return hit, total, misses, flagged, secs / len(truth)


if __name__ == "__main__":
    for mode in sys.argv[1:] or ["ocr"]:
        os.environ["REIMBURSE_EXTRACTOR"] = mode
        print(f"== {mode} ({get_extractor().name})")
        for label, folder in (("tuning set", HERE), ("held-out set", HERE / "heldout")):
            get_extractor().extract(next(folder.glob("*.png")))  # warm-up: model load is not per-file latency
            h, t, m, fl, s = measure(folder)
            print(f"{label}: {h}/{t} fields correct ({h / t:.0%}), {fl} files flagged, {s:.2f} s/file")
            print("".join(f"  {x}\n" for x in m), end="")
