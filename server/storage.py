"""Chunk storage, atomic writes, assembly, and hashing.

Every chunk is written atomically (write-to-temp then rename) so that a crash
mid-write can never leave a corrupt chunk marked as received.  Upload metadata
is persisted as a JSON file that survives server restarts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from .models import UploadMetadata, UploadState

logger = logging.getLogger("server.storage")

# Default base directory for all upload data
DEFAULT_DATA_DIR = Path(os.environ.get("UPLOAD_DATA_DIR", "./upload_data"))

# Stall timeout (seconds) – uploads with no activity beyond this are INCOMPLETE
STALL_TIMEOUT = int(os.environ.get("STALL_TIMEOUT", "300"))


def _data_dir() -> Path:
    d = DEFAULT_DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _upload_dir(upload_id: str) -> Path:
    d = _data_dir() / upload_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _chunks_dir(upload_id: str) -> Path:
    d = _upload_dir(upload_id) / "chunks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_path(upload_id: str) -> Path:
    return _upload_dir(upload_id) / "meta.json"


def _assembled_path(upload_id: str) -> Path:
    return _upload_dir(upload_id) / "assembled"


def _report_json_path(upload_id: str) -> Path:
    return _upload_dir(upload_id) / "report.json"


def _report_md_path(upload_id: str) -> Path:
    return _upload_dir(upload_id) / "report.md"


# ── Metadata persistence ─────────────────────────────────────────────────────


def save_metadata(meta: UploadMetadata) -> None:
    """Persist upload metadata to disk (atomic write)."""
    p = _meta_path(meta.upload_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(meta.model_dump_json(indent=2))
        # Atomic rename (Windows: need to remove target first)
        if os.name == "nt" and p.exists():
            p.unlink()
        os.rename(tmp_path, str(p))
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    logger.debug("Saved metadata for %s (state=%s)", meta.upload_id, meta.state.value)


def load_metadata(upload_id: str) -> Optional[UploadMetadata]:
    """Load upload metadata from disk.  Returns None if not found."""
    p = _meta_path(upload_id)
    if not p.exists():
        return None
    data = p.read_text(encoding="utf-8")
    return UploadMetadata.model_validate_json(data)


def list_uploads() -> list[UploadMetadata]:
    """List all uploads on disk."""
    base = _data_dir()
    results: list[UploadMetadata] = []
    if not base.exists():
        return results
    for child in base.iterdir():
        if child.is_dir():
            meta = load_metadata(child.name)
            if meta:
                results.append(meta)
    return results


# ── Chunk I/O ─────────────────────────────────────────────────────────────────


def chunk_path(upload_id: str, chunk_index: int) -> Path:
    return _chunks_dir(upload_id) / f"{chunk_index:06d}.chunk"


def has_chunk(upload_id: str, chunk_index: int) -> bool:
    return chunk_path(upload_id, chunk_index).exists()


def write_chunk_atomic(upload_id: str, chunk_index: int, data: bytes) -> None:
    """Write a chunk atomically: temp file → rename."""
    dest = chunk_path(upload_id, chunk_index)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(data)
        if os.name == "nt" and dest.exists():
            dest.unlink()
        os.rename(tmp_path, str(dest))
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    logger.debug("Wrote chunk %d for upload %s (%d bytes)", chunk_index, upload_id, len(data))


def read_chunk(upload_id: str, chunk_index: int) -> bytes:
    return chunk_path(upload_id, chunk_index).read_bytes()


# ── Hashing ───────────────────────────────────────────────────────────────────


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, buf_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(buf_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ── Assembly ──────────────────────────────────────────────────────────────────


def assemble_chunks(upload_id: str, meta: UploadMetadata) -> Path:
    """Concatenate all chunks into the assembled file.  Returns the path."""
    dest = _assembled_path(upload_id)
    with open(dest, "wb") as out:
        for i in range(meta.chunk_count):
            cp = chunk_path(upload_id, i)
            if not cp.exists():
                raise FileNotFoundError(f"Missing chunk {i} for upload {upload_id}")
            out.write(cp.read_bytes())
    logger.info("Assembled %d chunks into %s", meta.chunk_count, dest)
    return dest


def get_assembled_path(upload_id: str) -> Optional[Path]:
    p = _assembled_path(upload_id)
    return p if p.exists() else None


# ── Reports ───────────────────────────────────────────────────────────────────


def save_report_json(upload_id: str, data: str) -> Path:
    p = _report_json_path(upload_id)
    p.write_text(data, encoding="utf-8")
    return p


def save_report_md(upload_id: str, data: str) -> Path:
    p = _report_md_path(upload_id)
    p.write_text(data, encoding="utf-8")
    return p


def load_report_json(upload_id: str) -> Optional[str]:
    p = _report_json_path(upload_id)
    return p.read_text(encoding="utf-8") if p.exists() else None


def load_report_md(upload_id: str) -> Optional[str]:
    p = _report_md_path(upload_id)
    return p.read_text(encoding="utf-8") if p.exists() else None
