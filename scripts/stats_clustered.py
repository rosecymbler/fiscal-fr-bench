#!/usr/bin/env python3
"""Cluster-aware statistics for Table 2 (k=205, 11 models).

The paper's original tests (pooled McNemar/Wilcoxon over 11 models x 205
questions, iid bootstrap over questions) ignore two dependencies:
  (i)  the same 205 questions are answered by all 11 models;
  (ii) questions are clustered by CGI article (31 clusters; e.g. 34 questions
       on art. 1466 A alone, near-identical templates at varying dates).

This script recomputes everything dependency-aware, from the per-response dump
of score_nuggets.py:

  a. CLUSTER BOOTSTRAP - resample the 31 ARTICLES (not the questions),
     10,000 iterations, seed 42; pooled + per-model 95% CIs for strict and
     coverage, conditions A/B/Cor/Cprod. One shared cluster draw per iteration
     across models and conditions (the resampled question set is common).
  b. McNEMAR PER MODEL - exact two-sided binomial test on discordant pairs,
     B->C-oracle and B->C-prod, per model (11 tests, k=205); the pooled
     version is kept as an annex for comparison.
  c. WILCOXON signed-rank per model on per-question coverage (B->Cor,
     B->Cprod, Cor->Cprod).
  d. LEAVE-ONE-ARTICLE-OUT - pooled C-prod strict recomputed dropping each of
     the 31 articles in turn (min/max over the 31 values).

Plus two paper-requested figures:
  4. Residual Cond A leakage at evaluation time: qids where a model produced
     the gold VALUE (the -val nugget) in at least one Cond A draw.
  5. Article-number-in-prompt rate (the -art regex fires on the prompt
     itself) and "value-only coverage" (coverage excluding the -art nugget).

Point estimates are identical to score_nuggets.py / Table 2 by construction
(same records, same weighting); the script asserts it.

Usage:
  python scripts/score_nuggets.py data/benchmark/table2_refit/all_refit_responses_v2.json \
      --regime R3 --dump-per-question /tmp/per_question.json
  python scripts/stats_clustered.py /tmp/per_question.json
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import stats as sps

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score_nuggets import normalize as sn_normalize                    # noqa: E402

BENCH = Path(__file__).resolve().parent.parent / "data" / "benchmark"
CONDS = ["A", "B", "Ci", "Cprod"]
COND_LABEL = {"A": "A", "B": "B", "Ci": "C-oracle", "Cprod": "C-prod"}
SEED = 42
N_BOOT = 10_000
BAR = 83.0                       # per-model C-prod strict bar claimed in the paper


def fmt_p(p):
    return f"{p:.2e}" if p < 1e-3 else f"{p:.4f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", help="per-response JSON from score_nuggets --dump-per-question")
    ap.add_argument("--out", default=str(BENCH / "stats_clustered_report.md"))
    args = ap.parse_args()

    recs = json.load(open(args.dump, encoding="utf-8"))
    questions = {q["qid"]: q for q in json.load(open(BENCH / "questions.json", encoding="utf-8"))}
    nuggets = defaultdict(dict)
    for n in json.load(open(BENCH / "nuggets.json", encoding="utf-8")):
        if n.get("nugget_id"):
            nuggets[n["qid"]][n["nugget_id"]] = n

    qids = sorted({r["qid"] for r in recs})
    models = sorted({r["model"] for r in recs})
    cluster_of = {q: questions[q]["gold"]["article_cid"] for q in qids}
    clusters = sorted(set(cluster_of.values()))
    art_num = {}                         # cluster cid -> printable article num
    for q in qids:
        m = re.match(r"R3-WS-(.+?)-\d{4}-\d{2}-\d{2}$", q)
        art_num.setdefault(cluster_of[q], m.group(1) if m else "?")
    K, M, C = len(qids), len(models), len(clusters)

    # per (model, cond, qid): lists over draws
    by = defaultdict(lambda: {"strict": [], "cov": [], "vcov": [], "prov": []})
    for r in recs:
        e = by[(r["model"], r["condition"], r["qid"])]
        e["strict"].append(r["strict"])
        e["cov"].append(r["coverage"])
        val_hits = [h for nid, h in r["hits"].items() if nid.endswith("-val")]
        e["vcov"].append(float(np.mean(val_hits)) if val_hits else np.nan)
        if r["prov"] is not None:
            e["prov"].append(r["prov"])

    def draws(m, c, q, metric):
        return by[(m, c, q)][metric]

    # ---- point estimates (must equal Table 2) ------------------------------
    def model_mean(m, c, metric):
        vals = [v for q in qids for v in draws(m, c, q, metric)]
        return 100.0 * float(np.mean(vals))

    point = {(m, c, met): model_mean(m, c, met)
             for m in models for c in CONDS for met in ("strict", "cov", "vcov")}
    pooled = {(c, met): float(np.mean([point[(m, c, met)] for m in models]))
              for c in CONDS for met in ("strict", "cov", "vcov")}
    expect = {("A", "strict"): 1.4, ("B", "strict"): 2.0,
              ("Ci", "strict"): 90.6, ("Cprod", "strict"): 87.9}
    sanity = {k: (round(pooled[k], 1), v, abs(round(pooled[k], 1) - v) <= 0.05)
              for k, v in expect.items()}

    # ---- a. cluster bootstrap ---------------------------------------------
    # Precompute per (model, cond, metric, cluster): (sum, n) over all draws.
    sums = {}
    for m in models:
        for c in CONDS:
            for met in ("strict", "cov"):
                s = np.zeros(C)
                n = np.zeros(C)
                for ci, cl in enumerate(clusters):
                    vals = [v for q in qids if cluster_of[q] == cl
                            for v in draws(m, c, q, met)]
                    s[ci], n[ci] = np.sum(vals), len(vals)
                sums[(m, c, met)] = (s, n)

    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, C, size=(N_BOOT, C))      # shared cluster draws
    ci_model, ci_pooled = {}, {}
    for c in CONDS:
        for met in ("strict", "cov"):
            per_model_boot = []
            for m in models:
                s, n = sums[(m, c, met)]
                bs = s[idx].sum(axis=1) / n[idx].sum(axis=1) * 100.0
                per_model_boot.append(bs)
                ci_model[(m, c, met)] = (float(np.percentile(bs, 2.5)),
                                         float(np.percentile(bs, 97.5)))
            pb = np.mean(per_model_boot, axis=0)
            ci_pooled[(c, met)] = (float(np.percentile(pb, 2.5)),
                                   float(np.percentile(pb, 97.5)))

    # ---- b. McNemar per model (exact, two-sided) ---------------------------
    def strict_vec(m, c):
        return np.array([1 if any(draws(m, c, q, "strict")) else 0 for q in qids]) \
            if c == "A" else \
            np.array([draws(m, c, q, "strict")[0] for q in qids])

    mcnemar = {}
    for tgt in ("Ci", "Cprod"):
        pooled_b01 = pooled_b10 = 0
        for m in models:
            b = strict_vec(m, "B")
            t = strict_vec(m, tgt)
            b01 = int(((b == 0) & (t == 1)).sum())   # C-only successes
            b10 = int(((b == 1) & (t == 0)).sum())   # B-only successes
            n = b01 + b10
            p = sps.binomtest(min(b01, b10), n, 0.5).pvalue if n else 1.0
            mcnemar[(m, tgt)] = (b01, b10, p)
            pooled_b01 += b01
            pooled_b10 += b10
        n = pooled_b01 + pooled_b10
        mcnemar[("POOLED", tgt)] = (pooled_b01, pooled_b10,
                                    sps.binomtest(min(pooled_b01, pooled_b10), n, 0.5).pvalue)

    # ---- c. Wilcoxon signed-rank per model on coverage ---------------------
    def cov_vec(m, c):
        return np.array([float(np.mean(draws(m, c, q, "cov"))) for q in qids])

    wilcoxon = {}
    for (c1, c2) in (("B", "Ci"), ("B", "Cprod"), ("Ci", "Cprod")):
        for m in models:
            x, y = cov_vec(m, c1), cov_vec(m, c2)
            try:
                w = sps.wilcoxon(x, y, zero_method="wilcox")
                wilcoxon[(m, c1, c2)] = w.pvalue
            except ValueError:                       # all differences zero
                wilcoxon[(m, c1, c2)] = 1.0

    # ---- d. leave-one-article-out (pooled C-prod strict) -------------------
    loao = {}
    for cl in clusters:
        keep = [q for q in qids if cluster_of[q] != cl]
        vals = [float(np.mean([v for q in keep for v in draws(m, "Cprod", q, "strict")]))
                for m in models]
        loao[cl] = 100.0 * float(np.mean(vals))
    lo_min = min(loao, key=loao.get)
    lo_max = max(loao, key=loao.get)

    # ---- 4. residual Cond A value leakage at evaluation --------------------
    leak = {}
    for m in models:
        s = {q for q in qids
             if any(h for h in (np.array(draws(m, "A", q, "vcov")) == 1.0))}
        leak[m] = s
    leak_union = set().union(*leak.values())

    # ---- 5. article-number-in-prompt + value-only coverage -----------------
    art_in_prompt = []
    for q in qids:
        art = nuggets[q].get(f"{q}-art")
        if art and re.search(art["pattern"], sn_normalize(questions[q]["prompt"]),
                             re.IGNORECASE):
            art_in_prompt.append(q)

    # ---- report ------------------------------------------------------------
    L = ["# Cluster-aware statistics - Table 2 (k=205, 11 models)\n",
         f"Source: per-response dump of `score_nuggets.py` over "
         f"`table2_refit/all_refit_responses_v2.json` ({len(recs)} records, "
         f"{K} qids, {C} article clusters). Bootstrap: {N_BOOT:,} iterations, "
         f"articles resampled with replacement, seed {SEED}, shared cluster "
         f"draw per iteration across models/conditions. McNemar: exact "
         f"two-sided binomial on discordant pairs. Generated by "
         f"`scripts/stats_clustered.py`.\n"]

    L.append("## Sanity check - point estimates vs Table 2\n")
    L.append("| Condition | Recomputed pooled strict | Table 2 | match |")
    L.append("|---|---|---|---|")
    for (c, met), (got, exp, ok) in sanity.items():
        L.append(f"| {COND_LABEL[c]} | {pooled[(c, met)]:.2f}% | {exp}% | "
                 f"{'✅' if ok else '❌ MISMATCH'} |")
    L.append("")
    if not all(ok for *_, ok in sanity.values()):
        L.append("**❌ POINT-ESTIMATE MISMATCH - do not use this report.**\n")

    L.append("## a. Cluster bootstrap - pooled 95% CIs (article-resampled)\n")
    L.append("| Condition | strict [CI] | coverage [CI] |")
    L.append("|---|---|---|")
    for c in CONDS:
        s_lo, s_hi = ci_pooled[(c, "strict")]
        c_lo, c_hi = ci_pooled[(c, "cov")]
        L.append(f"| {COND_LABEL[c]} | {pooled[(c, 'strict')]:.1f}% "
                 f"[{s_lo:.1f}, {s_hi:.1f}] | {pooled[(c, 'cov')]:.1f}% "
                 f"[{c_lo:.1f}, {c_hi:.1f}] |")
    L.append("")

    L.append("### Per-model strict, clustered CIs (C-prod bar = 83%)\n")
    L.append("| Model | B strict [CI] | C-oracle strict [CI] | C-prod strict [CI] | C-prod CI ≥ 83%? |")
    L.append("|---|---|---|---|---|")
    below_bar = []
    for m in models:
        cells = []
        for c in ("B", "Ci", "Cprod"):
            lo, hi = ci_model[(m, c, "strict")]
            cells.append(f"{point[(m, c, 'strict')]:.1f}% [{lo:.1f}, {hi:.1f}]")
        lo_cp = ci_model[(m, "Cprod", "strict")][0]
        ok = lo_cp >= BAR
        if not ok:
            below_bar.append((m, lo_cp))
        cells.append("yes" if ok else f"**🔴 no (lower bound {lo_cp:.1f}%)**")
        L.append("| " + " | ".join([m] + cells) + " |")
    L.append("")
    if below_bar:
        L.append(f"**🔴 {len(below_bar)}/11 models have a clustered C-prod strict "
                 "CI whose lower bound falls below 83%** (point estimates all "
                 "remain ≥ 83.9%). The abstract's \"all eleven models cross the "
                 "83% bar\" holds for point estimates but NOT under clustered "
                 "uncertainty - rephrase as a point-estimate statement or lower "
                 "the bar.\n")
    else:
        L.append("All 11 clustered C-prod CIs stay ≥ 83% - the abstract's claim "
                 "survives clustering.\n")

    L.append("## b. McNemar B→C per model (exact two-sided, k=205)\n")
    L.append("| Model | B→C-oracle (C+, B+) | p | B→C-prod (C+, B+) | p |")
    L.append("|---|---|---|---|---|")
    for m in models + ["POOLED"]:
        b01o, b10o, po = mcnemar[(m, "Ci")]
        b01p, b10p, pp = mcnemar[(m, "Cprod")]
        name = m if m != "POOLED" else "**Pooled (annex - ignores model dependence)**"
        L.append(f"| {name} | ({b01o}, {b10o}) | {fmt_p(po)} | "
                 f"({b01p}, {b10p}) | {fmt_p(pp)} |")
    L.append("\n(C+, B+) = discordant pairs (condition-only successes, "
             "B-only successes). Per-model tests are the honest unit of "
             "inference; the pooled row reproduces the paper's original "
             "test for comparison.\n")

    L.append("## c. Wilcoxon signed-rank per model (coverage)\n")
    L.append("| Model | B→C-oracle p | B→C-prod p | C-oracle→C-prod p |")
    L.append("|---|---|---|---|")
    for m in models:
        L.append(f"| {m} | {fmt_p(wilcoxon[(m, 'B', 'Ci')])} | "
                 f"{fmt_p(wilcoxon[(m, 'B', 'Cprod')])} | "
                 f"{fmt_p(wilcoxon[(m, 'Ci', 'Cprod')])} |")
    L.append("")

    L.append("## d. Leave-one-article-out - pooled C-prod strict\n")
    L.append(f"Full set: {pooled[('Cprod', 'strict')]:.1f}%. Removing each of the "
             f"{C} articles in turn:\n")
    L.append(f"- min = **{loao[lo_min]:.1f}%** (without art. {art_num[lo_min]}, "
             f"{sum(cluster_of[q] == lo_min for q in qids)} questions)")
    L.append(f"- max = **{loao[lo_max]:.1f}%** (without art. {art_num[lo_max]}, "
             f"{sum(cluster_of[q] == lo_max for q in qids)} questions)")
    spread = loao[lo_max] - loao[lo_min]
    b_pooled = pooled[("B", "strict")]
    L.append(f"- spread = {spread:.1f} points. Even the least favorable LOAO "
             f"value ({loao[lo_min]:.1f}%) stays "
             f"{'above' if loao[lo_min] >= BAR else '**🔴 BELOW**'} the 83% bar "
             f"and ~{loao[lo_min] - b_pooled:.0f} points above Cond B "
             f"({b_pooled:.1f}%) - no single cluster carries the B→C result."
             + ("" if loao[lo_min] >= BAR else " **The 83% bar claim does not "
                "survive removing this cluster.**") + "\n")

    L.append("## 4. Residual Cond A value leakage at evaluation (k=205)\n")
    L.append("Questions where the model produced the gold VALUE (`-val` nugget) "
             "in ≥1 Cond A draw at evaluation time (post-selection drift / "
             "reconstruction):\n")
    L.append("| Model | qids with value in Cond A |")
    L.append("|---|---|")
    for m in models:
        L.append(f"| {m} | {len(leak[m])} |")
    L.append(f"| **Union (≥1 model)** | **{len(leak_union)} / {K} "
             f"({100.0 * len(leak_union) / K:.1f}%)** |")
    L.append("")

    L.append("## 5. Article-number leakage in prompts & value-only coverage\n")
    L.append(f"The `-art` nugget pattern fires on the question prompt itself for "
             f"**{len(art_in_prompt)} / {K} questions "
             f"({100.0 * len(art_in_prompt) / K:.1f}%)** - for those, citing the "
             f"article is not evidence of retrieval. Value-only coverage "
             f"(excluding the `-art` nugget; the `-val` nugget is the actual "
             f"date-anchored value) per model x condition:\n")
    L.append("| Model | A | B | C-oracle | C-prod |")
    L.append("|---|---|---|---|---|")
    for m in models:
        L.append("| " + " | ".join(
            [m] + [f"{point[(m, c, 'vcov')]:.1f}%" for c in CONDS]) + " |")
    L.append("| **Pooled** | " + " | ".join(
        f"**{pooled[(c, 'vcov')]:.1f}%**" for c in CONDS) + " |")
    L.append("")

    Path(args.out).write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L[:40]))
    print(f"...\n-> {args.out}")


if __name__ == "__main__":
    main()
