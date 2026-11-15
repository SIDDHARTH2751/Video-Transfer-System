"""Resilient chunked uploader with retry, backoff, resume, and manifest support.

Usage:
    python -m client.upload <video_file> [--server URL] [--chunk-size BYTES]
        [--resume-id UPLOAD_ID] [--manifest] [--max-retries N] [--timeout S]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [client] %(levelname)s  %(message)s",
)
logger = logging.getLogger("client.upload")

DEFAULT_SERVER = os.environ.get("UPLOAD_SERVER", "http://127.0.0.1:8000")
DEFAULT_CHUNK_SIZE = 256 * 1024  # 256 KiB
DEFAULT_MAX_RETRIES = 20
DEFAULT_TIMEOUT = 30.0  # seconds per request
STATE_FILE = ".upload_state.json"


# ── Hashing ───────────────────────────────────────────────────────────────────

def sha256_file(path: Path, buf_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            data = f.read(buf_size)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Manifest generation ──────────────────────────────────────────────────────

def generate_manifest(path: Path) -> dict[str, Any]:
    """Run ffprobe locally and build a capture manifest."""
    manifest: dict[str, Any] = {
        "source_sha256": sha256_file(path),
        "source_size": path.stat().st_size,
        "source_filename": path.name,
    }

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_format", "-show_streams",
                "-of", "json",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        info = json.loads(result.stdout)
        stream = info.get("streams", [{}])[0]
        fmt = info.get("format", {})

        manifest["expected_duration"] = float(fmt.get("duration", 0))

        r_frame_rate = stream.get("r_frame_rate", "30/1")
        parts = r_frame_rate.split("/")
        if len(parts) == 2 and float(parts[1]) > 0:
            manifest["declared_fps"] = float(parts[0]) / float(parts[1])
        else:
            manifest["declared_fps"] = float(r_frame_rate) if r_frame_rate else 30.0

        nb_frames = stream.get("nb_frames")
        if nb_frames and nb_frames != "N/A":
            manifest["expected_frame_count"] = int(nb_frames)
        else:
            manifest["expected_frame_count"] = round(
                manifest["expected_duration"] * manifest["declared_fps"]
            )

    except Exception as exc:
        logger.warning("Manifest ffprobe failed: %s — using defaults", exc)
        manifest.setdefault("expected_duration", 30.0)
        manifest.setdefault("declared_fps", 30.0)
        manifest.setdefault("expected_frame_count", 900)

    return manifest


# ── Retry helpers ─────────────────────────────────────────────────────────────

def _backoff_delay(attempt: int, base: float = 0.5, cap: float = 30.0) -> float:
    """Exponential backoff with full jitter."""
    delay = min(base * (2 ** attempt), cap)
    return random.uniform(0, delay)


# ── Local state for cross-restart resume ──────────────────────────────────────

def _save_local_state(upload_id: str, server: str, filepath: str) -> None:
    state = {"upload_id": upload_id, "server": server, "file": filepath}
    Path(STATE_FILE).write_text(json.dumps(state))


def _load_local_state() -> Optional[dict]:
    p = Path(STATE_FILE)
    if p.exists():
        return json.loads(p.read_text())
    return None


def _clear_local_state() -> None:
    p = Path(STATE_FILE)
    if p.exists():
        p.unlink()


# ── Main upload logic ─────────────────────────────────────────────────────────

def upload(
    filepath: str,
    server: str = DEFAULT_SERVER,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
    resume_id: Optional[str] = None,
    send_manifest: bool = False,
) -> dict[str, Any]:
    """Upload a file with chunked, resumable, verified protocol.

    Returns a dict with final status information.
    """
    path = Path(filepath)
    if not path.exists():
        logger.error("File not found: %s", filepath)
        return {"status": "FAILED", "reason": "File not found"}

    total_size = path.stat().st_size
    chunk_count = math.ceil(total_size / chunk_size)

    logger.info("File: %s (%d bytes, %d chunks of %d)", path.name, total_size, chunk_count, chunk_size)

    # Compute whole-file hash
    logger.info("Computing whole-file SHA-256...")
    file_hash = sha256_file(path)
    logger.info("SHA-256: %s", file_hash)

    client = httpx.Client(timeout=timeout)
    upload_id = resume_id

    # ── Step 1: Create or resume ──────────────────────────────────────────
    if upload_id:
        logger.info("Resuming upload %s", upload_id)
    else:
        # Build request body
        create_body: dict[str, Any] = {
            "filename": path.name,
            "total_size": total_size,
            "chunk_size": chunk_size,
            "chunk_count": chunk_count,
            "sha256": file_hash,
        }
        if send_manifest:
            logger.info("Generating capture manifest...")
            create_body["manifest"] = generate_manifest(path)

        for attempt in range(max_retries):
            try:
                resp = client.post(f"{server}/uploads", json=create_body)
                resp.raise_for_status()
                upload_id = resp.json()["upload_id"]
                logger.info("Created upload: %s", upload_id)
                _save_local_state(upload_id, server, filepath)
                break
            except Exception as exc:
                delay = _backoff_delay(attempt)
                logger.warning("Create failed (attempt %d): %s — retrying in %.1fs", attempt + 1, exc, delay)
                time.sleep(delay)
        else:
            return {"status": "FAILED", "reason": "Could not create upload after retries"}

    # ── Step 2: Get current state (for resume) ────────────────────────────
    received_chunks: set[int] = set()
    for attempt in range(max_retries):
        try:
            resp = client.get(f"{server}/uploads/{upload_id}")
            resp.raise_for_status()
            status = resp.json()
            received_chunks = set(status.get("received_chunks", []))
            state = status.get("state", "")
            if state in ("VERIFIED", "ANALYZED"):
                logger.info("Upload already verified!")
                _clear_local_state()
                return {"status": state, "upload_id": upload_id}
            if state in ("FAILED", "INCOMPLETE"):
                logger.error("Upload in terminal state: %s (%s)", state, status.get("fail_reason"))
                return {"status": state, "upload_id": upload_id, "reason": status.get("fail_reason")}
            break
        except Exception as exc:
            delay = _backoff_delay(attempt)
            logger.warning("Status check failed (attempt %d): %s — retrying in %.1fs", attempt + 1, exc, delay)
            time.sleep(delay)
    else:
        return {"status": "FAILED", "reason": "Could not check upload status after retries"}

    missing = [i for i in range(chunk_count) if i not in received_chunks]
    logger.info("Chunks received: %d/%d, sending %d missing", len(received_chunks), chunk_count, len(missing))

    # ── Step 3: Upload missing chunks ─────────────────────────────────────
    start_time = time.time()
    bytes_sent = 0
    total_retries = 0

    for seq, chunk_idx in enumerate(missing):
        offset = chunk_idx * chunk_size
        if chunk_idx == chunk_count - 1:
            this_chunk_size = total_size - offset
        else:
            this_chunk_size = chunk_size

        with open(path, "rb") as f:
            f.seek(offset)
            chunk_data = f.read(this_chunk_size)

        chunk_hash = sha256_bytes(chunk_data)

        for attempt in range(max_retries):
            try:
                resp = client.put(
                    f"{server}/uploads/{upload_id}/chunks/{chunk_idx}",
                    content=chunk_data,
                    headers={"X-Chunk-SHA256": chunk_hash},
                )
                resp.raise_for_status()
                bytes_sent += len(chunk_data)
                elapsed = time.time() - start_time
                speed = bytes_sent / elapsed if elapsed > 0 else 0
                logger.info(
                    "Chunk %d/%d sent (attempt %d) | %.1f KB/s | %d/%d done",
                    chunk_idx + 1, chunk_count, attempt + 1,
                    speed / 1024, seq + 1, len(missing),
                )
                break
            except Exception as exc:
                total_retries += 1
                delay = _backoff_delay(attempt)
                logger.warning(
                    "Chunk %d failed (attempt %d): %s — retrying in %.1fs",
                    chunk_idx, attempt + 1, exc, delay,
                )
                time.sleep(delay)

                # On failure, re-check which chunks the server has
                if attempt > 0 and attempt % 3 == 0:
                    try:
                        resp = client.get(f"{server}/uploads/{upload_id}")
                        resp.raise_for_status()
                        server_received = set(resp.json().get("received_chunks", []))
                        if chunk_idx in server_received:
                            logger.info("Server already has chunk %d (confirmed on re-check)", chunk_idx)
                            break
                    except Exception:
                        pass
        else:
            logger.error("Chunk %d failed after %d retries — giving up", chunk_idx, max_retries)
            return {
                "status": "INCOMPLETE",
                "upload_id": upload_id,
                "reason": f"Chunk {chunk_idx} failed after {max_retries} retries",
                "total_retries": total_retries,
            }

    # ── Step 4: Complete and verify ───────────────────────────────────────
    logger.info("All chunks sent. Requesting completion...")
    for attempt in range(max_retries):
        try:
            resp = client.post(f"{server}/uploads/{upload_id}/complete")
            resp.raise_for_status()
            result = resp.json()
            final_state = result.get("state", "UNKNOWN")
            logger.info("Server response: state=%s", final_state)

            if final_state in ("VERIFIED", "ANALYZED"):
                elapsed = time.time() - start_time
                logger.info(
                    "✅ Upload VERIFIED — %d bytes in %.1fs (%.1f KB/s), %d retries",
                    total_size, elapsed, total_size / elapsed / 1024 if elapsed > 0 else 0, total_retries,
                )
                _clear_local_state()
                return {
                    "status": final_state,
                    "upload_id": upload_id,
                    "sha256_expected": result.get("sha256_expected"),
                    "sha256_actual": result.get("sha256_actual"),
                    "total_retries": total_retries,
                    "elapsed_seconds": round(elapsed, 2),
                }
            else:
                reason = result.get("fail_reason", "unknown")
                logger.error("❌ Upload %s: %s", final_state, reason)
                return {
                    "status": final_state,
                    "upload_id": upload_id,
                    "reason": reason,
                    "total_retries": total_retries,
                }
        except Exception as exc:
            delay = _backoff_delay(attempt)
            logger.warning("Complete failed (attempt %d): %s — retrying in %.1fs", attempt + 1, exc, delay)
            time.sleep(delay)

    return {
        "status": "FAILED",
        "upload_id": upload_id,
        "reason": "Could not complete upload after retries",
        "total_retries": total_retries,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Resilient video uploader")
    parser.add_argument("file", help="Path to the video file to upload")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="Server base URL")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="Chunk size in bytes")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="Max retries per chunk")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Per-request timeout (seconds)")
    parser.add_argument("--resume-id", default=None, help="Resume an existing upload by ID")
    parser.add_argument("--manifest", action="store_true", help="Generate and send capture manifest")
    args = parser.parse_args()

    # Check for existing state file
    if not args.resume_id:
        local = _load_local_state()
        if local and local.get("file") == args.file:
            logger.info("Found saved state for this file — resuming upload %s", local["upload_id"])
            args.resume_id = local["upload_id"]

    result = upload(
        filepath=args.file,
        server=args.server,
        chunk_size=args.chunk_size,
        max_retries=args.max_retries,
        timeout=args.timeout,
        resume_id=args.resume_id,
        send_manifest=args.manifest,
    )

    print(json.dumps(result, indent=2))

    if result.get("status") in ("VERIFIED", "ANALYZED"):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
