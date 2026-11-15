"""FastAPI application — chunked, resumable, verified video upload with analysis."""
from __future__ import annotations

import logging
import shutil
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from . import storage
from .analyzer import AnalyzerGateError, analyze
from .models import (
    CompleteResponse,
    CreateUploadRequest,
    CreateUploadResponse,
    UploadMetadata,
    UploadState,
    UploadStatusResponse,
)
from .report import generate_report_json, generate_report_md

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
)
logger = logging.getLogger("server.main")

# ── Startup checks ────────────────────────────────────────────────────────────

def _check_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RuntimeError(
                f"'{tool}' not found on PATH.  Install FFmpeg and ensure both "
                f"'ffmpeg' and 'ffprobe' are accessible."
            )

_check_ffmpeg()

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Confluentia 2026 — Video Upload & Integrity Service")

STALL_TIMEOUT = storage.STALL_TIMEOUT


def _get_meta_or_404(upload_id: str) -> UploadMetadata:
    meta = storage.load_metadata(upload_id)
    if meta is None:
        raise HTTPException(404, detail=f"Upload {upload_id} not found")
    # Check for stall
    if meta.state in (UploadState.CREATED, UploadState.UPLOADING):
        if time.time() - meta.last_activity > STALL_TIMEOUT:
            meta.state = UploadState.INCOMPLETE
            meta.fail_reason = f"Upload stalled (no activity for >{STALL_TIMEOUT}s)"
            storage.save_metadata(meta)
    return meta


# ── POST /uploads — create upload ─────────────────────────────────────────────

@app.post("/uploads", response_model=CreateUploadResponse)
def create_upload(body: CreateUploadRequest) -> CreateUploadResponse:
    meta = UploadMetadata(
        filename=body.filename,
        total_size=body.total_size,
        chunk_size=body.chunk_size,
        chunk_count=body.chunk_count,
        sha256=body.sha256,
        manifest=body.manifest,
    )
    storage.save_metadata(meta)
    logger.info(
        "Created upload %s — file=%s size=%d chunks=%d",
        meta.upload_id, meta.filename, meta.total_size, meta.chunk_count,
    )
    return CreateUploadResponse(upload_id=meta.upload_id)


# ── PUT /uploads/{id}/chunks/{i} — upload one chunk ──────────────────────────

@app.put("/uploads/{upload_id}/chunks/{chunk_index}")
async def upload_chunk(
    upload_id: str,
    chunk_index: int,
    request: Request,
) -> JSONResponse:
    meta = _get_meta_or_404(upload_id)

    # Reject if terminal state
    if meta.state in (UploadState.VERIFIED, UploadState.ANALYZED, UploadState.FAILED, UploadState.INCOMPLETE):
        raise HTTPException(
            409, detail=f"Upload is in terminal state {meta.state.value}"
        )

    # Validate chunk index
    if chunk_index < 0 or chunk_index >= meta.chunk_count:
        raise HTTPException(
            400, detail=f"Chunk index {chunk_index} out of range [0, {meta.chunk_count})"
        )

    # Read chunk body
    body = await request.body()

    # Validate chunk size
    is_last = chunk_index == meta.chunk_count - 1
    if is_last:
        expected_size = meta.total_size - meta.chunk_size * (meta.chunk_count - 1)
    else:
        expected_size = meta.chunk_size

    if len(body) != expected_size:
        raise HTTPException(
            400,
            detail=f"Chunk {chunk_index} size mismatch: got {len(body)}, expected {expected_size}",
        )

    # Verify per-chunk hash
    expected_hash = request.headers.get("X-Chunk-SHA256", "")
    actual_hash = storage.sha256_bytes(body)
    if expected_hash and expected_hash != actual_hash:
        raise HTTPException(
            400,
            detail=f"Chunk {chunk_index} SHA-256 mismatch: header={expected_hash}, computed={actual_hash}",
        )

    # Idempotent: if chunk already stored with correct hash, accept silently
    if chunk_index in meta.received_chunks:
        existing_hash = storage.sha256_bytes(storage.read_chunk(upload_id, chunk_index))
        if existing_hash == actual_hash:
            logger.info("Chunk %d already stored for %s (idempotent)", chunk_index, upload_id)
            return JSONResponse({"status": "ok", "duplicate": True})
        # Different content for same index — overwrite
        logger.warning("Chunk %d re-sent with different content for %s", chunk_index, upload_id)

    # Atomic write
    storage.write_chunk_atomic(upload_id, chunk_index, body)

    # Update metadata
    if meta.state == UploadState.CREATED:
        meta.transition(UploadState.UPLOADING)
    if chunk_index not in meta.received_chunks:
        meta.received_chunks.append(chunk_index)
        meta.received_chunks.sort()
    meta.touch()
    storage.save_metadata(meta)

    logger.info(
        "Stored chunk %d/%d for %s (%d bytes, sha256=%s)",
        chunk_index + 1, meta.chunk_count, upload_id, len(body), actual_hash[:12],
    )
    return JSONResponse({"status": "ok", "chunk_index": chunk_index})


# ── GET /uploads/{id} — upload status (enables resume) ───────────────────────

@app.get("/uploads/{upload_id}", response_model=UploadStatusResponse)
def get_upload_status(upload_id: str) -> UploadStatusResponse:
    meta = _get_meta_or_404(upload_id)
    return UploadStatusResponse(
        upload_id=meta.upload_id,
        state=meta.state,
        fail_reason=meta.fail_reason,
        received_chunks=meta.received_chunks,
        chunk_count=meta.chunk_count,
        filename=meta.filename,
    )


# ── POST /uploads/{id}/complete — finalize and verify ─────────────────────────

@app.post("/uploads/{upload_id}/complete", response_model=CompleteResponse)
def complete_upload(upload_id: str) -> CompleteResponse:
    meta = _get_meta_or_404(upload_id)

    if meta.state not in (UploadState.UPLOADING, UploadState.CREATED):
        if meta.state in (UploadState.VERIFIED, UploadState.ANALYZED):
            # Already verified — idempotent
            return CompleteResponse(
                upload_id=meta.upload_id,
                state=meta.state,
                sha256_expected=meta.sha256,
                sha256_actual=meta.assembled_sha256,
            )
        raise HTTPException(
            409, detail=f"Cannot complete: upload is in state {meta.state.value}"
        )

    # Check all chunks present
    missing = [i for i in range(meta.chunk_count) if i not in meta.received_chunks]
    if missing:
        meta.transition(UploadState.FAILED, reason=f"Missing chunks: {missing}")
        storage.save_metadata(meta)
        return CompleteResponse(
            upload_id=meta.upload_id,
            state=meta.state,
            fail_reason=meta.fail_reason,
            sha256_expected=meta.sha256,
        )

    # Assemble
    try:
        assembled_path = storage.assemble_chunks(upload_id, meta)
        meta.transition(UploadState.ASSEMBLED)
    except Exception as exc:
        meta.transition(UploadState.FAILED, reason=f"Assembly failed: {exc}")
        storage.save_metadata(meta)
        return CompleteResponse(
            upload_id=meta.upload_id,
            state=meta.state,
            fail_reason=meta.fail_reason,
            sha256_expected=meta.sha256,
        )

    # Verify whole-file SHA-256
    actual_sha = storage.sha256_file(assembled_path)
    meta.assembled_sha256 = actual_sha

    if actual_sha != meta.sha256:
        meta.transition(
            UploadState.FAILED,
            reason=f"Whole-file SHA-256 mismatch: expected={meta.sha256}, actual={actual_sha}",
        )
        storage.save_metadata(meta)
        return CompleteResponse(
            upload_id=meta.upload_id,
            state=meta.state,
            fail_reason=meta.fail_reason,
            sha256_expected=meta.sha256,
            sha256_actual=actual_sha,
        )

    # Verify size
    actual_size = assembled_path.stat().st_size
    if actual_size != meta.total_size:
        meta.transition(
            UploadState.FAILED,
            reason=f"Size mismatch: expected={meta.total_size}, actual={actual_size}",
        )
        storage.save_metadata(meta)
        return CompleteResponse(
            upload_id=meta.upload_id,
            state=meta.state,
            fail_reason=meta.fail_reason,
            sha256_expected=meta.sha256,
            sha256_actual=actual_sha,
        )

    meta.transition(UploadState.VERIFIED)
    storage.save_metadata(meta)
    logger.info("Upload %s VERIFIED (sha256=%s, size=%d)", upload_id, actual_sha[:12], actual_size)

    # Trigger analysis
    try:
        analysis_result = analyze(meta, assembled_path)
        meta.transition(UploadState.ANALYZED)
        storage.save_metadata(meta)

        # Save reports
        report_json = generate_report_json(meta, analysis_result)
        report_md = generate_report_md(meta, analysis_result)
        storage.save_report_json(upload_id, report_json)
        storage.save_report_md(upload_id, report_md)
        logger.info("Analysis complete for %s", upload_id)
    except AnalyzerGateError as exc:
        logger.error("Analyzer gate error for %s: %s", upload_id, exc)
    except Exception as exc:
        logger.error("Analysis failed for %s: %s", upload_id, exc, exc_info=True)
        # Analysis failure does not revert VERIFIED state
        # Save a partial report
        report_json = generate_report_json(meta, None)
        report_md = generate_report_md(meta, None)
        storage.save_report_json(upload_id, report_json)
        storage.save_report_md(upload_id, report_md)

    return CompleteResponse(
        upload_id=meta.upload_id,
        state=meta.state,
        sha256_expected=meta.sha256,
        sha256_actual=actual_sha,
    )


# ── GET /uploads/{id}/report — integrity report ──────────────────────────────

@app.get("/uploads/{upload_id}/report")
def get_report(upload_id: str, format: Optional[str] = Query(None)) -> Response:
    meta = _get_meta_or_404(upload_id)

    if format == "md":
        data = storage.load_report_md(upload_id)
        if data:
            return PlainTextResponse(data, media_type="text/markdown")
    else:
        data = storage.load_report_json(upload_id)
        if data:
            return JSONResponse(content=__import__("json").loads(data))

    # No report yet — generate on the fly from current state
    from .report import generate_report_json as gen_json, generate_report_md as gen_md
    if format == "md":
        return PlainTextResponse(gen_md(meta, None), media_type="text/markdown")
    return JSONResponse(content=__import__("json").loads(gen_json(meta, None)))


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server.main:app", host="0.0.0.0", port=8000, log_level="info")
