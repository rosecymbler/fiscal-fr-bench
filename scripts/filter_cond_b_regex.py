#!/usr/bin/env python3
"""Deterministic Cond B filter — no LLM.

For each strict-N survivor (from batch2-5 + batch6 merged reports + initial 35),
loads the CURRENT (in-force) version of the article and checks whether the gold
value regex matches. If yes → RAG static B would find the correct answer by
accident (same figure in the current text as the applicable version) → DROP.
This removes questions where the value never changed, so the killer B→C gap
would be diluted.

Output:
  data/benchmark/cond_b_regex_filter.json
  data/benchmark/cond_b_regex_filter.md
"""
import json
import re
import unicodedata
from pathlib import Path

import psycopg2
import psycopg2.extras

BENCH = Path(__file__).resolve().parent.parent / "data" / "benchmark"
CGI = "LEGITEXT000006069577"


def db():
    return psycopg2.connect(host="localhost", port=5432, dbname="legifrance_db",
                            sslmode="disable", connect_timeout=5)


def normalize(text):
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = re.sub(r"(?<=\d)[\s  .](?=\d{3}\b)", "", t)
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)
    return t


def value_regex(value_field):
    """Same pattern as parametric_filter.value_regex — tolerant to FR separators."""
    alts = []
    for alt in value_field.split("|"):
        alt = alt.strip()
        esc = re.escape(alt)
        esc = re.sub(r"\\\s+", r"[\\s .]*", esc)
        alts.append(esc)
    return re.compile("|".join(a for a in alts if a), re.IGNORECASE)


def load_survivors():
    """Load 35 initial + strict-11 batch2-5 + strict-11 batch6 survivors,
    tagged by source. Returns list of (qid, article, gold_value, source)."""
    survivors = []
    # 1) Initial 35 killer set — read from killer35_qids.txt + questions.json
    killer35 = set(open(BENCH / "killer35_qids.txt").read().split())
    questions = json.load(open(BENCH / "questions.json"))
    for q in questions:
        if q["qid"] in killer35 and q["regime"] == "R3":
            # gold_value not stored per-row; use canonical answer? Grab from nuggets
            # (looking at build_r3_set.py, the value_nugget stores it in 'target' or 'pattern')
            # Load nuggets to get the value regex
            survivors.append({
                "qid": q["qid"],
                "article": q.get("sub_domain", "?").replace("art_", "").replace("_", " "),
                "gold_value": None,   # will be filled from nuggets
                "version_id": q["gold"].get("version_id"),
                "article_cid": q["gold"].get("article_cid"),
                "source": "initial-35",
            })
    # 2) Fill gold_value from nuggets.json for the initial 35
    nuggets = json.load(open(BENCH / "nuggets.json"))
    ng_by_qid = {}
    for n in nuggets:
        if n["nugget_id"].endswith("-val"):
            ng_by_qid[n["qid"]] = n
    for s in survivors:
        n = ng_by_qid.get(s["qid"])
        if n:
            if n["kind"] == "regex":
                s["gold_value"] = n["pattern"]   # already a regex string
                s["gold_regex_precompiled"] = re.compile(n["pattern"], re.IGNORECASE)
            elif n["kind"] == "numeric_tol":
                s["gold_value"] = n["label"]
                s["gold_regex_precompiled"] = value_regex(n["label"])
    # 3) batch2-5 strict-11 survivors — from merged report
    b25 = json.load(open(BENCH / "parametric_filter_report_batch2-5_merged.json"))
    for r in b25:
        if r.get("is_control"): continue
        if r.get("verdict_strict_8") == "KEEP":
            survivors.append({
                "qid": r["qid"],
                "article": r["article"],
                "gold_value": r["gold_value"],
                "gold_regex_precompiled": value_regex(r["gold_value"]),
                "version_id": None,   # need to look up via worksheet
                "article_cid": None,
                "source": "batch2-5",
            })
    # 4) batch6 strict-11 survivors
    b6 = json.load(open(BENCH / "parametric_filter_report_batch6_merged.json"))
    for r in b6:
        if r.get("is_control"): continue
        if r.get("verdict_strict_8") == "KEEP":
            survivors.append({
                "qid": r["qid"],
                "article": r["article"],
                "gold_value": r["gold_value"],
                "gold_regex_precompiled": value_regex(r["gold_value"]),
                "version_id": None,
                "article_cid": None,
                "source": "batch6",
            })
    return survivors


def lookup_current_text_by_article(cur, article_str):
    """Given an article label like '156' or '1466 A' or '199 undecies A', find the
    current in-force version. Same fallback logic as run_conditions.select_version_current:
    primary=VIGUEUR tag, fallback=date-based (many CGI articles carry MODIFIE only)."""
    for num_value in (article_str, article_str.replace(" ", "")):
        # Primary: VIGUEUR
        cur.execute(
            """SELECT id, num, texte_clean FROM articles
               WHERE code_id=%s AND replace(num, ' ', '')=replace(%s, ' ', '')
                 AND upper(etat) LIKE 'VIGUEUR%%'
                 AND upper(etat) NOT LIKE 'VIGUEUR_DIFF%%'
               ORDER BY date_debut DESC LIMIT 1""", (CGI, num_value))
        row = cur.fetchone()
        if row: return row
    # Fallback: date-based only. Excludes MODIFIE_MORT_NE (stillborn Légifrance
    # artefacts with inverted dates).
    cur.execute(
        """SELECT id, num, texte_clean FROM articles
           WHERE code_id=%s AND replace(num, ' ', '')=replace(%s, ' ', '')
             AND date_debut <= CURRENT_DATE
             AND (date_fin IS NULL OR date_fin >= CURRENT_DATE)
             AND upper(etat) != 'MODIFIE_MORT_NE'
           ORDER BY date_debut DESC LIMIT 1""", (CGI, article_str))
    return cur.fetchone()


def lookup_current_text_by_cid(cur, cid):
    cur.execute(
        """SELECT id, num, texte_clean FROM articles
           WHERE code_id=%s AND cid=%s AND upper(etat) LIKE 'VIGUEUR%%'
             AND upper(etat) NOT LIKE 'VIGUEUR_DIFF%%'
           ORDER BY date_debut DESC LIMIT 1""", (CGI, cid))
    row = cur.fetchone()
    if row: return row
    cur.execute(
        """SELECT id, num, texte_clean FROM articles
           WHERE code_id=%s AND cid=%s AND date_debut <= CURRENT_DATE
             AND (date_fin IS NULL OR date_fin >= CURRENT_DATE)
             AND upper(etat) != 'MODIFIE_MORT_NE'
           ORDER BY date_debut DESC LIMIT 1""", (CGI, cid))
    return cur.fetchone()


def main():
    survivors = load_survivors()
    print(f"Loaded {len(survivors)} survivors:")
    from collections import Counter
    src_counts = Counter(s["source"] for s in survivors)
    for src, n in src_counts.items():
        print(f"  {src}: {n}")

    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    results, kept, dropped, missing = [], 0, 0, 0
    for s in survivors:
        current = None
        if s.get("article_cid"):
            current = lookup_current_text_by_cid(cur, s["article_cid"])
        if not current:
            current = lookup_current_text_by_article(cur, s["article"])
        if not current:
            print(f"  [{s['qid']}] no current version found for '{s['article']}' — skipping (KEEP by default)")
            missing += 1
            results.append({**{k: s[k] for k in ("qid", "article", "gold_value", "source")},
                            "verdict": "KEEP", "reason": "current-version-not-found",
                            "gold_in_current": False})
            kept += 1
            continue
        current_text_norm = normalize(current["texte_clean"] or "")
        rgx = s.get("gold_regex_precompiled")
        if rgx is None:
            print(f"  [{s['qid']}] no regex — KEEP by default")
            results.append({**{k: s[k] for k in ("qid", "article", "gold_value", "source")},
                            "verdict": "KEEP", "reason": "no-gold-regex",
                            "gold_in_current": False})
            kept += 1
            continue
        gold_in_current = bool(rgx.search(current_text_norm))
        verdict = "DROP" if gold_in_current else "KEEP"
        results.append({**{k: s[k] for k in ("qid", "article", "gold_value", "source")},
                        "current_version_num": current["num"],
                        "gold_in_current": gold_in_current,
                        "verdict": verdict})
        if verdict == "KEEP": kept += 1
        else: dropped += 1

    cur.close(); conn.close()

    json.dump(results, open(BENCH / "cond_b_regex_filter.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    lines = ["# Cond B deterministic filter (gold value in current version text?)\n",
             f"**Input: {len(survivors)} strict-11 survivors** (35 initial + {src_counts.get('batch2-5', 0)} batch2-5 + {src_counts.get('batch6', 0)} batch6)\n",
             f"**Result: {kept} KEEP | {dropped} DROP | {missing} skipped (no current version found)**\n",
             "\n## Survival by source\n",
             "| Source | Input | KEEP | DROP |", "|---|---|---|---|"]
    from collections import defaultdict
    by_src = defaultdict(lambda: {"total": 0, "keep": 0, "drop": 0})
    for r in results:
        by_src[r["source"]]["total"] += 1
        by_src[r["source"]]["keep" if r["verdict"] == "KEEP" else "drop"] += 1
    for src, d in by_src.items():
        lines.append(f"| {src} | {d['total']} | {d['keep']} | {d['drop']} |")
    (BENCH / "cond_b_regex_filter.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"\n=== Cond B filter results ===")
    print(f"  KEEP: {kept} | DROP: {dropped} | missing: {missing}")
    print(f"\nBy source:")
    for src, d in by_src.items():
        pct = 100.0 * d['keep'] / d['total']
        print(f"  {src:<12} {d['total']:>4} → KEEP {d['keep']:>3} ({pct:.0f}%) | DROP {d['drop']:>3}")


if __name__ == "__main__":
    main()
