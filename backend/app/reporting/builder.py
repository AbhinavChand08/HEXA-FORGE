"""Text report of one device's rule results (the structured JSON report is task K1)."""
from __future__ import annotations

from collections import Counter


def text_report(doc: dict, results: list[dict]) -> str:
    counts = Counter(r["status"] for r in results)
    lines = [f"=== {doc['device']}   family={doc['vendor_family']}   {doc['file']}"
             f"   registry v{doc.get('registry_version', '?')}",
             "    " + "   ".join(f"{s} {counts[s]}" for s in ("PASS", "FAIL", "UNKNOWN", "ERROR")
                               if counts[s])]
    order = {"FAIL": 0, "ERROR": 1, "UNKNOWN": 2, "PASS": 3}
    sev = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for r in sorted(results, key=lambda r: (order[r["status"]], sev[r["severity"]], r["rule"])):
        actual = "-" if r["actual"] is None else r["actual"]
        where = f" [{r['scope']}]" if r["scope"] else ""
        src = f" ({r['source']}{' L' + str(r['line']) if r['line'] else ''})" if r["source"] else ""
        exp = "" if r["operator"] in ("present", "absent") else f" {r['expected']}"
        pend = ""
        if r["source"] == "pending review":
            parts = ([f"review #{', #'.join(map(str, r['pending']))}"] if r["pending"] else []) + \
                ([f"{r['unscored']} unscored command(s)"] if r["unscored"] else [])
            pend = f"   ({'; '.join(parts)})"
        lines.append(f"  {r['status']:<7} {r['severity']:<8} {r['rule']:<28} {r['key']}{where} = "
                     f"{actual}{src}   expected {r['operator']}{exp}   | {r['title']}{pend}")
    return "\n".join(lines)
