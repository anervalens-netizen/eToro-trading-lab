from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class DataArtifact:
    relative_path: str
    sha256: str
    bytes: int
    rows: int | None = None


@dataclass(frozen=True)
class DataSnapshotManifest:
    snapshot_id: str
    created_at: str
    source: str
    source_version: str
    license_note: str
    symbol_mapping_version: str
    calendar_version: str
    normalization_version: str
    artifacts: tuple[DataArtifact, ...]
    metadata: Mapping[str, object]

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=str)


class ImmutableDataCatalog:
    """Content-addressed raw/normalized research catalog.

    Files are never modified in place. A snapshot manifest is the reproducibility key
    consumed by research/backtest code: data_snapshot_id + code_sha + config_hash.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "objects").mkdir(exist_ok=True)
        (self.root / "manifests").mkdir(exist_ok=True)

    @staticmethod
    def sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def ingest_bytes(self, data: bytes, *, suffix: str = ".bin") -> DataArtifact:
        digest = hashlib.sha256(data).hexdigest()
        target = self.root / "objects" / f"{digest}{suffix}"
        if target.exists():
            if self.sha256_file(target) != digest:
                raise RuntimeError("content-addressed object hash mismatch")
        else:
            temporary = target.with_suffix(target.suffix + ".partial")
            temporary.write_bytes(data)
            if self.sha256_file(temporary) != digest:
                temporary.unlink(missing_ok=True)
                raise RuntimeError("object changed while being written")
            temporary.replace(target)
        return DataArtifact(str(target.relative_to(self.root)), digest, len(data), None)

    def ingest_file(self, path: str | Path, *, rows: int | None = None) -> DataArtifact:
        source = Path(path)
        data = source.read_bytes()
        artifact = self.ingest_bytes(data, suffix=source.suffix or ".bin")
        return DataArtifact(artifact.relative_path, artifact.sha256, artifact.bytes, rows)

    def create_snapshot(
        self,
        artifacts: Iterable[DataArtifact],
        *,
        source: str,
        source_version: str,
        license_note: str,
        symbol_mapping_version: str,
        calendar_version: str,
        normalization_version: str,
        metadata: Mapping[str, object] | None = None,
        created_at: datetime | None = None,
    ) -> DataSnapshotManifest:
        created = (created_at or datetime.now(UTC)).astimezone(UTC)
        ordered = tuple(sorted(artifacts, key=lambda item: (item.relative_path, item.sha256)))
        seed = json.dumps(
            {
                "artifacts": [asdict(item) for item in ordered],
                "source": source,
                "source_version": source_version,
                "symbol_mapping_version": symbol_mapping_version,
                "calendar_version": calendar_version,
                "normalization_version": normalization_version,
                "metadata": dict(metadata or {}),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        snapshot_id = f"data-{hashlib.sha256(seed.encode()).hexdigest()[:32]}"
        manifest = DataSnapshotManifest(
            snapshot_id,
            created.isoformat(),
            source,
            source_version,
            license_note,
            symbol_mapping_version,
            calendar_version,
            normalization_version,
            ordered,
            dict(metadata or {}),
        )
        target = self.root / "manifests" / f"{snapshot_id}.json"
        body = manifest.canonical() + "\n"
        if target.exists() and target.read_text(encoding="utf-8") != body:
            raise RuntimeError("immutable manifest id collision")
        target.write_text(body, encoding="utf-8")
        return manifest

    def verify(self, snapshot_id: str) -> bool:
        target = self.root / "manifests" / f"{snapshot_id}.json"
        if not target.is_file():
            return False
        value = json.loads(target.read_text(encoding="utf-8"))
        if value.get("snapshot_id") != snapshot_id:
            return False
        for artifact in value.get("artifacts", []):
            path = self.root / artifact["relative_path"]
            if not path.is_file() or self.sha256_file(path) != artifact["sha256"]:
                return False
        return True
