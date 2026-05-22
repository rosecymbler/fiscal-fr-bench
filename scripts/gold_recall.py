#!/usr/bin/env python3
"""Article-level precision/recall of the citation pipeline vs the fiscaliste's
50-decision gold standard (gold_standard_sample_ANNOTATED.csv).

Normalization is applied SYMMETRICALLY to both the gold and the pipeline output,
following the fiscaliste's convention:
  - keep alphabetic suffixes and Latin ordinals (209 B, 196 A bis, 44 ter)
  - keep L./R. series and annexe numbers whole (LPF R. 228-2, CGI annexe II 376)
  - strip paragraph/alinea markers: trailing Roman numerals (1647 V -> 1647),
    degree subdivisions (990 E 3 -> 990 E), and CGI hyphen subdivisions (38-1 -> 38)
This avoids artificial false-negatives from sub-division mismatches.
"""
import csv
import re
import psycopg2
import psycopg2.extras
from pathlib import Path
from collections import defaultdict

BENCH = Path(__file__).resolve().parent.parent / "data" / "benchmark"
CSV = Path(__file__).resolve().parent.parent / "data" / "gold_standard_sample_ANNOTATED.csv"

CODE_OF = {
    "LEGITEXT000006069577": "CGI", "LEGITEXT000006069568": "CGI annexe I",
    "LEGITEXT000006069569": "CGI annexe II", "LEGITEXT000006069574": "CGI annexe III",
    "LEGITEXT000006069576": "CGI annexe IV", "LEGITEXT000006069583": "LPF",
}
ALINEA_ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def norm_num(num):
    s = " ".join(str(num).lower().split())
    # LPF L./R. series: unify "l. 16 b" / "l16 b" / "l 16 b" -> "l 16 b" (keep whole,
    # including hyphen subdivisions like r. 228-2, per the fiscaliste's convention).
    m = re.match(r"^([lr])\.?\s*(\d.*)$", s)
    if m:
        return f"{m.group(1)} " + " ".join(m.group(2).split())
    # CGI-style: strip degree, hyphen subdivisions, trailing Roman alinea
    s = re.sub(r"\s*\d+\s*°", "", s)
    s = re.sub(r"-\d+$", "", s)
    toks = s.split()
    if len(toks) > 1 and toks[-1] in ALINEA_ROMAN:
        toks = toks[:-1]
    return " ".join(toks)


def parse_gold_cell(cell):
    out = set()
    for ref in cell.split(";"):
        ref = ref.strip()
        if not ref:
            continue
        m = re.match(r"(CGI annexe (?:I|II|III|IV)|CGI|LPF)\s+(.*)", ref)
        if not m:
            continue
        out.add((m.group(1), norm_num(m.group(2))))
    return out


def main():
    rows = list(csv.DictReader(open(CSV, encoding="utf-8")))
    gold = {r["jurisprudence_id"]: parse_gold_cell(r.get("articles_cites", "")) for r in rows}
    src = {r["jurisprudence_id"]: r["source"] for r in rows}

    conn = psycopg2.connect(host="localhost", port=5432, dbname="legifrance_db", sslmode="disable")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    pred = {}
    for jid in gold:
        cur.execute("""SELECT a.num, a.code_id FROM liens_jurisprudence_article l
                       JOIN articles a ON a.id = l.article_id
                       WHERE l.jurisprudence_id = %s""", (jid,))
        pred[jid] = {(CODE_OF.get(r["code_id"], r["code_id"]), norm_num(r["num"]))
                     for r in cur.fetchall() if r["code_id"] in CODE_OF}
    cur.close(); conn.close()

    TP = FP = FN = 0
    dec_with_gold = dec_recalled = 0
    per_src = defaultdict(lambda: [0, 0, 0])   # TP, FP, FN
    for jid, g in gold.items():
        p = pred.get(jid, set())
        tp = len(g & p); fp = len(p - g); fn = len(g - p)
        TP += tp; FP += fp; FN += fn
        per_src[src[jid]][0] += tp; per_src[src[jid]][1] += fp; per_src[src[jid]][2] += fn
        if g:
            dec_with_gold += 1
            if g & p:
                dec_recalled += 1

    prec = TP / (TP + FP) if TP + FP else 0
    rec = TP / (TP + FN) if TP + FN else 0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0
    print("=== Article-level (micro) on 50-decision gold standard ===")
    print(f"  TP={TP}  FP={FP}  FN={FN}")
    print(f"  Precision = {prec*100:.1f}%   Recall = {rec*100:.1f}%   F1 = {f1*100:.1f}%")
    print(f"  Decision-level recall (>=1 gold article caught): {dec_recalled}/{dec_with_gold} "
          f"= {dec_recalled/dec_with_gold*100:.0f}%")
    print("\n=== by source ===")
    for s, (tp, fp, fn) in sorted(per_src.items()):
        pr = tp/(tp+fp)*100 if tp+fp else 0
        rc = tp/(tp+fn)*100 if tp+fn else 0
        print(f"  {s:18s} P={pr:5.1f}%  R={rc:5.1f}%  (TP={tp} FP={fp} FN={fn})")

    print("\n=== false negatives (gold missed by pipeline) ===")
    for jid, g in gold.items():
        miss = g - pred.get(jid, set())
        if miss:
            print(f"  {jid[:34]:34s} {sorted(miss)}")


if __name__ == "__main__":
    main()
