"""Remove foreign-episode wait intervals; never edit samples or videos."""
import argparse
import json
import shutil
from pathlib import Path

from yam_abc_reproduce.hil.storage import atomic_json, read_rows


def repair(path):
    manifest = path / "manifest.json"
    data = json.loads(manifest.read_text())
    if data.get("outcome") == "recording":
        raise ValueError("episode is still recording")
    rows = list(read_rows(path))
    if not rows:
        raise ValueError("empty episode")
    lo, hi = rows[0]["tick"], rows[-1]["tick"]
    if rows[0].get("event") not in ("start", "resume_policy"):
        raise ValueError("no explicit episode start boundary; manual review required")
    waits = data.get("omitted_intervention_waits", [])
    # Do not guess about intervals straddling the retained data boundaries.
    if any(w["first_tick"] < lo <= w["last_tick"] or
           w["first_tick"] <= hi < w["last_tick"] for w in waits):
        raise ValueError("boundary interval needs manual review")
    # A trailing wait may legitimately extend past the final retained sample.
    kept = [w for w in waits if w["last_tick"] >= lo]
    if kept != waits:
        backup = path / "manifest.before-wait-audit-repair.json"
        if backup.exists():
            raise ValueError("backup already exists")
        shutil.copy2(manifest, backup)
        data["omitted_intervention_waits"] = kept
        atomic_json(manifest, data)
    print(json.dumps({"removed": len(waits)-len(kept), "kept": len(kept), "steps": len(rows)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    repair(parser.parse_args().episode)
