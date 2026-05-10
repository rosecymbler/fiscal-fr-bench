# Methodology

How the corpus was built, end-to-end. Designed for full reproducibility.

---

## 1. Versioned CGI/LPF extraction

### Problem
The Légifrance public-facing API (`/consult/getArticle`) returns only the **current version** of an article. To evaluate temporal drift in legal QA, we need **all historical versions** of every article.

### Solution
The endpoint `/consult/getArticleByCid` (PISTE OAuth) returns a `listArticle` field containing **all historical versions** of an article identified by its `cid` (constant identifier across versions).

### Pipeline

```
For each LEGITEXT (CGI principal + 4 annexes + LPF):
    For each CID listed in the table of contents:
        call_api("/consult/getArticleByCid", {"cid": cid})
        For each version returned in `listArticle`:
            UPSERT into `articles` (id, cid, num, etat, date_debut, date_fin,
                                    texte_html, texte_clean, code_id, ...)
```

### Implementation
- `scripts/chrono_cgi.py`
- Uses OAuth2 against `https://oauth.piste.gouv.fr/api/oauth/token`
- Rate limit: PISTE allows ~100 req/s, we throttle conservatively
- Idempotent (UPSERT ON CONFLICT)

### Output
- **32 436 article-versions** across 6 codes
- 1938-06-01 → 2031-01-01 (93 years)
- 5.69 versions/CID for CGI principal
- Future-vigueur articles included (`etat = VIGUEUR_DIFF`) — useful for testing models on already-voted but not-yet-effective dispositions

---

## 2. Version-aware jurisprudence ↔ CGI linking

### Problem
We want each link in `liens_jurisprudence_article` to point to the **specific version of the article that was applicable** at the date of the decision — not just the article in general.

### Pipeline

```
1. Build in-memory index of all fiscal articles, indexed by normalized `num`
   idx[ "209 B" ] = [(version_id, cid, code, num, etat, date_debut, date_fin), ...]

2. Filter fiscal subset of decisions from 4 sources:
   - decisions_unified : tsvector @@ to_tsquery('fiscal | impôt | impôts | TVA | CGI')
   - inca, arianeweb : ILIKE on fiscal terms
   - judilibre : visa::text ILIKE on CGI/LPF mentions

3. For each fiscal decision:
   a. Truncate text to 70k chars (motifs + dispositif concentration)
   b. Apply article regex (handles "L. 16 B", "1011 bis", "209 B", etc.)
   c. For each candidate match:
      - VETO if competing source attached in next 70 chars
        (du Code civil, du code des douanes, de la loi n°, ...)
      - Require fiscal context in ±100 chars window
        - "Strong" context (CGI/LPF named) for simple nums (1-3 digits)
        - "Weak" context (any fiscal keyword) for nums with suffix (L./R./bis/A-Z)
      - Look up the candidate in the article index
      - Pick the version applicable at decision_date (else VIGUEUR fallback)
      - INSERT into liens_jurisprudence_article with type_lien=CITE/VISE
```

### Key design choices

#### Regex for article extraction

```python
ARTICLE_RE = re.compile(
    r"\bart(?:icle|\.)?s?\s+"
    r"(L\.?\s*\d{1,4}(?:[\s\-]\d{1,4})?|R\.?\s*\d{1,4}(?:[\s\-]\d{1,4})?|\d{1,4})"
    r"((?:\s+(?:bis|ter|quater|...|(?-i:[A-Z])(?![a-z])|\d{1,2}\s*°|0\s+bis))*)",
    re.IGNORECASE,
)
```

Note the `(?-i:[A-Z])(?![a-z])` for the alphabetic suffix : a strict uppercase letter NOT followed by a lowercase letter. This prevents false matches like "1382 D" where the "D" comes from "du" (e.g., "article 1382 du Code civil").

#### Proximity veto (anti-FP)

```python
COMPETING_SOURCE_NEAR_RE = re.compile(
    r"\b(?:du\s+code\s+(?:civil|p[ée]nal|de\s+(?:proc[ée]dure|...)|"
    r"des\s+douanes|mon[ée]taire\s+et\s+financier)|"
    r"de\s+la\s+(?:loi|convention|directive)\s+(?:n[°o]|du\s+\d|...)|...)",
    re.IGNORECASE,
)
```

Applied in the **70 chars immediately after** the article number. If this regex matches, the candidate is rejected — it's attached to a competing legal source.

#### Version selection

```python
def pick_best_version(articles, decision_date):
    # 1. Date coherence: if oldest version is AFTER decision_date, the article didn't exist yet
    if oldest_debut > decision_date:
        return None
    # 2. Find the version valid at decision_date
    for a in articles:
        if a.date_debut <= decision_date <= a.date_fin:
            return (a.id, a.cid)
    # 3. Fallback: VIGUEUR or most recent by date_debut
```

### Output (V7)
- **~70 000 links** across **32 000+ fiscal decisions**
- 97% of links point to the version exactly applicable at `date_decision`
- Link types: VISE (from structured Judilibre `visa`) or CITE (from regex)

---

## 3. Quality audit protocol

### Stratified random sampling

- `n=100` links sampled with seed=42 (reproducible)
- Stratification proportional to source distribution (70 unified, 15 arianeweb, 13 inca, 2 judilibre)

### 2-stage auto-annotator

For each sampled link:

```
Stage 1 - NEAR window (±60 chars after the article number):
    if CGI/LPF mentioned → VP_HIGH
    elif other code attached → FP_HIGH
    elif loi numérotée / convention / décret → FP_HIGH

Stage 2 - WIDE window (±300 chars):
    if CGI/LPF mentioned and no competing code → VP_HIGH
    elif both CGI and competing code → VP_MED
    elif competing code only → FP_HIGH
    else → DOUTE
```

### Manual review

The 14 low-confidence cases (DOUTE + FP_HIGH) are reviewed by hand by Rose / tax expert. The remaining 86 high-confidence cases are accepted as-is.

### Metrics

- **Precision** = VP / (VP + FP) on the audited 100
- **Recall** (proxy) = (decisions with at least one link) / (decisions in fiscal subset)
- **True recall** (in progress) = (correct links found) / (correct links existing) — based on manual ground truth annotation of 50 random decisions

---

## 4. Limitations and known FP patterns

The 12% of FPs in V3 (eliminated to ≤2% in V7 via proximity veto) come from 4 patterns:

| Pattern | Frequency | Mitigation in V7 |
|---|---|---|
| Code collision (e.g., art. 1382 in Code civil and CGI) | 4/11 | Proximity veto + `(?-i:[A-Z])(?![a-z])` fix |
| Numbered law (e.g., "art. 33 de la loi n° 94-126") | 2/11 | Proximity veto on "de la loi" patterns |
| Other code (douanes, monétaire, etc.) | 2/11 | Proximity veto extended |
| Decision header / metadata noise | 2/11 | Truncation + fiscal context filter |
| Ambiguous "même code" reference | 1/11 | TODO: coreference resolution |

---

## 5. Future improvements (post-paper)

- **NER fine-tuned on CamemBERT** (in training as of 2026-05-10) — weak supervision from 70k regex links + 200-500 manual gold labels. Target: precision 99%+, recall +10pt.
- **Coreference resolution** for "même code" / "ledit code" / "le même article" — requires deeper NLP.
- **Cross-validation against Judilibre `visa`** field on the 110k structured visas — provides a strong validation signal for that source.
- **Multiple-occurrence pass**: iterate over all mentions of a num in the text instead of just the first.

---

## 6. Reproducibility

All steps are reproducible with the scripts in `scripts/`:

```bash
python -m scripts.chrono_cgi                                       # ~50 min for full CGI/LPF
python -m scripts.link_juris_cgi --clear-fiscal                    # ~100 sec
python -m scripts.audit_sample --out audit_sample.csv              # ~2 min
python -m scripts.audit_autoannotate --in audit_sample.csv \
    --out audit_auto.csv                                           # ~30 sec
```

Required: PostgreSQL with the `legifrance_db` schema, PISTE Légifrance API credentials (`LEGIFRANCE_CLIENT_ID`, `LEGIFRANCE_CLIENT_SECRET`).
