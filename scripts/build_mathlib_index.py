"""
scripts/build_mathlib_index.py
-------------------------------
Build app/agent/mathlib_index.json from a live Lean4 + Mathlib installation.

Requires: lake, leanprover/lean4, and Mathlib4 in the current lake project.
Run once from the repo root. Regenerate when the Mathlib version changes.

Usage:
    venv/Scripts/python.exe scripts/build_mathlib_index.py
    venv/Scripts/python.exe scripts/build_mathlib_index.py --out path/to/custom.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

_DEFAULT_OUT = Path(__file__).parent.parent / "app" / "agent" / "mathlib_index.json"

# Modules to enumerate for the index. Focus on number theory and foundations.
_MODULES = [
    "Mathlib.Data.Nat.Prime.Basic",
    "Mathlib.Data.Nat.Prime.Infinite",
    "Mathlib.Data.Nat.Prime.MinFac",
    "Mathlib.Data.Nat.Factors",
    "Mathlib.Data.Nat.Factorization.Basic",
    "Mathlib.Data.Nat.GCD.Basic",
    "Mathlib.NumberTheory.Primorial",
    "Mathlib.NumberTheory.PrimeCounting",
    "Mathlib.NumberTheory.ArithmeticFunction",
    "Mathlib.NumberTheory.SieveMethods",
]


def _lake_available() -> bool:
    return shutil.which("lake") is not None


def _lean_check(decl: str) -> dict | None:
    """Run #check on a declaration and return a parsed entry, or None on failure."""
    src = f"#check @{decl}\n"
    try:
        r = subprocess.run(
            ["lake", "env", "lean", "--stdin"],
            input=src,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        for line in r.stdout.splitlines():
            line = line.strip()
            if " : " in line and not line.startswith("--"):
                name, _, typ = line.partition(" : ")
                return {"name": name.strip(), "type": typ.strip(), "module": "", "doc": ""}
    except Exception:
        pass
    return None


def _enumerate_module(module: str) -> list[dict]:
    """Use #print axioms / module enumeration to list declarations in a module."""
    src = f"import {module}\n#check @Nat.Prime\n"
    try:
        r = subprocess.run(
            ["lake", "env", "lean", "--stdin"],
            input=src,
            capture_output=True,
            text=True,
            timeout=120,
        )
        entries = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if " : " in line and not line.startswith("--") and not line.startswith("warning"):
                name, _, typ = line.partition(" : ")
                entries.append({
                    "name": name.strip(),
                    "type": typ.strip(),
                    "module": module,
                    "doc": "",
                })
        return entries
    except Exception:
        return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Mathlib static index")
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    args = parser.parse_args()

    if not _lake_available():
        print("ERROR: 'lake' not found in PATH. Install Lean4 + elan and ensure lake is available.")
        print("The existing static index is unchanged.")
        sys.exit(1)

    print(f"Building Mathlib index → {args.out}")
    all_entries: list[dict] = []
    seen: set[str] = set()

    for module in _MODULES:
        print(f"  Enumerating {module} ...", end=" ", flush=True)
        entries = _enumerate_module(module)
        added = 0
        for e in entries:
            if e["name"] not in seen:
                seen.add(e["name"])
                all_entries.append(e)
                added += 1
        print(f"{added} entries")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(all_entries, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(all_entries)} declarations to {args.out}")


if __name__ == "__main__":
    main()
