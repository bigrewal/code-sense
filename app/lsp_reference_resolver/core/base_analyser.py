import asyncio, logging, os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Iterable, Set
from urllib.parse import urlparse, unquote
from .lsp_client import LSPClient

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
    Streaming analyzer:
      - Reads files sequentially and immediately enqueues positions
      - Bounded asyncio.Queue provides backpressure
      - Workers resolve definitions concurrently
      - For languages like Python, didOpen/didClose per file
    """

    def __init__(self, repo_path: Path, base_repo_path: str, show_progress: bool = True):
        self.repo_path = repo_path
        self.base_repo_path = base_repo_path
        self.client: Optional[LSPClient] = None
        self.show_progress = show_progress and (tqdm is not None)

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
        return 1.0

    def get_max_concurrency(self) -> int:
        cpu = (os.cpu_count() or 8)
        lang = self.get_language_id().lower()
        if lang == "python":
            return 4
        return min(32, max(8, cpu))

    def get_initialize_options(self) -> Dict:
        return {}

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
            "target", ".tox", ".eggs", "site-packages", "tests", "test", ".*"
        }
        files: List[Path] = []
        for ext in self.get_file_extensions():
            for f in self.repo_path.rglob(f"*{ext}"):
                if not any(s in f.parts for s in skip) and not any(part.startswith('.') for part in f.parts):
                    files.append(f)
        return files


    # --- analyze (streaming) ---
    async def analyze(self) -> List[Dict]:
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
        cache: Dict[Tuple[str, int, int], Optional[Dict]] = {}
        max_conc = self.get_max_concurrency()
        sem = asyncio.Semaphore(max_conc)

        timeout_primary = 45.0
        timeout_retry   = 90.0
        timeout_backoff = 0.2

        # progress bars
        files_bar = tqdm(total=len(files), desc="Reading files", leave=True) if self.show_progress else None
        defs_bar  = tqdm(total=0, desc="Querying definitions", leave=True) if self.show_progress else None

        async def worker():
            while True:
                item = await q.get()
                if item is None:
                    q.task_done()
                    return
                uri, path, pos = item
                k = (uri, pos["line"], pos["character"])
                try:
                    if k in cache:
                        r = cache[k]
                    else:
                        r = await self._query_def_streaming(uri, path, pos, sem, timeout=timeout_primary)
                        if not r:
                            await asyncio.sleep(timeout_backoff)
                            r = await self._query_def_streaming(uri, path, pos, sem, timeout=timeout_retry)
                        cache[k] = r
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

        # stream files → enqueue positions per file (and didOpen/didClose around the file)
        for fpath in files:
            # read file text
            try:
                text = fpath.read_text(encoding="utf-8")
            except Exception:
                logger.warning("Failed to read %s", fpath)
                if files_bar: files_bar.update(1)
                continue

            if self.needs_did_open():
                await self.client.send_notification(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": fpath.as_uri(),
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
                await q.put((fpath.as_uri(), fpath, {"line": line, "character": col}))

            if self.needs_did_open():
                # close after enqueue, workers operate on saved URI
                await self.client.send_notification(
                    "textDocument/didClose",
                    {"textDocument": {"uri": fpath.as_uri()}},
                )

            if files_bar:
                files_bar.update(1)

        # finished producing; send sentinels
        for _ in workers:
            await q.put(None)

        # wait for all tasks
        await q.join()

        # stop workers
        for w in workers:
            await w

        if files_bar: files_bar.close()
        if defs_bar: defs_bar.close()

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
                    # rel = p.relative_to(self.repo_path)
                    # rel = p
                    rel = f"{self.base_repo_path}/{str(p.relative_to(self.repo_path))}"
                except ValueError:
                    continue
                valid.append(
                    Location(
                        rel,
                        rng["start"]["line"],
                        rng["start"]["character"],
                    )
                )

            if not valid:
                return None

            ref = Location(
                # f"{self.base_repo_path}/{str(file_path)}",
                f"{self.base_repo_path}/{str(file_path.relative_to(self.repo_path))}",
                position["line"],
                position["character"],
            )
            valid = [d for d in valid if (d.file_path, d.line, d.column) != (ref.file_path, ref.line, ref.column)]
            return {"reference": ref.to_dict(), "definitions": [d.to_dict() for d in valid]} if valid else None
