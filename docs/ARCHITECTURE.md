# Architecture Overview

## Design Principles
1. **Never trust the client** — every hash is recomputed server-side.
2. **Atomic writes** — chunks are written to temp files then renamed; a crash can never leave a corrupt chunk marked as received.
3. **Explicit state machine** — every transition is validated and logged.
4. **Separation of concerns** — transfer verification is decoupled from video analysis.

## Upload Protocol

### State Machine
```
CREATED → UPLOADING → ASSEMBLED → VERIFIED → ANALYZED
                ↘          ↘          ↘
               FAILED     FAILED     FAILED
                ↘
             INCOMPLETE (stalled)
```

### Transfer Flow
1. Client computes whole-file SHA-256 and (optionally) a capture manifest via local ffprobe.
2. Client calls `POST /uploads` with file metadata and the SHA-256.
3. Client reads chunks sequentially, computes per-chunk SHA-256, and sends each via `PUT /uploads/{id}/chunks/{i}` with `X-Chunk-SHA256` header.
4. Server validates chunk hash, writes atomically (temp + rename), and updates metadata.
5. On any failure, client calls `GET /uploads/{id}` to learn which chunks are received, then sends only the missing ones (resume).
6. Client calls `POST /uploads/{id}/complete`. Server checks all chunks present, assembles them, verifies whole-file SHA-256 and size.
7. If verified, server runs analysis automatically.

### Retry Strategy
- **Exponential backoff with full jitter**: `delay = random(0, min(base × 2^attempt, cap))`
- Default: base=0.5s, cap=30s, max 20 retries per chunk.
- Every 3rd consecutive failure on a chunk triggers a server-side status re-check.

### Idempotency
- Re-uploading an already-stored chunk (same hash) is a no-op (returns `duplicate: true`).
- Re-calling `/complete` on a VERIFIED upload returns the existing result.

## Fault Proxy (`tools/fault_proxy.py`)
A cross-platform asyncio TCP proxy with configurable:
- **Bandwidth throttling** (bytes/sec) with a cyclic schedule
- **Added latency + jitter**
- **Connection resets** (probabilistic)
- **Hangs** (probabilistic, configurable duration)
- **Full outage windows** (refuse connections for N seconds)
- **Byte corruption** (probabilistic, flips a random byte)

The bandwidth schedule format is `"start-end:bps,start-end:bps,..."` and cycles automatically.

## Video Analyzer

### Pipeline
1. **Gate**: refuses unless upload state is `VERIFIED`.
2. **Validate**: ffprobe for container/codec info; ffmpeg full-decode error check.
3. **Frame table**: ffprobe `-show_frames` for per-frame PTS, DTS, duration, keyframe flag.
4. **Anomaly detection**:
   - PTS gaps (>1.5× nominal interval)
   - Non-monotonic PTS
   - Frozen/duplicate content (perceptual hash via PyAV + OpenCV thumbnail comparison)
   - VFR detection (declared vs measured FPS deviation >5%)
   - Frame count / duration mismatch
   - Periodic pattern grouping
5. **Manifest comparison**: if supplied, compare expected vs actual frame count, duration, FPS.
6. **Classification**: `transfer` / `capture_or_encoding` / `undetermined`.

### Classification Logic
- **transfer**: only when received data differs from what client sent (hash mismatch, truncation). Since the system verifies SHA-256, this is normally ruled out once VERIFIED.
- **capture_or_encoding**: anomalies present in a byte-identical file (timestamp gaps, duplicates in the source).
- **undetermined**: insufficient evidence (no manifest, ambiguous VFR).

### Critical Distinction
A byte-perfect transfer **cannot prove** the original recording has no dropped frames. Frame-level anomalies found after a verified transfer are attributed to capture/encoding, **not** to the transfer. The report states this explicitly.

## Capture Manifest
The client optionally generates a manifest with:
- Source file hash, size, filename
- Expected duration, declared FPS, expected frame count (from local ffprobe)

The server compares this against decoded results. Without a manifest, confidence in distinguishing transfer vs capture issues is downgraded.

## Assumptions & Design Decisions
- **Chunk size**: Default 256 KiB — small enough to retry cheaply, large enough to avoid excessive overhead.
- **ffmpeg/ffprobe**: assumed installed; checked at server startup with a clear error if missing.
- **Storage**: flat directory per upload, JSON metadata file — simple, crash-safe, no database needed.
- **Analysis is synchronous**: triggered in the `/complete` endpoint. For a production system this would be async (task queue), but for the challenge scope synchronous is simpler and more testable.
- **Per-frame content hashing** uses a 16×16 grayscale thumbnail MD5 — fast, good enough for freeze detection, but not a true perceptual hash. Very similar but distinct frames could be false positives.
- **VFR threshold**: 5% deviation between declared and measured FPS. Codec rounding may cause small discrepancies that are not true VFR.
- **Stall timeout**: configurable via `STALL_TIMEOUT` env var (default 300s). Uploads with no activity past this are marked INCOMPLETE.

## Limitations
1. A byte-perfect transfer cannot prove the original recording had no dropped frames.
2. Converting to 30 FPS may deliberately duplicate or omit frames during resampling.
3. Without a capture manifest, source-side drops cannot be distinguished from capture-time drops.
4. Frozen-content detection uses coarse perceptual hashing; similar-but-distinct frames may be flagged.
5. VFR detection compares declared vs measured FPS — small discrepancies may be codec rounding.
6. Analysis is synchronous and may be slow for large files.
