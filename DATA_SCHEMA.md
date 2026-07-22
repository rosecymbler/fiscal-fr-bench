# Data Schema

Complete data dictionary for the FiscalQA Pro corpus, as it lives in PostgreSQL.

---

## Core tables

### `articles`
The versioned legislation table - each row is **one historical version** of a CGI/LPF article.

| Column | Type | Description |
|---|---|---|
| `id` | varchar(50) PK | Légifrance article version id (e.g., `LEGIARTI000029355796`) - **changes** between versions |
| `cid` | varchar(50) | Constant identifier across versions - **stable** across the entire history of an article |
| `num` | varchar(100) | Article number as printed (e.g., `"209 B"`, `"L16 B"`, `"1011 bis"`) |
| `etat` | varchar(50) | One of `VIGUEUR`, `ABROGE`, `MODIFIE`, `VIGUEUR_DIFF` (already voted, not yet effective) |
| `texte_html` | text | Raw HTML content |
| `texte_clean` | text | Cleaned plain-text content (HTML stripped) |
| `nota` | text | Editor notes (typically provenance of the version) |
| `date_debut` | timestamp | Start of effectivity for this version |
| `date_fin` | timestamp | End of effectivity for this version (`2999-01-01` = currently in force) |
| `code_id` | varchar(50) | LEGITEXT id of the parent code |
| `version` | varchar(50) | Version label (rare) |
| `nature` | varchar(100) | `LEGIARTI` or `KALIART` |
| `metadata` | jsonb | Catch-all for fields not mapped elsewhere |
| `extractor_version` | varchar(20) | Set to `chrono-cgi-1.0.0` for entries from this pipeline |

### `liens_jurisprudence_article`
The version-aware citation table - each row is **one citation** of a CGI/LPF article by a jurisprudence decision.

| Column | Type | Description |
|---|---|---|
| `id` | serial PK | Auto-increment |
| `jurisprudence_id` | varchar(100) | FK-like reference to `decisions_unified.id`, `inca.id`, `arianeweb_decisions.id`, or `judilibre_decisions.id` |
| `article_id` | varchar(50) | **Specific version** of the cited article (i.e., the version applicable at `date_decision`). Joins to `articles.id` |
| `article_cid` | varchar(50) | Constant identifier of the cited article (joins to `articles.cid`). Stable across versions |
| `type_lien` | varchar(100) | One of `VISE` (structured visa from Judilibre), `CITE` (extracted by regex), `APPLIQUE`, `INTERPRETE` |
| `imported_at` | timestamp | Insertion timestamp |

**Unique constraint** on `(jurisprudence_id, article_id, type_lien)` - same article cannot be cited twice with the same type.

### Tax codes - LEGITEXT mapping

| `code_id` | Code | Articles | Earliest `date_debut` |
|---|---|---|---|
| `LEGITEXT000006069577` | Code général des impôts | 21 040 | 1950-04-30 |
| `LEGITEXT000006069583` | Livre des procédures fiscales | 3 031 | 1938-06-01 |
| `LEGITEXT000006069568` | CGI annexe I | 295 | 1950-04-30 |
| `LEGITEXT000006069569` | CGI annexe II | 2 390 | 1970-11-11 |
| `LEGITEXT000006069574` | CGI annexe III | 3 801 | 1950-04-30 |
| `LEGITEXT000006069576` | CGI annexe IV | 1 879 | 1979-03-11 |

Total: **32 436 article-versions**, **93 ans** d'historique (1938–2031 : le LPF
commence en 1938 ; les versions 2026–2031 sont des versions **`VIGUEUR_DIFF`**,
déjà votées mais à effet différé - elles comptent dans l'axe temporel mais ne
sont jamais servies comme version « en vigueur » par `select_version_current`).

---

## Jurisprudence sources

The fiscal decisions come from 4 source tables. **`decisions_unified` is an aggregation table** - it consolidates `inca`, `judilibre`, `arianeweb`, plus other sources (arcep, hudoc, arcom, amf). When computing a global recall, treating `unified` and the underlying sources as independent leads to double-counting.

### Recommended approach
For paper-quality metrics, treat the 4 sources independently and report by source.

### Source breakdown (in `decisions_unified.source_principale`)

| Source | Decisions in unified | Coverage |
|---|---|---|
| `inca` | 300 383 | Cour de cassation historical |
| `judilibre` | 265 891 | Cour de cassation modern (with structured `visa`) |
| `arianeweb` | 51 564 | Conseil d'État |
| `arcep` | 36 808 | Telecom regulator |
| `hudoc` | 6 187 | European Court of Human Rights |
| `arcom`, `amf` | 2 915 | Sectoral regulators |

### Schema highlights per source

#### `decisions_unified`
- `id`, `ecli`, `source_principale`, `juridiction`, `chambre`, `formation`
- `numero_pourvoi`, `date_decision`, `solution`
- `texte_complet`, `texte_nettoye`, `sommaire`
- `articles_cites` (text[]) - *currently empty, future enrichment*
- `themes` (jsonb), `mots_cles` (text[])
- `texte_tsvector` - indexed for fast fiscal filter

#### `judilibre_decisions`
Same as `unified`, plus:
- `visa` (jsonb) - **structured citation list**, e.g., `[{"title": "Code général des impôts", "articles": ["209 B"]}]`. Used for `VISE` link type.
- `formation`, `chamber`, `nac_code`

#### `inca`, `arianeweb_decisions`
Standard schema with `texte_clean`, `date_decision`, `juridiction` / `chambre` / `formation`.

---

## Fiscal subset filters

| Source | SQL filter |
|---|---|
| `decisions_unified` | `texte_tsvector @@ to_tsquery('french', 'fiscal | impôt | impôts | TVA | CGI')` |
| `inca` | `texte_clean ILIKE` on `'%CGI%'`, `'%LPF%'`, `'%procédures fiscales%'`, `'%code général des impôts%'` |
| `arianeweb_decisions` | `texte_clean ILIKE` on `'%CGI%'`, `'%TVA%'`, `'%impôt sur les sociétés%'`, etc. |
| `judilibre_decisions` | `visa::text ILIKE` on `'%général des impôts%'`, `'%CGI%'`, `'%procédures fiscales%'` |

**Fiscal subset sizes (V7)**:

| Source | Total | Fiscal subset | Linked decisions | Recall |
|---|---|---|---|---|
| arianeweb (CE) | 51 564 | 6 568 | 5 451 | **83.0%** |
| inca (Cass) | 300 383 | 9 424 | 8 077 | **85.7%** |
| judilibre visa | 265 891 | 907 | 842 | **92.8%** |
| unified (mixed) | 663 748 | 44 936 | 17 635 | 39.2% (high noise) |

---

## Useful queries

### Get an article at a specific date

```sql
SELECT id, num, etat, date_debut, date_fin, texte_clean
FROM articles
WHERE cid = 'LEGIARTI000006308669'      -- art. 219 CGI (taux IS)
  AND date_debut::date <= '2018-06-12'
  AND date_fin::date   >= '2018-06-12'
LIMIT 1;
```

### List jurisprudence citing an article

```sql
SELECT
    l.jurisprudence_id,
    l.article_id    AS version_cited,
    l.type_lien,
    COALESCE(d.date_decision, i.date_decision, ar.date_decision, j.decision_date) AS date_decision
FROM liens_jurisprudence_article l
LEFT JOIN decisions_unified d     ON d.id  = l.jurisprudence_id
LEFT JOIN inca i                  ON i.id  = l.jurisprudence_id
LEFT JOIN arianeweb_decisions ar  ON ar.id = l.jurisprudence_id
LEFT JOIN judilibre_decisions j   ON j.id  = l.jurisprudence_id
WHERE l.article_cid = 'LEGIARTI000006303451'    -- art. 209 B CGI
ORDER BY date_decision DESC;
```

### Distribution of links by decade (longitudinal eval matériel)

```sql
WITH liens_dates AS (
  SELECT l.jurisprudence_id,
         COALESCE(d.date_decision, i.date_decision, ar.date_decision, j.decision_date) AS dd
  FROM liens_jurisprudence_article l
  LEFT JOIN decisions_unified d ON d.id = l.jurisprudence_id
  LEFT JOIN inca i ON i.id = l.jurisprudence_id
  LEFT JOIN arianeweb_decisions ar ON ar.id = l.jurisprudence_id
  LEFT JOIN judilibre_decisions j ON j.id = l.jurisprudence_id
)
SELECT
  CASE
    WHEN dd < '1990-01-01' THEN 'avant 1990'
    WHEN dd < '2000-01-01' THEN '1990-1999'
    WHEN dd < '2010-01-01' THEN '2000-2009'
    WHEN dd < '2020-01-01' THEN '2010-2019'
    ELSE                          '2020-2026'
  END AS decennie,
  COUNT(DISTINCT jurisprudence_id) AS nb_decisions,
  COUNT(*) AS nb_liens
FROM liens_dates
WHERE dd IS NOT NULL
GROUP BY decennie
ORDER BY decennie;
```

---

## Exports for downstream consumption

To be generated and added to `data/`:

```bash
# Articles (Parquet, ~30 MB)
COPY (SELECT * FROM articles
      WHERE code_id IN ('LEGITEXT000006069577','LEGITEXT000006069568',
                        'LEGITEXT000006069569','LEGITEXT000006069574',
                        'LEGITEXT000006069576','LEGITEXT000006069583'))
TO '/data/articles_cgi_versioned.csv' WITH (FORMAT csv, HEADER);

# Liens (Parquet, ~10 MB)
COPY (SELECT l.* FROM liens_jurisprudence_article l
      JOIN articles a ON a.id = l.article_id
      WHERE a.code_id IN (...))
TO '/data/liens_juris_cgi.csv' WITH (FORMAT csv, HEADER);
```
