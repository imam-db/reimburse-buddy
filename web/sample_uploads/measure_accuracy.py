"""Measure extraction accuracy on the synthetic samples: uv run python web/sample_uploads/measure_accuracy.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reimburse_web.extract import get_extractor  # noqa: E402

HERE = Path(__file__).parent


def measure():
    truth = json.loads((HERE / "truth.json").read_text())
    ex, hit, total, misses = get_extractor(), 0, 0, []
    for name, want in truth.items():
        got = ex.extract(HERE / name).fields
        for k, v in want.items():
            total += 1
            g = got[k]
            if g.value == v:
                hit += 1
            else:
                misses.append(f"{name}:{k} want {v!r} got {g.value!r} (conf {g.confidence:.2f})")
    return hit, total, misses


if __name__ == "__main__":
    h, t, m = measure()
    print(f"{h}/{t} fields correct ({h / t:.0%})")
    print("\n".join(m))
