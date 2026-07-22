# C-prod dense encoder - training data & contamination check

The end-to-end C-prod condition (Table 2) retrieves articles with
`--retriever dense_hybrid` ([scripts/dense_retriever.py](../scripts/dense_retriever.py)):
a fine-tuned bi-encoder (`bge-m3-fiscal-v1`, base
[BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3)) fused with BM25 via
reciprocal-rank fusion over a chunked multi-version index (3 versions per
article cid), top-5 articles fed to the LLM. No cross-encoder is used in this
mode.

## Fine-tuning data (reduced release)

`bge-m3-fiscal-v1` was fine-tuned with MultipleNegativesRankingLoss on 1,078
(anchor, positive) pairs (3 epochs, batch 32, lr 2e-5):

- **anchors** - expert-curated French tax-practitioner questions from internal
  question pools (no user or client data);
- **positives** - 724 CGI/LPF/annexe article texts (current in-force versions
  at fine-tuning time) and 354 BOFiP doctrine extracts.

The pairs are released in **reduced form** in
[data/encoder_training_pairs.jsonl](../data/encoder_training_pairs.jsonl):

1. the **48 pairs whose positive is one of the 5 benchmark-overlapping CGI
   articles are released in full** (anchor + positive text) - exactly the
   records the public value-leakage check below needs;
2. every remaining pair is released as its **positive document reference**
   (the article/BOFiP header line) plus a **salted SHA256 of the anchor**
   (salt in the file header). Anyone can therefore verify that any given
   question - in particular each of the 209 released benchmark questions - is
   **not** a training anchor, without the anchor texts being disclosed.

The withheld anchor texts largely originate from Fiscal-FR-Bench v1, the
proprietary internal benchmark referenced in the paper; releasing them
verbatim would de facto publish that benchmark. Full pairs are available to
reviewers and auditors on request.

## Contamination check vs the k=205 benchmark

Reviewers raised the possibility that the encoder saw benchmark material during
fine-tuning. The check is deterministic and reproducible:

```bash
python scripts/check_encoder_contamination.py
```

Output on the released training set (2026-07-22):

| Check | Result |
|---|---|
| Benchmark gold articles appearing as a training positive | **5 of 31**: art. 150 U (16 pairs), 50-0 (16), 158 (10), 156 (4), 196 B (2) |
| Benchmark questions on those 5 articles | 29 of 205 |
| Gold nugget values present in the training positives (after the scorer's arithmetic normalization) | **0 of 29 questions - none** |

**Why the article-level overlap does not leak answers.** The training positives
are the *current* versions of those articles; every benchmark question on them
targets a *historical* version (e.g., the francs-era art. 196 B thresholds,
1989–1996), whose values were verified absent from all 48 overlapping training
passages - so answer leakage is excluded by the value check above. The residual
effect of the overlap is article-level retrieval familiarity, which can only
advantage the *article-finding* stage of C-prod: Cond B and C-oracle use the
gold article id and involve no encoder at all. That advantage is therefore
bounded by the C-oracle to C-prod gap (2.7 strict points overall) and touches
at most the 29 questions on the five overlapping articles. The paper's measured
contribution, version selection, is one the encoder never performs: `select_version_at()` in
[scripts/run_conditions.py](../scripts/run_conditions.py) is deterministic SQL
over `date_anchor`, identical under every retriever.

No benchmark question appears among the training anchors. The anchors are
expert-curated practitioner questions drawn from internal question pools that
predate the R3 worksheet (no user or client data); the benchmark questions were
hand-written from the temporal drift worksheet
([data/benchmark/R3_WORKSHEET.md](../data/benchmark/R3_WORKSHEET.md)).
Verified question-level (`check_encoder_contamination.py`): across all
209 released R3 questions x 1,078 anchors, zero exact matches (publicly
re-checkable against the salted anchor hashes), zero substring containments,
and zero pairs above 0.7 token-Jaccard similarity (fuzzy checks run by the
authors on the full pre-reduction set).

## Encoder weights

The fine-tuned weights (~2.1 GB) are **not included in this repository**; see
the README's *Note on retrieval conditions* for status and how to obtain an
equivalent setup (the indexing, BM25, RRF, and inference code are all released,
and `dense_retriever.py` runs unchanged with the base `BAAI/bge-m3` checkpoint,
reproducing the pipeline with a weaker article-finding stage).
