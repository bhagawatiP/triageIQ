#!/usr/bin/env python3
"""
build_embeddings.py - Pre-compute semantic embeddings for the test corpus.

One-time KB builder that embeds every test case (summary + description + steps)
into a 384-dimensional vector using the all-MiniLM-L6-v2 model via fastembed.

The resulting index is saved as a compressed NumPy archive (.npz) for fast
loading at runtime.  At analysis time, only the diff text needs to be embedded
(< 0.1s) and cosine-similarity is computed against the pre-built vectors.

This closes two remaining matching gaps:
  - Synonyms  (trade ↔ exchange) — different words, same meaning
  - Acronyms  (SSO ↔ self service override) — abbreviation ↔ full form
  - Typos     (requesyt ↔ request) — the model is typo-tolerant

Usage:
    python3 build_embeddings.py \\
        --corpus .github/RIA_INPUT/all_tcs_extracted.json \\
        --output .github/RIA_OUTPUT/knowledge_base/embeddings_index.npz
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Dict, List

import numpy as np

# ---------------------------------------------------------------------------
# Text extraction (mirrors term_idf._extract_test_text)
# ---------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE = re.compile(r'\s{2,}')


def _normalize_chars(text: str) -> str:
    t = text.lower()
    t = _PUNCT_RE.sub(' ', t)
    t = _MULTI_SPACE.sub(' ', t)
    return t.strip()


def extract_test_text(test: Dict) -> str:
    """Combine summary + description + all step actions/data/results."""
    parts = [
        test.get('summary') or '',
        test.get('description') or '',
    ]
    for step in test.get('steps', []):
        parts.append(step.get('action') or '')
        parts.append(step.get('data') or '')
        parts.append(step.get('result') or '')
    return _normalize_chars(' '.join(parts))


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

# Model: all-MiniLM-L6-v2 — 384 dims, ~25MB download, fast on CPU
_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


# ---------------------------------------------------------------------------
# UPGRADE 5: Brute-force-cosine migration warning (>100K tests)
# ---------------------------------------------------------------------------
# `semantic_similarity()` below computes cosine similarity by normalising
# the corpus matrix and doing a dense matrix-vector product. That is
# O(N * D) per query (N = test count, D = 384) and the entire (N, D)
# matrix lives in RAM. On a 10K-test corpus this completes in < 50 ms
# and uses ~15 MB; both grow LINEARLY with N.
#
# Empirically the brute-force path becomes the bottleneck around 100K
# tests (>500 ms / query, > 150 MB resident). Past that point the right
# fix is an Approximate Nearest Neighbour (ANN) index.
#
# MIGRATION GUIDE (>100K tests):
#   1. Build an ANN index alongside the dense `.npz`:
#        - FAISS    (Meta, IndexHNSWFlat or IndexIVFPQ)  — fastest, GPU
#                   capable; pip install faiss-cpu / faiss-gpu
#        - hnswlib  (pure-CPU, single-file dependency)   — easiest deploy
#        - scaNN    (Google) for even larger corpora
#   2. Suggested FAISS recipe (HNSW, near-exact recall on 384-d MiniLM):
#         import faiss
#         index = faiss.IndexHNSWFlat(384, M=32)
#         index.hnsw.efConstruction = 200
#         index.hnsw.efSearch       = 64
#         faiss.normalize_L2(embeddings_matrix)   # so inner-product = cos
#         index.add(embeddings_matrix)
#         faiss.write_index(index, output_path.replace('.npz', '.faiss'))
#   3. At query time replace the dense dot-product:
#         faiss.normalize_L2(query[None, :])
#         scores, indices = index.search(query[None, :], top_k)
#      and look up `test_keys[i]` for each returned index.
#   4. Keep the .npz around for offline batch operations / debugging —
#      FAISS index is for online top-k retrieval only.
#
# This module logs a WARNING when the corpus exceeds the threshold so
# operators see the migration prompt without surprise.
# ---------------------------------------------------------------------------

LARGE_EMBEDDING_CORPUS_WARN_THRESHOLD = 100_000


def _check_embedding_corpus_size(test_count: int) -> None:
    """Emit a single WARNING when the embedding corpus is large enough to
    warrant migrating from brute-force cosine to a FAISS / hnswlib ANN
    index.  Idempotent: safe to call from multiple paths."""
    if test_count > LARGE_EMBEDDING_CORPUS_WARN_THRESHOLD:
        print(
            f"[build_embeddings] WARNING: corpus has {test_count} tests "
            f"(> {LARGE_EMBEDDING_CORPUS_WARN_THRESHOLD}). Brute-force "
            f"cosine similarity in semantic_similarity() will dominate "
            f"query latency and memory. Consider migrating to a FAISS / "
            f"hnswlib ANN index — see the MIGRATION GUIDE comment block "
            f"above _check_embedding_corpus_size() in this file for a "
            f"step-by-step recipe."
        )


def build_embeddings_index(corpus_path: str,
                           output_path: str,
                           batch_size: int = 256) -> Dict:
    """
    Build pre-computed embedding vectors for every test in the corpus.

    Args:
        corpus_path: Path to all_tcs_extracted.json
        output_path: Path to save the .npz file
        batch_size:  Number of texts to embed at once

    Returns:
        Dict with metadata (test_count, model, dims)
    """
    from fastembed import TextEmbedding

    print(f"[build_embeddings] Loading corpus from {corpus_path}")
    with open(corpus_path, 'r', encoding='utf-8') as f:
        corpus = json.load(f)

    total = len(corpus)
    print(f"[build_embeddings] Corpus: {total} test cases")

    # UPGRADE 5: warn when corpus exceeds the brute-force-friendly threshold.
    _check_embedding_corpus_size(total)

    # Extract text and keys
    test_keys: List[str] = []
    test_texts: List[str] = []
    for test in corpus:
        key = test.get('issue_key') or test.get('key') or ''
        text = extract_test_text(test)
        test_keys.append(key)
        # fastembed needs non-empty strings
        test_texts.append(text if text.strip() else 'empty test')

    # Initialize model (downloads on first use, ~25MB)
    print(f"[build_embeddings] Initializing model: {_MODEL_NAME}")
    model = TextEmbedding(model_name=_MODEL_NAME)

    # Embed all tests
    print(f"[build_embeddings] Embedding {total} tests (batch_size={batch_size})...")
    start = time.time()
    embeddings_list = list(model.embed(test_texts, batch_size=batch_size))
    elapsed = time.time() - start
    print(f"[build_embeddings] Embedded {total} tests in {elapsed:.1f}s "
          f"({total / elapsed:.0f} tests/sec)")

    # Stack into numpy matrix: shape (N, 384)
    embeddings_matrix = np.array(embeddings_list, dtype=np.float32)
    print(f"[build_embeddings] Embeddings shape: {embeddings_matrix.shape}")

    # Save as compressed numpy archive
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    np.savez_compressed(
        output_path,
        embeddings=embeddings_matrix,
        test_keys=np.array(test_keys, dtype=object),
    )
    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"[build_embeddings] Saved: {output_path} ({file_size_mb:.1f} MB)")

    return {
        'test_count': total,
        'model': _MODEL_NAME,
        'dims': embeddings_matrix.shape[1],
        'file_size_mb': round(file_size_mb, 1),
    }


# ---------------------------------------------------------------------------
# Runtime API — used by stage4 / term_idf at analysis time
# ---------------------------------------------------------------------------

def load_embeddings_index(index_path: str) -> Dict:
    """
    Load pre-computed embeddings from the .npz file.

    Returns:
        Dict with:
            'embeddings': np.ndarray of shape (N, 384)
            'test_keys': list of test keys (str)
            'key_to_idx': dict mapping test_key -> row index
    """
    data = np.load(index_path, allow_pickle=True)
    embeddings = data['embeddings']
    test_keys = list(data['test_keys'])
    key_to_idx = {k: i for i, k in enumerate(test_keys)}
    # UPGRADE 5: warn at runtime too, in case the build was on a smaller
    # snapshot but the loaded artefact has since grown.
    _check_embedding_corpus_size(len(test_keys))
    return {
        'embeddings': embeddings,
        'test_keys': test_keys,
        'key_to_idx': key_to_idx,
    }


def embed_text(text: str, _model_cache=[]) -> np.ndarray:
    """
    Embed a single text string at runtime.

    Uses a module-level cache so the model is loaded only once per process.
    Returns a 1D numpy array of shape (384,).
    """
    from fastembed import TextEmbedding
    if not _model_cache:
        _model_cache.append(TextEmbedding(model_name=_MODEL_NAME))
    model = _model_cache[0]
    result = list(model.embed([text]))
    return np.array(result[0], dtype=np.float32)


def semantic_similarity(query_embedding: np.ndarray,
                        corpus_embeddings: np.ndarray) -> np.ndarray:
    """
    Compute cosine similarity between a query vector and all corpus vectors.

    Args:
        query_embedding: shape (384,)
        corpus_embeddings: shape (N, 384)

    Returns:
        1D array of cosine similarities, shape (N,)
    """
    # Normalize
    q_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
    c_norms = np.linalg.norm(corpus_embeddings, axis=1, keepdims=True) + 1e-10
    c_normalized = corpus_embeddings / c_norms
    # Dot product = cosine similarity (both normalized)
    return c_normalized @ q_norm


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Build semantic embedding index for test corpus.')
    parser.add_argument('--corpus', required=True,
                        help='Path to all_tcs_extracted.json')
    parser.add_argument('--output', required=True,
                        help='Path to save embeddings_index.npz')
    parser.add_argument('--batch-size', type=int, default=256,
                        help='Embedding batch size (default: 256)')
    args = parser.parse_args()

    result = build_embeddings_index(args.corpus, args.output, args.batch_size)
    print(f"\nDone: {result['test_count']} tests, {result['dims']}d vectors, "
          f"{result['file_size_mb']} MB")
