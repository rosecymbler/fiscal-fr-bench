"""
Génère un échantillon de 100 liens fiscaux à annoter manuellement
pour mesurer la précision réelle du linking jurisprudence ↔ CGI.

Stratifie par source pour que les 4 sources soient représentées proportionnellement
au volume réel.

Output: CSV avec snippet de texte autour de la mention.

Usage:
    python -m data_pipeline.scripts.audit_sample
    python -m data_pipeline.scripts.audit_sample --n 100 --out /tmp/audit_links.csv
"""
import sys
import os
import argparse
import csv
import random
import re

import psycopg2

def get_db_connection():
    return psycopg2.connect(dbname=os.environ.get("PGDATABASE", "legifrance_db"))

CODES_FISCAUX_LIST = (
    'LEGITEXT000006069577', 'LEGITEXT000006069568', 'LEGITEXT000006069569',
    'LEGITEXT000006069574', 'LEGITEXT000006069576', 'LEGITEXT000006069583',
)


def get_text_for_decision(cursor, jid):
    """Tente de récupérer le texte clean depuis les 4 tables."""
    queries = [
        ("decisions_unified", "texte_nettoye", "date_decision", "juridiction"),
        ("inca", "texte_clean", "date_decision", "chambre"),
        ("arianeweb_decisions", "texte_clean", "date_decision", "juridiction"),
        ("judilibre_decisions", "text_clean", "decision_date", "jurisdiction"),
    ]
    for table, text_col, date_col, jur_col in queries:
        cursor.execute(
            f"SELECT {text_col}, {date_col}, {jur_col} FROM {table} WHERE id = %s LIMIT 1",
            (jid,),
        )
        row = cursor.fetchone()
        if row:
            return row[0], row[1], row[2], table
    return None, None, None, None


def find_snippet(text, num, window=120):
    """Renvoie un snippet du texte autour de la 1re mention de num."""
    if not text or not num:
        return ""
    # On nettoie les espaces multiples
    text_clean = re.sub(r"\s+", " ", text)
    num_normalized = re.sub(r"\s+", " ", num.strip())
    # Tente 1: mention textuelle exacte
    pos = text_clean.lower().find(f"article {num_normalized.lower()}")
    if pos < 0:
        pos = text_clean.lower().find(num_normalized.lower())
    if pos < 0:
        return text_clean[:200] + "…"
    start = max(0, pos - window)
    end = min(len(text_clean), pos + len(num_normalized) + window)
    snippet = text_clean[start:end].replace("\n", " ")
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text_clean) else "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--out", default="/tmp/audit_links.csv")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)

    conn = get_db_connection()
    cursor = conn.cursor()

    # Stratification proportionnelle aux résultats du batch :
    # unified 30851, inca 5830, arianeweb 6441, judilibre 758 (total 43880)
    # → ~70%, 13%, 15%, 2% - on prend 100 liens stratifiés
    quotas = {
        "decisions_unified": 70,
        "arianeweb_decisions": 15,
        "inca": 13,
        "judilibre_decisions": 2,
    }

    sampled_links = []

    for table, n in quotas.items():
        # Échantillonner n décisions de cette table qui ont au moins 1 lien fiscal
        cursor.execute(
            f"""
            SELECT l.jurisprudence_id, l.article_id, l.article_cid, l.type_lien,
                   a.num, a.code_id
            FROM liens_jurisprudence_article l
            JOIN articles a ON a.id = l.article_id
            WHERE a.code_id IN %s
              AND l.jurisprudence_id IN (SELECT id FROM {table})
            ORDER BY RANDOM()
            LIMIT %s
            """,
            (CODES_FISCAUX_LIST, n),
        )
        rows = cursor.fetchall()
        for row in rows:
            sampled_links.append((table, *row))

    print(f"📊 {len(sampled_links)} liens échantillonnés (stratifiés par source)")

    # Pour chaque lien : récup snippet
    rows_csv = []
    for i, (table, jid, art_id, art_cid, type_lien, num, code_id) in enumerate(sampled_links, 1):
        text, date, juridiction, _ = get_text_for_decision(cursor, jid)
        snippet = find_snippet(text, num) if text else "(texte indisponible)"

        code_label = "CGI" if code_id == "LEGITEXT000006069577" else \
                     "LPF" if code_id == "LEGITEXT000006069583" else "CGI annexe"

        rows_csv.append({
            "n°": i,
            "source": table,
            "jurisprudence_id": jid,
            "juridiction": juridiction or "",
            "date_decision": str(date) if date else "",
            "article_matche": f"{code_label} art. {num}",
            "type_lien": type_lien,
            "snippet": snippet,
            "annotation_VP_FP": "",  # à remplir : VP (vrai positif) ou FP (faux positif) ou DOUTE
            "commentaire": "",
        })
        if i % 20 == 0:
            print(f"   {i}/{len(sampled_links)}…")

    # Écrire CSV
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows_csv[0].keys())
        writer.writeheader()
        writer.writerows(rows_csv)

    cursor.close()
    conn.close()

    print(f"\n✅ Échantillon prêt : {args.out}")
    print(f"   Format : CSV avec colonnes annotation_VP_FP et commentaire à remplir")
    print(f"   Légende : VP = vrai positif, FP = faux positif, DOUTE = pas sûr")


if __name__ == "__main__":
    main()
