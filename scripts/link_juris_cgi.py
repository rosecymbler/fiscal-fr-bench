"""
Batch linking jurisprudence ↔ CGI sur sous-ensemble FISCAL.

Pour chaque décision fiscale dans (decisions_unified, judilibre_decisions,
arianeweb_decisions, inca), extrait les références d'articles CGI / annexes
CGI / LPF du texte (regex CGI-spécifiques + visa structuré Judilibre) et
peuple `liens_jurisprudence_article`.

- Filtrage fiscal : tsvector ou ILIKE sur "CGI" / "code général des impôts" /
  "fiscal" / "TVA" / "impôt".
- Lookup : restreint aux articles fiscaux (CGI + 4 annexes + LPF).
- Type de lien : VISE pour les visas Judilibre structurés, CITE sinon.

Usage:
    python -m data_pipeline.scripts.link_juris_cgi --limit 100  # smoke
    python -m data_pipeline.scripts.link_juris_cgi --source unified
    python -m data_pipeline.scripts.link_juris_cgi
    python -m data_pipeline.scripts.link_juris_cgi --clear-fiscal
"""
import sys
import os
import re
import json
import argparse
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv()

from data_pipeline.extractors.base import (
    get_db_connection, print_progress,
    update_realtime_status, update_realtime_counters,
    reset_error_tracker, track_success, should_abort,
)

CODES_FISCAUX = (
    'LEGITEXT000006069577',  # CGI
    'LEGITEXT000006069568',  # Annexe I
    'LEGITEXT000006069569',  # Annexe II
    'LEGITEXT000006069574',  # Annexe III
    'LEGITEXT000006069576',  # Annexe IV
    'LEGITEXT000006069583',  # LPF
)

ORDINAUX = (
    "bis", "ter", "quater", "quinquies", "sexies", "septies",
    "octies", "nonies", "decies", "undecies", "duodecies",
    "terdecies", "quaterdecies", "quindecies", "sexdecies",
)

# Regex extraction "articles XXX[ suffixes...]"
# Capture: numéro (ex 209), suite optionnelle (ex "B", "octies", "ter", "WA septies", "0 bis")
# (?-i:[A-Z])(?![a-z]) : suffixe lettre seule en MAJUSCULE STRICTE et non suivi d'une minuscule
# (évite que "D" matche le "d" de "du Code civil")
ARTICLE_RE = re.compile(
    r"\bart(?:icle|\.)?s?\s+"
    r"(L\.?\s*\d{1,4}(?:[\s\-]\d{1,4})?|R\.?\s*\d{1,4}(?:[\s\-]\d{1,4})?|\d{1,4})"
    r"((?:\s+(?:" + "|".join(ORDINAUX) + r"|(?-i:[A-Z])(?![a-z])|\d{1,2}\s*°|0\s+bis))*)",
    re.IGNORECASE,
)

# Désambiguïsation faible : un mot fiscal vague suffit pour les num ambigus (long, avec suffixe, L./R.)
FISCAL_CONTEXT_RE = re.compile(
    r"\b(?:cgi|code\s+g[ée]n[ée]ral\s+des\s+imp[oô]ts|"
    r"livre\s+des\s+proc[ée]dures\s+fiscales|lpf|code\s+des\s+douanes|"
    r"imp[oô]ts?|fiscal|tva|imposition|redress|d[ée]ductibles?|"
    r"contribuable|redevable|cotisation|assujetti|taxes?|"
    r"plus[\-\s]values?|b[ée]n[ée]fices?\s+(?:industriels|non\s+commerciaux|agricoles))",
    re.IGNORECASE,
)

# Désambiguïsation forte : pour les num simples (1-3 chiffres sans suffixe), exiger CGI/LPF/code des douanes nommé
STRONG_FISCAL_CONTEXT_RE = re.compile(
    r"\b(?:cgi|code\s+g[ée]n[ée]ral\s+des\s+imp[oô]ts|"
    r"livre\s+des\s+proc[ée]dures\s+fiscales|lpf|code\s+des\s+douanes|"
    r"annexe\s+(?:i|ii|iii|iv)\s+(?:du\s+)?(?:cgi|code\s+g[ée]n[ée]ral))",
    re.IGNORECASE,
)

# Pattern num simple ambigu (ex: "39", "700", "8" — sans lettre, sans bis/ter, < 4 chiffres)
SIMPLE_NUM_RE = re.compile(r"^\d{1,3}$")


def normalize_num(num_main, suffix):
    """Normalise '209' + ' B' → '209 B'  /  'L. 169' + '' → 'L169'."""
    if not num_main:
        return None
    s = num_main.strip()
    s = re.sub(r"\s+", " ", s)
    # LPF style "L. 169" → "L169" (ou "L 169") — on tente plusieurs variantes plus tard
    s = re.sub(r"^([LR])\.?\s*", r"\1", s, flags=re.IGNORECASE).upper()
    suffix_clean = re.sub(r"\s+", " ", suffix or "").strip()
    if suffix_clean:
        s = f"{s} {suffix_clean}".strip()
    return s


def build_article_index(cursor):
    """Charge tous les articles fiscaux en mémoire et indexe par num normalisé.
    Retourne: dict { normalized_num: [(article_id, cid, code_id, num, etat)] }
    """
    cursor.execute(
        """
        SELECT id, cid, code_id, num, etat, date_debut, date_fin
        FROM articles
        WHERE code_id IN %s AND num IS NOT NULL AND num != ''
        """,
        (CODES_FISCAUX,),
    )
    idx = defaultdict(list)
    for aid, cid, code_id, num, etat, debut, fin in cursor.fetchall():
        # Normaliser le num en BDD pour matching
        n = re.sub(r"\s+", " ", num.strip()).upper()
        # Variantes : "L. 169" en BDD → on stocke aussi "L169"
        keys = {n}
        m = re.match(r"^([LR])\.\s*(.+)$", n)
        if m:
            keys.add(f"{m.group(1)}{m.group(2).replace(' ', '')}")
            keys.add(f"{m.group(1)} {m.group(2)}")
        for k in keys:
            idx[k].append((aid, cid, code_id, num, etat, debut, fin))
    return idx


# Sources concurrentes (autres codes / lois / conventions) — proximité courte = veto
COMPETING_SOURCE_NEAR_RE = re.compile(
    r"\b(?:du\s+code\s+(?:civil|p[ée]nal|de\s+(?:proc[ée]dure|commerce|"
    r"travail|consommation|sant[ée]|s[ée]curit[ée]|l[']\s*environnement|"
    r"l[']\s*urbanisme|justice\s+administrative|propri[ée]t[ée])|"
    r"des\s+douanes|mon[ée]taire\s+et\s+financier)|"
    r"de\s+la\s+(?:loi|convention|directive)\s+(?:n[°o]|du\s+\d|europ[ée]enne|de\s+sauvegarde)|"
    r"du\s+(?:nouveau\s+)?code\s+de\s+proc[ée]dure|"
    r"du\s+r[èe]glement\s+(?:n[°o]|\(ce\)|\(ue\))|"
    r"de\s+l[']\s*arr[êe]t[ée]\s+(?:n[°o]|du)|"
    r"du\s+d[ée]cret\s+(?:n[°o]|du))",
    re.IGNORECASE,
)


def extract_candidates(text, head=60000, tail=10000, context_window=100):
    """Extrait les références d'articles candidates (num normalisés).

    Tronque les textes très longs : motifs et dispositif sont concentrés
    au début et à la fin, le milieu est souvent du factuel répétitif.

    Désambiguïsation en 2 niveaux :
    1. Veto proximité courte (±70 chars APRÈS le num) : si "du code des douanes" /
       "de la loi n°" / "de la convention" → exclude immédiatement (FP attaché).
    2. Contexte fiscal :
       - Num simples (1-3 chiffres sans suffixe) : contexte fort (CGI/LPF nommé)
       - Num avec suffixe ou L./R. : contexte fiscal vague suffit
    """
    if not text:
        return set()
    if len(text) > head + tail:
        text = text[:head] + "\n" + text[-tail:]
    out = set()
    for m in ARTICLE_RE.finditer(text):
        norm = normalize_num(m.group(1), m.group(2))
        if not norm:
            continue
        # Veto proximité courte : autre code/loi attaché au num → FP, on skip
        veto_end = min(len(text), m.end() + 70)
        veto_ctx = text[m.end():veto_end]
        if COMPETING_SOURCE_NEAR_RE.search(veto_ctx):
            continue
        # Contexte fiscal en fenêtre ±context_window
        start = max(0, m.start() - context_window)
        end = min(len(text), m.end() + context_window)
        ctx = text[start:end]
        if SIMPLE_NUM_RE.match(norm):
            if STRONG_FISCAL_CONTEXT_RE.search(ctx):
                out.add(norm)
        else:
            if FISCAL_CONTEXT_RE.search(ctx):
                out.add(norm)
    return out


def extract_visa_judilibre(visa_jsonb):
    """Extrait les refs CGI/LPF depuis les visa structurés Judilibre."""
    if not visa_jsonb:
        return []
    if isinstance(visa_jsonb, str):
        try:
            visa_jsonb = json.loads(visa_jsonb)
        except json.JSONDecodeError:
            return []
    if not isinstance(visa_jsonb, list):
        return []
    refs = []
    for v in visa_jsonb:
        if not isinstance(v, dict):
            continue
        title = (v.get("title") or "").lower()
        if "général des impôts" not in title and "cgi" not in title \
                and "procédures fiscales" not in title and "lpf" not in title:
            continue
        # Extraire le numéro depuis le titre ou champ articles séparé
        articles = v.get("articles") or v.get("refs") or []
        if isinstance(articles, str):
            articles = [articles]
        for art in articles:
            refs.append(str(art).strip())
        # Aussi parser depuis le titre lui-même : "Code général des impôts 209 B"
        for m in ARTICLE_RE.finditer(v.get("title") or ""):
            n = normalize_num(m.group(1), m.group(2))
            if n:
                refs.append(n)
        # Cas "Code général des impôts 209" sans préfixe "article"
        m2 = re.search(
            r"(?:imp[oô]ts|CGI|fiscales|LPF)\b[^\d]{0,20}([LR]?\.?\s*\d{1,4}(?:\s+[A-Z\w]+)?)",
            v.get("title") or "",
            re.IGNORECASE,
        )
        if m2:
            refs.append(m2.group(1).strip())
    return refs


def lookup_articles(candidates, idx, decision_date=None):
    """Pour chaque candidate, retourne la liste de (article_id, cid)
    en privilégiant la version VIGUEUR ou celle valide à la date de décision.
    """
    matches = []
    for cand in candidates:
        cand_up = re.sub(r"\s+", " ", cand.strip()).upper()
        articles = idx.get(cand_up)
        if not articles:
            # Tenter sans espaces dans le préfixe : "L 169" → "L169"
            cand_alt = re.sub(r"^([LR])\s+", r"\1", cand_up)
            articles = idx.get(cand_alt)
        if not articles:
            continue
        chosen = pick_best_version(articles, decision_date)
        if chosen:
            matches.append(chosen)
    return matches


def pick_best_version(articles, decision_date):
    """Choisit la version d'article applicable à la date de décision,
    sinon la version VIGUEUR, sinon la dernière par date_debut.

    Cohérence date : si toutes les versions disponibles ont date_debut
    POSTÉRIEURE à la date_decision, c'est qu'à cette date l'article
    n'existait pas encore → on rejette le lien (impossible)."""
    if not articles:
        return None
    if decision_date:
        # Vérification cohérence : la décision ne peut pas citer un article créé après
        oldest_debut = min(
            (a[5].date() if hasattr(a[5], "date") else a[5]) for a in articles if a[5]
        ) if any(a[5] for a in articles) else None
        if oldest_debut and oldest_debut > decision_date:
            return None  # article n'existait pas encore
        # Normaliser en date pour comparaison homogène (debut/fin sont datetime, decision_date est date)
        ddate = decision_date if hasattr(decision_date, "date") and callable(decision_date.date) is False else decision_date
        for a in articles:
            aid, cid, code_id, num, etat, debut, fin = a
            if debut and fin:
                debut_d = debut.date() if hasattr(debut, "date") else debut
                fin_d = fin.date() if hasattr(fin, "date") else fin
                if debut_d <= decision_date <= fin_d:
                    return (aid, cid)
    for a in articles:
        if a[4] == "VIGUEUR":
            return (a[0], a[1])
    a = sorted(articles, key=lambda x: x[5] or datetime.min, reverse=True)[0]
    return (a[0], a[1])


def insert_lien(cursor, juri_id, article_id, article_cid, type_lien):
    cursor.execute(
        """
        INSERT INTO liens_jurisprudence_article
            (jurisprudence_id, article_id, article_cid, type_lien)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (juri_id, article_id, article_cid, type_lien),
    )
    return cursor.rowcount > 0


def process_decisions_unified(cursor, conn, idx, limit=None):
    print("\n📊 decisions_unified — sous-ensemble fiscal")
    # Filtre fiscal : on revient au filtre serré V3 (qui donnait le meilleur compromis recall/précision)
    # Les améliorations sur la précision se font via veto proximité + cohérence date
    # appliqués dans extract_candidates / pick_best_version
    q = """
        SELECT id, texte_nettoye, date_decision
        FROM decisions_unified
        WHERE texte_tsvector @@ to_tsquery('french', 'fiscal | impôt | impôts | TVA | CGI')
          AND texte_nettoye IS NOT NULL
    """
    if limit:
        q += f" LIMIT {limit}"
    cursor.execute(q)
    rows = cursor.fetchall()
    return _process_text_rows(cursor, conn, rows, idx, "unified", text_col=1, date_col=2)


def process_inca(cursor, conn, idx, limit=None):
    print("\n📊 inca (Cour de cassation) — sous-ensemble fiscal")
    q = """
        SELECT id, texte_clean, date_decision
        FROM inca
        WHERE (texte_clean ILIKE '%code général des impôts%' OR texte_clean ILIKE '%CGI%'
            OR texte_clean ILIKE '%procédures fiscales%' OR texte_clean ILIKE '%LPF%')
          AND texte_clean IS NOT NULL
    """
    if limit:
        q += f" LIMIT {limit}"
    cursor.execute(q)
    rows = cursor.fetchall()
    return _process_text_rows(cursor, conn, rows, idx, "inca", text_col=1, date_col=2)


def process_arianeweb(cursor, conn, idx, limit=None):
    print("\n📊 arianeweb_decisions (Conseil d'État) — sous-ensemble fiscal")
    q = """
        SELECT id, texte_clean, date_decision
        FROM arianeweb_decisions
        WHERE (texte_clean ILIKE '%code général des impôts%' OR texte_clean ILIKE '%CGI%'
            OR texte_clean ILIKE '%TVA%' OR texte_clean ILIKE '%impôt sur les sociétés%'
            OR texte_clean ILIKE '%impôt sur le revenu%')
          AND texte_clean IS NOT NULL
    """
    if limit:
        q += f" LIMIT {limit}"
    cursor.execute(q)
    rows = cursor.fetchall()
    return _process_text_rows(cursor, conn, rows, idx, "arianeweb", text_col=1, date_col=2)


def process_judilibre(cursor, conn, idx, limit=None):
    print("\n📊 judilibre_decisions — visa fiscal")
    q = """
        SELECT id, text_clean, decision_date, visa
        FROM judilibre_decisions
        WHERE visa::text ILIKE '%général des impôts%' OR visa::text ILIKE '%CGI%'
            OR visa::text ILIKE '%procédures fiscales%' OR visa::text ILIKE '%LPF%'
    """
    if limit:
        q += f" LIMIT {limit}"
    cursor.execute(q)
    rows = cursor.fetchall()
    print(f"   📊 {len(rows)} décisions à traiter")

    update_realtime_status(0, len(rows), phase="Linking judilibre fiscal")
    links = 0
    juris_with_links = 0
    for i, (jid, text_clean, ddate, visa) in enumerate(rows):
        if should_abort():
            break
        print_progress(i + 1, len(rows), "   ")
        update_realtime_status(i + 1, len(rows))

        cands = extract_candidates(text_clean) if text_clean else set()
        # Visa structuré (type_lien VISE)
        visa_refs = extract_visa_judilibre(visa)
        visa_cands = {re.sub(r"\s+", " ", r.strip()).upper() for r in visa_refs}

        n_links = 0
        for cand in cands - visa_cands:
            for aid, cid in lookup_articles({cand}, idx, ddate):
                if insert_lien(cursor, jid, aid, cid, "CITE"):
                    n_links += 1
        for cand in visa_cands:
            for aid, cid in lookup_articles({cand}, idx, ddate):
                if insert_lien(cursor, jid, aid, cid, "VISE"):
                    n_links += 1

        if n_links > 0:
            juris_with_links += 1
            links += n_links
            track_success()
        if (i + 1) % 500 == 0:
            conn.commit()
    conn.commit()
    print(f"\n   ✅ judilibre: {links} liens depuis {juris_with_links} décisions")
    return links, juris_with_links


def _process_text_rows(cursor, conn, rows, idx, source, text_col, date_col):
    print(f"   📊 {len(rows)} décisions à traiter")
    update_realtime_status(0, len(rows), phase=f"Linking {source} fiscal")
    links = 0
    juris_with_links = 0
    for i, row in enumerate(rows):
        if should_abort():
            break
        jid = row[0]
        text = row[text_col]
        ddate = row[date_col]
        print_progress(i + 1, len(rows), "   ")
        update_realtime_status(i + 1, len(rows))

        cands = extract_candidates(text)
        n_links = 0
        for cand in cands:
            for aid, cid in lookup_articles({cand}, idx, ddate):
                if insert_lien(cursor, jid, aid, cid, "CITE"):
                    n_links += 1
        if n_links > 0:
            juris_with_links += 1
            links += n_links
            track_success()
        if (i + 1) % 500 == 0:
            conn.commit()
    conn.commit()
    print(f"\n   ✅ {source}: {links} liens depuis {juris_with_links} décisions")
    return links, juris_with_links


def main():
    parser = argparse.ArgumentParser(description="Linking jurisprudence ↔ CGI sur sous-ensemble fiscal")
    parser.add_argument("--source", choices=["unified", "inca", "arianeweb", "judilibre", "all"],
                        default="all")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--clear-fiscal", action="store_true",
                        help="Vide les liens existants pointant vers le CGI/LPF")
    args = parser.parse_args()

    reset_error_tracker()

    print("=" * 60)
    print(" LINKING JURISPRUDENCE ↔ CGI (sous-ensemble fiscal)")
    print(f" Source: {args.source}")
    print("=" * 60)

    conn = get_db_connection()
    cursor = conn.cursor()

    if args.clear_fiscal:
        print("\n🗑️  Suppression des liens fiscaux existants...")
        cursor.execute(
            """
            DELETE FROM liens_jurisprudence_article
            WHERE article_id IN (SELECT id FROM articles WHERE code_id IN %s)
            """,
            (CODES_FISCAUX,),
        )
        conn.commit()
        print(f"   ✅ {cursor.rowcount} liens supprimés")

    print("\n🧠 Chargement de l'index articles fiscaux...")
    idx = build_article_index(cursor)
    print(f"   📚 {sum(len(v) for v in idx.values())} articles indexés sur {len(idx)} num distincts")

    total_links = 0
    total_juris = 0

    if args.source in ("unified", "all"):
        l, j = process_decisions_unified(cursor, conn, idx, args.limit)
        total_links += l; total_juris += j
    if args.source in ("inca", "all"):
        l, j = process_inca(cursor, conn, idx, args.limit)
        total_links += l; total_juris += j
    if args.source in ("arianeweb", "all"):
        l, j = process_arianeweb(cursor, conn, idx, args.limit)
        total_links += l; total_juris += j
    if args.source in ("judilibre", "all"):
        l, j = process_judilibre(cursor, conn, idx, args.limit)
        total_links += l; total_juris += j

    cursor.close()
    conn.close()

    print()
    print("=" * 60)
    print(" RÉSULTAT FINAL")
    print("=" * 60)
    print(f"   🔗 Liens créés          : {total_links}")
    print(f"   ⚖️  Décisions linkées   : {total_juris}")
    print("=" * 60)


if __name__ == "__main__":
    main()
