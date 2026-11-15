# Reliable Video Upload & Frame Integrity Detection
**Confluentia 2026 (IIT BHU, Nihilent / nSEPIA) Technical Challenge**

A fault-tolerant video transfer protocol and 30 FPS frame-level integrity analyzer built with Python 3.11+, FastAPI, Uvicorn, and FFmpeg/PyAV/OpenCV.

---

## Quick Start

### 1. Requirements & Setup
- Python 3.11+
- FFmpeg and FFprobe installed and available on PATH

```bash
# Install dependencies
pip install -r requirements.txt

# Generate sample test clips (clean, dropped, duplicated, vfr, truncated, non-video)
python tools/make_test_videos.py
```

### 2. Run the Service & Transfer Video
```bash
# Start the backend server (runs on port 8000)
python -m server.main

# In another terminal, run the resilient uploader (with capture manifest)
python -m client.upload tools/sample_videos/clean.mp4 --manifest

# View the generated integrity report
curl http://127.0.0.1:8000/uploads/<upload_id>/report?format=md
```

### 3. Test Network Fluctuations (Fault-Injecting Proxy)
```bash
# Start the proxy on port 9000 pointing to the backend at port 8000
python tools/fault_proxy.py --listen-port 9000 --target-port 8000 \
    --schedule "0-10:100000,10-20:20000,20-30:60000,30-40:100000" \
    --latency 50 --jitter 30

# Upload through the proxy
python -m client.upload tools/sample_videos/clean.mp4 --server http://127.0.0.1:9000 --manifest
```

### 4. Run Automated Test Suites
```bash
# Run unit and protocol tests (20 passed)
pytest tests/ -v

# Run the 10 end-to-end fault scenarios (all logs saved to docs/evidence/)
python tests/run_scenarios.py
```

---

## Documentation Links
- **Architecture & Technical Distinction:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **Detailed System Documentation:** [docs/README.md](docs/README.md)
- **Sample Integrity Report (Markdown):** [docs/sample_report.md](docs/sample_report.md)
- **Sample Integrity Report (JSON):** [docs/sample_report.json](docs/sample_report.json)
- **Submission Email Draft:** [docs/email.md](docs/email.md)
- **Test Evidence Logs:** [docs/evidence/](docs/evidence/)
