"""
app/agent/mathlib_cheatsheet.py
--------------------------------
Hand-curated cheat sheet mapping common proof goals to canonical Mathlib lemma names.
Edit this file directly to add or update entries. No DB or JSON involved.

Each key is a natural-language goal an agent might be asked to prove.
Values are dicts with:
  lemmas   — ordered list of Mathlib lemma names (most direct first)
  modules  — import paths
  tactics  — Lean4 tactics most likely to close the goal
  notes    — gotchas or usage notes
"""

from __future__ import annotations

CHEATSHEET: dict[str, dict] = {
    "Fermat's little theorem": {
        "lemmas": ["ZMod.pow_card_sub_one_eq_one", "ZMod.units_pow_card_sub_one"],
        "modules": ["Mathlib.Data.ZMod.Units"],
        "tactics": ["exact ZMod.pow_card_sub_one_eq_one", "apply?"],
        "notes": "Requires [Fact (Nat.Prime p)] instance on p.",
    },
    "Infinitely many primes (Euclid)": {
        "lemmas": ["Nat.infinite_setOf_prime", "Nat.exists_infinite_primes"],
        "modules": ["Mathlib.Data.Nat.Prime.Infinite"],
        "tactics": ["exact Nat.infinite_setOf_prime"],
        "notes": "Already in Mathlib — use directly, do not re-prove.",
    },
    "Prime divides product": {
        "lemmas": ["Nat.Prime.dvd_mul", "Nat.Prime.dvd_of_dvd_pow"],
        "modules": ["Mathlib.Data.Nat.Prime.Basic"],
        "tactics": ["exact (Nat.Prime.dvd_mul hp).mp h"],
        "notes": "",
    },
    "Euler's theorem / Lagrange": {
        "lemmas": ["ZMod.pow_card_sub_one_eq_one", "orderOf_dvd_card_sub_one"],
        "modules": ["Mathlib.Data.ZMod.Units", "Mathlib.GroupTheory.OrderOfElement"],
        "tactics": ["apply orderOf_dvd_card_sub_one"],
        "notes": "Generalizes Fermat to arbitrary groups.",
    },
    "GCD basic properties": {
        "lemmas": ["Nat.gcd_comm", "Nat.gcd_assoc", "Nat.gcd_dvd_left", "Nat.gcd_dvd_right"],
        "modules": ["Mathlib.Data.Nat.GCD.Basic"],
        "tactics": ["simp [Nat.gcd_comm]", "omega"],
        "notes": "",
    },
    "Bezout's identity": {
        "lemmas": ["Nat.gcd_eq_gcd_ab", "Int.gcd_eq_natAbs"],
        "modules": ["Mathlib.Data.Nat.GCD.Basic"],
        "tactics": ["exact Nat.gcd_eq_gcd_ab m n"],
        "notes": "",
    },
    "Finset cardinality": {
        "lemmas": [
            "Finset.card_union_add_card_inter",
            "Finset.card_filter",
            "Finset.card_image_of_injOn",
        ],
        "modules": ["Mathlib.Data.Finset.Basic", "Mathlib.Data.Finset.Card"],
        "tactics": ["simp [Finset.card_union_add_card_inter]"],
        "notes": "",
    },
    "Sum/product over Finset": {
        "lemmas": [
            "Finset.sum_add_distrib",
            "Finset.prod_mul_distrib",
            "Finset.sum_comm",
            "Finset.prod_pow_eq_pow_sum",
        ],
        "modules": ["Mathlib.Algebra.BigOperators.Basic"],
        "tactics": ["simp [Finset.sum_comm]", "ring_nf"],
        "notes": "Open BigOperators notation: `open Finset BigOperators`.",
    },
    "Modular arithmetic / Int.ModEq": {
        "lemmas": ["Int.ModEq", "Int.modEq_iff_dvd", "Int.ModEq.add", "Int.ModEq.mul"],
        "modules": ["Mathlib.Data.Int.ModCast", "Mathlib.Data.Int.GCD"],
        "tactics": ["ring_nf", "omega"],
        "notes": "Int.ModEq n a b ↔ n | a - b.",
    },
    "Order of a group element": {
        "lemmas": [
            "orderOf_dvd_card",
            "orderOf_eq_card_of_forall_mem_zpowers",
            "pow_orderOf_eq_one",
        ],
        "modules": ["Mathlib.GroupTheory.OrderOfElement"],
        "tactics": ["exact pow_orderOf_eq_one _"],
        "notes": "",
    },
}


def format_for_prompt() -> str:
    """Return a compact multi-line string suitable for injection into a system prompt."""
    lines = ["MATHLIB QUICK REFERENCE — call list_mathlib_topics() for full topic list:"]
    for goal, info in CHEATSHEET.items():
        lemma_str = ", ".join(info["lemmas"][:2])
        notes = f"  ({info['notes']})" if info["notes"] else ""
        lines.append(f"  • {goal}: {lemma_str}{notes}")
    return "\n".join(lines)
