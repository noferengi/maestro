"""
scripts/build_mathlib_index.py
-------------------------------
Build app/agent/mathlib_index.json from Loogle (default) or a live Lean4 installation.

The Loogle path requires no local Lean4 installation — it queries the public API.
The --lake path requires lake + Mathlib4 in PATH.

Usage:
    venv/Scripts/python.exe scripts/build_mathlib_index.py          # Loogle (default)
    venv/Scripts/python.exe scripts/build_mathlib_index.py --lake   # lake #check (requires Lean4)
    venv/Scripts/python.exe scripts/build_mathlib_index.py --out path/to/custom.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

_DEFAULT_OUT = Path(__file__).parent.parent / "app" / "agent" / "mathlib_index.json"

_LOOGLE_API = "https://loogle.lean-lang.org/json"
_HTTP_TIMEOUT = 20

_LOOGLE_QUERIES = [
    "ZMod",
    "ZMod.pow",
    "ZMod.units",
    "ZMod.val",
    "Nat.Prime",
    "prime dvd",
    "prime infinite",
    "Nat.minFac",
    "Finset.card",
    "Finset.sum",
    "Finset.prod",
    "Finset.filter",
    "orderOf",
    "pow_card_sub_one",
    "Lagrange",
    "Nat.gcd",
    "Int.gcd",
    "IsCoprime",
    "Nat.Coprime",
    "Nat.choose",
    "Nat.factorial",
    "Int.ModEq",
    "Nat.sqrt",
    "Int.sqrt",
    "Nat.Factorization",
    "Ring",
    "Field",
    "Group",
    "CommGroup",
    "Monoid",
    "Function.Injective",
    "Function.Bijective",
    "Equiv",
    "Fintype.card",
    "Nat.card",
    "Set.Infinite",
    "Nat.totient",
    "euler totient",
    "Real.sqrt",
    "Irrational",
    "polynomial eval",
    "Polynomial.roots",
]

# Legacy lake-based modules (used with --lake flag)
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


def _query_loogle(query: str, max_results: int = 20) -> list[dict]:
    params = urllib.parse.urlencode({"q": query})
    url = f"{_LOOGLE_API}?{params}"
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "maestro-build/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"    [warn] Loogle request failed for {query!r}: {exc}", flush=True)
        return []

    if data.get("error"):
        print(f"    [warn] Loogle error for {query!r}: {data['error']}", flush=True)
        return []

    results = []
    for hit in (data.get("hits") or [])[:max_results]:
        results.append({
            "name": hit.get("name", ""),
            "type": hit.get("type", ""),
            "module": hit.get("module", ""),
            "doc": hit.get("docstring") or hit.get("doc") or "",
        })
    return results


def _build_loogle(max_per_query: int = 20) -> list[dict]:
    all_entries: dict[str, dict] = {}
    for i, query in enumerate(_LOOGLE_QUERIES, 1):
        print(f"  [{i:02d}/{len(_LOOGLE_QUERIES)}] Querying Loogle: {query!r} ...", end=" ", flush=True)
        hits = _query_loogle(query, max_results=max_per_query)
        added = 0
        for entry in hits:
            name = entry["name"]
            if name and name not in all_entries:
                all_entries[name] = entry
                added += 1
        print(f"{added} new")
        # Be polite to the public API
        if i < len(_LOOGLE_QUERIES):
            time.sleep(0.4)
    return list(all_entries.values())


def _lake_available() -> bool:
    return shutil.which("lake") is not None


def _enumerate_module(module: str) -> list[dict]:
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


def _build_lake() -> list[dict]:
    if not _lake_available():
        print("ERROR: 'lake' not found in PATH. Install Lean4 + elan.")
        sys.exit(1)
    all_entries: dict[str, dict] = {}
    for module in _MODULES:
        print(f"  Enumerating {module} ...", end=" ", flush=True)
        entries = _enumerate_module(module)
        added = 0
        for e in entries:
            if e["name"] and e["name"] not in all_entries:
                all_entries[e["name"]] = e
                added += 1
        print(f"{added} entries")
    return list(all_entries.values())


def _merge_with_existing(existing: list[dict], new_entries: list[dict]) -> tuple[list[dict], int]:
    """Merge new_entries into existing. Preserves manually-edited doc fields. Returns (merged, added_count)."""
    by_name: dict[str, dict] = {e["name"]: e for e in existing if e.get("name")}
    added = 0
    for entry in new_entries:
        name = entry.get("name", "")
        if not name:
            continue
        if name in by_name:
            # Preserve hand-edited doc if the existing entry has one
            existing_doc = by_name[name].get("doc", "")
            new_doc = entry.get("doc", "")
            by_name[name] = entry
            if existing_doc and not new_doc:
                by_name[name]["doc"] = existing_doc
        else:
            by_name[name] = entry
            added += 1
    return list(by_name.values()), added


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Mathlib static index")
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--lake", action="store_true", help="Use lake env lean --stdin instead of Loogle")
    args = parser.parse_args()

    existing: list[dict] = []
    if args.out.exists():
        try:
            existing = json.loads(args.out.read_text(encoding="utf-8"))
            print(f"Loaded {len(existing)} existing entries from {args.out}")
        except Exception as exc:
            print(f"[warn] Could not read existing index: {exc} — starting fresh")

    print(f"Building Mathlib index -> {args.out}")
    if args.lake:
        print("Mode: lake env lean --stdin")
        new_entries = _build_lake()
    else:
        print("Mode: Loogle API")
        new_entries = _build_loogle()

    merged, added = _merge_with_existing(existing, new_entries)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDone: {len(existing)} before / {added} added / {len(merged)} total -> {args.out}")


if __name__ == "__main__":
    main()
