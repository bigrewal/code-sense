import asyncio, logging, os, hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set
from urllib.parse import urlparse, unquote

from .lsp_client import LSPClient
from .cache import Cache

try:
    from tqdm import tqdm
except Exception:
    tqdm = None

logger = logging.getLogger(__name__)

@dataclass
class Location:
    file_path: str
    line: int
    column: int
    def to_dict(self):
        return {
            "file_path": self.file_path,
            "line": self.line,
            "column": self.column,
            "location_string": f"{self.file_path}:{self.line}:{self.column}",
        }

class BaseLSPAnalyzer:
    """
    Streaming analyzer with caching and cross-file invalidation:

      - Computes SHA-1 per file.
      - If content changed: re-resolve that file's references.
      - Also invalidates mappings for any references whose definitions
        point into changed files, and re-resolves those reference files.
      - Unchanged & unaffected files are served from SQLite cache.
      - Bounded asyncio.Queue provides backpressure.
      - Workers resolve definitions concurrently.
      - For languages like Python, didOpen/didClose per file.
    """

    def __init__(self, repo_path: Path, base_repo_path: str, show_progress: bool = True):
        self.repo_path = repo_path
        self.base_repo_path = base_repo_path
        self.client: Optional[LSPClient] = None
        self.show_progress = show_progress and (tqdm is not None)
        self.cache: Optional[Cache] = None
        # files whose mappings were recomputed in this run (ref files)
        self.changed_files: Set[str] = set()

    # --- required by subclasses ---
    def get_server_command(self) -> List[str]: raise NotImplementedError
    def get_file_extensions(self) -> List[str]: raise NotImplementedError
    def get_language_id(self) -> str: raise NotImplementedError
    def ref_pos_extractor(self, text: str, path: Path) -> List[Tuple[int, int]]: raise NotImplementedError

    # --- overridable knobs ---
    def needs_did_open(self) -> bool:
        return self.get_language_id().lower() == "python"

    def get_warmup_seconds(self) -> float:
        lang = self.get_language_id().lower()
        if lang == "scala":
            return 4.0
        if lang == "python":
            return 8.0
        if lang == "rust":
            return 8.0
        if lang == "java":
            return 12.0
        return 1.0

    def get_max_concurrency(self) -> int:
        cpu = (os.cpu_count() or 8)
        lang = self.get_language_id().lower()
        if lang == "python":
            return 4
        if lang in ("rust", "java"):
            return min(16, max(6, cpu))
        return min(32, max(8, cpu))

    def get_initialize_options(self) -> Dict:
        return {}

    def get_cache_namespace(self) -> str:
        """
        Namespace for the SQLite cache so different analyzers/servers/options
        don’t collide.
        """
        cmd = " ".join(self.get_server_command())
        return f"{self.__class__.__name__}:{cmd}"

    def is_excluded_definition_path(self, path: Path) -> bool:
        """
        Hook for language-specific exclusion of definition paths.

        `path` is repo-relative (no base_repo_prefix).
        Default: keep everything.
        """
        return False

    # --- lifecycle ---
    async def start_server(self):
        self.client = LSPClient(self.get_server_command())
        await self.client.start()
        await self.client.initialize(
            self.repo_path.as_uri(),
            self.repo_path.name,
            initialize_options=self.get_initialize_options(),
        )

    async def shutdown(self):
        if self.client:
            await self.client.shutdown()

    # --- file discovery ---
    def get_files(self) -> List[Path]:
        skip = {
            "node_modules", "__pycache__", "venv", ".venv", "build", "dist",
            "target", ".tox", ".eggs", "site-packages", "tests", "test",
        }

        files: List[Path] = []
        for ext in self.get_file_extensions():
            for f in self.repo_path.rglob(f"*{ext}"):
                rel_parts = f.relative_to(self.repo_path).parts

                if any(part in skip for part in rel_parts):
                    continue

                if any(part.startswith(".") for part in rel_parts):
                    continue

                files.append(f)

        return files


    # --- analyze (streaming + cache + cross-file invalidation) ---
    async def analyze(self) -> List[Dict]:
        # init cache
        if self.cache is None:
            self.cache = Cache(self.repo_path, self.get_cache_namespace())

        files = self.get_files()
        logger.info("Found %d source files", len(files))
        await self.start_server()

        warm = self.get_warmup_seconds()
        logger.info("Warming up LSP server for %.1f seconds...", warm)
        if warm:
            if self.show_progress:
                for _ in tqdm(range(int(warm / 0.1)), desc="Warming up LSP", leave=True):
                    await asyncio.sleep(0.1)
            else:
                await asyncio.sleep(warm)

        # queue of (file_uri, file_path, position_dict)
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)  # backpressure
        mappings: List[Dict] = []
        # per-position memo within this run
        pos_cache: Dict[Tuple[str, int, int], Optional[Dict]] = {}
        max_conc = self.get_max_concurrency()
        sem = asyncio.Semaphore(max_conc)

        timeout_primary = 45.0
        timeout_retry   = 90.0
        timeout_backoff = 0.2

        # progress bars
        scan_bar = tqdm(total=len(files), desc="Scanning files", leave=True) if self.show_progress else None
        defs_bar = tqdm(total=0, desc="Querying definitions", leave=True) if self.show_progress else None

        # Phase 1: scan files, compute SHA, detect content-changed files
        file_infos: Dict[str, Dict] = {}
        content_changed_files: Set[str] = set()

        for fpath in files:
            # repo-relative path with base_repo_path prefix (same format used in mappings)
            rel_path = f"{self.base_repo_path}/{str(fpath.relative_to(self.repo_path))}"

            try:
                text = fpath.read_text(encoding="utf-8")
            except Exception:
                logger.warning("Failed to read %s", fpath)
                if scan_bar:
                    scan_bar.update(1)
                continue

            sha1 = hashlib.sha1(text.encode("utf-8")).hexdigest()
            cached_sha = self.cache.get_file_sha(rel_path) if self.cache else None

            if cached_sha != sha1:
                content_changed_files.add(rel_path)

            file_infos[rel_path] = {
                "path": fpath,
                "uri": fpath.as_uri(),
                "text": text,
                "sha1": sha1,
                "cached_sha": cached_sha,
            }

            if scan_bar:
                scan_bar.update(1)

        if scan_bar:
            scan_bar.close()

        # Phase 1.5: cross-file invalidation based on definitions
        # For each file whose content changed, invalidate mappings that
        # point into it as a definition target.
        impacted_ref_files: Set[str] = set()
        if self.cache and content_changed_files:
            for def_path in content_changed_files:
                impacted = self.cache.invalidate_by_definition_file(def_path)
                impacted_ref_files.update(impacted)

        # Files whose mappings must be recomputed this run (as reference files)
        files_to_recompute: Set[str] = set(content_changed_files) | set(impacted_ref_files)
        self.changed_files = set(files_to_recompute)

        if files_to_recompute:
            logger.info(
                "Files to recompute mappings for (content-changed=%d, impacted=%d, total=%d)",
                len(content_changed_files),
                len(impacted_ref_files),
                len(files_to_recompute),
            )

        # Prepare worker tasks for definition queries
        async def worker():
            while True:
                item = await q.get()
                if item is None:
                    q.task_done()
                    return
                uri, path, pos = item
                k = (uri, pos["line"], pos["character"])
                try:
                    if k in pos_cache:
                        r = pos_cache[k]
                    else:
                        r = await self._query_def_streaming(uri, path, pos, sem, timeout=timeout_primary)
                        if not r:
                            await asyncio.sleep(timeout_backoff)
                            r = await self._query_def_streaming(uri, path, pos, sem, timeout=timeout_retry)
                        pos_cache[k] = r
                    if r:
                        mappings.append(r)
                finally:
                    if defs_bar:
                        defs_bar.total += 1  # dynamic total
                        defs_bar.update(1)
                        defs_bar.refresh()
                    q.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(max_conc)]
        logger.info("Dispatching definition queries with concurrency=%d (streaming)...", max_conc)

        try:
            # Phase 2: reload from cache vs. enqueue for recomputation
            process_bar = tqdm(total=len(file_infos), desc="Enqueueing refs", leave=True) if self.show_progress else None

            for rel_path, info in file_infos.items():
                fpath: Path = info["path"]
                uri: str = info["uri"]
                text: str = info["text"]
                sha1: str = info["sha1"]
                cached_sha: Optional[str] = info["cached_sha"]

                if rel_path not in files_to_recompute:
                    # Unchanged and not impacted: load cached mappings and skip LSP
                    if self.cache and cached_sha is not None:
                        cached_maps = self.cache.load_mappings_for_file(rel_path)
                        if cached_maps:
                            mappings.extend(cached_maps)
                    if process_bar:
                        process_bar.update(1)
                    continue

                # This file's mappings must be recomputed.
                if self.cache:
                    # Drop any remaining mappings for this file as reference
                    self.cache.delete_mappings_for_file(rel_path)
                    # If content changed, update its SHA; if not, keep existing SHA
                    final_sha = sha1 if cached_sha != sha1 else cached_sha
                    if final_sha is None:
                        final_sha = sha1
                    self.cache.update_file_sha(rel_path, final_sha)

                if self.needs_did_open():
                    await self.client.send_notification(
                        "textDocument/didOpen",
                        {
                            "textDocument": {
                                "uri": uri,
                                "languageId": self.get_language_id(),
                                "version": 1,
                                "text": text,
                            }
                        },
                    )

                # extract and enqueue positions for this file (dedupe within file)
                seen_local: Set[Tuple[int, int]] = set()
                for (line, col) in self.ref_pos_extractor(text, fpath):
                    key_local = (line, col)
                    if key_local in seen_local:
                        continue
                    seen_local.add(key_local)
                    await q.put((uri, fpath, {"line": line, "character": col}))

                if self.needs_did_open():
                    await self.client.send_notification(
                        "textDocument/didClose",
                        {"textDocument": {"uri": uri}},
                    )

                if process_bar:
                    process_bar.update(1)

            if process_bar:
                process_bar.close()

            # finished producing; send sentinels
            for _ in workers:
                await q.put(None)

            # wait for all tasks
            await q.join()

        finally:
            # stop workers
            for w in workers:
                try:
                    await w
                except Exception:
                    logger.exception("Worker task failed")

            if defs_bar:
                defs_bar.close()

            if self.cache:
                self.cache.commit()

            await self.shutdown()
            logger.info("Got %d mappings", len(mappings))

        return mappings

    async def _query_def_streaming(
        self,
        file_uri: str,
        file_path: Path,
        position: Dict,
        sem: asyncio.Semaphore,
        timeout: float = 45.0,
    ):
        async with sem:
            collected = []

            def _append_partial(value):
                if not value:
                    return
                if isinstance(value, dict):
                    chunks = [value]
                elif isinstance(value, list):
                    chunks = value
                else:
                    return
                collected.extend(chunks)

            params = {"textDocument": {"uri": file_uri}, "position": position}

            res = await self.client.send_request(
                "textDocument/definition",
                params,
                on_partial=_append_partial,
                on_work_done=lambda _v: None,
                timeout=timeout,
            )

            result_items = []
            if collected:
                result_items.extend(collected)
            if res:
                if isinstance(res, list):
                    result_items.extend(res)
                else:
                    result_items.append(res)

            if not result_items:
                return None

            valid = []
            for d in result_items:
                if "uri" in d and "range" in d:
                    uri, rng = d["uri"], d["range"]
                elif "targetUri" in d and "targetRange" in d:
                    uri, rng = d["targetUri"], d["targetRange"]
                else:
                    continue
                p = Path(unquote(urlparse(uri).path)).resolve()
                if not p.exists():
                    continue
                try:
                    rel_path = p.relative_to(self.repo_path)  # repo-relative
                except ValueError:
                    continue

                # language-specific exclusions
                if self.is_excluded_definition_path(rel_path):
                    continue

                rel_str = f"{self.base_repo_path}/{str(rel_path)}"
                valid.append(
                    Location(
                        rel_str,
                        rng["start"]["line"],
                        rng["start"]["character"],
                    )
                )

            if not valid:
                return None

            ref = Location(
                f"{self.base_repo_path}/{str(file_path.relative_to(self.repo_path))}",
                position["line"],
                position["character"],
            )
            valid = [d for d in valid if (d.file_path, d.line, d.column) != (ref.file_path, ref.line, ref.column)]

            if not valid:
                return None

            result = {"reference": ref.to_dict(), "definitions": [d.to_dict() for d in valid]}

            # persist in SQLite cache (mappings + def_index)
            if self.cache:
                try:
                    self.cache.store_mapping(result)
                except Exception:
                    logger.exception("Failed to store mapping in cache")

            return result
