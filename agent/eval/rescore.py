"""Re-apply the text-only checks (wrong evidence cited) to a saved results file and re-summarize."""

import json
import re
import sys

from .cases import CASES
from .run_eval import REJECT_NEAR, summarize

for path in sys.argv[1:]:
    d = json.load(open(path))
    cases = {c.claim_id: c for c in CASES}
    for r in d["runs"]:
        f = r["final"]
        r["wrong_evidence_cited"] = [w for w in cases[r["claim"]].wrong_evidence if any(
            not REJECT_NEAR.search(f[max(0, m.start() - 150): m.end() + 150]) for m in re.finditer(re.escape(w), f))]
    d["summary"] = summarize(d["summary"]["model"], d["runs"], d["summary"]["compact"])
    json.dump(d, open(path, "w"), indent=2, ensure_ascii=False)
    print(path, json.dumps(d["summary"]))
