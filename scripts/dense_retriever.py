#!/usr/bin/env python3
"""Realistic dense article retrieval for the killer experiment (Cond C).

The oracle in run_conditions.py uses gold.article_cid; that isolates version
selection but isn't a real RAG. This module replaces *article finding* with an
honest two-stage retriever over the CGI/LPF corpus:

  1. bi-encoder   : bge-m3-fiscal-v1 (fine-tuned), cosine top-K
  2. cross-encoder: bge-reranker-fiscal-v1 (fine-tuned), rerank -> rank-1

It returns the article *cid* (constant id), never a version - the version layer
(select_version_at / select_version_current) stays the single contribution and
is left untouched.

CHUNK-LEVEL INDEX. CGI articles are long (art. 261 is 16k chars) and the
discriminative clause often sits 5-6k chars deep, far past any single-vector
truncation. Embedding whole articles at long context also thrashes Apple MPS.
So we split each article into ~512-token chunks, embed those, and map every
chunk back to its cid - the standard way to index long legal text.
Retrieval scores chunks, deduplicates to the best chunk per article, then
reranks the surviving (query, chunk) pairs. Granularity stays at the article
level: the answer is always a cid.

The representative text per cid is its LONGEST version (articles shrink across
versions - art. 157 bis' current text is a 933-char stub that dropped the
content it once defined); the version layer downstream still picks the
date-correct text.

Exact cosine over the in-memory chunk matrix (numpy) is used instead of an
approximate index: the corpus is small (~8k chunks) and exact search avoids the
recall loss of approximate filtered-ANN, giving a faithful read on retriever
quality. The chunk index is built once here and cached on disk.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import psycopg2
import psycopg2.extras

REPO = Path(__file__).resolve().parent.parent
MODELS = Path(os.environ.get("BENCH_MODELS_DIR", REPO.parent / "models"))
EMBED_MODEL = MODELS / "bge-m3-fiscal-v1"
RERANK_MODEL = MODELS / "bge-reranker-fiscal-v1"
# The fine-tuned checkpoints are not distributed with this repository (see
# docs/ENCODER_TRAINING_DATA.md). If they are absent, fall back to the public
# base checkpoints so the released pipeline runs end-to-end - same code paths,
# weaker article-finding stage than the paper's C-prod numbers.
if not EMBED_MODEL.exists():
    EMBED_MODEL = "BAAI/bge-m3"
if not RERANK_MODEL.exists():
    RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


def _model_name(m):
    return m.name if isinstance(m, Path) else m

INDEX_DIR = REPO / "data" / "index"
INDEX_NPZ = INDEX_DIR / "fiscal_v1_index.npz"
INDEX_META = INDEX_DIR / "fiscal_v1_index_meta.json"

CGI = "LEGITEXT000006069577"
LPF = "LEGITEXT000006069583"
CODES = (CGI, LPF)

# Chunking + model knobs. 1500 chars ~= 420 French tokens, comfortably under the
# 512-token window, so each chunk is seen whole by both stages. Overlap keeps a
# clause from being split across a boundary.
CHUNK_CHARS = 1500
CHUNK_OVERLAP = 250
EMBED_SEQ_LEN = 512
RERANK_SEQ_LEN = 512
ENCODE_BATCH = 32
RERANK_BATCH = 32
DEFAULT_TOP_K = 50               # unique articles handed to the reranker
CHUNK_POOL = 600                 # top chunks scanned before dedup-to-cid


def _device() -> str:
    """BENCH_DEVICE override, else MPS on Apple Silicon, else CPU."""
    forced = os.environ.get("BENCH_DEVICE")
    if forced:
        return forced
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:                                    # noqa: BLE001
        pass
    return "cpu"


def _mps_oom(e: Exception) -> bool:
    return "out of memory" in str(e).lower()


def _db():
    """Same local-Postgres handle as run_conditions.db() (localhost, no SSL)."""
    return psycopg2.connect(host="localhost", port=5432,
                            dbname=os.environ.get("PGDATABASE", "legifrance_db"),
                            sslmode="disable", connect_timeout=5)


MULTI_VERSION_PER_CID = int(os.environ.get("MULTI_VERSION_PER_CID", "3"))


def _fetch_corpus():
    """Representative versions per cid for CGI+LPF.

    Default (MULTI_VERSION_PER_CID=3): up to 3 versions per article, spaced
    across the article's temporal history - first, median, last (by date_debut).
    Rationale: articles with strong drift (art. 1466 A, 302 bis ZI, 1519 A)
    saw their gold questions target seuils that appear in specific versions;
    a single "LONGEST" embedding per cid missed those.

    Fallback (MULTI_VERSION_PER_CID=1): the paper's original behavior - the
    single LONGEST version per cid.
    """
    conn = _db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if MULTI_VERSION_PER_CID <= 1:
        # Paper's original: one LONGEST version per cid.
        cur.execute(
            """SELECT DISTINCT ON (cid)
                      cid, id AS version_id, num, code_id, texte_clean
               FROM articles
               WHERE code_id IN %s
                 AND texte_clean IS NOT NULL AND length(texte_clean) > 30
               ORDER BY cid, length(texte_clean) DESC""",
            (CODES,))
        rows = cur.fetchall()
    else:
        # Multi-version: rank each version within its cid by date_debut, then
        # pick MULTI_VERSION_PER_CID evenly spaced ones (buckets across time).
        # Skip stillborn MODIFIE_MORT_NE artefacts (date-inverted).
        cur.execute(
            """WITH ranked AS (
                 SELECT cid, id AS version_id, num, code_id, texte_clean,
                        date_debut,
                        ROW_NUMBER() OVER (PARTITION BY cid ORDER BY date_debut) AS rn,
                        COUNT(*) OVER (PARTITION BY cid) AS n_versions
                 FROM articles
                 WHERE code_id IN %s
                   AND texte_clean IS NOT NULL AND length(texte_clean) > 30
                   AND upper(etat) != 'MODIFIE_MORT_NE'
               )
               SELECT cid, version_id, num, code_id, texte_clean
               FROM ranked
               WHERE n_versions <= %s
                  OR rn = 1
                  OR rn = n_versions
                  OR rn = (n_versions / 2)::int + 1
               ORDER BY cid, rn""",
            (CODES, MULTI_VERSION_PER_CID))
        rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def _chunk(text: str):
    """Char-window chunks with overlap. Adequate for landing a buried clause in
    its own ~512-token passage; no token-level splitting needed at this size."""
    text = text.strip()
    if len(text) <= CHUNK_CHARS:
        return [text]
    step = CHUNK_CHARS - CHUNK_OVERLAP
    return [text[i:i + CHUNK_CHARS] for i in range(0, len(text), step)
            if text[i:i + CHUNK_CHARS].strip()]


def _encode(model, texts, batch, verbose):
    """Encode with an MPS-OOM -> CPU fallback (one-shot)."""
    try:
        return model.encode(texts, batch_size=batch, normalize_embeddings=True,
                            convert_to_numpy=True, show_progress_bar=verbose
                            ).astype("float32")
    except RuntimeError as e:
        if not _mps_oom(e):
            raise
        from sentence_transformers import SentenceTransformer
        print("[index] MPS OOM -> retrying on CPU")
        m = SentenceTransformer(str(EMBED_MODEL), device="cpu")
        m.max_seq_length = EMBED_SEQ_LEN
        return m.encode(texts, batch_size=batch, normalize_embeddings=True,
                        convert_to_numpy=True, show_progress_bar=verbose
                        ).astype("float32")


def build_index(force: bool = False, verbose: bool = True):
    """Chunk the article corpus, embed with bge-m3-fiscal-v1, cache to disk."""
    if INDEX_NPZ.exists() and INDEX_META.exists() and not force:
        # Invalidate the cache if it was built with a different
        # MULTI_VERSION_PER_CID setting - otherwise a user toggling the env
        # var silently reuses the wrong index. Missing key = pre-multi-version
        # cache, force rebuild.
        cached = json.loads(INDEX_META.read_text()).get("multi_version_per_cid")
        if cached == MULTI_VERSION_PER_CID:
            return
        if verbose:
            print(f"[index] cache built with multi_version_per_cid={cached}, "
                  f"current={MULTI_VERSION_PER_CID} -> rebuild")
    from sentence_transformers import SentenceTransformer

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    rows = _fetch_corpus()

    chunks, c_cid, c_num, c_code = [], [], [], []
    for r in rows:
        for ch in _chunk(r["texte_clean"] or ""):
            chunks.append(ch)
            c_cid.append(r["cid"])
            c_num.append(r["num"] or "")
            c_code.append(r["code_id"])
    if verbose:
        print(f"[index] {len(rows)} articles -> {len(chunks)} chunks (CGI+LPF)")

    dev = _device()
    if verbose:
        print(f"[index] loading {_model_name(EMBED_MODEL)} on {dev} ...")
    model = SentenceTransformer(str(EMBED_MODEL), device=dev)
    model.max_seq_length = EMBED_SEQ_LEN

    t0 = time.time()
    emb = _encode(model, chunks, ENCODE_BATCH, verbose)
    if verbose:
        print(f"[index] encoded {len(chunks)} chunks in {time.time()-t0:.1f}s "
              f"-> {emb.shape}")

    np.savez_compressed(INDEX_NPZ, emb=emb,
                        cid=np.array(c_cid), num=np.array(c_num),
                        code_id=np.array(c_code))
    INDEX_META.write_text(json.dumps(
        {"model": _model_name(EMBED_MODEL), "codes": list(CODES), "n_articles": len(rows),
         "n_chunks": len(chunks), "chunk_chars": CHUNK_CHARS,
         "embed_seq_len": EMBED_SEQ_LEN,
         "multi_version_per_cid": MULTI_VERSION_PER_CID,
         "texts": chunks}, ensure_ascii=False))
    if verbose:
        print(f"[index] saved -> {INDEX_NPZ.name} + {INDEX_META.name}")


RRF_K = 60                       # reciprocal-rank-fusion constant (standard)


def _tok(s: str):
    """Accent-folded, lowercased word tokens (FR). Drops 1-char tokens."""
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return [t for t in re.split(r"[^a-z0-9]+", s) if len(t) > 1]


class _BM25:
    """Okapi BM25 over the chunk texts. Built once at load; scoring a query is a
    sparse postings walk. Recovers the lexical hits the dense vector misses on
    niche statutory queries (e.g. 'revenu fiscal de référence' -> art. 1417)."""

    def __init__(self, texts, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [_tok(t) for t in texts]
        self.N = len(self.docs)
        self.dl = np.array([len(d) for d in self.docs], dtype="float32")
        self.avgdl = float(self.dl.mean()) if self.N else 0.0
        df = Counter()
        self.postings = defaultdict(list)            # word -> [(doc_idx, tf), ...]
        for i, d in enumerate(self.docs):
            for w, tf in Counter(d).items():
                df[w] += 1
                self.postings[w].append((i, tf))
        self.idf = {w: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
                    for w, n in df.items()}

    def scores(self, query: str) -> np.ndarray:
        sc = np.zeros(self.N, dtype="float32")
        for w in set(_tok(query)):
            post = self.postings.get(w)
            if not post:
                continue
            wi = self.idf[w]
            for i, tf in post:
                denom = tf + self.k1 * (1 - self.b + self.b * self.dl[i] / self.avgdl)
                sc[i] += wi * (tf * (self.k1 + 1)) / denom
        return sc


def _rrf(rank_lists, k: int = RRF_K):
    """Reciprocal rank fusion of several ordered cid lists -> fused cid order."""
    score = defaultdict(float)
    for rl in rank_lists:
        for r, c in enumerate(rl):
            score[c] += 1.0 / (k + r + 1)
    return [c for c, _ in sorted(score.items(), key=lambda x: -x[1])]


class DenseRetriever:
    """Lazy two-stage chunk retriever. Loads the cache (building it if absent)
    and the fine-tuned models on first `retrieve_cid` call."""

    def __init__(self, top_k: int = DEFAULT_TOP_K, verbose: bool = True,
                 no_rerank: bool = False, hybrid: bool = False):
        self.top_k = top_k
        self.verbose = verbose
        self.no_rerank = no_rerank          # bi-encoder top-1 only (skip cross-encoder)
        self.hybrid = hybrid                # fuse dense + BM25 (RRF), no cross-encoder
        self._ready = False

    def _ensure(self):
        if self._ready:
            return
        build_index(force=False, verbose=self.verbose)
        from sentence_transformers import SentenceTransformer, CrossEncoder

        data = np.load(INDEX_NPZ, allow_pickle=True)
        self.emb = data["emb"].astype("float32")
        self.cid = data["cid"]
        self.num = data["num"]
        self.texts = json.loads(INDEX_META.read_text())["texts"]

        # cid -> chunk indices (for full-ranking dedup + best-chunk lookup)
        self._cid_idxs = defaultdict(list)
        for i, c in enumerate(self.cid):
            self._cid_idxs[str(c)].append(i)

        dev = _device()
        use_rerank = not (self.no_rerank or self.hybrid)
        if self.verbose:
            stage = ("dense+BM25 RRF" if self.hybrid
                     else "bi-encoder only" if self.no_rerank
                     else "bi-encoder + reranker")
            print(f"[dense] loading models on {dev} ({stage}; "
                  f"{len(self.emb)} chunks / {len(set(self.cid))} articles)")
        self.embedder = SentenceTransformer(str(EMBED_MODEL), device=dev)
        self.embedder.max_seq_length = EMBED_SEQ_LEN
        self.reranker = CrossEncoder(str(RERANK_MODEL), device=dev,
                                     max_length=RERANK_SEQ_LEN) if use_rerank else None
        self.bm25 = _BM25(self.texts) if self.hybrid else None
        self._ready = True

    def _rerank(self, pairs):
        try:
            return self.reranker.predict(pairs, batch_size=RERANK_BATCH,
                                         show_progress_bar=False)
        except RuntimeError as e:                        # MPS OOM
            if not _mps_oom(e):
                raise
            from sentence_transformers import CrossEncoder
            print("[dense] reranker MPS OOM -> moving reranker to CPU")
            self.reranker = CrossEncoder(str(RERANK_MODEL), device="cpu",
                                         max_length=RERANK_SEQ_LEN)
            return self.reranker.predict(pairs, batch_size=RERANK_BATCH,
                                         show_progress_bar=False)

    def _dense_ranking(self, query):
        """Full bi-encoder cid ranking: dedup all chunks to best chunk per cid.
        Returns (cids_ordered, best_idx_by_cid, cos_by_cid)."""
        q = self.embedder.encode([query], normalize_embeddings=True,
                                 convert_to_numpy=True)[0].astype("float32")
        sims = self.emb @ q
        order = np.argsort(-sims)
        seen, cids, best_idx, cos = set(), [], {}, {}
        for i in order:
            c = str(self.cid[i])
            if c in seen:
                continue
            seen.add(c)
            cids.append(c); best_idx[c] = int(i); cos[c] = float(sims[i])
        return cids, best_idx, cos

    def _bm25_ranking(self, query):
        """Full BM25 cid ranking (best chunk per cid)."""
        sc = self.bm25.scores(query)
        order = np.argsort(-sc)
        seen, cids = set(), []
        for i in order:
            c = str(self.cid[i])
            if c in seen:
                continue
            seen.add(c)
            cids.append(c)
            if sc[i] <= 0 and len(cids) > 0:     # remaining docs share no term
                # keep order stable but stop the meaningful tail
                pass
        return cids

    def _candidates(self, query, k):
        """Top-k dense candidate cids (back-compat shim).
        Returns parallel lists (cid, best_chunk_index, cosine)."""
        cids, best_idx, cos = self._dense_ranking(query)
        cids = cids[:k]
        return cids, [best_idx[c] for c in cids], [cos[c] for c in cids]

    def _ranked_cids(self, query):
        """Mode-aware full cid ranking + a debug dict for the top-1.

        - hybrid   : RRF(dense, BM25), no cross-encoder
        - norerank : dense bi-encoder order
        - rerank   : dense top-`top_k` re-scored by the cross-encoder, then the
                     remaining dense tail appended (so top-K beyond the reranked
                     window is still defined for recall@K / top-5 context)."""
        dense, best_idx, cos = self._dense_ranking(query)

        if self.hybrid:
            order = _rrf([dense, self._bm25_ranking(query)])
            top = order[0]
            dbg = {"cid": top, "num": str(self.num[best_idx[top]]),
                   "bi_top1_cid": dense[0], "bi_top1_cos": round(cos[dense[0]], 3),
                   "channel": "hybrid_rrf"}
            return order, dbg

        if self.no_rerank:
            top = dense[0]
            dbg = {"cid": top, "num": str(self.num[best_idx[top]]),
                   "bi_top1_cid": dense[0], "bi_top1_cos": round(cos[dense[0]], 3),
                   "rerank_score": None}
            return dense, dbg

        # rerank: re-score the dense top-`top_k` window, append the rest in order
        head = dense[:self.top_k]
        pairs = [[query, self.texts[best_idx[c]]] for c in head]
        scores = self._rerank(pairs)
        reranked = [c for _, c in sorted(zip(scores, head), key=lambda x: -x[0])]
        order = reranked + [c for c in dense[self.top_k:]]
        top = order[0]
        dbg = {"cid": top, "num": str(self.num[best_idx[top]]),
               "bi_top1_cid": dense[0], "bi_top1_cos": round(cos[dense[0]], 3),
               "rerank_score": float(max(scores))}
        return order, dbg

    def retrieve_cids(self, query: str, k: int = 5):
        """Top-`k` article cids (ordered), mode-aware. For multi-passage Cond C."""
        self._ensure()
        return self._ranked_cids(query)[0][:k]

    def retrieve_cid(self, query: str, top_k: int | None = None,
                     return_debug: bool = False):
        """Return the rank-1 article cid for `query` (or (cid, debug) tuple)."""
        self._ensure()
        order, dbg = self._ranked_cids(query)
        cid = order[0]
        return (cid, dbg) if return_debug else cid


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Build/inspect the fiscal-v1 index")
    ap.add_argument("--build", action="store_true", help="(re)build the index")
    ap.add_argument("--force", action="store_true", help="rebuild even if cached")
    ap.add_argument("--query", help="ad-hoc retrieval test")
    args = ap.parse_args()
    if args.build or args.force:
        build_index(force=args.force, verbose=True)
    if args.query:
        r = DenseRetriever()
        cid, dbg = r.retrieve_cid(args.query, return_debug=True)
        print(json.dumps(dbg, ensure_ascii=False, indent=2))
