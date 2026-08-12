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
    artifact_path: str = ""
    connection_epoch: str = ""
    snapshot_complete: bool = False
    eligible_for_decision: bool = False


class FeedResynchronizationRequired(RuntimeError):
    """A sequence gap requires reconnect plus a fresh broker snapshot."""


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


def _connect_without_redirects(url: str, **kwargs: Any) -> Any:
    """Build a WebSocket connection that never changes authenticated origin."""

    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise RuntimeError("install the 'live' extra to enable eToro WebSocket") from exc

    class NoRedirectConnect(connect):
        def process_redirect(self, exc: Exception) -> Exception | str:
            # Returning the exception tells websockets to raise it before the
            # application can send Authenticate with broker credentials.
            return exc

    return NoRedirectConnect(url, **kwargs)


class SequenceTracker:
    def __init__(self) -> None:
        self._last: dict[str, int] = {}

    def observe(self, topic: str, sequence: int | None) -> bool:
        if sequence is None:
            return False
        previous = self._last.get(topic)
        self._last[topic] = sequence
        return previous is not None and sequence != previous + 1

    def reset(self) -> None:
        self._last.clear()


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
        persist_raw: Callable[[bytes, datetime], Awaitable[str | None]] | None = None,
        on_transport_heartbeat: Callable[[], object] | None = None,
        url: str = ETORO_WS_URL,
        stale_after_seconds: int = 30,
    ) -> None:
        if url != ETORO_WS_URL:
            raise ValueError("WebSocket egress is pinned to the official eToro endpoint")
        if not instrument_ids:
            raise ValueError("at least one instrument is required")
        self.instrument_ids = {key.upper(): int(value) for key, value in instrument_ids.items()}
        self.allowed_topics = {f"instrument:{value}" for value in self.instrument_ids.values()}
        self.on_event = on_event
        self.persist_raw = persist_raw
        self.on_transport_heartbeat = on_transport_heartbeat
        self.url = url
        self.stale_after_seconds = max(5, stale_after_seconds)
        self.sequence_tracker = SequenceTracker()
        self.last_message_monotonic = 0.0
        self.reconnects = 0
        self.authenticated = False
        self.subscribed = False
        self.pending_operation_ids: dict[str, str] = {}
        self.consumed_operation_ids: dict[str, str] = {}
        self.connection_epoch = str(uuid.uuid4())
        self.snapshot_topics: set[str] = set()
        self.snapshot_complete = False

    def auth_message(self, user_key: str, api_key: str) -> str:
        message_id = str(uuid.uuid4())
        self.pending_operation_ids["Authenticate"] = message_id
        return json.dumps(
            {
                "id": message_id,
                "operation": "Authenticate",
                "data": {"userKey": user_key, "apiKey": api_key},
            },
            separators=(",", ":"),
        )

    def subscribe_message(self) -> str:
        message_id = str(uuid.uuid4())
        self.pending_operation_ids["Subscribe"] = message_id
        return json.dumps(
            {
                "id": message_id,
                "operation": "Subscribe",
                "data": {
                    "topics": [f"instrument:{value}" for value in self.instrument_ids.values()],
                    "snapshot": True,
                },
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _string_alias(value: Mapping[str, Any], keys: tuple[str, ...], *, label: str) -> str:
        raw_values = [value[key] for key in keys if key in value]
        if not raw_values:
            raise ValueError(f"WebSocket {label} is missing")
        if any(not isinstance(raw, str) or not raw.strip() for raw in raw_values):
            raise ValueError(f"WebSocket {label} is invalid")
        normalized = [raw.strip() for raw in raw_values]
        if any(item != normalized[0] for item in normalized[1:]):
            raise ValueError(f"WebSocket {label} aliases disagree")
        return normalized[0]

    @staticmethod
    def _content_alias(value: Mapping[str, Any]) -> Mapping[str, Any]:
        raw_values = [value[key] for key in ("content", "Content") if key in value]
        if not raw_values:
            raise ValueError("WebSocket message content is missing")
        normalized: list[Mapping[str, Any]] = []
        for raw in raw_values:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(parsed, Mapping):
                raise ValueError("WebSocket message content must be an object")
            normalized.append(parsed)
        canonical = [
            json.dumps(dict(item), sort_keys=True, separators=(",", ":")) for item in normalized
        ]
        if any(item != canonical[0] for item in canonical[1:]):
            raise ValueError("WebSocket message content aliases disagree")
        return normalized[0]

    @staticmethod
    def _positive_integer_alias(values: list[Any], *, label: str, allow_zero: bool = False) -> int:
        if not values:
            raise ValueError(f"WebSocket {label} is missing")
        normalized: list[int] = []
        for raw in values:
            if isinstance(raw, bool):
                raise ValueError(f"WebSocket {label} is invalid")
            if isinstance(raw, int):
                parsed = raw
            elif isinstance(raw, str) and raw.isascii() and raw.isdigit():
                parsed = int(raw)
            else:
                raise ValueError(f"WebSocket {label} is invalid")
            if parsed < 0 or (parsed == 0 and not allow_zero):
                raise ValueError(f"WebSocket {label} is invalid")
            normalized.append(parsed)
        if any(item != normalized[0] for item in normalized[1:]):
            raise ValueError(f"WebSocket {label} aliases disagree")
        return normalized[0]

    @classmethod
    def _bind_instrument_identity(cls, topic: str, content: Mapping[str, Any]) -> None:
        instrument_id = cls._positive_integer_alias(
            [
                content[key]
                for key in ("InstrumentID", "instrumentId", "instrumentID")
                if key in content
            ],
            label="instrument identity",
        )
        expected = cls._positive_integer_alias(
            [topic.removeprefix("instrument:")], label="topic instrument identity"
        )
        if instrument_id != expected:
            raise ValueError("WebSocket topic and instrument identity disagree")

    @staticmethod
    def _strict_sequence(envelope: Mapping[str, Any], content: Mapping[str, Any]) -> int | None:
        raw_values = [
            source[key]
            for source in (envelope, content)
            for key in ("sequence", "Sequence")
            if key in source
        ]
        if not raw_values:
            return None
        if any(isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 for raw in raw_values):
            raise ValueError("WebSocket sequence is invalid")
        if any(raw != raw_values[0] for raw in raw_values[1:]):
            raise ValueError("WebSocket sequence aliases disagree")
        return raw_values[0]

    @staticmethod
    def _snapshot_flag(envelope: Mapping[str, Any], content: Mapping[str, Any]) -> bool | None:
        raw_values = [
            source[key]
            for source in (envelope, content)
            for key in ("isSnapshot", "IsSnapshot", "snapshot", "Snapshot")
            if key in source
        ]
        for key in ("type", "Type"):
            if key not in envelope:
                continue
            raw_type = envelope[key]
            if raw_type not in {"Snapshot", "Update"}:
                raise ValueError("WebSocket snapshot type is invalid")
            raw_values.append(raw_type == "Snapshot")
        if not raw_values:
            return None
        if any(type(raw) is not bool for raw in raw_values):
            raise ValueError("WebSocket snapshot marker is invalid")
        if any(raw is not raw_values[0] for raw in raw_values[1:]):
            raise ValueError("WebSocket snapshot marker aliases disagree")
        return raw_values[0]

    @staticmethod
    def _parse_event_time(value: Mapping[str, Any], received: datetime) -> datetime:
        for key in (
            "timestamp",
            "Timestamp",
            "date",
            "Date",
            "eventTime",
            "EventTime",
            "time",
            "Time",
        ):
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
        if payload_bytes == b"\0":
            self.last_message_monotonic = time.monotonic()
            if self.on_transport_heartbeat is not None:
                self.on_transport_heartbeat()
            return
        artifact_path = ""
        if self.persist_raw is not None:
            artifact_path = (await self.persist_raw(payload_bytes, received)) or ""
        value = json.loads(payload_bytes)
        if not isinstance(value, dict):
            raise ValueError("WebSocket payload must be an object")
        if "operation" in value:
            operation = value["operation"]
            if not isinstance(operation, str) or operation not in {
                "Authenticate",
                "Subscribe",
            }:
                raise ValueError("unsupported eToro WebSocket operation")
            if any(key in value for key in ("messages", "topic", "Topic", "content", "Content")):
                raise ValueError("eToro WebSocket control and market payloads are mixed")
            if value.get("success") is not True:
                raise PermissionError(f"eToro WebSocket {operation} failed")
            expected_id = self.pending_operation_ids.get(operation)
            received_id = value.get("id")
            if expected_id is None:
                if self.consumed_operation_ids.get(operation) == received_id:
                    return
                raise PermissionError(f"eToro WebSocket {operation} ACK identity mismatch")
            if received_id != expected_id:
                raise PermissionError(f"eToro WebSocket {operation} ACK identity mismatch")
            if operation == "Subscribe" and not self.authenticated:
                raise PermissionError("eToro WebSocket Subscribe preceded authentication")
            del self.pending_operation_ids[operation]
            self.consumed_operation_ids[operation] = expected_id
            if operation == "Authenticate":
                self.authenticated = True
                self.subscribed = False
            else:
                self.subscribed = True
            self.last_message_monotonic = time.monotonic()
            return
        if not self.subscribed:
            raise PermissionError("eToro WebSocket event preceded completed handshake")

        logical_messages: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
        if "messages" in value:
            messages = value["messages"]
            if not isinstance(messages, list) or not messages:
                raise ValueError("WebSocket messages envelope must be a non-empty list")
            for message in messages:
                if not isinstance(message, Mapping):
                    raise ValueError("WebSocket message envelope item must be an object")
                topic = self._string_alias(message, ("topic", "Topic"), label="message topic")
                content = self._content_alias(message)
                logical_messages.append((topic, content, message))
        else:
            topic = self._string_alias(value, ("topic", "Topic"), label="event topic")
            logical_messages.append((topic, value, value))

        if any(topic not in self.allowed_topics for topic, _, _ in logical_messages):
            raise ValueError("WebSocket event topic is outside the subscription")
        for topic, content, _ in logical_messages:
            self._bind_instrument_identity(topic, content)

        validated_messages = [
            (
                topic,
                content,
                self._strict_sequence(envelope, content),
                self._snapshot_flag(envelope, content),
            )
            for topic, content, envelope in logical_messages
        ]
        raw_hash = hashlib.sha256(payload_bytes).hexdigest()
        self.last_message_monotonic = time.monotonic()
        observed = [
            (topic, content, sequence, snapshot, self.sequence_tracker.observe(topic, sequence))
            for topic, content, sequence, snapshot in validated_messages
        ]
        gap_detected = any(item[4] for item in observed)
        if gap_detected:
            self.snapshot_topics.clear()
            self.snapshot_complete = False
        for topic, content, sequence, snapshot, gap in observed:
            if snapshot is True and not gap_detected:
                self.snapshot_topics.add(topic)
                self.snapshot_complete = self.snapshot_topics == self.allowed_topics
            event_time = self._parse_event_time(content, received)
            await self.on_event(
                WebSocketEvent(
                    topic,
                    content,
                    event_time,
                    received,
                    raw_hash,
                    sequence,
                    gap,
                    artifact_path,
                    self.connection_epoch,
                    self.snapshot_complete,
                    self.snapshot_complete and not gap_detected,
                )
            )
        if gap_detected:
            raise FeedResynchronizationRequired("WebSocket sequence gap requires resubscription")

    async def _complete_handshake(self, socket: Any, user_key: str, api_key: str) -> None:
        async with asyncio.timeout(self.stale_after_seconds):
            await socket.send(self.auth_message(user_key, api_key))
            while not self.authenticated:
                await self._handle(await socket.recv())
            await socket.send(self.subscribe_message())
            while not self.subscribed:
                await self._handle(await socket.recv())

    async def run_forever(self) -> None:
        user_key = CredentialReader.get("ETORO_USER_KEY")
        api_key = CredentialReader.get("ETORO_API_KEY")
        backoff = 1.0
        while True:
            try:
                async with _connect_without_redirects(
                    self.url,
                    open_timeout=15,
                    close_timeout=10,
                    max_size=1_000_000,
                    ping_interval=15,
                    ping_timeout=15,
                ) as socket:
                    self.sequence_tracker.reset()
                    self.connection_epoch = str(uuid.uuid4())
                    self.snapshot_topics.clear()
                    self.snapshot_complete = False
                    self.authenticated = False
                    self.subscribed = False
                    self.pending_operation_ids.clear()
                    self.consumed_operation_ids.clear()
                    await self._complete_handshake(socket, user_key, api_key)
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
