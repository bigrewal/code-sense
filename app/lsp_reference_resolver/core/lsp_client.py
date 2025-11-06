import asyncio, json, logging, uuid
from typing import Optional, Dict, List, Callable, Any

logger = logging.getLogger(__name__)

JsonDict = Dict[str, Any]
ProgressHandler = Callable[[Any], None]

class LSPClient:
    def __init__(self, command: List[str]):
        self.command = command
        self.process = None
        self.request_id = 0
        self.server_capabilities: Dict = {}
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task = None
        self._stderr_task = None
        self._workspace_folders: List[Dict] = []

        self._partial_handlers: Dict[str, ProgressHandler] = {}
        self._work_handlers: Dict[str, ProgressHandler] = {}
        self._req_tokens: Dict[int, Dict[str, Optional[str]]] = {}

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        logger.info(f"Started LSP server: {' '.join(self.command)}")

    async def _drain_stderr(self):
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                logger.debug("[LSP stderr] %s", line.decode(errors="ignore").rstrip())
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("stderr drain error")

    async def _reader_loop(self):
        try:
            while True:
                msg = await self._read_one_message()
                if msg is None:
                    break
                if not msg:
                    continue

                if "method" in msg and "id" not in msg:
                    method = msg["method"]
                    params = msg.get("params", {})
                    if method == "$/progress":
                        token = params.get("token")
                        value = params.get("value")
                        if isinstance(value, dict) and "kind" in value:
                            handler = self._work_handlers.get(str(token))
                        else:
                            handler = self._partial_handlers.get(str(token))
                        if handler:
                            try:
                                handler(value)
                            except Exception:
                                logger.exception("progress handler error")
                    else:
                        await self._handle_server_notification(msg)
                    continue

                if "id" in msg and "method" in msg:
                    await self._handle_server_request(msg)
                    continue

                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Reader loop error")

    async def _read_one_message(self):
        headers = {}
        while True:
            line = await self.process.stdout.readline()
            if not line:
                return None
            line = line.decode("ascii", errors="ignore").strip()
            if not line:
                break
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        length_str = headers.get("content-length")
        if not length_str:
            return {}
        try:
            length = int(length_str)
        except ValueError:
            return {}
        if length <= 0:
            return {}

        try:
            body = await self.process.stdout.readexactly(length)
        except asyncio.IncompleteReadError:
            return None

        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            logger.exception("Failed to decode LSP message (len=%s)", length)
            return {}

    async def _send_response(self, id_, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": id_}
        msg["result" if error is None else "error"] = result if error is None else error
        raw = json.dumps(msg)
        content = f"Content-Length: {len(raw)}\r\n\r\n{raw}"
        self.process.stdin.write(content.encode())
        await self.process.stdin.drain()

    async def _handle_server_notification(self, msg: dict):
        method = msg.get("method")
        if method in ("window/logMessage", "telemetry/event", "textDocument/publishDiagnostics"):
            return

    async def _handle_server_request(self, msg: dict):
        m, id_, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if m in ("workspace/configuration","window/showMessageRequest","client/registerCapability",
                 "client/unregisterCapability","window/workDoneProgress/create","$/setTrace"):
            return await self._send_response(id_, result=( [{} for _ in p.get("items", [])] if m=="workspace/configuration" else None ))
        if m == "workspace/workspaceFolders":
            return await self._send_response(id_, result=self._workspace_folders)
        return await self._send_response(id_, error={"code": -32601, "message": f"Not implemented: {m}"})

    async def send_request(self, method: str, params: dict,
                           on_partial: Optional[ProgressHandler] = None,
                           on_work_done: Optional[ProgressHandler] = None,
                           timeout: float = 60.0) -> dict:
        self.request_id += 1
        rid = self.request_id

        req_params: JsonDict = dict(params or {})
        tokens: Dict[str, Optional[str]] = {"partial": None, "work": None}

        if on_partial is not None:
            pr_token = str(uuid.uuid4())
            req_params["partialResultToken"] = pr_token
            self._partial_handlers[pr_token] = on_partial
            tokens["partial"] = pr_token

        if on_work_done is not None:
            wd_token = str(uuid.uuid4())
            req_params["workDoneToken"] = wd_token
            self._work_handlers[wd_token] = on_work_done
            tokens["work"] = wd_token

        self._req_tokens[rid] = tokens

        raw = json.dumps({"jsonrpc":"2.0","id":rid,"method":method,"params":req_params})
        content = f"Content-Length: {len(raw)}\r\n\r\n{raw}"
        self.process.stdin.write(content.encode())
        await self.process.stdin.drain()

        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        try:
            res_msg = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            await self.cancel_request(rid)
            logger.warning("LSP request timed out: %s", method)
            res_msg = {"error": {"code": -32800, "message": "timeout"}}
        finally:
            toks = self._req_tokens.pop(rid, {})
            pr = toks.get("partial"); wd = toks.get("work")
            if pr: self._partial_handlers.pop(pr, None)
            if wd: self._work_handlers.pop(wd, None)

        if res_msg.get("error"):
            return {}
        return res_msg.get("result", {})

    async def cancel_request(self, request_id: int):
        try:
            raw = json.dumps({"jsonrpc":"2.0","method":"$/cancelRequest","params":{"id": request_id}})
            content = f"Content-Length: {len(raw)}\r\n\r\n{raw}"
            self.process.stdin.write(content.encode())
            await self.process.stdin.drain()
        except Exception:
            logger.debug("Failed to send $/cancelRequest")

    async def send_notification(self, method: str, params: dict):
        raw = json.dumps({"jsonrpc":"2.0","method":method,"params":params})
        content = f"Content-Length: {len(raw)}\r\n\r\n{raw}"
        self.process.stdin.write(content.encode())
        await self.process.stdin.drain()

    async def initialize(self, root_uri: str, name: str, initialize_options: Optional[Dict] = None):
        params = {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": {
                "general":{"positionEncodings":["utf-16"]},
                "window": {"workDoneProgress": True},
                "textDocument":{
                    "definition":{"linkSupport":True},
                    "references":{},
                },
                "workspace":{"workspaceFolders":True}
            },
            "workspaceFolders":[{"uri":root_uri,"name":name}],
        }
        if initialize_options:
            params["initializeOptions"] = initialize_options

        res = await self.send_request("initialize", params)
        self.server_capabilities = res.get("capabilities", {})
        self._workspace_folders = [{"uri": root_uri, "name": name}]
        await self.send_notification("initialized", {})
        logger.info("LSP initialized")

    async def shutdown(self):
        if not self.process:
            return
        try:
            await self.send_request("shutdown", {})
            await self.send_notification("exit", {})
        finally:
            if self._reader_task:
                self._reader_task.cancel()
            if self._stderr_task:
                self._stderr_task.cancel()
            self.process.terminate()
            await self.process.wait()
