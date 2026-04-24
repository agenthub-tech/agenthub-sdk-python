"""SSE (Server-Sent Events) stream parser."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional

from webaa_sdk.models import AGUIEvent


@dataclass
class ParseResult:
    """Result of parsing an SSE stream."""

    received_terminal: bool


def parse_sse_line_iter(
    line_iter,
    on_event: Callable[[AGUIEvent], None],
    on_data: Optional[Callable[[], None]] = None,
) -> ParseResult:
    """
    Parse an SSE line iterator into AGUIEvent objects.

    Args:
        line_iter: iterable of text lines (from httpx streaming)
        on_event: callback for each parsed AGUIEvent
        on_data: called whenever data is received (for heartbeat reset); may be None

    Returns:
        ParseResult indicating whether a terminal event was received.
    """
    received_terminal = False
    data_buffer = ""

    for line in line_iter:
        if on_data is not None:
            on_data()

        if line == "" or line == "\n":
            # Blank line = end of event
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
                    on_event(event)
                except (json.JSONDecodeError, KeyError):
                    pass  # skip malformed JSON
                data_buffer = ""
        elif line.startswith("data: "):
            data_buffer += line[6:].rstrip("\n")
        # Ignore other SSE fields (event:, id:, retry:, comments)

    # Handle trailing data without final blank line
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
            on_event(event)
        except (json.JSONDecodeError, KeyError):
            pass

    return ParseResult(received_terminal=received_terminal)
