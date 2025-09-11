# attention_integration.py
from __future__ import annotations
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
import asyncio
import concurrent.futures
import os
import time

import numpy as np

# Import attentionDB pieces
from attentiondb.keybank import build_keybank as adb_build_keybank
from attentiondb.score import load_keybank, embed_queries, score_records
from attentiondb.pack import load_records, render_pack_md
from attentiondb.pack import select_records_pure  # pure score-driven selector

class AttentionDBRuntime:
    """
    Per-repo attentionDB facade.
    - Build key banks for a repo
    - Load/cached key banks
    - Produce Navigation Packs per query
    """

    # Fixed packer knobs (your chosen defaults)
    BUDGET_TOKENS = 1000
    CANDIDATE_TOPK = 10
    TOP_P = 0.85
    TEMP = 0.05
    MIN_ITEMS = 5
    RATIO_MIN = 0.35

    def __init__(self, data_root: Path = Path("data")):
        self.data_root = Path(data_root)
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        # cache: repo_id -> dict(K, metas, dim, jsonl, kb_dir, mtimes)
        self._cache: Dict[str, Dict[str, Any]] = {}

    # ---------- path helpers ----------
    def _paths(self, repo_id: str) -> tuple[Path, Path]:
        repo_dir = Path(repo_id)

        #jsonl dir needs to look like<repo_id>/mental_model/mental_model.jsonl
        jsonl = repo_dir / "mental_model" / "mental_model.jsonl"
        kb_dir = repo_dir / "kb"
        return jsonl, kb_dir

    # ---------- building ----------
    def _build_kb_blocking(
        self,
        repo_id: str,
        *,
        facets: tuple[str, ...] = ("file_path","summary","cross_file_deps"),
        max_keys_per_record: int = 999_999,  # unlimited (we chunk summaries fully)
        model: str = "voyage-3-large",
        batch_size: int = 128,
        dtype: str = "float16",
        summary_chunk_chars: int = 400,
        summary_overlap_chars: int = 60,
    ) -> Dict[str, Any]:
        jsonl, kb_dir = self._paths(repo_id)
        kb_dir.mkdir(parents=True, exist_ok=True)
        if not jsonl.exists():
            raise FileNotFoundError(f"Mental model JSONL not found for repo {repo_id}: {jsonl}")

        # Ensure VOYAGE_API_KEY is set
        if not os.getenv("VOYAGE_API_KEY"):
            raise RuntimeError("VOYAGE_API_KEY is not set in the environment.")

        K_path, meta_path, n_records, n_keys, dim = adb_build_keybank(
            jsonl=jsonl,
            out_dir=kb_dir,
            facets=facets,
            max_keys_per_record=max_keys_per_record,
            model=model,
            batch_size=batch_size,
            dtype=dtype,
            summary_chunk_chars=summary_chunk_chars,
            summary_overlap_chars=summary_overlap_chars,
        )
        # Invalidate cache for this repo so next pack() reloads fresh
        self._cache.pop(repo_id, None)
        return {
            "repo_id": repo_id,
            "jsonl": str(jsonl),
            "kb_dir": str(kb_dir),
            "K_path": str(K_path),
            "keys_meta": str(meta_path),
            "records": n_records,
            "keys": n_keys,
            "dim": dim,
        }

    async def build_keybank(self, repo_id: str, **kwargs) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._build_kb_blocking, repo_id, **kwargs)

    # ---------- loading / caching ----------
    def _load_repo_artifacts(self, repo_id: str) -> Dict[str, Any]:
        """
        Ensure K, metas, dim, jsonl are loaded & cached for this repo.
        Reloads if files changed.
        """
        jsonl, kb_dir = self._paths(repo_id)
        if not jsonl.exists():
            raise FileNotFoundError(f"Mental model JSONL not found: {jsonl}")
        if not (kb_dir / "K.npy").exists() or not (kb_dir / "keys_meta.jsonl").exists():
            raise FileNotFoundError(f"Key bank not found for {repo_id} in {kb_dir}; build it first.")

        # mtimes to detect changes
        m_k = (kb_dir / "K.npy").stat().st_mtime
        m_meta = (kb_dir / "keys_meta.jsonl").stat().st_mtime
        m_jsonl = jsonl.stat().st_mtime
        stamp = (m_k, m_meta, m_jsonl)

        cached = self._cache.get(repo_id)
        if cached and cached.get("stamp") == stamp:
            return cached

        # (Re)load K + metas
        K, metas, dim = load_keybank(kb_dir)
        recs = load_records(jsonl)
        payload = {
            "repo_id": repo_id,
            "jsonl": jsonl,
            "kb_dir": kb_dir,
            "K": K, "metas": metas, "dim": dim,
            "records": recs,
            "stamp": stamp,
            "loaded_at": time.time(),
        }
        self._cache[repo_id] = payload
        return payload

    # ---------- packing ----------
    def _pack_blocking(self, repo_id: str, question: str) -> Tuple[str, List[Dict[str, Any]]]:
        # Load (cached) artifacts
        art = self._load_repo_artifacts(repo_id)
        K, metas, records = art["K"], art["metas"], art["records"]

        # Embed queries (Voyage)
        Q = embed_queries(question)

        # Score globally (already aggregates per record)
        hits = score_records(Q, K, metas, topk=max(self.CANDIDATE_TOPK, 400))

        # Pure, adaptive selection
        chosen, _used = select_records_pure(
            hits=hits,
            records=records,
            budget_tokens=self.BUDGET_TOKENS,
            candidate_topk=self.CANDIDATE_TOPK,
            top_p=self.TOP_P,
            temp=self.TEMP,
            min_items=self.MIN_ITEMS,
            ratio_min=self.RATIO_MIN,
            dedupe_by_path=True,
        )

        md = render_pack_md(question, chosen, self.BUDGET_TOKENS)
        return md, chosen

    async def pack(self, repo_id: str, question: str) -> Tuple[str, List[Dict[str, Any]]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._pack_blocking, repo_id, question)
