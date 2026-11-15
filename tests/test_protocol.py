"""Pytest tests for the upload protocol (endpoints, state machine, hashing)."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

# Set test data dir before importing server modules
_test_dir = tempfile.mkdtemp(prefix="upload_test_")
os.environ["UPLOAD_DATA_DIR"] = _test_dir

from server.main import app  # noqa: E402
from server.models import UploadState  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up upload data between tests."""
    yield
    # Remove all subdirectories in the test data dir
    for entry in os.listdir(_test_dir):
        path = os.path.join(_test_dir, entry)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def _make_file_bytes(size: int = 1024, seed: int = 42) -> bytes:
    """Generate deterministic pseudo-random bytes."""
    import random
    rng = random.Random(seed)
    return bytes(rng.getrandbits(8) for _ in range(size))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _chunk_sha256(data: bytes, chunk_size: int, idx: int) -> str:
    offset = idx * chunk_size
    chunk = data[offset:offset + chunk_size]
    return _sha256(chunk)


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestHealth:
    def test_health(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestCreateUpload:
    def test_create(self):
        data = _make_file_bytes(2048)
        resp = client.post("/uploads", json={
            "filename": "test.mp4",
            "total_size": len(data),
            "chunk_size": 1024,
            "chunk_count": 2,
            "sha256": _sha256(data),
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "upload_id" in body

    def test_get_status_after_create(self):
        data = _make_file_bytes(1024)
        resp = client.post("/uploads", json={
            "filename": "test.mp4",
            "total_size": len(data),
            "chunk_size": 1024,
            "chunk_count": 1,
            "sha256": _sha256(data),
        })
        uid = resp.json()["upload_id"]
        resp2 = client.get(f"/uploads/{uid}")
        assert resp2.status_code == 200
        assert resp2.json()["state"] == "CREATED"
        assert resp2.json()["received_chunks"] == []


class TestChunkUpload:
    def test_upload_single_chunk(self):
        data = _make_file_bytes(512)
        file_hash = _sha256(data)
        resp = client.post("/uploads", json={
            "filename": "small.bin",
            "total_size": len(data),
            "chunk_size": 512,
            "chunk_count": 1,
            "sha256": file_hash,
        })
        uid = resp.json()["upload_id"]

        chunk_hash = _sha256(data)
        resp2 = client.put(
            f"/uploads/{uid}/chunks/0",
            content=data,
            headers={"X-Chunk-SHA256": chunk_hash},
        )
        assert resp2.status_code == 200

        # Check status
        status = client.get(f"/uploads/{uid}").json()
        assert 0 in status["received_chunks"]
        assert status["state"] == "UPLOADING"

    def test_chunk_hash_mismatch_rejected(self):
        data = _make_file_bytes(512)
        resp = client.post("/uploads", json={
            "filename": "small.bin",
            "total_size": len(data),
            "chunk_size": 512,
            "chunk_count": 1,
            "sha256": _sha256(data),
        })
        uid = resp.json()["upload_id"]

        resp2 = client.put(
            f"/uploads/{uid}/chunks/0",
            content=data,
            headers={"X-Chunk-SHA256": "0000000000000000000000000000000000000000000000000000000000000000"},
        )
        assert resp2.status_code == 400
        assert "mismatch" in resp2.json()["detail"].lower()

    def test_chunk_index_out_of_range(self):
        data = _make_file_bytes(512)
        resp = client.post("/uploads", json={
            "filename": "small.bin",
            "total_size": len(data),
            "chunk_size": 512,
            "chunk_count": 1,
            "sha256": _sha256(data),
        })
        uid = resp.json()["upload_id"]

        resp2 = client.put(
            f"/uploads/{uid}/chunks/5",
            content=data,
            headers={"X-Chunk-SHA256": _sha256(data)},
        )
        assert resp2.status_code == 400

    def test_idempotent_reupload(self):
        data = _make_file_bytes(512)
        resp = client.post("/uploads", json={
            "filename": "small.bin",
            "total_size": len(data),
            "chunk_size": 512,
            "chunk_count": 1,
            "sha256": _sha256(data),
        })
        uid = resp.json()["upload_id"]
        chunk_hash = _sha256(data)

        # Upload twice
        client.put(f"/uploads/{uid}/chunks/0", content=data, headers={"X-Chunk-SHA256": chunk_hash})
        resp2 = client.put(f"/uploads/{uid}/chunks/0", content=data, headers={"X-Chunk-SHA256": chunk_hash})
        assert resp2.status_code == 200
        assert resp2.json().get("duplicate") is True


class TestComplete:
    def test_complete_success(self):
        data = _make_file_bytes(1024)
        file_hash = _sha256(data)
        chunk_size = 512

        resp = client.post("/uploads", json={
            "filename": "test.bin",
            "total_size": len(data),
            "chunk_size": chunk_size,
            "chunk_count": 2,
            "sha256": file_hash,
        })
        uid = resp.json()["upload_id"]

        for i in range(2):
            chunk = data[i * chunk_size:(i + 1) * chunk_size]
            client.put(
                f"/uploads/{uid}/chunks/{i}",
                content=chunk,
                headers={"X-Chunk-SHA256": _sha256(chunk)},
            )

        resp2 = client.post(f"/uploads/{uid}/complete")
        assert resp2.status_code == 200
        body = resp2.json()
        assert body["state"] in ("VERIFIED", "ANALYZED")
        assert body["sha256_actual"] == file_hash

    def test_complete_missing_chunks(self):
        data = _make_file_bytes(1024)
        resp = client.post("/uploads", json={
            "filename": "test.bin",
            "total_size": len(data),
            "chunk_size": 512,
            "chunk_count": 2,
            "sha256": _sha256(data),
        })
        uid = resp.json()["upload_id"]

        # Only upload chunk 0
        chunk = data[:512]
        client.put(
            f"/uploads/{uid}/chunks/0",
            content=chunk,
            headers={"X-Chunk-SHA256": _sha256(chunk)},
        )

        resp2 = client.post(f"/uploads/{uid}/complete")
        body = resp2.json()
        assert body["state"] == "FAILED"
        assert "missing" in body.get("fail_reason", "").lower()

    def test_complete_hash_mismatch(self):
        data = _make_file_bytes(512)
        wrong_hash = "a" * 64  # definitely wrong

        resp = client.post("/uploads", json={
            "filename": "test.bin",
            "total_size": len(data),
            "chunk_size": 512,
            "chunk_count": 1,
            "sha256": wrong_hash,
        })
        uid = resp.json()["upload_id"]

        client.put(
            f"/uploads/{uid}/chunks/0",
            content=data,
            headers={"X-Chunk-SHA256": _sha256(data)},
        )

        resp2 = client.post(f"/uploads/{uid}/complete")
        body = resp2.json()
        assert body["state"] == "FAILED"
        assert "mismatch" in body.get("fail_reason", "").lower()


class TestReport:
    def test_report_before_analysis(self):
        data = _make_file_bytes(512)
        resp = client.post("/uploads", json={
            "filename": "test.bin",
            "total_size": len(data),
            "chunk_size": 512,
            "chunk_count": 1,
            "sha256": _sha256(data),
        })
        uid = resp.json()["upload_id"]
        resp2 = client.get(f"/uploads/{uid}/report")
        assert resp2.status_code == 200

    def test_report_md_format(self):
        data = _make_file_bytes(512)
        resp = client.post("/uploads", json={
            "filename": "test.bin",
            "total_size": len(data),
            "chunk_size": 512,
            "chunk_count": 1,
            "sha256": _sha256(data),
        })
        uid = resp.json()["upload_id"]
        resp2 = client.get(f"/uploads/{uid}/report?format=md")
        assert resp2.status_code == 200
        assert "Integrity Report" in resp2.text


class TestNotFound:
    def test_missing_upload(self):
        resp = client.get("/uploads/nonexistent")
        assert resp.status_code == 404
