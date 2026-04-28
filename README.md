# Agenthub Python SDK

Agenthub 平台的 Python SDK，基于 AG-UI 协议实现 Agent 通信。

## 安装

```bash
pip install agenthub-sdk
```

## 快速开始

```python
import asyncio
from agenthub_sdk import WebAASDK, InitOptions, SkillDefinition, RunOptions

async def my_skill_handler(params):
    # 你的业务逻辑
    return {"success": True}

async def main():
    sdk = WebAASDK()
    
    await sdk.init(InitOptions(
        channel_key="your-channel-key",
        api_base="https://your-agenthub-server",
        skills=[
            SkillDefinition(
                name="my_skill",
                schema={
                    "type": "function",
                    "function": {
                        "name": "my_skill",
                        "description": "执行自定义操作",
                        "parameters": {"type": "object", "properties": {}}
                    }
                },
                execution_mode="sdk",
                execute=my_skill_handler
            )
        ]
    ))
    
    # 发送消息
    emitter = await sdk.run_async(RunOptions(user_input="帮我完成任务"))
    
    @emitter.on("TextMessageDelta")
    def on_delta(event):
        print(event.payload.get("delta", ""), end="")
    
    @emitter.on("done")
    def on_done(event):
        print("\n任务完成")

asyncio.run(main())
```

## 核心功能

- 异步设计（httpx + asyncio）
- Skill 注册与执行
- SSE 事件流处理
- 自动重连与心跳
- 会话管理（Thread）
- 用户身份识别
- L1 Skill 缓存
- 文件上传支持

## API

### `init(options)`

初始化 SDK，获取 Token 并注册 Skills。

### `run(options)` / `run_async(options)`

发送用户消息，返回 EventEmitter 接收 AG-UI 事件流。

### `identify(user)`

标识当前用户身份。

### `register_local_skill(name, execute)`

注册本地 Skill 处理器（不上报后端）。

### `disconnect()`

断开连接，停止当前任务。

## 事件类型

| 事件 | 说明 |
|------|------|
| `RunStarted` | 任务开始 |
| `TextMessageDelta` | 流式文本输出 |
| `ToolCallStart/End` | 工具调用开始/结束 |
| `SkillExecuteInstruction` | SDK 端 Skill 执行指令 |
| `RunFinished` | 任务完成 |
| `Error` | 错误 |

## License

MIT
