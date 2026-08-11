from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ETORO_WS_URL = "wss://ws.etoro.com/ws"


@dataclass(frozen=True)
class WebSocketEvent:
    topic: str
    payload: Mapping[str, Any]
    event_time: datetime
    received_at: datetime
    raw_hash: str
    sequence: int | None
    gap_detected: bool


class CredentialReader:
    @staticmethod
    def get(name: str) -> str:
        file_path = os.getenv(f"{name}_FILE")
        direct = os.getenv(name)
        if file_path and direct:
            raise RuntimeError(f"{name} and {name}_FILE cannot both be set")
        if file_path:
            value = Path(file_path).read_text(encoding="utf-8").strip()
        else:
            value = (direct or "").strip()
        if not value:
            raise RuntimeError(f"{name} is unavailable")
        return value


class SequenceTracker:
    def __init__(self) -> None:
        self._last: dict[str, int] = {}

    def observe(self, topic: str, sequence: int | None) -> bool:
        if sequence is None:
            return False
        previous = self._last.get(topic)
        self._last[topic] = sequence
        return previous is not None and sequence != previous + 1


class EtoroWebSocketCollector:
    """Official eToro WebSocket collector with reconnect/gap/stale instrumentation.

    Network transport is optional at import time. Install project extra `live` to run it.
    Credentials are read only when `run_forever` starts.
    """

    def __init__(
        self,
        instrument_ids: Mapping[str, int],
        *,
        on_event: Callable[[WebSocketEvent], Awaitable[None]],
        persist_raw: Callable[[bytes, datetime], Awaitable[None]] | None = None,
        url: str = ETORO_WS_URL,
        stale_after_seconds: int = 30,
    ) -> None:
        if url != ETORO_WS_URL:
            raise ValueError("WebSocket egress is pinned to the official eToro endpoint")
        if not instrument_ids:
            raise ValueError("at least one instrument is required")
        self.instrument_ids = {key.upper(): int(value) for key, value in instrument_ids.items()}
        self.on_event = on_event
        self.persist_raw = persist_raw
        self.url = url
        self.stale_after_seconds = max(5, stale_after_seconds)
        self.sequence_tracker = SequenceTracker()
        self.last_message_monotonic = 0.0
        self.reconnects = 0

    def auth_message(self, user_key: str, api_key: str) -> str:
        return json.dumps(
            {
                "id": str(uuid.uuid4()),
                "operation": "Authenticate",
                "data": {"userKey": user_key, "apiKey": api_key},
            },
            separators=(",", ":"),
        )

    def subscribe_message(self) -> str:
        return json.dumps(
            {
                "id": str(uuid.uuid4()),
                "operation": "Subscribe",
                "data": {
                    "topics": [f"instrument:{value}" for value in self.instrument_ids.values()],
                    "snapshot": True,
                },
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _parse_event_time(value: Mapping[str, Any], received: datetime) -> datetime:
        for key in ("timestamp", "date", "eventTime", "time"):
            raw = value.get(key)
            if raw is None:
                continue
            try:
                if isinstance(raw, (int, float)):
                    seconds = float(raw) / 1000 if float(raw) > 10_000_000_000 else float(raw)
                    return datetime.fromtimestamp(seconds, UTC)
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    return parsed.astimezone(UTC)
            except (ValueError, TypeError, OSError):
                continue
        return received

    async def _handle(self, raw: str | bytes) -> None:
        received = datetime.now(UTC)
        payload_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(payload_bytes) > 1_000_000:
            raise ValueError("WebSocket message exceeds one-megabyte limit")
        if self.persist_raw is not None:
            await self.persist_raw(payload_bytes, received)
        value = json.loads(payload_bytes)
        if not isinstance(value, dict):
            raise ValueError("WebSocket payload must be an object")
        topic = str(value.get("topic") or value.get("Topic") or "control")
        sequence_raw = value.get("sequence", value.get("Sequence"))
        sequence = (
            int(sequence_raw)
            if isinstance(sequence_raw, int) and not isinstance(sequence_raw, bool)
            else None
        )
        gap = self.sequence_tracker.observe(topic, sequence)
        event_time = self._parse_event_time(value, received)
        raw_hash = hashlib.sha256(payload_bytes).hexdigest()
        self.last_message_monotonic = time.monotonic()
        await self.on_event(
            WebSocketEvent(topic, value, event_time, received, raw_hash, sequence, gap)
        )

    async def run_forever(self) -> None:
        try:
            from websockets.asyncio.client import connect  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("install the 'live' extra to enable eToro WebSocket") from exc
        user_key = CredentialReader.get("ETORO_USER_KEY")
        api_key = CredentialReader.get("ETORO_API_KEY")
        backoff = 1.0
        while True:
            try:
                async with connect(
                    self.url,
                    open_timeout=15,
                    close_timeout=10,
                    max_size=1_000_000,
                    ping_interval=15,
                    ping_timeout=15,
                ) as socket:
                    await socket.send(self.auth_message(user_key, api_key))
                    await socket.send(self.subscribe_message())
                    self.last_message_monotonic = time.monotonic()
                    backoff = 1.0
                    while True:
                        try:
                            raw = await asyncio.wait_for(
                                socket.recv(), timeout=self.stale_after_seconds
                            )
                        except TimeoutError as exc:
                            raise TimeoutError("eToro WebSocket feed became stale") from exc
                        await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.reconnects += 1
                # Reconnect jitter is deliberately non-cryptographic.
                await asyncio.sleep(
                    backoff + random.random() * min(1.0, backoff / 4)  # nosec B311
                )
                backoff = min(60.0, backoff * 2)
