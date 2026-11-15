# Confluentia 2026 — Reliable Video Upload & Frame Integrity Detection

## Overview
A fault-tolerant video transfer system that reliably delivers a 30-second video from a client to a backend despite fluctuating network conditions (20%–100% strength), then runs 30 FPS frame-level integrity analysis.

## Quick Start

### Prerequisites
- Python 3.11+
- `ffmpeg` and `ffprobe` installed and accessible on PATH

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Generate test videos
python tools/make_test_videos.py

# 3. Start the FastAPI server
python -m server.main
# (or: uvicorn server.main:app --host 127.0.0.1 --port 8000)

# 4. Upload a video (with capture manifest)
python -m client.upload tools/sample_videos/clean.mp4 --manifest

# 5. Fetch the integrity report (JSON or Markdown)
curl http://127.0.0.1:8000/uploads/<upload_id>/report
curl "http://127.0.0.1:8000/uploads/<upload_id>/report?format=md"

# 6. Run the automated pytest suite
pytest tests/ -v

# 7. Run all 10 end-to-end fault scenarios
python tests/run_scenarios.py
```

## Running with Fault Injection

To simulate variable network conditions (20%–100% signal strength), added latency, and disconnections:

```bash
# Terminal 1: Start Server
python -m server.main

# Terminal 2: Start Fault Proxy on port 9000 targeting port 8000
python tools/fault_proxy.py --listen-port 9000 --target-port 8000 \
    --schedule "0-10:100000,10-20:20000,20-30:60000,30-40:100000" \
    --latency 50 --jitter 30

# Terminal 3: Upload via the Proxy
python -m client.upload tools/sample_videos/clean.mp4 --server http://127.0.0.1:9000 --manifest
```

## Repository Structure

```
├── server/               # FastAPI backend
│   ├── main.py           # Endpoints, health checks, & startup validation
│   ├── storage.py        # Atomic chunk I/O, persistence, hashing
│   ├── models.py         # Pydantic schemas, state machine definitions
│   ├── analyzer.py       # Hard-gated video validation & anomaly detection
│   └── report.py         # JSON and Markdown report generation
├── client/
│   └── upload.py         # Resilient chunked uploader with backoff & resume
├── tools/
│   ├── fault_proxy.py    # Cross-platform TCP fault-injecting proxy
│   └── make_test_videos.py # Test video generator using ffmpeg
├── tests/
│   ├── test_protocol.py  # Protocol endpoints & state machine unit tests
│   ├── test_analyzer.py  # Video analysis & anomaly detection tests
│   └── run_scenarios.py  # 10 end-to-end fault scenarios runner
├── docs/
│   ├── README.md         # Detailed documentation
│   ├── ARCHITECTURE.md   # Design decisions & state machine specs
│   ├── sample_report.md  # Sample integrity report in Markdown
│   ├── sample_report.json# Sample integrity report in JSON
│   ├── email.md          # Submission email draft
│   └── evidence/         # Logs and summaries from scenario runs
├── requirements.txt      # Pinned Python dependencies
└── README.md             # Top-level quick start guide
```

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST`   | `/uploads`                | Create upload session (filename, total_size, chunk_size, chunk_count, sha256, manifest) |
| `PUT`    | `/uploads/{id}/chunks/{i}`| Upload chunk `i` with `X-Chunk-SHA256` header. Idempotent & atomic. |
| `GET`    | `/uploads/{id}`           | Get upload status and list of received chunk indices (for resume) |
| `POST`   | `/uploads/{id}/complete`  | Reassemble, verify whole-file SHA-256 and size, transition to `VERIFIED` and trigger analysis |
| `GET`    | `/uploads/{id}/report`    | Retrieve integrity report (JSON; Markdown via `?format=md`) |
| `GET`    | `/health`                 | Server health check |

## Test Scenarios & Results (10/10 PASS)

| # | Scenario | Fault Setup | Result | Evidence File |
|---|----------|-------------|--------|---------------|
| 1 | Stable Upload | No faults | `VERIFIED`, hashes match | `docs/evidence/scenario_01_summary.txt` |
| 2 | Variable Signal | 20%–100% bandwidth schedule + latency | `VERIFIED`, hashes match | `docs/evidence/scenario_02_summary.txt` |
| 3 | Network Outage | 4s outage mid-upload | Resumes; only missing chunks sent | `docs/evidence/scenario_03_summary.txt` |
| 4 | Resets & Hangs | Connection resets & hang injection | Exponential backoff succeeds | `docs/evidence/scenario_04_summary.txt` |
| 5 | Fluctuating Speeds | Dynamic bandwidth & forced drops | `VERIFIED` across connection switches | `docs/evidence/scenario_05_summary.txt` |
| 6 | Truncated Upload | Client killed mid-transfer | Stays non-verified; analyzer refused | `docs/evidence/scenario_06_summary.txt` |
| 7 | Corrupted Chunk | Byte flipped in transit | Server rejects chunk hash; retried & verified | `docs/evidence/scenario_07_summary.txt` |
| 8 | Tampered Whole File | Declared SHA-256 mismatch | Server rejects at assembly (`FAILED`) | `docs/evidence/scenario_08_summary.txt` |
| 9 | Non-Video File | Random bytes uploaded | Transfer `VERIFIED`, video flagged `INVALID_VIDEO` | `docs/evidence/scenario_09_summary.txt` |
| 10 | Frame Anomalies | Video with dropped frames | PTS gaps detected & classified as `capture_or_encoding` | `docs/evidence/scenario_10_summary.txt` |
