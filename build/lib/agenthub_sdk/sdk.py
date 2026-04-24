"""WebAA Python SDK — headless AG-UI protocol client."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from webaa_sdk.event_emitter import EventEmitter
from webaa_sdk.exceptions import WebAAError
from webaa_sdk.models import (
    AGUIEvent,
    ChannelConfig,
    InitOptions,
    RunOptions,
    SkillDefinition,
    UserIdentity,
)
from webaa_sdk.skill_cache import SkillCache
from webaa_sdk.sse_parser import parse_sse_line_iter

SDK_VERSION = "0.1.0"
DEFAULT_PROTOCOL_VERSION = "1.0.0"
CONNECT_TIMEOUT = 10.0  # seconds

KNOWN_EVENT_TYPES = frozenset(
    [
        "RunStarted",
        "RunFinished",
        "TextMessageStart",
        "TextMessageDelta",
        "TextMessageEnd",
        "ToolCallStart",
        "ToolCallDelta",
        "ToolCallEnd",
        "SkillExecuteInstruction",
        "StateSnapshotEvent",
        "Error",
    ]
)

logger = logging.getLogger("webaa_sdk")


# Type alias for local skill executor
SkillExecutor = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


class WebAASDK:
    """
    WebAA Python SDK — headless AG-UI protocol client.

    Async-first design using httpx + asyncio.
    """

    def __init__(self) -> None:
        # State
        self._channel_id: Optional[str] = None
        self._channel_key: str = ""
        self._access_token: Optional[str] = None
        self._run_id: Optional[str] = None
        self._thread_id: Optional[str] = None
        self._user_id: Optional[str] = None
        self._api_base: str = ""
        self._protocol_version: str = DEFAULT_PROTOCOL_VERSION
        self._channel_config: Optional[ChannelConfig] = None
        self._debug: bool = False

        # Connection lifecycle
        self._max_retries: int = 3
        self._retry_delay: float = 1.0
        self._heartbeat_timeout: float = 45.0
        self._disconnected: bool = False

        # Skills: init-registered skills take priority over local skills
        self._skills: Dict[str, SkillDefinition] = {}
        self._local_skills: Dict[str, SkillExecutor] = {}

        # L1 Cache
        self._skill_cache = SkillCache()

        # Callbacks
        self._on_identify_callbacks: List[Callable[[], None]] = []
        self._on_reset_callbacks: List[Callable[[], None]] = []

        # Heartbeat
        self._heartbeat_task: Optional[asyncio.Task] = None

        # HTTP client (created lazily in init)
        self._client: Optional[httpx.AsyncClient] = None

    # ── Debug Logging ──

    def _log(self, msg: str, *args: Any) -> None:
        if not self._debug:
            return
        formatted = msg % args if args else msg
        logger.debug("[WebAA SDK] %s", formatted)
        # Also print for convenience when debug=True
        print(f"[WebAA SDK] {formatted}")

    # ── HTTP helpers ──

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(CONNECT_TIMEOUT, read=None))
        return self._client

    def _auth_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    @staticmethod
    def _extract_detail(data: Any, status_code: int) -> str:
        if isinstance(data, dict):
            return str(data.get("detail") or data.get("message") or f"HTTP {status_code}")
        return f"HTTP {status_code}"

    # ── Init ──

    async def init(self, options: InitOptions) -> None:
        """
        Initialize the SDK:
        1. Acquire access token
        2. Fetch channel config
        3. Register skills with backend
        4. Identify user if provided
        5. Register default skill handlers
        """
        self._api_base = options.api_base
        self._channel_key = options.channel_key
        self._protocol_version = options.protocol_version
        self._max_retries = options.max_retries
        self._retry_delay = options.retry_delay
        self._heartbeat_timeout = options.heartbeat_timeout
        self._debug = options.debug
        self._disconnected = False

        self._log(
            "init start | apiBase=%s channelKey=%s protocol=%s debug=%s",
            self._api_base, self._channel_key, self._protocol_version, self._debug,
        )

        for skill in options.skills:
            self._skills[skill.name] = skill

        # 1. Acquire access token
        await self._acquire_token()
        self._log("token acquired")

        # 2. Fetch channel config (non-critical)
        self._channel_config = await self._fetch_channel_config()
        self._log("config fetched | channelConfig=%s", "ok" if self._channel_config else "null")

        # 3. Register skills with backend
        if options.skills:
            await self._register_skills(options.skills)
            self._log("skills registered | count=%d channelId=%s", len(options.skills), self._channel_id)

        # 4. Identify user if provided
        if options.user:
            await self.identify(options.user)
            self._log("user identified | userId=%s", options.user.user_id)

        # 5. Register default handlers for platform builtin skills
        self._register_default_skill_handlers()

        self._log("init complete")

    async def _acquire_token(self) -> None:
        client = self._ensure_client()
        resp = await client.post(
            f"{self._api_base}/api/auth/token",
            json={"channel_key": self._channel_key},
        )
        if resp.status_code != 200:
            detail = self._extract_detail(resp.json(), resp.status_code)
            raise WebAAError(f"Token acquisition failed ({resp.status_code}): {detail}", resp.status_code)
        data = resp.json()
        self._access_token = data["access_token"]

    async def _fetch_channel_config(self) -> Optional[ChannelConfig]:
        try:
            client = self._ensure_client()
            resp = await client.get(
                f"{self._api_base}/api/config",
                headers=self._auth_headers(),
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            return ChannelConfig(
                channel_id=data.get("channel_id"),
                name=data.get("name"),
                permission_scope=data.get("permission_scope"),
                ui_theme=data.get("ui_theme"),
            )
        except Exception:
            return None

    async def _register_skills(self, skills: List[SkillDefinition]) -> None:
        skills_meta = [
            {
                "name": s.name,
                "schema": s.schema,
                "prompt_injection": s.prompt_injection,
                "execution_mode": s.execution_mode,
                **({"result_cache_fields": s.result_cache_fields} if s.result_cache_fields else {}),
            }
            for s in skills
        ]
        client = self._ensure_client()
        resp = await client.post(
            f"{self._api_base}/api/sdk/register",
            json={"skills": skills_meta, "protocol_version": self._protocol_version},
            headers={**self._auth_headers(), "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            detail = self._extract_detail(resp.json(), resp.status_code)
            raise WebAAError(f"Register failed ({resp.status_code}): {detail}", resp.status_code)
        data = resp.json()
        self._channel_id = data.get("channel_id")

    # ── Default Skill Handlers ──

    def _register_default_skill_handlers(self) -> None:
        """Register default local handlers for platform builtin skills (headless mode)."""

        if "dialog_skill" not in self._local_skills:
            async def _dialog_handler(params: Dict[str, Any]) -> Dict[str, Any]:
                action = str(params.get("action", "notify"))
                message = str(params.get("message", ""))
                self._log("dialog_skill (default) | action=%s message=%s", action, message)
                result: Dict[str, Any] = {"action": action, "message": message, "success": True}
                if action == "confirm":
                    result["confirmed"] = True
                elif action == "input":
                    result["value"] = ""
                return result

            self._local_skills["dialog_skill"] = _dialog_handler

        if "wait_skill" not in self._local_skills:
            async def _wait_handler(params: Dict[str, Any]) -> Dict[str, Any]:
                condition = str(params.get("condition", "duration"))
                timeout_ms = int(params.get("timeout_ms", 5000))
                self._log("wait_skill (default) | condition=%s timeout=%dms", condition, timeout_ms)
                if condition == "duration":
                    await asyncio.sleep(min(timeout_ms, 30000) / 1000.0)
                return {"success": True, "condition": condition}

            self._local_skills["wait_skill"] = _wait_handler

        if "http_skill" not in self._local_skills:
            async def _http_handler(params: Dict[str, Any]) -> Dict[str, Any]:
                method = str(params.get("method", "GET"))
                url = str(params.get("url", ""))
                self._log("http_skill (default) | %s %s", method, url)
                try:
                    async with httpx.AsyncClient(timeout=10.0) as c:
                        headers = params.get("headers") or {}
                        body = params.get("body")
                        content = str(body).encode() if body is not None else None
                        if content and "Content-Type" not in headers:
                            headers["Content-Type"] = "application/json"
                        resp = await c.request(method, url, headers=headers, content=content)
                        return {
                            "success": 200 <= resp.status_code < 300,
                            "status": resp.status_code,
                            "data": resp.text,
                        }
                except Exception as e:
                    return {"success": False, "error": str(e)}

            self._local_skills["http_skill"] = _http_handler

    # ── Heartbeat ──

    def _reset_heartbeat(
        self,
        options: RunOptions,
        emitter: EventEmitter,
        retry_count: int,
    ) -> None:
        self._clear_heartbeat()
        if self._disconnected:
            return

        async def _heartbeat_timeout() -> None:
            await asyncio.sleep(self._heartbeat_timeout)
            if self._disconnected:
                return
            self._log("heartbeat timeout | %.0fs elapsed with no data", self._heartbeat_timeout)
            if retry_count < self._max_retries:
                await self._schedule_reconnect(options, emitter, retry_count)
            else:
                self._emit_error(emitter, WebAAError("Heartbeat timeout: no events received"))

        try:
            loop = asyncio.get_running_loop()
            self._heartbeat_task = loop.create_task(_heartbeat_timeout())
        except RuntimeError:
            pass

    def _clear_heartbeat(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    # ── Run ──

    def run(self, options: RunOptions) -> EventEmitter:
        """
        Send a user prompt to the agent and return an EventEmitter that streams AG-UI events.
        Starts the SSE stream in a background asyncio task.
        """
        emitter = EventEmitter()
        self._disconnected = False

        if options.run_id:
            self._run_id = options.run_id
        if options.thread_id:
            self._thread_id = options.thread_id

        self._log(
            'run | userInput="%s" runId=%s threadId=%s',
            (options.user_input or "")[:80], options.run_id, options.thread_id,
        )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._start_sse_stream(options, emitter, 0, False))
        except RuntimeError:
            # No running loop — caller should await run_async() instead
            pass

        return emitter

    async def run_async(self, options: RunOptions) -> EventEmitter:
        """Async version of run() — awaits the full SSE stream."""
        emitter = EventEmitter()
        self._disconnected = False

        if options.run_id:
            self._run_id = options.run_id
        if options.thread_id:
            self._thread_id = options.thread_id

        self._log(
            'run | userInput="%s" runId=%s threadId=%s',
            (options.user_input or "")[:80], options.run_id, options.thread_id,
        )

        await self._start_sse_stream(options, emitter, 0, False)
        return emitter

    async def _start_sse_stream(
        self,
        options: RunOptions,
        emitter: EventEmitter,
        retry_count: int,
        is_retry_after_refresh: bool = False,
    ) -> None:
        if self._disconnected:
            return

        self._log("sse-connect | retry=%d/%d", retry_count, self._max_retries)
        self._reset_heartbeat(options, emitter, retry_count)
        received_terminal = False

        try:
            body: Dict[str, Any] = {
                "user_input": options.user_input,
                "context": options.context or {},
            }

            # L1: inject skill cache
            cache_ctx = self._skill_cache.build_context()
            if cache_ctx:
                body["context"]["skill_cache"] = cache_ctx

            if options.run_id is not None:
                body["run_id"] = options.run_id
            if options.tool_result is not None:
                body["tool_result"] = options.tool_result
            if self._user_id:
                body["user_id"] = self._user_id
            if options.thread_id is not None:
                body["thread_id"] = options.thread_id
            elif self._thread_id:
                body["thread_id"] = self._thread_id

            has_files = options.files and len(options.files) > 0
            client = self._ensure_client()

            if has_files:
                # Multipart streaming request with files
                data_fields: Dict[str, str] = {
                    "user_input": options.user_input,
                    "context": json.dumps(body.get("context", {})),
                }
                if "run_id" in body:
                    data_fields["run_id"] = str(body["run_id"])
                if "user_id" in body:
                    data_fields["user_id"] = str(body["user_id"])
                if "thread_id" in body:
                    data_fields["thread_id"] = str(body["thread_id"])

                files_list = [
                    ("files", (os.path.basename(fp), open(fp, "rb"), "application/octet-stream"))
                    for fp in options.files  # type: ignore[union-attr]
                ]
                try:
                    req = client.build_request(
                        "POST",
                        f"{self._api_base}/api/agent/run",
                        data=data_fields,
                        files=files_list,
                        headers=self._auth_headers(),
                    )
                finally:
                    for _, (_, f, _) in files_list:
                        f.close()
            else:
                # JSON streaming request
                req = client.build_request(
                    "POST",
                    f"{self._api_base}/api/agent/run",
                    json=body,
                    headers={**self._auth_headers(), "Content-Type": "application/json"},
                )

            async with client.stream("POST", req.url, content=req.content, headers=req.headers) as stream:
                status = stream.status_code

                if status != 200:
                    self._clear_heartbeat()
                    err_text = ""
                    async for chunk in stream.aiter_text():
                        err_text += chunk
                    try:
                        err_data = json.loads(err_text)
                    except Exception:
                        err_data = {"detail": err_text or f"HTTP {status}"}
                    detail = self._extract_detail(err_data, status)
                    self._log("sse-error | status=%d detail=%s", status, detail[:120])
                    error = WebAAError(f"Run failed ({status}): {detail}", status)

                    if status == 401 and not is_retry_after_refresh:
                        try:
                            await self._acquire_token()
                            await self._start_sse_stream(options, emitter, retry_count, True)
                            return
                        except Exception as refresh_err:
                            self._emit_error(emitter, refresh_err)
                            return

                    if 400 <= status < 500:
                        self._emit_error(emitter, error)
                        return

                    if retry_count < self._max_retries and not self._disconnected:
                        await self._schedule_reconnect(options, emitter, retry_count)
                        return

                    self._emit_error(emitter, error)
                    return

                # Parse SSE stream
                data_buffer = ""
                async for line in stream.aiter_lines():
                    if self._disconnected:
                        break

                    self._reset_heartbeat(options, emitter, retry_count)

                    if line == "":
                        if data_buffer:
                            try:
                                raw = json.loads(data_buffer)
                                event = AGUIEvent(
                                    type=raw.get("type", ""),
                                    payload=raw.get("payload", {}),
                                    protocol_version=raw.get("protocol_version", ""),
                                    timestamp=raw.get("timestamp", ""),
                                )
                                if event.type in ("RunFinished", "Error"):
                                    received_terminal = True
                                should_stop = await self._handle_event(event, emitter, options)
                                if should_stop:
                                    return
                            except (json.JSONDecodeError, KeyError):
                                pass
                            data_buffer = ""
                    elif line.startswith("data: "):
                        data_buffer += line[6:]

                # Handle trailing data
                if data_buffer:
                    try:
                        raw = json.loads(data_buffer)
                        event = AGUIEvent(
                            type=raw.get("type", ""),
                            payload=raw.get("payload", {}),
                            protocol_version=raw.get("protocol_version", ""),
                            timestamp=raw.get("timestamp", ""),
                        )
                        if event.type in ("RunFinished", "Error"):
                            received_terminal = True
                        await self._handle_event(event, emitter, options)
                    except (json.JSONDecodeError, KeyError):
                        pass

        except Exception as e:
            self._clear_heartbeat()
            if self._disconnected:
                return
            self._log("sse-exception | %s", str(e))
            if retry_count < self._max_retries and not self._disconnected:
                await self._schedule_reconnect(options, emitter, retry_count)
                return
            self._emit_error(emitter, e)
            return

        self._clear_heartbeat()

        # Abnormal stream end: stream closed without RunFinished/Error — retry
        if not received_terminal and not self._disconnected and retry_count < self._max_retries:
            self._log("sse-abnormal-end | stream ended without terminal event, reconnecting")
            await self._schedule_reconnect(options, emitter, retry_count)

    async def _handle_event(
        self,
        event: AGUIEvent,
        emitter: EventEmitter,
        options: RunOptions,
    ) -> bool:
        """
        Process a single AG-UI event. Returns True if the stream should stop.
        """
        if self._disconnected:
            return True

        if event.type == "RunStarted":
            rid = event.payload_string("run_id")
            if rid:
                self._run_id = rid
            tid = event.payload_string("thread_id")
            if tid:
                self._thread_id = tid
            self._log("event RunStarted | runId=%s threadId=%s", rid, tid)

        if event.type not in KNOWN_EVENT_TYPES:
            self._log("event unknown (skipped) | type=%s", event.type)
            return False

        emitter.emit(event.type, event)
        emitter.emit("event", event)

        if event.type == "RunFinished":
            self._log("event RunFinished")
            self._clear_heartbeat()
            emitter.emit("done", event)
            return True

        if event.type == "Error":
            self._log("event Error | %s", event.payload_string("message"))
            self._clear_heartbeat()
            emitter.emit("error", event)
            return True

        if event.type == "SkillExecuteInstruction":
            skill_name = event.payload_string("skill_name") or ""
            params = event.payload.get("params", {})
            if not isinstance(params, dict):
                params = {}
            tool_call_id = event.payload_string("tool_call_id") or ""

            self._log("event SkillExecuteInstruction | skill=%s toolCallId=%s", skill_name, tool_call_id)

            # Skill lookup: init-registered skills take priority over local skills
            skill_def = self._skills.get(skill_name)
            execute_func: Optional[SkillExecutor] = None
            if skill_def is not None:
                execute_func = skill_def.execute
            if execute_func is None:
                execute_func = self._local_skills.get(skill_name)

            if execute_func is not None:
                try:
                    self._log("skill-exec | skill=%s", skill_name)
                    result = await execute_func(params)
                    tool_result = {"tool_call_id": tool_call_id, "result": result}
                    self._log("skill-exec ok | skill=%s", skill_name)
                    # L1: cache the result
                    if skill_def is not None:
                        self._skill_cache.put(skill_name, result, skill_def.cache)
                except Exception as e:
                    msg = str(e)
                    self._log("skill-exec error | skill=%s error=%s", skill_name, msg)
                    tool_result = {"tool_call_id": tool_call_id, "result": {"error": msg}}
            else:
                self._log("skill-exec miss | skill=%s not registered", skill_name)
                tool_result = {
                    "tool_call_id": tool_call_id,
                    "result": {"error": f"Skill '{skill_name}' not registered locally"},
                }

            # Emit synthetic ToolCallEnd
            tool_call_end = AGUIEvent(
                type="ToolCallEnd",
                payload={
                    "tool_call_id": tool_call_id,
                    "tool_name": skill_name,
                    "result": tool_result["result"],
                },
                protocol_version=event.protocol_version,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            emitter.emit("ToolCallEnd", tool_call_end)
            emitter.emit("event", tool_call_end)

            self._clear_heartbeat()

            # Check if skill result contains files to upload
            files_to_upload = None
            if isinstance(result, dict) and "__files__" in result:
                files_to_upload = result.pop("__files__")
                if isinstance(files_to_upload, str):
                    files_to_upload = [files_to_upload]
                self._log("skill-exec files | skill=%s files=%s", skill_name, files_to_upload)

            # Refresh token before resume (skill execution may take long, token could expire)
            try:
                await self._acquire_token()
                self._log("token refreshed before resume")
            except Exception as e:
                self._log("token refresh failed before resume: %s", str(e))

            # Follow-up run to resume
            resume_options = RunOptions(
                user_input="",
                run_id=self._run_id,
                tool_result=tool_result,
                files=files_to_upload,
            )
            await self._start_sse_stream(resume_options, emitter, 0, False)
            return True

        return False

    async def _schedule_reconnect(
        self,
        options: RunOptions,
        emitter: EventEmitter,
        retry_count: int,
    ) -> None:
        if self._disconnected:
            return
        self._log("reconnect scheduled | retry=%d delay=%.1fs", retry_count + 1, self._retry_delay)
        await asyncio.sleep(self._retry_delay)
        if not self._disconnected:
            await self._start_sse_stream(options, emitter, retry_count + 1, False)

    def _emit_error(self, emitter: EventEmitter, exc: Exception) -> None:
        msg = str(exc) if str(exc) else "Unknown error"
        error_event = AGUIEvent(
            type="Error",
            payload={"message": msg},
            protocol_version=self._protocol_version,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        emitter.emit("error", error_event)

    # ── Identify ──

    async def identify(self, user: UserIdentity) -> None:
        """Identify the current end user. Can be called during init or later."""
        self._user_id = user.user_id
        if not self._access_token:
            return

        client = self._ensure_client()
        resp = await client.post(
            f"{self._api_base}/api/sdk/identify",
            json={
                "user_id": user.user_id,
                "name": user.name,
                "avatar": user.avatar,
                "metadata": user.metadata or {},
            },
            headers={**self._auth_headers(), "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            detail = self._extract_detail(resp.json(), resp.status_code)
            raise WebAAError(f"Identify failed ({resp.status_code}): {detail}", resp.status_code)

        for cb in self._on_identify_callbacks:
            try:
                cb()
            except Exception:
                pass

    def on_identify(self, callback: Callable[[], None]) -> None:
        """Register a callback to run after identify() succeeds."""
        self._on_identify_callbacks.append(callback)

    # ── Thread Management ──

    async def create_thread(self, title: Optional[str] = None) -> Dict[str, Any]:
        """Create a new thread for the current user."""
        if not self._user_id:
            raise WebAAError("Call identify() before creating threads")
        if not self._access_token:
            raise WebAAError("SDK not initialized")

        client = self._ensure_client()
        resp = await client.post(
            f"{self._api_base}/api/sdk/threads",
            json={"user_id": self._user_id, "title": title},
            headers={**self._auth_headers(), "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            detail = self._extract_detail(resp.json(), resp.status_code)
            raise WebAAError(f"Create thread failed: {detail}", resp.status_code)

        data = resp.json()
        self._thread_id = data.get("id")
        return data

    async def new_thread(self) -> Optional[str]:
        """Start a new conversation thread, resetting current run/thread state."""
        self.disconnect()
        self._run_id = None
        self._thread_id = None
        self._disconnected = False
        self._skill_cache.clear()

        if self._user_id and self._access_token:
            thread = await self.create_thread()
            return thread.get("id")
        return None

    async def switch_thread(self, thread_id: str) -> Dict[str, Any]:
        """Switch to an existing thread by id, loading its message history."""
        if not self._access_token:
            raise WebAAError("SDK not initialized")

        self.disconnect()
        self._run_id = None
        self._thread_id = thread_id

        client = self._ensure_client()
        resp = await client.get(
            f"{self._api_base}/api/sdk/threads/{thread_id}",
            headers=self._auth_headers(),
        )
        if resp.status_code != 200:
            detail = self._extract_detail(resp.json(), resp.status_code)
            raise WebAAError(f"Switch thread failed: {detail}", resp.status_code)
        return resp.json()

    async def list_threads(self, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        """List threads for the current user."""
        if not self._user_id or not self._access_token:
            return []

        client = self._ensure_client()
        resp = await client.get(
            f"{self._api_base}/api/sdk/threads",
            params={"user_id": self._user_id, "limit": limit, "offset": offset},
            headers=self._auth_headers(),
        )
        if resp.status_code != 200:
            return []
        return resp.json()

    # ── Local Skills ──

    def register_local_skill(self, name: str, execute: SkillExecutor) -> None:
        """Register a local skill execute handler without sending it to the backend."""
        self._local_skills[name] = execute

    # ── Connection Lifecycle ──

    def disconnect(self) -> None:
        """Disconnect from the backend."""
        self._log("disconnect")
        self._disconnected = True
        self._clear_heartbeat()

    def reset(self) -> None:
        """Reset user state (logout). Disconnects, clears userId/threadId/runId."""
        self._log("reset")
        self.disconnect()
        self._user_id = None
        self._run_id = None
        self._thread_id = None
        self._disconnected = False
        self._skill_cache.clear()
        for cb in self._on_reset_callbacks:
            try:
                cb()
            except Exception:
                pass

    def on_reset(self, callback: Callable[[], None]) -> None:
        """Register a callback to run when reset() is called."""
        self._on_reset_callbacks.append(callback)

    # ── L1 Cache ──

    def invalidate_cache(self, event_name: str) -> None:
        """Invalidate cache entries by event name."""
        self._skill_cache.invalidate(event_name)

    # ── Public properties ──

    @property
    def version(self) -> str:
        return SDK_VERSION

    @property
    def channel_id(self) -> Optional[str]:
        return self._channel_id

    @property
    def run_id(self) -> Optional[str]:
        return self._run_id

    @property
    def thread_id(self) -> Optional[str]:
        return self._thread_id

    @property
    def user_id(self) -> Optional[str]:
        return self._user_id

    @property
    def access_token(self) -> Optional[str]:
        return self._access_token

    @property
    def api_base(self) -> str:
        return self._api_base

    @property
    def channel_config(self) -> Optional[ChannelConfig]:
        return self._channel_config
