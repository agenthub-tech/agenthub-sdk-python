"""Data models for the WebAA Python SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional


@dataclass
class SkillCachePolicy:
    """L1 SDK Auto Cache policy for a skill."""

    enabled: bool = False
    ttl: int = 0  # ms, 0 = no expiry
    mode: Literal["snapshot", "append", "none"] = "none"
    invalidate_on: List[str] = field(default_factory=list)


# Type alias for skill executor: async (params) -> result dict
SkillExecutor = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


@dataclass
class SkillDefinition:
    """A skill definition to register with the backend."""

    name: str
    schema: Dict[str, Any]
    execute: SkillExecutor
    prompt_injection: Optional[str] = None
    execution_mode: Literal["sdk", "backend"] = "sdk"
    cache: Optional[SkillCachePolicy] = None
    result_cache_fields: Optional[List[Dict[str, Any]]] = None  # [{"path": "x[*].y", "ttl": 600}]


@dataclass
class UserIdentity:
    """End-user identity for the identify() call."""

    user_id: str
    name: Optional[str] = None
    avatar: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class InitOptions:
    """Options for WebAASDK.init()."""

    channel_key: str
    skills: List[SkillDefinition] = field(default_factory=list)
    user: Optional[UserIdentity] = None
    api_base: str = ""
    protocol_version: str = "1.0.0"
    max_retries: int = 3
    retry_delay: float = 1.0  # seconds
    heartbeat_timeout: float = 45.0  # seconds
    debug: bool = False


@dataclass
class RunOptions:
    """Options for WebAASDK.run()."""

    user_input: str
    context: Optional[Dict[str, Any]] = None
    thread_id: Optional[str] = None
    run_id: Optional[str] = None
    tool_result: Optional[Dict[str, Any]] = None
    files: Optional[List[str]] = None  # file paths for multipart upload


@dataclass
class AGUIEvent:
    """A single AG-UI protocol event received from the SSE stream."""

    type: str
    payload: Dict[str, Any]
    protocol_version: str = ""
    timestamp: str = ""

    def payload_string(self, key: str) -> Optional[str]:
        """Convenience: get a string value from payload."""
        v = self.payload.get(key)
        return str(v) if v is not None else None


@dataclass
class ChannelConfig:
    """Channel configuration fetched from GET /api/config."""

    channel_id: Optional[str] = None
    name: Optional[str] = None
    permission_scope: Optional[Dict[str, Any]] = None
    ui_theme: Optional[Dict[str, Any]] = None
