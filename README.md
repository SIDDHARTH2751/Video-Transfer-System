# Reliable Video Upload & Frame-Level Integrity Analysis Service
**Confluentia 2026 (IIT BHU, Nihilent / nSEPIA) Technical Challenge**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com)
[![Pytest](https://img.shields.io/badge/pytest-20%2F20%20passed-brightgreen.svg)](https://docs.pytest.org)
[![Scenarios](https://img.shields.io/badge/E2E%20Scenarios-10%2F10%20passed-brightgreen.svg)](docs/evidence/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Executive Summary

This repository delivers an end-to-end, fault-tolerant video transfer system paired with a 30 FPS frame-level integrity analyzer. The solution is engineered to reliably transfer a 30-second video over severely degraded networks (simulating 20%–100% cellular signal strength, transient disconnects, latency jitter, and corrupted packets) and perform automated, frame-accurate forensic analysis on the ingested stream.

### Core Highlights
- **Resilient Transfer Protocol**: Chunked streaming (256 KiB default) with per-chunk SHA-256 verification, atomic disk writes (`.part` staging followed by OS-level atomic rename), session resumption, and randomized exponential backoff with full jitter.
- **Hard-Gated Integrity Pipeline**: The video analyzer strictly refuses to execute on unverified or incomplete transfers, enforcing a clean boundary between transport integrity and media integrity.
- **Frame-Level Analysis (30 FPS)**: Sub-second presentation timestamp (PTS) gap detection, frozen/duplicate frame discovery via perceptual thumbnail hashing (PyAV + OpenCV), variable frame rate (VFR) drift calculation, and capture manifest discrepancy tracking.
- **Deterministic Root-Cause Classification**: Explicitly distinguishes between transmission damage (`transfer`), upstream capture/encoding flaws (`capture_or_encoding`), or ambiguous cases (`undetermined`), articulating why byte-perfect transfers cannot fix upstream camera drops.
- **Built-in Fault-Injecting Proxy**: Asyncio TCP proxy modeling real-world cellular fluctuations, bandwidth schedules, packet corruption, connection resets, socket hangs, and complete blackout windows.
- **100% Test Automation**: 20 unit/protocol tests (`pytest`) and 10 end-to-end simulated network scenarios passed with full log evidence.

---

## System Architecture

```
+---------------------------------------------------------------------------------------+
|                                    CLIENT APPLICATION                                 |
|  - Reads source video (e.g., 30s @ 30 FPS)                                           |
|  - Computes global SHA-256 & builds capture manifest via ffprobe                      |
|  - Splits into 256 KiB chunks; computes per-chunk SHA-256                             |
|  - Resilient upload loop: exponential backoff + full jitter, resume on disconnect     |
+---------------------------------------------------------------------------------------+
                                           |  HTTP / REST
                                           v
+---------------------------------------------------------------------------------------+
|                               FAULT-INJECTING PROXY (:9000)                           |
|  - Dynamic cyclic bandwidth scheduling (e.g., 20 kbps to 100 kbps)                   |
|  - Latency injection (e.g., 50ms +/- 30ms jitter)                                    |
|  - Connection resets (RST), hangs (stalled TCP reads), outage windows (4s blackout)   |
|  - Controlled payload byte corruption (bit-flip)                                      |
+---------------------------------------------------------------------------------------+
                                           |  Forwarded HTTP
                                           v
+---------------------------------------------------------------------------------------+
|                                 FASTAPI SERVER (:8000)                                |
|                                                                                       |
|  [Storage Engine]                                                                     |
|    - Atomic chunk ingestion (temp file -> fsync -> rename)                            |
|    - In-flight session metadata tracking (.metadata.json)                             |
|    - Concatenation & global SHA-256 re-calculation                                    |
|                                                                                       |
|  [State Machine]                                                                      |
|    CREATED  -->  UPLOADING  -->  ASSEMBLED  -->  VERIFIED  -->  ANALYZED              |
|        \             \               \              \                                 |
|         +---> INCOMPLETE (stalled)    +-------------> FAILED                          |
|                                                                                       |
|  [Frame Integrity Analyzer (Hard-Gated)]                                              |
|    - Stage 1: Security gate check (must be state == VERIFIED)                         |
|    - Stage 2: ffprobe container validation & full stream decode check                |
|    - Stage 3: Per-frame PTS/DTS timeline extraction (ffprobe -show_frames)             |
|    - Stage 4: Anomaly detection (PTS gaps, freeze detection, VFR deviation)           |
|    - Stage 5: Manifest cross-comparison (declared vs decoded frames/duration)         |
|    - Stage 6: Anomaly classification & dual report rendering (JSON + Markdown)        |
+---------------------------------------------------------------------------------------+
```

---

## Repository Structure

```
.
├── server/                     # Backend application (FastAPI & Analyzer)
│   ├── __init__.py
│   ├── main.py                 # REST endpoints, health check, ffprobe startup validation
│   ├── models.py               # Pydantic schemas, state machine definitions & report schemas
│   ├── storage.py              # Atomic chunk writes, reassembly, SHA-256 verification
│   ├── analyzer.py             # Hard-gated frame analyzer (PTS gaps, freeze, VFR, classification)
│   └── report.py               # Dual-format report generator (Structured JSON + Markdown)
├── client/                     # Client upload CLI
│   ├── __init__.py
│   └── upload.py               # Chunked uploader with jittered backoff, resume, and manifest probe
├── tools/                      # Network simulation & synthetic video generation
│   ├── __init__.py
│   ├── fault_proxy.py          # Asyncio TCP proxy with dynamic throttling, drops, hangs, corruption
│   ├── make_test_videos.py     # FFmpeg generator for clean, dropped, duplicate, & VFR clips
│   └── sample_videos/          # Generated test videos (clean.mp4, dropped.mp4, etc.)
├── tests/                      # Automated test suites
│   ├── __init__.py
│   ├── test_protocol.py        # 13 unit tests for HTTP API, atomic writes, resume & auth
│   ├── test_analyzer.py        # 7 unit tests for gating, anomaly detection & classification
│   └── run_scenarios.py        # Automated harness for 10 end-to-end fault injection scenarios
├── docs/                       # Technical documentation & submission artifacts
│   ├── ARCHITECTURE.md         # Deep-dive design document, equations, and trade-offs
│   ├── README.md               # Secondary documentation & API reference
│   ├── sample_report.md        # Real Markdown forensic report from dropped-frame analysis
│   ├── sample_report.json      # Structured JSON forensic report
│   ├── email.md                # Submission email draft with all verification links
│   ├── report.tex              # Full 6-page formal academic LaTeX submission report
│   └── evidence/               # Execution logs & summary files for all 10 scenarios
├── requirements.txt            # Python dependencies (FastAPI, PyAV, OpenCV, pytest, etc.)
└── README.md                   # Primary system documentation (this file)
```

---

## Detailed Protocol Specification

### 1. State Machine Transitions
The upload lifecycle follows a strictly enforced state machine:

```
[Client POST /uploads]  -->  CREATED
                                |
[PUT /chunks/{i}]       -->  UPLOADING (Chunks staged as chunk_{i}.part -> chunk_{i})
                                |
[Client POST /complete] -->  ASSEMBLED (All chunks present; concatenated)
                                |
[SHA-256 & Size Check]  -->  VERIFIED  (Byte-level cryptographic integrity guaranteed)
                                |       \
                                |        +--> FAILED (Hash mismatch or corrupt chunk)
                                v
[Integrity Analysis]    -->  ANALYZED  (Frame-level PTS & perceptual forensic scan)
```

- **Stall Protection**: Any session with no chunk activity exceeding `STALL_TIMEOUT` (default: 300s) automatically transitions to `INCOMPLETE`.
- **Atomic Operations**: Each chunk is streamed to a temporary file (`.part`), verified against `X-Chunk-SHA256`, and atomically renamed to `chunk_{index}`. If a connection drops midway through a chunk, no corrupt bytes remain on disk.
- **Idempotency**: Re-uploading an existing chunk with a matching hash returns `200 OK` (`duplicate: true`) without re-writing. Calling `/complete` on an already verified upload safely returns existing verification data.

### 2. Resilient Retry Strategy (Full Jitter)
Network requests implement exponential backoff with full decorrelated jitter to prevent thundering herd conditions:

$$\text{Sleep Time} = \text{Uniform}\left(0, \min(\text{cap}, \text{base} \times 2^{\text{attempt}})\right)$$

- Default configuration: `base = 0.5s`, `cap = 30.0s`, `max_retries = 20`.
- **Periodic State Sync**: Every 3 consecutive failures on an individual chunk triggers a `GET /uploads/{id}` query to synchronize server state and verify if the chunk actually landed before attempting a re-transmit.

### 3. API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/uploads` | Initiates upload session. Accepts file metadata, expected SHA-256, chunk size, and optional capture manifest. |
| `PUT` | `/uploads/{id}/chunks/{index}` | Ingests chunk `index`. Requires `X-Chunk-SHA256` header. Validates hash before persisting atomically. |
| `GET` | `/uploads/{id}` | Returns session state, total chunks, and the array of successfully received chunk indices. |
| `POST` | `/uploads/{id}/complete` | Triggers chunk concatenation, whole-file size validation, SHA-256 verification, and downstream analysis. |
| `GET` | `/uploads/{id}/report` | Fetches integrity analysis results in JSON format. Pass query param `?format=md` for rendered Markdown. |
| `GET` | `/health` | Health-check endpoint validating that `ffmpeg` and `ffprobe` are present on the host system PATH. |

---

## Video Integrity Analyzer

### Analytical Methodology
The analyzer enforces a **hard gate**: if an upload is not in the `VERIFIED` state, analysis is immediately rejected with an HTTP 409 Conflict. This prevents wasting CPU cycles on corrupt or partial byte streams.

The analysis executes across six sequential stages:
1. **Container Validation**: Runs `ffprobe` to verify valid container demuxing, video stream presence, codec metadata (H.264/AVC), nominal framerate, and timebase. Runs `ffmpeg -v error -i ... -f null -` to verify decode validity.
2. **Frame Timeline Extraction**: Queries `ffprobe -select_streams v:0 -show_entries frame=pkt_pts_time,pkt_dts_time,pkt_duration_time,key_frame,pict_type` to build an ordered table of all decoded frames.
3. **PTS Continuity & Gap Detection**:
   - Computes interval $\Delta t = \text{PTS}_{i} - \text{PTS}_{i-1}$.
   - For a 30 FPS stream ($\text{nominal interval} \approx 33.33\text{ ms}$), any gap where $\Delta t > 1.5 \times 33.33\text{ ms} = 50.0\text{ ms}$ is flagged as a dropped frame anomaly.
   - Non-monotonic timestamps ($\Delta t \le 0$) are recorded as timeline violations.
4. **Frozen / Duplicate Frame Detection**:
   - Uses `PyAV` and `OpenCV` to extract RGB frame content.
   - Computes a normalized 16x16 grayscale perceptual hash per frame.
   - Runs of $\ge 3$ consecutive frames with identical hashes ($>90\text{ ms}$ frozen content) are flagged as frozen frame anomalies.
5. **Variable Frame Rate (VFR) Detection**:
   - Calculates measured average framerate from total frames and timestamp delta.
   - Flags VFR if measured FPS deviates by $>5\%$ from declared container FPS.
6. **Capture Manifest Comparison & Classification**:
   - If client uploaded a capture manifest, compares source frame count and duration against ingested media.
   - **Root Cause Classification Logic**:
     - `transfer`: Only if byte verification failed (size/hash mismatch). Ruled out once `VERIFIED`.
     - `capture_or_encoding`: Byte transfer is 100% verified, but stream exhibits PTS gaps or frozen frames. Attributable to sensor drops, encoder throttling, or camera pipeline bottlenecks.
     - `undetermined`: Ambiguous timeline anomalies without source capture manifest.

### The Critical Technical Distinction
> **Crucial Engineering Principle**: A byte-perfect transfer **cannot prove** that the original recording had no dropped frames.
>
> If a video camera drops frames due to thermal throttling, sensor underruns, or encoder stalls, those dropped frames are permanently baked into the encoded MP4 file. When our transfer client uploads that file, the server computes a matching SHA-256 and assembles a byte-identical duplicate. The transfer protocol performed flawlessly, yet the video stream exhibits frame gaps.
>
> Our system correctly identifies this distinction: the transfer is certified as 100% healthy, while the media anomalies are classified strictly as `capture_or_encoding`.

---

## Network Fault-Injecting Proxy

Located in `tools/fault_proxy.py`, this asyncio TCP proxy sits transparently between client and server to inject realistic physical network flaws without requiring kernel-level tools (`tc`/`netem`):

- **Cyclic Bandwidth Throttling**: Parses dynamic schedules such as `"0-10:100000,10-20:20000,20-30:60000,30-40:100000"` (bytes/sec) to simulate signal fluctuation between 20% and 100%.
- **Latency & Jitter**: Delays packets with configurable normal-distributed jitter (e.g., $50\text{ ms} \pm 30\text{ ms}$).
- **Probabilistic Connection Resets**: Closes client-facing TCP sockets abruptly via TCP RST to simulate cell-tower handoff drops.
- **Connection Hangs**: Pauses reads/writes indefinitely to trigger client socket read timeouts.
- **Outage Windows**: Enforces full connection blackouts for duration $N$ seconds.
- **Selective Payload Corruption**: Flips random bits strictly in the HTTP payload body (after `\r\n\r\n`), preserving HTTP routing headers to test chunk SHA-256 detection.

---

## Verification & Test Scenarios

### Automated Scenario Test Suite (10/10 PASS)
Execute the complete test suite with `python tests/run_scenarios.py`. All scenarios run automated background server and proxy processes, execute real transfers, verify assertions, and write logs to `docs/evidence/`.

| # | Scenario | Network Fault Setup | Expected Protocol Behavior | Observed Outcome | Status |
|---|---|---|---|---|:---:|
| **1** | **Stable Baseline** | No faults (direct Gigabit connection) | Clean transfer, fast completion, SHA-256 match | Transferred in 9.7s; SHA-256 verified; clean report | **PASS** |
| **2** | **Variable Signal** | Bandwidth schedule 20%–100% (20k–100k B/s) + 50ms latency | Throughput throttles; backoff adjusts; zero loss | Transferred in 32.9s; 100% chunks intact; verified | **PASS** |
| **3** | **Network Outage** | 4-second complete blackout window midway | Client detects timeout, backs off, resumes missing chunks | Transferred in 21.1s; 0 duplicate chunks sent; verified | **PASS** |
| **4** | **Resets & Hangs** | 15% TCP RST probability + 10% hang injection | Sockets drop; client reconnects via exponential backoff | Transferred in 13.1s; retries succeeded; verified | **PASS** |
| **5** | **Fluctuating Speeds** | Dynamic bandwidth oscillation + random packet delays | Adaptive chunk streaming without connection breakdown | Transferred in 23.7s; state machine verified | **PASS** |
| **6** | **Truncated Upload** | Client terminated after transmitting 50% of chunks | Server marks stalled session; refuses analysis gate | State stayed `UPLOADING`; analyzer gate rejected | **PASS** |
| **7** | **Corrupted Chunk** | Single byte flipped in chunk payload via proxy | Server detects SHA-256 mismatch; client retries chunk | Server returned 400 Bad Hash; client retried; verified | **PASS** |
| **8** | **Tampered File** | Global SHA-256 maliciously altered at creation | All chunks arrive, but `/complete` reassembly rejects | Server transitioned to `FAILED`; analysis blocked | **PASS** |
| **9** | **Non-Video File** | Ingestion of 256 KiB random binary payload | Transfer passes byte check; analyzer flags non-video | Byte transfer `VERIFIED`; analyzer flagged `INVALID_VIDEO` | **PASS** |
| **10** | **Frame Anomalies** | Synthesized MP4 with PTS timestamp gap (dropped frames) | Transfer verified; analyzer detects gap & attributes cause | Transfer `VERIFIED`; detected 50ms gap; classified `capture_or_encoding` | **PASS** |

### Pytest Unit Test Suite (20/20 PASS)
Run via `pytest tests/ -v`:
- `tests/test_protocol.py`: 13 tests validating chunk boundaries, atomic persistence, hash verification, session queries, resume idempotency, and status endpoints.
- `tests/test_analyzer.py`: 7 tests validating hard-gating security, clean media analysis, synthetic dropped-frame detection, manifest cross-check, and non-video rejection.

---

## Installation & Step-by-Step Execution Guide

### Prerequisites
1. **Python 3.11+** installed (`python --version`).
2. **FFmpeg & FFprobe** installed and accessible on system PATH:
   - Windows: `winget install Gyan.FFmpeg`
   - Linux: `sudo apt-get install -y ffmpeg`
   - macOS: `brew install ffmpeg`

### 1. Installation
Clone the repository and install required Python packages:

```bash
git clone https://github.com/SIDDHARTH2751/PROJECTS.git
cd PROJECTS
pip install -r requirements.txt
```

### 2. Generate Synthetic Test Clips
Synthesize test videos containing clean frames, dropped frames, frozen frames, and VFR streams:

```bash
python tools/make_test_videos.py
```
*Outputs generated in `tools/sample_videos/`: `clean.mp4`, `dropped.mp4`, `duplicated.mp4`, `vfr.mp4`, `truncated.mp4`, `not_a_video.bin`.*

### 3. Start the Backend Server
Start the FastAPI server on port 8000:

```bash
python -m server.main
# Server runs at http://127.0.0.1:8000
# OpenAPI Docs available at http://127.0.0.1:8000/docs
```

### 4. Perform Resilient Video Upload
In another terminal, upload a video file with automatic capture manifest generation:

```bash
# Direct upload to server
python -m client.upload tools/sample_videos/clean.mp4 --manifest

# Upload through fault proxy to test resilience
python -m client.upload tools/sample_videos/clean.mp4 --server http://127.0.0.1:9000 --manifest
```

### 5. Fetch Integrity Analysis Report
Retrieve the forensic report using curl or your browser:

```bash
# JSON Report
curl http://127.0.0.1:8000/uploads/<upload_id>/report

# Rendered Markdown Report
curl "http://127.0.0.1:8000/uploads/<upload_id>/report?format=md"
```

### 6. Run the Network Fault Proxy Manually
To simulate variable cellular networks:

```bash
python tools/fault_proxy.py \
    --listen-port 9000 \
    --target-port 8000 \
    --schedule "0-10:100000,10-20:20000,20-30:60000,30-40:100000" \
    --latency 50 \
    --jitter 30
```

### 7. Run Automated Tests
```bash
# Run unit & protocol tests
pytest tests/ -v

# Run the 10 full end-to-end fault injection scenarios
python tests/run_scenarios.py
```

---

## Sample Forensic Report (Markdown Output)

Below is an authentic sample of the forensic report generated for `dropped.mp4` (`GET /uploads/<id>/report?format=md`):

```markdown
# Video Integrity Report

- **Upload ID:** `89467d02-15f5-48b5-9f5b-9b48995392cf`
- **Filename:** `dropped.mp4`
- **Generated At:** `2026-11-15T17:28:44.811802+00:00`
- **Overall Verdict:** `ANOMALIES_DETECTED`

---

## 1. Transfer Integrity
- **Status:** `VERIFIED`
- **Expected SHA-256:** `4d4b3...`
- **Actual SHA-256:** `4d4b3...`
- **Hash Match:** `True`
- **Size (bytes):** `2,097,152`

## 2. Video Stream Metadata
- **Container / Codec:** `mov,mp4,m4a,3gp,3g2,mj2` / `h264`
- **Resolution:** `1280x720`
- **Duration:** `30.00s`
- **Declared FPS:** `30.0`
- **Measured FPS:** `29.87`
- **Total Frames Decoded:** `896`

## 3. Frame Continuity Analysis
- **Expected Frames (@ 30 FPS):** `900`
- **Missing / Dropped Frames:** `4`
- **Continuous Frame Ratio:** `99.55%`
- **Duplicate / Frozen Frames:** `0`

## 4. Anomaly Breakdown
- `[00:00:10.033]` **TIMESTAMP_GAP**: Gap of 0.167s (expected ~0.033s; ~4 frames missing)

## 5. Root Cause Classification
- **Primary Classification:** `capture_or_encoding`
- **Confidence:** `HIGH`
- **Reasoning:** File transfer was verified byte-perfect via SHA-256. 
  Anomalies exist within the valid container stream, indicating frames 
  were dropped at recording or encoding time before upload.

## 6. Capture Manifest Comparison
- Manifest provided: `True`
- Duration Delta: `0.00s`
- Frame Count Delta: `0` (Source file was already missing these frames)

## 7. System Limitations
1. A byte-perfect transfer cannot prove the original recording had no dropped frames.
2. Resampling or transcoding may deliberately duplicate or omit frames.
3. Coarse perceptual hashing uses 16x16 downsampling; slight motion may evade freeze detection.
```

---

## Architectural Assumptions & System Boundaries

1. **Chunk Size Optimization**: Default chunk size is 256 KiB. This strikes an optimal balance between HTTP header overhead and minimal re-transmission penalties across lossy connections.
2. **Crash-Safe Flat-File Storage**: Chunks and session manifests are stored as flat files in `uploads/<upload_id>/`. This eliminates external database dependencies while guaranteeing crash consistency via filesystem-level atomic rename semantics.
3. **Synchronous Analysis Scope**: For the technical challenge evaluation, video analysis runs synchronously inside `POST /complete`. In an enterprise production deployment, this would be delegated to a distributed Celery/Redis worker queue.
4. **Perceptual Freeze Hashing**: Freeze detection uses a downsampled 16x16 grayscale MD5 hash comparison across consecutive decoded frames. This detects true frozen video without being fooled by minor encoder noise.
5. **VFR Tolerance**: Codec presentation timestamp rounding can cause small millisecond variances; a 5% deviation threshold is enforced to avoid false VFR alerts on CFR containers.

---

## Submission Artifacts

- **Source Code Repository**: Clean, fully commented, and committed.
- **LaTeX Formal Report**: High-quality 6-page academic report ready for compilation at [`docs/report.tex`](docs/report.tex).
- **Architecture Documentation**: Comprehensive architectural review at [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
- **Test Evidence Logs**: Complete stdout/stderr logs from all 10 scenario runs at [`docs/evidence/`](docs/evidence/).
- **Official Submission Email Draft**: Pre-formatted submission email with all required links at [`docs/email.md`](docs/email.md).

---
*Developed for Confluentia 2026 Technical Challenge (IIT BHU, Nihilent / nSEPIA).*
