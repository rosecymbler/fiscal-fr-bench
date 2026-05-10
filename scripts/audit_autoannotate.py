"""
Auto-annoteur de l'échantillon audit pour mesurer la précision du linking.

Pour chaque lien (jurisprudence_id, article_id, num) :
  1. Récupère le texte complet de la décision
  2. Cherche le `num` exact (avec variantes) dans le texte
  3. Si trouvé → vérifie le contexte fiscal (CGI/LPF nommé) ±150 chars
  4. Annote : VP_HIGH (article + CGI explicite), VP_MED (article + contexte fiscal),
     FP_HIGH (article ABSENT du texte), DOUTE (article présent sans contexte fiscal fort)

Usage:
    python -m data_pipeline.scripts.audit_autoannotate --in /tmp/audit_links_v3.csv --out /tmp/audit_v3_auto.csv
"""
import os
import re
import csv
import argparse

import psycopg2

# CGI/LPF nommés (périmètre fiscal Talia)
# Le code des douanes EST EXCLU — c'est un autre code dans cet audit
CGI_HINT = re.compile(
    r"\b(?:cgi|code\s+g[ée]n[ée]ral\s+des\s+imp[oô]ts|"
    r"livre\s+des\s+proc[ée]dures\s+fiscales|lpf|"
    r"annexe\s+(?:i|ii|iii|iv)\s+(?:du\s+)?(?:cgi|code\s+g[ée]n[ée]ral))",
    re.IGNORECASE,
)

# Autres codes juridiques
OTHER_CODES = re.compile(
    r"\b(?:code\s+des\s+douanes|"
    r"code\s+civil|code\s+p[ée]nal|code\s+de\s+(?:proc[ée]dure|commerce|"
    r"travail|consommation|sant[ée]\s+publique|s[ée]curit[ée]\s+sociale|"
    r"l[']\s*environnement|l[']\s*urbanisme|justice\s+administrative|"
    r"propri[ée]t[ée]\s+intellectuelle|l[']\s*action\s+sociale)|"
    r"code\s+mon[ée]taire\s+et\s+financier|"
    r"convention\s+(?:europ[ée]enne|de\s+sauvegarde)|"
    r"nouveau\s+code\s+de\s+proc[ée]dure)",
    re.IGNORECASE,
)

# Sources juridiques alternatives (loi numérotée, convention, décret, arrêté, directive)
OTHER_SOURCES = re.compile(
    r"\b(?:loi\s+n[°o]\s*\d+|loi\s+du\s+\d{1,2}\s+(?:janvier|f[ée]vrier|mars|avril|mai|"
    r"juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre)|"
    r"convention\s+(?:europ[ée]enne|de\s+sauvegarde|internationale|fiscale)|"
    r"d[ée]cret\s+(?:n[°o]|du)|arr[êe]t[ée]\s+(?:n[°o]|du|minist[ée]riel)|"
    r"directive\s+(?:\d+|du|n[°o]|\(ce\)|\(ue\))|"
    r"r[èe]glement\s+(?:n[°o]|\(ce\)|\(ue\)|du))",
    re.IGNORECASE,
)


def get_db_connection():
    return psycopg2.connect(dbname=os.environ.get("PGDATABASE", "legifrance_db"))


def get_text_for_decision(cursor, jid):
    """Récupère le texte clean depuis les 4 tables possibles."""
    queries = [
        ("decisions_unified", "texte_nettoye"),
        ("inca", "texte_clean"),
        ("arianeweb_decisions", "texte_clean"),
        ("judilibre_decisions", "text_clean"),
    ]
    for table, text_col in queries:
        cursor.execute(
            f"SELECT {text_col} FROM {table} WHERE id = %s LIMIT 1", (jid,),
        )
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]
    return ""


def num_to_search_patterns(num):
    """Génère les variantes textuelles d'un num d'article.
    Ex: '209 B' → ['209 B', '209B', '209-B']
        'L16 B' → ['L. 16 B', 'L 16 B', 'L16 B', 'L.16 B']
        '1011 bis' → ['1011 bis', '1011bis']
    """
    n = re.sub(r"\s+", " ", num.strip())
    patterns = {n}
    # LPF style
    m = re.match(r"^([LR])\s*(\d+(?:\s*[A-Za-z\-\d]+)*)", n)
    if m:
        prefix = m.group(1)
        rest = m.group(2)
        for sep in [". ", " ", ".", ""]:
            patterns.add(f"{prefix}{sep}{rest}")
    # Sans espace
    patterns.add(n.replace(" ", ""))
    return list(patterns)


def find_num_position(text, num):
    """Cherche la position de la 1ère mention de num dans le texte.
    Retourne (pos, matched_pattern) ou (-1, None).
    """
    text_lower = text.lower()
    for pattern in num_to_search_patterns(num):
        # Recherche case-insensitive
        idx = text_lower.find(pattern.lower())
        if idx >= 0:
            return idx, pattern
    return -1, None


def annotate_link(text, num, code_label):
    """Annote un lien : VP_HIGH, VP_MED, FP_HIGH, DOUTE.

    Logique en 2 étages :
    1. Fenêtre PROCHE (±60 chars après le num) — le plus déterministe :
       - "article X du CGI" → VP_HIGH
       - "article X du code des douanes" / "art. X du code civil" → FP_HIGH
       - "article X de la loi du …" / "art. X de la convention …" → FP_HIGH
    2. Fenêtre LARGE (±300 chars) en backup :
       - CGI/LPF + pas d'autre code/source → VP_HIGH
       - CGI/LPF + autre code → VP_MED
       - autre code seul → FP_HIGH
       - rien → DOUTE
    """
    if not text:
        return "DOUTE", "texte_indisponible", ""

    pos, matched = find_num_position(text, num)
    if pos < 0:
        return "FP_HIGH", "num_absent_du_texte", ""

    # Étage 1 : fenêtre PROCHE — focus sur ce qui suit immédiatement le numéro
    near_start = max(0, pos - 30)
    near_end = min(len(text), pos + len(matched) + 80)
    near_ctx = text[near_start:near_end]

    if CGI_HINT.search(near_ctx):
        return "VP_HIGH", "CGI_LPF_nomme_proche", re.sub(r"\s+", " ", near_ctx).strip()
    if OTHER_CODES.search(near_ctx):
        return "FP_HIGH", "num_attache_autre_code", re.sub(r"\s+", " ", near_ctx).strip()
    if OTHER_SOURCES.search(near_ctx):
        return "FP_HIGH", "num_attache_loi_convention_decret", re.sub(r"\s+", " ", near_ctx).strip()

    # Étage 2 : fenêtre LARGE en backup
    wide_start = max(0, pos - 300)
    wide_end = min(len(text), pos + len(matched) + 300)
    wide_ctx = text[wide_start:wide_end]

    cgi_match = CGI_HINT.search(wide_ctx)
    other_match = OTHER_CODES.search(wide_ctx) or OTHER_SOURCES.search(wide_ctx)

    snippet_dump = re.sub(r"\s+", " ", wide_ctx).strip()

    if cgi_match and not other_match:
        return "VP_HIGH", "CGI_seul_dans_contexte_large", snippet_dump
    if cgi_match and other_match:
        # CGI mentionné dans la zone large mais aussi un autre code → ambigu, classer VP_MED
        return "VP_MED", "CGI_et_autre_code_pas_proches", snippet_dump
    if other_match and not cgi_match:
        return "FP_HIGH", "autre_code_dominant", snippet_dump
    return "DOUTE", "num_present_sans_contexte_clair", snippet_dump


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="input", default="/tmp/audit_links_v3.csv")
    parser.add_argument("--out", default="/tmp/audit_v3_auto.csv")
    args = parser.parse_args()

    conn = get_db_connection()
    cursor = conn.cursor()

    rows_out = []
    counts = {"VP_HIGH": 0, "VP_MED": 0, "FP_HIGH": 0, "DOUTE": 0}

    with open(args.input, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, 1):
            jid = row["jurisprudence_id"]
            text = get_text_for_decision(cursor, jid)
            article = row["article_matche"]
            # Extraire num depuis "CGI art. 209 B" / "LPF art. L16 B"
            num_match = re.search(r"art\.\s+(.+)$", article)
            num = num_match.group(1).strip() if num_match else ""
            code_label = article.split(" art.")[0]

            label, reason, ctx = annotate_link(text, num, code_label)
            counts[label] += 1

            rows_out.append({
                **row,
                "auto_annotation": label,
                "auto_reason": reason,
                "auto_context": ctx[:300],
            })

            if i % 25 == 0:
                print(f"   {i}/100…")

    cursor.close()
    conn.close()

    # Écrire CSV enrichi
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows_out[0].keys())
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"\n📊 Auto-annotation:")
    print(f"   VP_HIGH (CGI/LPF nommé, autre code absent)  : {counts['VP_HIGH']:3d}")
    print(f"   VP_MED  (CGI/LPF + autre code, CGI + proche): {counts['VP_MED']:3d}")
    print(f"   FP_HIGH (num absent OU autre code dominant) : {counts['FP_HIGH']:3d}")
    print(f"   DOUTE   (à valider à la main)               : {counts['DOUTE']:3d}")
    print(f"\n   Précision optimiste (HIGH+MED) : {(counts['VP_HIGH']+counts['VP_MED']):3d}/100 = {counts['VP_HIGH']+counts['VP_MED']}%")
    print(f"   Précision pessimiste (HIGH only): {counts['VP_HIGH']:3d}/100 = {counts['VP_HIGH']}%")
    print(f"\n✅ {args.out}")


if __name__ == "__main__":
    main()
