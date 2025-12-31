import asyncio, logging, os, hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set
from urllib.parse import urlparse, unquote

from .lsp_client import LSPClient
from .cache import Cache
from app.config import Config

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
    Batch analyzer (adaptive + cache-friendly):

    Flow:
      1) initialize LSP + warmup + (best-effort) readiness probe
      2) scan files -> compute SHA1 -> decide which files need recompute
      3) for batches of N files:
           didOpen N files
           extract refs for those files
           query definitions for those refs (bounded concurrency)
           didClose N files
      4) unchanged files served from SQLite cache
    """

    def __init__(self, repo_path: Path, base_repo_path: str, show_progress: bool = True):
        self.repo_path = repo_path
        self.base_repo_path = base_repo_path
        # self.client: Optional[LSPClient] = None
        self.show_progress = show_progress and (tqdm is not None)
        self.cache: Optional[Cache] = None
        self.changed_files: Set[str] = set()
        self.server_list: List[LSPClient] = []

    # --- required by subclasses ---
    def get_server_command(self) -> List[str]:
        raise NotImplementedError

    def get_file_extensions(self) -> List[str]:
        raise NotImplementedError

    def get_language_id(self) -> str:
        raise NotImplementedError

    def ref_pos_extractor(self, text: str, path: Path) -> List[Tuple[int, int]]:
        raise NotImplementedError

    # --- overridable knobs ---
    def needs_did_open(self) -> bool:
        # for many servers, opening docs improves correctness/latency
        return self.get_language_id().lower() == "python"

    def get_warmup_seconds(self) -> float:
        lang = self.get_language_id().lower()
        if lang == "scala":
            return 4.0
        if lang in ("java", "rust"):
            return 12.0
        if lang == "python":
            return 30.0
        return 1.0

    def get_max_concurrency(self) -> int:
        cpu = (os.cpu_count() or 8)
        lang = self.get_language_id().lower()
        if lang == "python":
            return 2  # safer default for big repos; tune up later
        return min(16, max(6, cpu))

    def get_batch_size(self) -> int:
        return 5

    def get_initialize_options(self) -> Dict:
        return {}

    def get_cache_namespace(self) -> str:
        cmd = " ".join(self.get_server_command())
        return f"{self.__class__.__name__}:{cmd}"

    # --- lifecycle ---
    async def start_server(self):
        # Start 4 language servers
        for _ in range(4):
            self.client = LSPClient(self.get_server_command())
            await self.client.start()
            await self.client.initialize(
                self.repo_path.as_uri(),
                self.repo_path.name,
                initialize_options=self.get_initialize_options(),
            )

            self.server_list.append(self.client)


    async def shutdown(self):
        for client in self.server_list:
            await client.shutdown()

    # --- file discovery ---
    def get_files(self) -> List[Path]:
        skip = {
            "node_modules", "__pycache__", "venv", ".venv", "build", "dist",
            "target", ".tox", ".eggs", "site-packages", "tests", "test", ".*"
        }
        files: List[Path] = []
        for ext in self.get_file_extensions():
            for f in self.repo_path.rglob(f"*{ext}"):
                if not any(s in f.parts for s in Config.IGNORE_FOLDERS) and not any(part.startswith(".") for part in f.parts):
                    files.append(f)
        return files

    # --- main ---
    async def analyze(self) -> List[Dict]:
        if self.cache is None:
            self.cache = Cache(self.repo_path, self.get_cache_namespace())

        files = self.get_files()
        logger.info("Found %d source files", len(files))
        await self.start_server()

        timeout_primary = 120.0 if self.get_language_id().lower() == "python" else 45.0
        timeout_retry = 180.0 if self.get_language_id().lower() == "python" else 90.0
        timeout_backoff = 0.2

        mappings: List[Dict] = []

        # ---------- Phase 1: scan files, sha1, cache hits ----------
        scan_bar = tqdm(total=len(files), desc="Scanning files", leave=True) if self.show_progress else None

        file_infos: Dict[str, Dict] = {}
        content_changed_files: Set[str] = set()

        for fpath in files:
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

        # ---------- Phase 1.5: invalidate referencing files if definitions changed ----------
        impacted_ref_files: Set[str] = set()
        if self.cache and content_changed_files:
            # optional: your Cache may implement this; if not, just skip.
            invalidate = getattr(self.cache, "invalidate_by_definition_file", None)
            if callable(invalidate):
                for def_path in content_changed_files:
                    impacted = invalidate(def_path)
                    if impacted:
                        impacted_ref_files.update(impacted)

        files_to_recompute: Set[str] = set(content_changed_files) | set(impacted_ref_files)
        self.changed_files = set(files_to_recompute)

        logger.info(
            "Files to recompute mappings for (content-changed=%d, impacted=%d, total=%d)",
            len(content_changed_files),
            len(impacted_ref_files),
            len(files_to_recompute),
        )

        # Load cached mappings for files not recomputed
        for rel_path, info in file_infos.items():
            if rel_path in files_to_recompute:
                continue
            if self.cache and info["cached_sha"] is not None:
                cached_maps = self.cache.load_mappings_for_file(rel_path)
                if cached_maps:
                    mappings.extend(cached_maps)

        # ---------- Phase 2: process recompute files in batches ----------
        file_queue = asyncio.Queue()
        recompute_list: List[str] = []
        for rp in file_infos.keys():
            if rp in files_to_recompute:
                await file_queue.put(rp)
                recompute_list.append(rp)

        batch_size = max(1, self.get_batch_size())

        defs_bar = tqdm(total=0, desc="Querying definitions", leave=True) if self.show_progress else None
        batch_bar = tqdm(total=len(recompute_list), desc="Resolving references", leave=True) if self.show_progress else None

        async def resolve_queries(client: LSPClient, queries: List[Tuple[str, Path, Dict]], worker_sem: asyncio.Semaphore):
            # per-position memo inside this batch
            pos_cache: Dict[Tuple[str, int, int], Optional[Dict]] = {}

            async def run_one(uri: str, path: Path, pos: Dict):
                k = (uri, pos["line"], pos["character"])
                if k in pos_cache:
                    return pos_cache[k]

                r = await self._query_def_streaming(client, uri, path, pos, worker_sem, timeout=timeout_primary)
                if not r:
                    await asyncio.sleep(timeout_backoff)
                    r = await self._query_def_streaming(client, uri, path, pos, worker_sem, timeout=timeout_retry)
                pos_cache[k] = r
                return r

            tasks = [asyncio.create_task(run_one(uri, path, pos)) for (uri, path, pos) in queries]
            for fut in asyncio.as_completed(tasks):
                r = await fut
                if r:
                    mappings.append(r)
                if defs_bar:
                    defs_bar.update(1)

        async def worker(client: LSPClient, worker_id: int):
            worker_sem = asyncio.Semaphore(self.get_max_concurrency())
            while True:
                try:
                    rel_path = file_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                info = file_infos[rel_path]
                fpath: Path = info["path"]
                uri: str = info["uri"]
                text: str = info["text"]
                sha1: str = info["sha1"]
                cached_sha: Optional[str] = info["cached_sha"]

                # didOpen
                if self.needs_did_open():
                    await client.send_notification(
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

                seen_local: Set[Tuple[int, int]] = set()
                queries: List[Tuple[str, Path, Dict]] = []
                for (line, col) in self.ref_pos_extractor(text, fpath):
                    if (line, col) in seen_local:
                        continue
                    seen_local.add((line, col))

                    queries.append((uri, fpath, {"line": line, "character": col}))

                if defs_bar:
                    defs_bar.total += len(queries)
                    defs_bar.refresh()

                await resolve_queries(client, queries, worker_sem=worker_sem)

                # didClose
                if self.needs_did_open():
                    await client.send_notification(
                        "textDocument/didClose",
                        {"textDocument": {"uri": uri}},
                    )

                # update cache bookkeeping
                if self.cache:
                    self.cache.delete_mappings_for_file(rel_path)
                    final_sha = sha1 if cached_sha != sha1 else (cached_sha or sha1)
                    self.cache.update_file_sha(rel_path, final_sha)
                    self.cache.commit()

                if batch_bar:
                    batch_bar.update(1)

                file_queue.task_done()

        # Launch workers
        worker_tasks = [
            asyncio.create_task(worker(client, i)) 
            for i, client in enumerate(self.server_list)
        ]

        try:
            await asyncio.gather(*worker_tasks)

        finally:
            if batch_bar:
                batch_bar.close()
            if defs_bar:
                defs_bar.close()
            if self.cache:
                self.cache.commit()
            await self.shutdown()
            logger.info("Got %d mappings", len(mappings))

        # logger.info("======= Total definition queries issued: %d ======", total_queries)
        logger.info("Got %d mappings", len(mappings))
        return mappings

    async def _query_def_streaming(
        self,
        client: LSPClient,
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
                    collected.append(value)
                elif isinstance(value, list):
                    collected.extend(value)

            params = {"textDocument": {"uri": file_uri}, "position": position}

            import time
            res = await client.send_request(
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

            valid: List[Location] = []
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
                    rel = f"{self.base_repo_path}/{str(p.relative_to(self.repo_path))}"
                except ValueError:
                    continue

                valid.append(Location(rel, rng["start"]["line"], rng["start"]["character"]))

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

            if self.cache:
                try:
                    self.cache.store_mapping(result)
                except Exception:
                    logger.exception("Failed to store mapping in cache")

            return result