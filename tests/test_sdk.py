"""Tests for WebAA Python SDK — mirrors JS SDK test coverage."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest
import pytest_asyncio

from webaa_sdk import (
    AGUIEvent,
    EventEmitter,
    InitOptions,
    RunOptions,
    SkillCachePolicy,
    SkillDefinition,
    UserIdentity,
    WebAAError,
    WebAASDK,
)
from webaa_sdk.skill_cache import Freshness, SkillCache


# ── Helpers ──


def _sse_event(event_type: str, payload: Dict[str, Any] | None = None) -> str:
    """Build a single SSE event string."""
    data = json.dumps(
        {
            "type": event_type,
            "payload": payload or {},
            "protocol_version": "1.0.0",
            "timestamp": "2025-01-01T00:00:00Z",
        }
    )
    return f"data: {data}\n\n"


def _sse_body(*events: str) -> str:
    return "".join(events)


async def _noop_execute(params: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True}


def _make_skill(**overrides: Any) -> SkillDefinition:
    defaults = {
        "name": "test_skill",
        "schema": {"type": "function", "function": {"name": "test_skill"}},
        "execute": _noop_execute,
        "execution_mode": "sdk",
    }
    defaults.update(overrides)
    return SkillDefinition(**defaults)


def _token_response() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "tok-123"})


def _config_response() -> httpx.Response:
    return httpx.Response(200, json={"channel_id": "ch-1", "name": "test"})


def _register_response(channel_id: str = "ch-123") -> httpx.Response:
    return httpx.Response(200, json={"registered": True, "channel_id": channel_id})


def _sse_stream_response(*events: str) -> httpx.Response:
    """Build a streaming SSE response."""
    body = _sse_body(*events)
    return httpx.Response(
        200,
        content=body.encode(),
        headers={"content-type": "text/event-stream"},
    )


# ── Init Tests ──


class TestInit:
    @pytest.mark.asyncio
    async def test_init_registers_skills(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={"channel_id": "ch-1"})
        httpx_mock.add_response(url="http://test/api/sdk/register", json={"registered": True, "channel_id": "ch-123"})

        sdk = WebAASDK()
        skill = _make_skill()
        await sdk.init(InitOptions(channel_key="key-1", skills=[skill], api_base="http://test"))

        assert sdk.channel_id == "ch-123"
        assert sdk.access_token == "tok"

        # Verify register request payload
        reqs = httpx_mock.get_requests()
        register_req = [r for r in reqs if "/api/sdk/register" in str(r.url)][0]
        body = json.loads(register_req.content)
        assert len(body["skills"]) == 1
        assert body["skills"][0]["name"] == "test_skill"
        assert body["skills"][0]["execution_mode"] == "sdk"
        assert "execute" not in body["skills"][0]  # execute should NOT be sent

    @pytest.mark.asyncio
    async def test_init_token_failure(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", status_code=401, json={"detail": "Invalid key"})

        sdk = WebAASDK()
        with pytest.raises(WebAAError, match="Token acquisition failed"):
            await sdk.init(InitOptions(channel_key="bad-key", api_base="http://test"))

    @pytest.mark.asyncio
    async def test_init_register_failure(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})
        httpx_mock.add_response(url="http://test/api/sdk/register", status_code=400, json={"detail": "Bad schema"})

        sdk = WebAASDK()
        with pytest.raises(WebAAError, match="Register failed"):
            await sdk.init(InitOptions(channel_key="key-1", skills=[_make_skill()], api_base="http://test"))

    @pytest.mark.asyncio
    async def test_init_with_user(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})
        httpx_mock.add_response(url="http://test/api/sdk/register", json={"channel_id": "ch-1"})
        httpx_mock.add_response(url="http://test/api/sdk/identify", json={"ok": True})

        sdk = WebAASDK()
        user = UserIdentity(user_id="u-1", name="Alice")
        await sdk.init(InitOptions(channel_key="key-1", skills=[_make_skill()], user=user, api_base="http://test"))

        assert sdk.user_id == "u-1"

    @pytest.mark.asyncio
    async def test_init_config_failure_non_critical(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", status_code=500)

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))
        assert sdk.channel_config is None  # should not raise

    @pytest.mark.asyncio
    async def test_init_no_skills(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))
        assert sdk.channel_id is None  # no register call

    @pytest.mark.asyncio
    async def test_init_default_values(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))
        assert sdk.version == "0.1.0"
        assert sdk.api_base == "http://test"


# ── Identify Tests ──


class TestIdentify:
    @pytest.mark.asyncio
    async def test_identify_sends_correct_payload(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})
        httpx_mock.add_response(url="http://test/api/sdk/identify", json={"ok": True})

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))
        await sdk.identify(UserIdentity(user_id="u-1", name="Bob", metadata={"role": "admin"}))

        reqs = httpx_mock.get_requests()
        identify_req = [r for r in reqs if "/api/sdk/identify" in str(r.url)][0]
        body = json.loads(identify_req.content)
        assert body["user_id"] == "u-1"
        assert body["name"] == "Bob"
        assert body["metadata"] == {"role": "admin"}

    @pytest.mark.asyncio
    async def test_identify_fires_callbacks(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})
        httpx_mock.add_response(url="http://test/api/sdk/identify", json={"ok": True})

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))

        called = []
        sdk.on_identify(lambda: called.append(True))
        await sdk.identify(UserIdentity(user_id="u-1"))
        assert called == [True]


# ── Thread Management Tests ──


class TestThreads:
    @pytest.mark.asyncio
    async def test_create_thread(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})
        httpx_mock.add_response(url="http://test/api/sdk/identify", json={"ok": True})
        httpx_mock.add_response(url="http://test/api/sdk/threads", method="POST", json={"id": "t-1"})

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))
        await sdk.identify(UserIdentity(user_id="u-1"))
        result = await sdk.create_thread("My Thread")
        assert result["id"] == "t-1"
        assert sdk.thread_id == "t-1"

    @pytest.mark.asyncio
    async def test_create_thread_requires_identify(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))
        with pytest.raises(WebAAError, match="identify"):
            await sdk.create_thread()

    @pytest.mark.asyncio
    async def test_list_threads(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})
        httpx_mock.add_response(url="http://test/api/sdk/identify", json={"ok": True})
        httpx_mock.add_response(
            url=httpx.URL("http://test/api/sdk/threads", params={"user_id": "u-1", "limit": "20", "offset": "0"}),
            json=[{"id": "t-1"}, {"id": "t-2"}],
        )

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))
        await sdk.identify(UserIdentity(user_id="u-1"))
        threads = await sdk.list_threads()
        assert len(threads) == 2

    @pytest.mark.asyncio
    async def test_list_threads_no_user(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))
        threads = await sdk.list_threads()
        assert threads == []


# ── EventEmitter Tests ──


class TestEventEmitter:
    def test_on_and_emit(self):
        emitter = EventEmitter()
        received: List[str] = []
        emitter.on("test", lambda x: received.append(x))
        emitter.emit("test", "hello")
        assert received == ["hello"]

    def test_off(self):
        emitter = EventEmitter()
        received: List[str] = []
        handler = lambda x: received.append(x)
        emitter.on("test", handler)
        emitter.off("test", handler)
        emitter.emit("test", "hello")
        assert received == []

    def test_remove_all_listeners(self):
        emitter = EventEmitter()
        received: List[str] = []
        emitter.on("a", lambda x: received.append(x))
        emitter.on("b", lambda x: received.append(x))
        emitter.remove_all_listeners()
        emitter.emit("a", "1")
        emitter.emit("b", "2")
        assert received == []

    def test_listener_exception_swallowed(self):
        emitter = EventEmitter()
        received: List[str] = []

        def bad_handler(x: str) -> None:
            raise ValueError("boom")

        emitter.on("test", bad_handler)
        emitter.on("test", lambda x: received.append(x))
        emitter.emit("test", "hello")
        assert received == ["hello"]  # second handler still called


# ── SkillCache Tests ──


class TestSkillCache:
    def test_put_snapshot(self):
        cache = SkillCache()
        policy = SkillCachePolicy(enabled=True, ttl=0, mode="snapshot")
        cache.put("s1", {"data": 1}, policy)
        ctx = cache.build_context()
        assert ctx is not None
        assert ctx["s1"]["result"] == {"data": 1}
        assert ctx["s1"]["freshness"] == "fresh"

    def test_put_append(self):
        cache = SkillCache()
        policy = SkillCachePolicy(enabled=True, ttl=0, mode="append")
        cache.put("s1", {"a": 1}, policy)
        cache.put("s1", {"b": 2}, policy)
        ctx = cache.build_context()
        assert ctx is not None
        assert ctx["s1"]["result"] == {"a": 1, "b": 2}

    def test_put_none_mode(self):
        cache = SkillCache()
        policy = SkillCachePolicy(enabled=True, ttl=0, mode="none")
        cache.put("s1", {"data": 1}, policy)
        assert cache.build_context() is None

    def test_put_disabled(self):
        cache = SkillCache()
        policy = SkillCachePolicy(enabled=False)
        cache.put("s1", {"data": 1}, policy)
        assert cache.build_context() is None

    def test_invalidate(self):
        cache = SkillCache()
        policy = SkillCachePolicy(enabled=True, ttl=0, mode="snapshot", invalidate_on=["url_change"])
        cache.put("s1", {"data": 1}, policy)
        cache.invalidate("url_change")
        ctx = cache.build_context()
        assert ctx is not None
        assert ctx["s1"]["freshness"] == "stale"

    def test_clear(self):
        cache = SkillCache()
        policy = SkillCachePolicy(enabled=True, ttl=0, mode="snapshot")
        cache.put("s1", {"data": 1}, policy)
        cache.clear()
        assert cache.build_context() is None

    def test_ttl_expiry(self):
        import time as _time

        cache = SkillCache()
        policy = SkillCachePolicy(enabled=True, ttl=1, mode="snapshot")  # 1ms TTL
        cache.put("s1", {"data": 1}, policy)
        _time.sleep(0.01)  # wait 10ms
        ctx = cache.build_context()
        assert ctx is None  # should be expired


# ── Connection Lifecycle Tests ──


class TestConnectionLifecycle:
    def test_disconnect(self, httpx_mock):
        sdk = WebAASDK()
        sdk.disconnect()
        assert sdk._disconnected is True

    def test_disconnect_idempotent(self, httpx_mock):
        sdk = WebAASDK()
        sdk.disconnect()
        sdk.disconnect()
        assert sdk._disconnected is True

    def test_reset_clears_state(self, httpx_mock):
        sdk = WebAASDK()
        sdk._user_id = "u-1"
        sdk._run_id = "r-1"
        sdk._thread_id = "t-1"

        called: List[bool] = []
        sdk.on_reset(lambda: called.append(True))
        sdk.reset()

        assert sdk.user_id is None
        assert sdk.run_id is None
        assert sdk.thread_id is None
        assert called == [True]

    def test_reset_clears_cache(self, httpx_mock):
        sdk = WebAASDK()
        policy = SkillCachePolicy(enabled=True, ttl=0, mode="snapshot")
        sdk._skill_cache.put("s1", {"data": 1}, policy)
        sdk.reset()
        assert sdk._skill_cache.build_context() is None


# ── Skill Lookup Priority Tests ──


class TestSkillLookupPriority:
    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_requests_were_expected=False)
    async def test_init_skill_takes_priority(self, httpx_mock):
        """Init-registered skills should take priority over local skills."""
        init_called: List[bool] = []
        local_called: List[bool] = []

        async def init_execute(params: Dict[str, Any]) -> Dict[str, Any]:
            init_called.append(True)
            return {"source": "init"}

        async def local_execute(params: Dict[str, Any]) -> Dict[str, Any]:
            local_called.append(True)
            return {"source": "local"}

        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})
        httpx_mock.add_response(url="http://test/api/sdk/register", json={"channel_id": "ch-1"})

        sdk = WebAASDK()
        skill = _make_skill(name="my_skill", execute=init_execute)
        await sdk.init(InitOptions(channel_key="key-1", skills=[skill], api_base="http://test"))

        # Also register a local skill with the same name
        sdk.register_local_skill("my_skill", local_execute)

        # Simulate SkillExecuteInstruction handling
        event = AGUIEvent(
            type="SkillExecuteInstruction",
            payload={"skill_name": "my_skill", "params": {}, "tool_call_id": "tc-1"},
        )
        emitter = EventEmitter()

        # Mock the follow-up SSE stream
        httpx_mock.add_response(
            url="http://test/api/agent/run",
            status_code=200,
            content=_sse_event("RunFinished", {"run_id": "r-1"}).encode(),
            headers={"content-type": "text/event-stream"},
        )

        await sdk._handle_event(event, emitter, RunOptions(user_input="test"))

        assert init_called == [True]
        assert local_called == []  # local should NOT be called

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_requests_were_expected=False)
    async def test_local_skill_fallback(self, httpx_mock):
        """Local skills should be used when no init skill matches."""
        local_called: List[bool] = []

        async def local_execute(params: Dict[str, Any]) -> Dict[str, Any]:
            local_called.append(True)
            return {"source": "local"}

        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))
        sdk.register_local_skill("local_only", local_execute)

        event = AGUIEvent(
            type="SkillExecuteInstruction",
            payload={"skill_name": "local_only", "params": {}, "tool_call_id": "tc-1"},
        )
        emitter = EventEmitter()

        httpx_mock.add_response(
            url="http://test/api/agent/run",
            status_code=200,
            content=_sse_event("RunFinished", {"run_id": "r-1"}).encode(),
            headers={"content-type": "text/event-stream"},
        )

        await sdk._handle_event(event, emitter, RunOptions(user_input="test"))
        assert local_called == [True]


# ── Default Skill Handler Tests ──


class TestDefaultSkillHandlers:
    @pytest.mark.asyncio
    async def test_dialog_skill_auto_confirm(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))

        handler = sdk._local_skills["dialog_skill"]
        result = await handler({"action": "confirm", "message": "Are you sure?"})
        assert result["confirmed"] is True
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_dialog_skill_notify(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))

        handler = sdk._local_skills["dialog_skill"]
        result = await handler({"action": "notify", "message": "Done"})
        assert result["success"] is True
        assert "confirmed" not in result

    @pytest.mark.asyncio
    async def test_wait_skill(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))

        handler = sdk._local_skills["wait_skill"]
        result = await handler({"condition": "duration", "timeout_ms": 10})
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_default_handlers_can_be_overridden(self, httpx_mock):
        httpx_mock.add_response(url="http://test/api/auth/token", json={"access_token": "tok"})
        httpx_mock.add_response(url="http://test/api/config", json={})

        sdk = WebAASDK()
        await sdk.init(InitOptions(channel_key="key-1", api_base="http://test"))

        async def custom_dialog(params: Dict[str, Any]) -> Dict[str, Any]:
            return {"custom": True}

        sdk.register_local_skill("dialog_skill", custom_dialog)
        handler = sdk._local_skills["dialog_skill"]
        result = await handler({})
        assert result == {"custom": True}
