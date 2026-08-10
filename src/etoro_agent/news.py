from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlparse

from .audit import AuditLog


@dataclass(frozen=True)
class NewsSource:
    source_id: str
    publisher: str
    url: str
    default_symbol: str | None = None


NEWS_SOURCES: tuple[NewsSource, ...] = (
    NewsSource("eia-petroleum", "U.S. EIA", "https://www.eia.gov/petroleum/supply/weekly/", "OIL"),
    NewsSource("eia-natural-gas", "U.S. EIA", "https://ir.eia.gov/ngs/ngs.html", "NATGAS"),
    NewsSource("opec", "OPEC", "https://www.opec.org/", "OIL"),
    NewsSource("white-house", "The White House", "https://www.whitehouse.gov/news/"),
    NewsSource("us-treasury", "U.S. Treasury", "https://home.treasury.gov/news/press-releases"),
    NewsSource("nhc", "NOAA National Hurricane Center", "https://www.nhc.noaa.gov/"),
)

_ALLOWED_HOSTS = frozenset(urlparse(source.url).hostname for source in NEWS_SOURCES)
_OIL_TERMS = (
    "oil", "crude", "petroleum", "opec", "barrel", "refinery",
    "hormuz", "spr ", "strategic petroleum",
)
_GAS_TERMS = (
    "natural gas", "natgas", "lng", "gas storage", "henry hub",
    "freezing", "heat wave",
)
_CATALYST_TERMS = (
    "inventory", "inventories", "storage", "production", "output", "quota",
    "cut", "increase", "sanction", "attack", "strike", "disruption",
    "ceasefire", "blockade", "release", "export", "terminal", "pipeline",
    "hurricane", "storm", "freeze", "heat wave", "war", "tariff",
)
_BULLISH_TERMS = (
    "production cut", "output cut", "inventory draw", "inventories fell",
    "storage withdrawal", "sanction", "attack", "strike", "disruption",
    "blockade", "pipeline outage", "hurricane", "freeze", "export increase",
)
_BEARISH_TERMS = (
    "production increase", "output increase", "inventory build", "inventories rose",
    "storage build", "ceasefire", "strategic petroleum reserve release",
    "pipeline restored", "lng outage", "mild weather",
)


class _HeadlineParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capture = False
        self._href = ""
        self._parts: list[str] = []
        self.items: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"a", "h1", "h2", "h3"}:
            self._capture = True
            self._href = dict(attrs).get("href") or ""
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture and tag in {"a", "h1", "h2", "h3"}:
            text = " ".join("".join(self._parts).split())
            if 24 <= len(text) <= 500:
                self.items.append((html.unescape(text), self._href))
            self._capture = False
            self._href = ""
            self._parts = []


def _fetch(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise PermissionError("news source is outside the fixed HTTPS allowlist")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "etoro-demo-research-news-scanner/0.3",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("news source returned an unsupported content type")
        payload = response.read(2_000_001)
    if len(payload) > 2_000_000:
        raise ValueError("news source exceeded the two-megabyte limit")
    return payload.decode("utf-8", errors="replace")


def classify_headline(source: NewsSource, headline: str) -> dict[str, object] | None:
    normalized = " ".join(headline.lower().split())
    symbols: list[str] = []
    if source.default_symbol == "OIL" or any(term in normalized for term in _OIL_TERMS):
        symbols.append("OIL")
    if source.default_symbol == "NATGAS" or any(term in normalized for term in _GAS_TERMS):
        symbols.append("NATGAS")
    if source.source_id == "nhc" and any(term in normalized for term in ("hurricane", "storm")):
        symbols = ["OIL", "NATGAS"]
    if not symbols or not any(term in normalized for term in _CATALYST_TERMS):
        return None
    bullish = sum(term in normalized for term in _BULLISH_TERMS)
    bearish = sum(term in normalized for term in _BEARISH_TERMS)
    direction = "bullish" if bullish > bearish else "bearish" if bearish > bullish else "ambiguous"
    matched = sorted(
        term for term in _CATALYST_TERMS + _BULLISH_TERMS + _BEARISH_TERMS
        if term in normalized
    )
    return {
        "symbols": symbols,
        "direction_hint": direction,
        "matched_terms": matched[:12],
        "classifier_version": "commodity-keywords-v1",
    }


class CommodityNewsStore:
    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        self.audit.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS commodity_news_sources(
                source_id TEXT PRIMARY KEY,
                page_hash TEXT NOT NULL,
                bootstrapped_at TEXT NOT NULL,
                last_checked_at TEXT NOT NULL,
                last_success_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS commodity_news_seen(
                item_hash TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                first_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS commodity_news_events(
                event_hash TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                publisher TEXT NOT NULL,
                headline TEXT NOT NULL,
                url TEXT NOT NULL,
                symbols_json TEXT NOT NULL,
                direction_hint TEXT NOT NULL,
                matched_terms_json TEXT NOT NULL,
                classifier_version TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_commodity_news_events_expiry
            ON commodity_news_events(expires_at, observed_at);
            """
        )
        self.audit.db.commit()

    def source_bootstrapped(self, source_id: str) -> bool:
        return self.audit.db.execute(
            "SELECT 1 FROM commodity_news_sources WHERE source_id=?", (source_id,)
        ).fetchone() is not None

    def record_source(
        self,
        source: NewsSource,
        page_hash: str,
        item_hashes: tuple[str, ...],
        observed_at: datetime,
    ) -> bool:
        timestamp = observed_at.astimezone(timezone.utc).isoformat()
        bootstrap = not self.source_bootstrapped(source.source_id)
        self.audit.db.execute(
            """
            INSERT INTO commodity_news_sources(
                source_id,page_hash,bootstrapped_at,last_checked_at,last_success_at
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
                page_hash=excluded.page_hash,
                last_checked_at=excluded.last_checked_at,
                last_success_at=excluded.last_success_at
            """,
            (source.source_id, page_hash, timestamp, timestamp, timestamp),
        )
        self.audit.db.executemany(
            "INSERT OR IGNORE INTO commodity_news_seen(item_hash,source_id,first_seen_at) VALUES(?,?,?)",
            ((item_hash, source.source_id, timestamp) for item_hash in item_hashes),
        )
        self.audit.db.commit()
        return bootstrap

    def seen(self, item_hash: str) -> bool:
        return self.audit.db.execute(
            "SELECT 1 FROM commodity_news_seen WHERE item_hash=?", (item_hash,)
        ).fetchone() is not None

    def append_event(
        self,
        source: NewsSource,
        headline: str,
        url: str,
        classification: dict[str, object],
        observed_at: datetime,
    ) -> bool:
        canonical = json.dumps(
            {
                "source_id": source.source_id,
                "headline": headline,
                "url": url,
                "classification": classification,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        event_hash = hashlib.sha256(canonical.encode()).hexdigest()
        expires_at = observed_at + timedelta(hours=6)
        cursor = self.audit.db.execute(
            """
            INSERT OR IGNORE INTO commodity_news_events(
                event_hash,source_id,publisher,headline,url,symbols_json,
                direction_hint,matched_terms_json,classifier_version,observed_at,expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_hash,
                source.source_id,
                source.publisher,
                headline,
                url,
                json.dumps(classification["symbols"], separators=(",", ":")),
                str(classification["direction_hint"]),
                json.dumps(classification["matched_terms"], separators=(",", ":")),
                str(classification["classifier_version"]),
                observed_at.astimezone(timezone.utc).isoformat(),
                expires_at.astimezone(timezone.utc).isoformat(),
            ),
        )
        created = cursor.rowcount == 1
        self.audit.db.commit()
        if created:
            self.audit.append(
                "commodity_news_event",
                {
                    "event_hash": event_hash,
                    "source_id": source.source_id,
                    "publisher": source.publisher,
                    "headline": headline,
                    "url": url,
                    **classification,
                    "expires_at": expires_at.isoformat(),
                    "research_only": True,
                },
            )
        return created

    def active_events(self, observed_at: datetime, limit: int = 20) -> tuple[dict[str, object], ...]:
        rows = self.audit.db.execute(
            """
            SELECT event_hash,source_id,publisher,headline,url,symbols_json,
                   direction_hint,matched_terms_json,classifier_version,observed_at,expires_at
            FROM commodity_news_events
            WHERE expires_at>? ORDER BY observed_at DESC LIMIT ?
            """,
            (observed_at.astimezone(timezone.utc).isoformat(), max(1, min(limit, 50))),
        ).fetchall()
        return tuple(
            {
                "event_hash": str(row[0]),
                "source_id": str(row[1]),
                "publisher": str(row[2]),
                "headline": str(row[3]),
                "url": str(row[4]),
                "symbols": json.loads(str(row[5])),
                "direction_hint": str(row[6]),
                "matched_terms": json.loads(str(row[7])),
                "classifier_version": str(row[8]),
                "observed_at": str(row[9]),
                "expires_at": str(row[10]),
            }
            for row in rows
        )


class CommodityNewsScanner:
    def __init__(
        self,
        audit: AuditLog,
        *,
        sources: tuple[NewsSource, ...] = NEWS_SOURCES,
        fetcher: Callable[[str], str] = _fetch,
    ) -> None:
        self.audit = audit
        self.store = CommodityNewsStore(audit)
        self.sources = sources
        self.fetcher = fetcher

    @staticmethod
    def _headlines(source: NewsSource, page: str) -> tuple[tuple[str, str, str], ...]:
        parser = _HeadlineParser()
        parser.feed(page)
        unique: dict[str, tuple[str, str, str]] = {}
        for headline, href in parser.items:
            normalized = " ".join(headline.split())
            item_url = urljoin(source.url, href) if href else source.url
            item_hash = hashlib.sha256(
                f"{source.source_id}\n{normalized}\n{item_url}".encode()
            ).hexdigest()
            unique[item_hash] = (item_hash, normalized, item_url)
        return tuple(unique.values())

    def scan_once(self, observed_at: datetime | None = None) -> dict[str, int]:
        now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        successful = created = failed = 0
        for source in self.sources:
            try:
                page = self.fetcher(source.url)
                page_hash = hashlib.sha256(page.encode()).hexdigest()
                items = self._headlines(source, page)
                unseen = tuple(item for item in items if not self.store.seen(item[0]))
                bootstrap = self.store.record_source(
                    source, page_hash, tuple(item[0] for item in items), now
                )
                successful += 1
                if bootstrap:
                    continue
                for _, headline, url in unseen:
                    classification = classify_headline(source, headline)
                    if classification and self.store.append_event(
                        source, headline, url, classification, now
                    ):
                        created += 1
            except Exception as exc:
                failed += 1
                self.audit.append(
                    "commodity_news_source_error",
                    {"source_id": source.source_id, "error_type": type(exc).__name__},
                )
        status = "healthy" if successful else "error"
        self.audit.heartbeat(
            "commodity-news-scanner",
            status,
            {
                "sources": len(self.sources),
                "successful": successful,
                "failed": failed,
                "new_events": created,
                "real_money": False,
            },
        )
        return {"successful": successful, "failed": failed, "new_events": created}

    def run_forever(self, interval_seconds: int = 120) -> None:
        if interval_seconds < 30:
            raise ValueError("news scan interval must be at least 30 seconds")
        while True:
            self.scan_once()
            time.sleep(interval_seconds)
