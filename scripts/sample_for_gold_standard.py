"""
Génère un échantillon de 50 décisions pour annotation manuelle gold standard.

Stratification proportionnelle :
- 25 decisions_unified (mais on prend depuis les sources brutes pour éviter double-comptage)
- 12 inca (Cour de cassation)
- 10 arianeweb_decisions (Conseil d'État)
- 3 judilibre_decisions

Critères :
- Décision ≥3 000 chars (suffisamment de matière à analyser)
- Pas dans le sample audit n=100 (seed=42)
- Du sous-ensemble fiscal (mêmes filtres que link_juris_cgi.py)

Sortie : CSV avec colonnes vides `articles_cites` et `commentaire` pour annotation.

Usage:
    python -m scripts.sample_for_gold_standard
    python -m scripts.sample_for_gold_standard --out /tmp/gold_standard_sample.csv
"""
import os
import csv
import random
import argparse

import psycopg2

CODES_FISCAUX = (
    'LEGITEXT000006069577', 'LEGITEXT000006069568', 'LEGITEXT000006069569',
    'LEGITEXT000006069574', 'LEGITEXT000006069576', 'LEGITEXT000006069583',
)

# Stratification : 25 unified + 12 inca + 10 arianeweb + 3 judilibre = 50 total
STRATA = [
    ("decisions_unified", 25, "texte_nettoye", "date_decision", "juridiction", "chambre"),
    ("inca", 12, "texte_clean", "date_decision", "chambre", "formation"),
    ("arianeweb_decisions", 10, "texte_clean", "date_decision", "juridiction", "formation"),
    ("judilibre_decisions", 3, "text_clean", "decision_date", "jurisdiction_name", "chamber_name"),
]


def get_db_connection():
    return psycopg2.connect(dbname=os.environ.get("PGDATABASE", "legifrance_db"))


def get_audit_sample_ids(cursor, seed=42):
    """Récupère les IDs déjà utilisés dans le sample audit (audit_sample.py).
    On les exclut pour éviter le double-test."""
    random.seed(seed)
    quotas = {
        "decisions_unified": 70,
        "arianeweb_decisions": 15,
        "inca": 13,
        "judilibre_decisions": 2,
    }
    audit_ids = set()
    for table, n in quotas.items():
        cursor.execute(
            f"""
            SELECT l.jurisprudence_id
            FROM liens_jurisprudence_article l
            JOIN articles a ON a.id = l.article_id
            WHERE a.code_id IN %s
              AND l.jurisprudence_id IN (SELECT id FROM {table})
            ORDER BY RANDOM()
            LIMIT %s
            """,
            (CODES_FISCAUX, n),
        )
        audit_ids.update(row[0] for row in cursor.fetchall())
    return audit_ids


def fetch_stratum(cursor, table, n, text_col, date_col, juri_col, formation_col, exclude_ids):
    """Tire n décisions fiscales aléatoires depuis une source, en excluant les IDs."""
    # Filtre fiscal selon la source (cohérent avec link_juris_cgi.py)
    if table == "decisions_unified":
        fiscal_filter = "texte_tsvector @@ to_tsquery('french','fiscal | impôt | impôts | TVA | CGI')"
    elif table == "judilibre_decisions":
        fiscal_filter = ("(visa::text ILIKE '%général des impôts%' OR visa::text ILIKE '%CGI%' "
                         "OR visa::text ILIKE '%procédures fiscales%' OR visa::text ILIKE '%LPF%')")
    elif table == "inca":
        fiscal_filter = ("(texte_clean ILIKE '%code général des impôts%' OR texte_clean ILIKE '%CGI%' "
                         "OR texte_clean ILIKE '%procédures fiscales%' OR texte_clean ILIKE '%LPF%')")
    else:  # arianeweb_decisions
        fiscal_filter = ("(texte_clean ILIKE '%code général des impôts%' OR texte_clean ILIKE '%CGI%' "
                         "OR texte_clean ILIKE '%TVA%' OR texte_clean ILIKE '%impôt sur les sociétés%' "
                         "OR texte_clean ILIKE '%impôt sur le revenu%')")

    # Récupère un grand pool, on filtrera longueur + exclude après
    # Note : on escape les % du fiscal_filter pour psycopg2 (sinon interprétés comme placeholders)
    fiscal_filter_esc = fiscal_filter.replace("%", "%%")
    cursor.execute(
        f"""
        SELECT id, {text_col}, {date_col}, {juri_col}, {formation_col}
        FROM {table}
        WHERE {fiscal_filter_esc}
          AND {text_col} IS NOT NULL
          AND LENGTH({text_col}) >= 3000
        ORDER BY RANDOM()
        LIMIT %s
        """,
        (n * 5,),  # pool 5x pour avoir de la marge après exclusion
    )
    rows = []
    for row in cursor.fetchall():
        if row[0] in exclude_ids:
            continue
        rows.append((table, *row))
        if len(rows) >= n:
            break
    return rows


def build_url(table, jid, decision_date):
    """Construit l'URL Légifrance pour la décision."""
    if table == "arianeweb_decisions":
        if jid.startswith("aw__Ariane_Web"):
            num = jid.split("__")[-1]
            return f"https://www.conseil-etat.fr/arianeweb/CE/decision/{decision_date}/{num}" if decision_date else ""
    if table == "judilibre_decisions":
        return f"https://www.courdecassation.fr/decision/{jid}"
    if table == "inca":
        if jid.startswith("inca_") or jid.startswith("JURITEXT"):
            id_clean = jid.replace("inca_", "")
            return f"https://www.courdecassation.fr/decision/{id_clean}"
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/gold_standard_sample.csv")
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()

    random.seed(args.seed)

    conn = get_db_connection()
    cursor = conn.cursor()

    print("📊 Récupération des IDs déjà dans le sample audit (à exclure)...")
    exclude_ids = get_audit_sample_ids(cursor)
    print(f"   {len(exclude_ids)} IDs à exclure")

    all_rows = []
    for table, n, text_col, date_col, juri_col, formation_col in STRATA:
        print(f"\n📊 Tirage {n} de {table}...")
        rows = fetch_stratum(cursor, table, n, text_col, date_col, juri_col, formation_col, exclude_ids)
        print(f"   ✅ {len(rows)} décisions tirées")
        all_rows.extend(rows)

    # Mélanger l'ordre pour que le fiscaliste alterne entre sources
    random.shuffle(all_rows)

    # Construire CSV
    csv_rows = []
    for i, (table, jid, text, dd, juri, formation) in enumerate(all_rows, 1):
        text_extrait = (text[:5000] + "…") if len(text) > 5000 else text
        # Nettoyer pour CSV
        text_extrait = text_extrait.replace("\n", " ").replace("\r", " ")
        csv_rows.append({
            "n°": i,
            "jurisprudence_id": jid,
            "source": table.replace("_decisions", ""),
            "juridiction": juri or "",
            "formation": formation or "",
            "date_decision": str(dd) if dd else "",
            "numero_pourvoi": "",  # sera complété si besoin
            "texte_url": build_url(table, jid, dd),
            "texte_extrait": text_extrait,
            "articles_cites": "",  # à remplir
            "commentaire": "",      # à remplir
        })

    cursor.close()
    conn.close()

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_rows[0].keys())
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f"\n✅ CSV gold standard prêt : {args.out}")
    print(f"   {len(csv_rows)} décisions à annoter")
    print(f"   À transmettre au fiscaliste avec SPEC_GOLD_STANDARD.md")


if __name__ == "__main__":
    main()
