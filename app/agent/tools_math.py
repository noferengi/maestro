"""
app/agent/tools_math.py
-----------------------
Math-specific agent tools: literature search (arXiv, OEIS, Mathlib).
Registered in tools.py TOOL_REGISTRY / TOOL_SCHEMAS.
"""

from __future__ import annotations

import json
import logging
import pathlib
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

_ARXIV_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_API = "https://export.arxiv.org/api/query"
_OEIS_API = "https://oeis.org/search"

_HTTP_TIMEOUT = 15  # seconds


def search_arxiv(query: str, max_results: int = 5, category: str = "") -> str:
    """
    Search arXiv for mathematical papers.

    Returns JSON list of {id, title, authors, year, abstract, url, pdf}.
    """
    max_results = max(1, min(20, int(max_results)))
    params: dict[str, str] = {
        "search_query": f"cat:{category} {query}".strip() if category else query,
        "max_results": str(max_results),
        "sortBy": "relevance",
    }
    url = f"{_ARXIV_API}?{urllib.parse.urlencode(params)}"

    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as resp:
            xml_bytes = resp.read()
    except urllib.error.URLError as exc:
        return json.dumps({"error": f"arXiv request failed: {exc}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        return json.dumps({"error": f"arXiv XML parse error: {exc}"})

    ns = {"a": _ARXIV_ATOM_NS}
    records = []
    for entry in root.findall("a:entry", ns):
        raw_id = (entry.findtext("a:id", default="", namespaces=ns) or "").strip()
        arxiv_id = raw_id.split("/abs/")[-1].split("v")[0] if "/abs/" in raw_id else raw_id

        title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
        abstract = (entry.findtext("a:summary", default="", namespaces=ns) or "").strip()[:500]
        published = (entry.findtext("a:published", default="", namespaces=ns) or "")
        year = int(published[:4]) if len(published) >= 4 and published[:4].isdigit() else 0

        authors = [
            (n.text or "").strip()
            for author in entry.findall("a:author", ns)
            for n in author.findall("a:name", ns)
            if n.text
        ]

        url = ""
        pdf = ""
        for link in entry.findall("a:link", ns):
            rel = link.get("rel", "")
            href = link.get("href", "")
            mime = link.get("type", "")
            if rel == "alternate":
                url = href
            elif mime == "application/pdf" or rel == "related" and "pdf" in href:
                pdf = href

        records.append({
            "id": arxiv_id,
            "title": title,
            "authors": authors,
            "year": year,
            "abstract": abstract,
            "url": url,
            "pdf": pdf,
        })

    return json.dumps(records, ensure_ascii=False)


def search_oeis(query: str, max_results: int = 5) -> str:
    """
    Search the OEIS for integer sequences.

    Returns JSON list of {id, name, values, offset, formula, url}.
    """
    max_results = max(1, min(20, int(max_results)))
    params = {"q": query, "fmt": "json"}
    url = f"{_OEIS_API}?{urllib.parse.urlencode(params)}"

    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.URLError as exc:
        return json.dumps({"error": f"OEIS request failed: {exc}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"OEIS JSON parse error: {exc}"})

    results = data.get("results") or []
    records = []
    for item in results[:max_results]:
        number = item.get("number", 0)
        oeis_id = f"A{number:06d}" if number else ""
        raw_values = item.get("data", "")
        values: list[int] = []
        if isinstance(raw_values, str) and raw_values:
            try:
                values = [int(v) for v in raw_values.split(",")[:20]]
            except ValueError:
                values = []

        formula_raw = item.get("formula") or []
        formula = formula_raw[0] if isinstance(formula_raw, list) and formula_raw else ""

        records.append({
            "id": oeis_id,
            "name": item.get("name", ""),
            "values": values,
            "offset": item.get("offset", ""),
            "formula": formula,
            "url": f"https://oeis.org/{oeis_id}" if oeis_id else "",
        })

    return json.dumps(records, ensure_ascii=False)


# ---------------------------------------------------------------------------
# search_mathlib — Gap 12
# ---------------------------------------------------------------------------

_MATHLIB_INDEX_PATH = pathlib.Path(__file__).parent / "mathlib_index.json"
_mathlib_index: list[dict] | None = None

_LOOGLE_API = "https://loogle.lean-lang.org/json"


def _load_mathlib_index() -> list[dict]:
    global _mathlib_index
    if _mathlib_index is None:
        if _MATHLIB_INDEX_PATH.exists():
            try:
                _mathlib_index = json.loads(
                    _MATHLIB_INDEX_PATH.read_text(encoding="utf-8")
                )
            except Exception as exc:
                logger.warning("[search_mathlib] Failed to load static index: %s", exc)
                _mathlib_index = []
        else:
            _mathlib_index = []
    return _mathlib_index


def _search_mathlib_live(query: str, max_results: int) -> list[dict] | None:
    """Try lake env lean --stdin with #check. Returns None when lake is unavailable."""
    if not shutil.which("lake"):
        return None
    lean_src = f"#check @{query.strip()}\n"
    try:
        r = subprocess.run(
            ["lake", "env", "lean", "--stdin"],
            input=lean_src,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        # Parse: "Name.Space : some type" from stdout
        results: list[dict] = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if " : " in line and not line.startswith("--"):
                name, _, typ = line.partition(" : ")
                results.append({
                    "name": name.strip(),
                    "type": typ.strip(),
                    "module": "",
                    "doc": "",
                })
        return results[:max_results] if results else None
    except Exception:
        return None


def _search_mathlib_loogle(query: str, max_results: int) -> list[dict] | None:
    """
    Query the Loogle API (loogle.lean-lang.org).

    Supports name fragments, type signatures, and keyword queries.
    Returns None on any network/parse failure so the caller can fall back.
    Returns an empty list when the query succeeds but matches nothing.
    """
    params = urllib.parse.urlencode({"q": query})
    url = f"{_LOOGLE_API}?{params}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "maestro-agent/1.0"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.debug("[search_mathlib] Loogle request failed: %s", exc)
        return None

    if data.get("error"):
        logger.debug("[search_mathlib] Loogle returned error: %s", data["error"])
        return None

    hits = data.get("hits") or []
    results: list[dict] = []
    for hit in hits[:max_results]:
        results.append({
            "name": hit.get("name", ""),
            "type": hit.get("type", ""),
            "module": hit.get("module", ""),
            # Loogle uses "docstring" or "doc" depending on version
            "doc": hit.get("docstring") or hit.get("doc") or "",
        })
    return results


def _search_mathlib_static(query: str, max_results: int) -> list[dict]:
    """Keyword search over the bundled static index (offline fallback)."""
    index = _load_mathlib_index()
    terms = query.lower().split()
    scored: list[tuple[int, dict]] = []
    for entry in index:
        haystack = " ".join([
            entry.get("name", ""),
            entry.get("doc", ""),
            entry.get("type", ""),
            entry.get("module", ""),
        ]).lower()
        score = sum(1 for t in terms if t in haystack)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:max_results]]


_MATHLIB_TOPICS_PATH = pathlib.Path(__file__).parent / "mathlib_topics.json"
_mathlib_topics: list[dict] | None = None


def _load_mathlib_topics() -> list[dict]:
    global _mathlib_topics
    if _mathlib_topics is None:
        try:
            _mathlib_topics = json.loads(_MATHLIB_TOPICS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[list_mathlib_topics] Failed to load topics: %s", exc)
            _mathlib_topics = []
    return _mathlib_topics


def list_mathlib_topics(category: str = "") -> list[dict]:
    """
    List curated Mathlib topic areas with key lemma names.

    Call with no argument to see all topics. Pass a category name
    (case-insensitive, partial match) to filter:
    "number theory", "algebra", "combinatorics", "logic", etc.

    Returns list of {category, topic, key_lemmas, modules, description}.
    Use search_mathlib(query) to look up specific lemmas after identifying the topic.
    """
    topics = _load_mathlib_topics()
    if not category:
        return topics
    cat_lower = category.lower()
    return [t for t in topics if cat_lower in t.get("category", "").lower()]


def search_mathlib(query: str, max_results: int = 10) -> list[dict]:
    """
    Search Lean4 Mathlib for theorems, lemmas, and definitions matching query.

    Search order (first success wins):
      1. lake env lean --stdin  — exact #check lookup (requires lake in PATH)
      2. Loogle API             — full-text / name / type search over all of Mathlib
      3. Bundled static index   — offline keyword fallback (51 hand-curated entries)

    Returns list of {name, type, module, doc}.
    """
    max_results = max(1, min(50, int(max_results)))

    live = _search_mathlib_live(query, max_results)
    if live is not None:
        return live

    loogle = _search_mathlib_loogle(query, max_results)
    if loogle is not None:
        return loogle

    return _search_mathlib_static(query, max_results)
