"""
Batch chrono CGI : récupère toutes les versions historiques des articles
du CGI (principal + annexes I-IV + LPF) via /consult/getArticleByCid
et les insère dans la table `articles`.

Chaque version a un `id` Légifrance distinct mais partage le même `cid`.
Le versioning est implicite via (cid, date_debut, date_fin).

Usage:
    python -m data_pipeline.scripts.chrono_cgi
    python -m data_pipeline.scripts.chrono_cgi --code LEGITEXT000006069577
    python -m data_pipeline.scripts.chrono_cgi --limit 50  # smoke test
    python -m data_pipeline.scripts.chrono_cgi --resume
"""
import sys
import os
import argparse
import hashlib
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv()

from data_pipeline.extractors.base import (
    call_api, get_db_connection, parse_date, clean_html, print_progress,
    update_realtime_status, update_realtime_counters,
    reset_error_tracker, track_error, track_success, should_abort,
)

ENDPOINT = "/consult/getArticleByCid"
EXTRACTOR_VERSION = "chrono-cgi-1.0.0"

# Périmètre fiscal : CGI + annexes I-IV + LPF (Code des douanes optionnel)
CODES_FISCAUX = {
    "LEGITEXT000006069577": "Code général des impôts",
    "LEGITEXT000006069568": "CGI annexe I",
    "LEGITEXT000006069569": "CGI annexe II",
    "LEGITEXT000006069574": "CGI annexe III",
    "LEGITEXT000006069576": "CGI annexe IV",
    "LEGITEXT000006069583": "Livre des procédures fiscales",
}


def epoch_ms_to_datetime(value):
    """Convertit un timestamp epoch ms en datetime ou retourne None."""
    if value is None:
        return None
    if isinstance(value, str):
        return parse_date(value)
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def get_cids_for_code(cursor, code_id):
    """Retourne les CID distincts d'un code, en commençant par ceux qui ont déjà 1 seule version."""
    cursor.execute("""
        SELECT cid, COUNT(*) AS nb_versions
        FROM articles
        WHERE code_id = %s AND cid IS NOT NULL
        GROUP BY cid
        ORDER BY nb_versions ASC, cid
    """, (code_id,))
    return cursor.fetchall()


def upsert_version(cursor, version, code_id, run_id):
    """Insère/MAJ une version d'article dans `articles`."""
    article_id = version.get("id")
    if not article_id:
        return False

    texte_html = version.get("texteHtml") or version.get("texte")
    texte_clean = clean_html(texte_html) if texte_html else None

    contenu_brut = json.dumps(version, ensure_ascii=False, default=str)
    content_hash = hashlib.sha256(contenu_brut.encode()).hexdigest()
    content_size = len(contenu_brut.encode())

    num = version.get("num")
    source_ref = f"Article {num}" if num else f"Article {article_id}"

    MAPPED = {"id", "cid", "num", "etat", "texte", "texteHtml", "nota",
              "dateDebut", "dateFin", "versionArticle", "nature", "type"}
    metadata = json.dumps(
        {k: v for k, v in version.items() if k not in MAPPED and v is not None},
        ensure_ascii=False, default=str
    )

    cursor.execute("""
        INSERT INTO articles (
            id, cid, num, etat,
            texte_html, texte_clean, nota,
            date_debut, date_fin,
            code_id, version, nature, type_article, url_source,
            source_ref, content_hash, content_size_bytes,
            extractor_version, run_id, contenu_brut, metadata,
            last_checked_at
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s,
            NOW()
        )
        ON CONFLICT (id) DO UPDATE SET
            cid = COALESCE(EXCLUDED.cid, articles.cid),
            num = COALESCE(EXCLUDED.num, articles.num),
            etat = COALESCE(EXCLUDED.etat, articles.etat),
            texte_html = COALESCE(EXCLUDED.texte_html, articles.texte_html),
            texte_clean = COALESCE(EXCLUDED.texte_clean, articles.texte_clean),
            nota = COALESCE(EXCLUDED.nota, articles.nota),
            date_debut = COALESCE(EXCLUDED.date_debut, articles.date_debut),
            date_fin = COALESCE(EXCLUDED.date_fin, articles.date_fin),
            code_id = COALESCE(articles.code_id, EXCLUDED.code_id),
            version = COALESCE(EXCLUDED.version, articles.version),
            nature = COALESCE(EXCLUDED.nature, articles.nature),
            type_article = COALESCE(EXCLUDED.type_article, articles.type_article),
            url_source = COALESCE(EXCLUDED.url_source, articles.url_source),
            content_hash = EXCLUDED.content_hash,
            content_size_bytes = EXCLUDED.content_size_bytes,
            extractor_version = EXCLUDED.extractor_version,
            run_id = EXCLUDED.run_id,
            contenu_brut = EXCLUDED.contenu_brut,
            metadata = COALESCE(EXCLUDED.metadata, articles.metadata),
            last_checked_at = NOW(),
            updated_at = NOW()
    """, (
        article_id,
        version.get("cid"),
        num,
        version.get("etat"),
        texte_html,
        texte_clean,
        version.get("nota"),
        epoch_ms_to_datetime(version.get("dateDebut")),
        epoch_ms_to_datetime(version.get("dateFin")),
        code_id,
        version.get("versionArticle"),
        version.get("nature"),
        version.get("type"),
        f"https://www.legifrance.gouv.fr/codes/article_lc/{article_id}",
        source_ref,
        content_hash,
        content_size,
        EXTRACTOR_VERSION,
        run_id,
        contenu_brut,
        metadata,
    ))
    return True


def process_cid(cursor, cid, code_id, run_id):
    """Récupère toutes les versions d'un article via son CID et les upsert."""
    data = call_api(ENDPOINT, {"cid": cid})
    update_realtime_counters(requests_delta=1)
    if not data:
        return 0, 0, "no_data"

    versions = data.get("listArticle") or []
    inserted = 0
    errors = 0
    for v in versions:
        try:
            if upsert_version(cursor, v, code_id, run_id):
                inserted += 1
        except Exception as e:
            errors += 1
            print(f"\n  ⚠️ Erreur insertion version {v.get('id')}: {e}")
    return inserted, errors, None


def main():
    parser = argparse.ArgumentParser(description="Batch chrono CGI : versions historiques")
    parser.add_argument("--code", help="Limiter à un code (ex: LEGITEXT000006069577)")
    parser.add_argument("--limit", type=int, help="Limiter le nombre de CID (smoke test)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip les CID qui ont déjà >1 version en BDD")
    args = parser.parse_args()

    reset_error_tracker()

    print("=" * 60)
    print(" BATCH CHRONO CGI : versions historiques")
    print(f" Endpoint: {ENDPOINT}")
    print("=" * 60)

    codes = {args.code: CODES_FISCAUX.get(args.code, "Code")} if args.code else CODES_FISCAUX

    conn = get_db_connection()
    cursor = conn.cursor()

    run_id = f"chrono-cgi-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    grand_total_inserted = 0
    grand_total_versions = 0
    grand_total_errors = 0
    cids_done = 0

    for code_id, code_titre in codes.items():
        print(f"\n📚 {code_titre} [{code_id}]")
        cids = get_cids_for_code(cursor, code_id)

        if args.resume:
            cids = [(cid, n) for cid, n in cids if n <= 1]
            print(f"   ↩️ Resume: skip CID avec >1 version → {len(cids)} CID restants")

        if args.limit:
            cids = cids[:args.limit]

        total = len(cids)
        print(f"   📊 {total} CID à traiter")
        if total == 0:
            continue

        update_realtime_status(0, total, phase=f"Chrono {code_titre}")

        code_inserted = 0
        code_versions = 0
        code_errors = 0

        for i, (cid, nb_existing) in enumerate(cids):
            if should_abort():
                print("\n  🛑 ABORT")
                break

            print_progress(i + 1, total, "   ")
            update_realtime_status(i + 1, total, phase=f"Chrono {code_titre}")

            inserted, errors, err = process_cid(cursor, cid, code_id, run_id)
            code_versions += inserted
            code_errors += errors
            cids_done += 1
            if inserted > 0:
                code_inserted += 1
                track_success()
            else:
                track_error(err or "no_versions")

            update_realtime_counters(inserted_delta=inserted, errors_delta=errors)

            if (i + 1) % 25 == 0:
                conn.commit()

        conn.commit()
        print()
        print(f"   ✅ {code_inserted}/{total} CID enrichis | {code_versions} versions | {code_errors} erreurs")
        grand_total_inserted += code_inserted
        grand_total_versions += code_versions
        grand_total_errors += code_errors

    cursor.close()
    conn.close()

    print()
    print("=" * 60)
    print(" RÉSULTAT FINAL")
    print("=" * 60)
    print(f"   📚 CID traités          : {cids_done}")
    print(f"   ✅ CID enrichis         : {grand_total_inserted}")
    print(f"   📜 Versions upsertées   : {grand_total_versions}")
    print(f"   ❌ Erreurs              : {grand_total_errors}")
    print("=" * 60)


if __name__ == "__main__":
    main()
