"""
Cross-validation : compare nos liens regex vs le `visa` structuré Judilibre
pour mesurer un recall externe sans annotation manuelle.

Pour chaque décision Judilibre du sous-ensemble fiscal :
  1. Parse le `visa` (jsonb) pour extraire les (source, num) attendus côté CGI/LPF
  2. Récupère ce qu'on a linké via le pipeline regex
  3. Calcule recall (lower bound) = |regex ∩ visa| / |visa|
     (la précision n'est pas comparable car notre regex peut linker à raison
     des articles cités dans les motifs en plus du visa)

Usage:
    python -m data_pipeline.scripts.cross_validate_judilibre
    python -m data_pipeline.scripts.cross_validate_judilibre --limit 100  # smoke
"""
import os
import re
import json
import argparse

import psycopg2

CODES_FISCAUX = (
    'LEGITEXT000006069577', 'LEGITEXT000006069568', 'LEGITEXT000006069569',
    'LEGITEXT000006069574', 'LEGITEXT000006069576', 'LEGITEXT000006069583',
)

# Patterns de source dans les visa Judilibre
SOURCE_FISCAL_RE = re.compile(
    r"\b(?:CGI(?:AN[1-4])?|"
    r"livre\s+des\s+proc[ée]dures\s+fiscales|LPF|"
    r"code\s+g[ée]n[ée]ral\s+des\s+imp[oô]ts)\b",
    re.IGNORECASE,
)

# Pattern article num (formats fiscaux variés)
NUM_RE = re.compile(
    r"(L\.?\s*\d{1,4}(?:-\d{1,4})?(?:\s*[A-Z]{1,3})?|"
    r"R\.?\s*\d{1,4}(?:-\d{1,4})?(?:\s*[A-Z]{1,3})?|"
    r"\d{1,4}(?:[-\s]\d{1,3})?(?:\s+(?:bis|ter|quater|quinquies|sexies|septies|"
    r"octies|nonies|decies))?(?:\s+[A-Z](?:-\d)?)?)",
    re.IGNORECASE,
)


def get_db_connection():
    return psycopg2.connect(dbname=os.environ.get("PGDATABASE", "legifrance_db"))


def parse_visa_fiscal(visa_jsonb):
    """Parse un visa Judilibre et extrait l'ensemble de num CGI/LPF attendus.
    Format typique : [{"title": "CGI 1649-septies, L147"}, {"title": "Loi 55-349"}]
    Retourne un set de num normalisés (uppercase, espaces réduits).
    """
    if not visa_jsonb:
        return set()
    if isinstance(visa_jsonb, str):
        try:
            visa_jsonb = json.loads(visa_jsonb)
        except json.JSONDecodeError:
            return set()
    if not isinstance(visa_jsonb, list):
        return set()

    expected = set()
    for v in visa_jsonb:
        if not isinstance(v, dict):
            continue
        title = v.get("title") or ""
        # Couper aux changements de source (split par "Code civil", "Loi", etc.)
        # Plus simple : ne traiter que si CGI/LPF apparaît dans le titre
        if not SOURCE_FISCAL_RE.search(title):
            continue
        # Extraire la portion fiscale du titre
        # Approche : trouver toutes les positions des sources CGI/LPF, prendre les chunks après
        positions = [m.start() for m in SOURCE_FISCAL_RE.finditer(title)]
        positions.append(len(title))
        for i in range(len(positions) - 1):
            chunk = title[positions[i]:positions[i+1]]
            # Extraire les num du chunk
            for m in NUM_RE.finditer(chunk):
                num = re.sub(r"\s+", " ", m.group(1).strip()).upper()
                # Filtrer les pseudo-num (années, décrets confondus)
                if re.fullmatch(r"\d{4}", num) and int(num) > 1900:
                    continue  # année probable
                # Normaliser "L. 47" → "L47", "L 47" → "L47"
                num_compact = re.sub(r"^([LR])\.?\s*", r"\1", num)
                expected.add(num_compact)
                # Ajouter aussi la variante sans normalisation pour le fuzzy match
                expected.add(num)
    return expected


def normalize_for_match(num):
    """Normalise un num pour comparaison robuste."""
    n = re.sub(r"\s+", " ", num.strip()).upper()
    n_compact = re.sub(r"^([LR])\.?\s*", r"\1", n)
    return n_compact


def stems_for_match(num):
    """Génère plusieurs niveaux de "stems" pour comparaison fuzzy.
    Permet de matcher "L16" avec "L16 B" ou "L16 A" (le visa Judilibre
    est souvent moins précis que notre extraction)."""
    norm = normalize_for_match(num)
    stems = {norm}
    # Stem 1 : retirer le suffixe alpha (ex "L16 B" → "L16", "1805 A" → "1805")
    m = re.match(r"^(.+?)\s+[A-Z](?:\s|$)", norm + " ")
    if m:
        stems.add(m.group(1).strip())
    # Stem 2 : retirer ordinal latin (ex "39 quinquies" → "39")
    m = re.match(r"^(.+?)\s+(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies)", norm, re.IGNORECASE)
    if m:
        stems.add(m.group(1).strip())
    # Stem 3 : retirer suffixe -nombre (ex "R281-1" → "R281")
    m = re.match(r"^([LR]\d+)-\d+", norm)
    if m:
        stems.add(m.group(1))
    # Stem 4 : seul le num pur (sans préfixe L/R)
    m = re.match(r"^[LR](\d+.*)", norm)
    if m:
        stems.add(m.group(1).split()[0].strip())
    return stems


def fuzzy_match(set_a, set_b):
    """Matche au niveau des stems (catch les cas L16 vs L16 B).
    Retourne (matched_a, matched_b) où matched_a ⊂ set_a et matched_b ⊂ set_b
    sont les éléments matchés via stems."""
    matched_a = set()
    matched_b = set()
    for a in set_a:
        stems_a = stems_for_match(a)
        for b in set_b:
            stems_b = stems_for_match(b)
            if stems_a & stems_b:
                matched_a.add(a)
                matched_b.add(b)
                break
    return matched_a, matched_b


def get_linked_articles(cursor, jurisprudence_id):
    """Récupère les num d'articles liés à une décision (via notre pipeline)."""
    cursor.execute(
        """
        SELECT DISTINCT a.num
        FROM liens_jurisprudence_article l
        JOIN articles a ON a.id = l.article_id
        WHERE l.jurisprudence_id = %s
          AND a.code_id IN %s
          AND a.num IS NOT NULL
        """,
        (jurisprudence_id, CODES_FISCAUX),
    )
    return {normalize_for_match(row[0]) for row in cursor.fetchall()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Smoke test")
    args = parser.parse_args()

    conn = get_db_connection()
    cursor = conn.cursor()

    # Récupérer toutes les décisions Judilibre avec visa fiscal
    q = """
        SELECT id, visa
        FROM judilibre_decisions
        WHERE visa::text ILIKE '%général des impôts%' OR visa::text ILIKE '%CGI%'
            OR visa::text ILIKE '%procédures fiscales%' OR visa::text ILIKE '%LPF%'
    """
    if args.limit:
        q += f" LIMIT {args.limit}"
    cursor.execute(q)
    decisions = cursor.fetchall()
    print(f"📊 {len(decisions)} décisions Judilibre avec visa fiscal")

    # Stats globales
    n_decisions_with_expected = 0
    total_expected = 0
    total_linked = 0
    total_intersection = 0
    decisions_full_recall = 0   # toutes les attentes du visa retrouvées
    decisions_partial = 0
    decisions_zero = 0
    decisions_extra_linked = 0  # liens regex non dans le visa (probable motifs ou FP)

    miss_examples = []  # exemples de num manqués
    extra_examples = []  # exemples de num linkés en plus

    for jid, visa in decisions:
        expected = parse_visa_fiscal(visa)
        expected_norm = {normalize_for_match(n) for n in expected}
        if not expected_norm:
            continue
        n_decisions_with_expected += 1

        linked = get_linked_articles(cursor, jid)
        # Match fuzzy par stems (catch "L16" vs "L16 B")
        matched_expected, matched_linked = fuzzy_match(expected_norm, linked)

        total_expected += len(expected_norm)
        total_linked += len(linked)
        total_intersection += len(matched_expected)

        if not matched_expected:
            decisions_zero += 1
        elif matched_expected == expected_norm:
            decisions_full_recall += 1
        else:
            decisions_partial += 1

        # Articles manqués (dans visa, pas linké même fuzzy)
        missed = expected_norm - matched_expected
        if missed and len(miss_examples) < 10:
            miss_examples.append((jid, missed, list(expected_norm)[:5]))

        # Liens supplémentaires (linkés, pas dans visa fuzzy) - probablement motifs ou FP
        extras = linked - matched_linked
        if extras:
            decisions_extra_linked += 1
            if len(extra_examples) < 10:
                extra_examples.append((jid, extras, list(expected_norm)[:5]))

    cursor.close()
    conn.close()

    print(f"\n📊 Sous-ensemble exploitable : {n_decisions_with_expected} décisions avec visa fiscal parsable")
    print()
    print("=" * 70)
    print(" RÉSULTATS - CROSS-VALIDATION VS VISA JUDILIBRE")
    print("=" * 70)

    if n_decisions_with_expected == 0:
        print("❌ Aucune décision exploitable - le parser de visa peut être trop restrictif")
        return

    print(f"\n  Total articles attendus (visa fiscal)  : {total_expected}")
    print(f"  Total articles linkés (regex)          : {total_linked}")
    print(f"  Intersection (recall vs visa)          : {total_intersection}")
    print(f"\n  RECALL EXTERNE (sur articles)          : {100*total_intersection/total_expected:.1f}%")
    print(f"  Liens regex couverts par visa          : {100*total_intersection/total_linked:.1f}% (rest = motifs ou bruit)")
    print()
    print(f"  Décisions full recall (∀ visa retrouvé): {decisions_full_recall} ({100*decisions_full_recall/n_decisions_with_expected:.1f}%)")
    print(f"  Décisions partial recall               : {decisions_partial} ({100*decisions_partial/n_decisions_with_expected:.1f}%)")
    print(f"  Décisions zero recall (rien retrouvé)  : {decisions_zero} ({100*decisions_zero/n_decisions_with_expected:.1f}%)")
    print(f"  Décisions avec liens extras (>visa)    : {decisions_extra_linked} ({100*decisions_extra_linked/n_decisions_with_expected:.1f}%)")

    print("\n" + "=" * 70)
    print(" EXEMPLES DE NUM MANQUÉS (visa attendu, pas dans liens)")
    print("=" * 70)
    for jid, missed, expected in miss_examples[:5]:
        print(f"  {jid}")
        print(f"    Manqué : {missed}")
        print(f"    Visa attendu : {expected}")
        print()

    print("=" * 70)
    print(" EXEMPLES DE LIENS EXTRA (linkés, pas dans visa)")
    print("=" * 70)
    for jid, extras, expected in extra_examples[:5]:
        print(f"  {jid}")
        print(f"    Extras : {extras}")
        print(f"    Visa attendu : {expected}")
        print()


if __name__ == "__main__":
    main()
