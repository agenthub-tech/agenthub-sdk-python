# AgentHub Python SDK

Async-first Python SDK for Agent Hub.

## Install

```bash
pip install agenthub-sdk
```

## Quick Start

```python
import asyncio

from agenthub_sdk import AgentHubSDK, InitOptions, RunOptions


async def main():
    sdk = AgentHubSDK()

    await sdk.init(
        InitOptions(
            channel_key="your-channel-key",
            api_base="https://your-agenthub-server",
        )
    )

    done = asyncio.get_running_loop().create_future()
    emitter = sdk.run(RunOptions(
        user_input="杭州明天天气如何？",
        web_search_enabled=True,
    ))

    emitter.on("TextMessageDelta", lambda event: print(event.payload.get("delta", ""), end=""))
    emitter.on("done", lambda _event: None if done.done() else done.set_result(None))
    emitter.on("error", lambda event: None if done.done() else done.set_exception(RuntimeError(event.payload.get("message", "run failed"))))

    await done


asyncio.run(main())
```

## Notes

- Import from `agenthub_sdk`.
- `channel_key` is required.
- `run()` requires a running event loop.
- `web_search_enabled` overrides web search for the current session; it only takes effect when the channel allows web search.
- SDK-side skills must use `execution_mode="sdk"`.
- `SkillExecuteInstruction` is auto-dispatched and auto-resumed by the SDK.
