# Submission Email Draft

- **To:** iitbhu_2026@nihilent.com
- **Subject:** Confluentia 2026 | Technical Challenge | Video Upload & Frame Integrity Team
- **Date:** 15 Nov 2026

---

Dear Confluentia 2026 Evaluation Committee,

Please find our submission for the **Confluentia 2026 (IIT BHU, Nihilent / nSEPIA) Technical Challenge: Reliable Video Upload & Frame Integrity Detection**.

### 1. Approach Summary (5 Lines)
1. **Fault-Tolerant Protocol:** A chunked, resumable HTTP transfer protocol (`POST /uploads`, `PUT /chunks/{i}`, `GET /uploads/{id}`, `POST /complete`) with server-side atomic file writes, per-chunk SHA-256 verification, exponential backoff with full jitter, and cross-restart resumption.
2. **Hard-Gated Analysis:** The video analyzer strictly gates execution, refusing to process any upload not in the `VERIFIED` state (guaranteeing exact whole-file SHA-256 and byte-size match).
3. **Capture Manifest Disambiguation:** The client pre-computes a capture manifest (source SHA-256, expected frames, PTS summary) used by the backend to conclusively differentiate network transfer issues from capture/encoding frame drops.
4. **Frame-Level Anomaly Detection:** Extracts per-frame PTS timelines at nominal 30 FPS, flags timestamp gaps (>1.5× nominal interval), non-monotonic PTS, duplicate/frozen content runs via perceptual hashing, and VFR deviations.
5. **Cross-Platform Fault Injection:** Includes a configurable asyncio TCP/HTTP fault proxy simulating bandwidth sweeps (20%–100%), added latency/jitter, connection resets, hangs, outages, and packet corruption.

### 2. How to Run
```bash
# 1. Install dependencies (Python 3.11+, FFmpeg/FFprobe required)
pip install -r requirements.txt

# 2. Generate test videos
python tools/make_test_videos.py

# 3. Run unit & integration tests
pytest tests/ -v

# 4. Run all 10 end-to-end scenarios (with fault proxy and evidence logs)
python tests/run_scenarios.py

# 5. Manual run:
python -m server.main &
python -m client.upload tools/sample_videos/clean.mp4 --manifest
curl http://127.0.0.1:8000/uploads/<upload_id>/report?format=md
```

### 3. Tested Scenarios (10/10 Passed)
All test logs, summaries, and verification outputs are archived under `docs/evidence/`:
- **Scenario 1:** Stable upload (baseline) — PASS
- **Scenario 2:** Variable signal sweep (20%–100% nominal bandwidth) — PASS
- **Scenario 3:** Mid-upload network outage & resume (only missing chunks resent) — PASS
- **Scenario 4:** Connection resets & hangs (backoff retries succeed) — PASS
- **Scenario 5:** Connection switching & fluctuating speeds — PASS
- **Scenario 6:** Truncated/abandoned upload (server leaves in non-verified state; analyzer hard-gated) — PASS
- **Scenario 7:** Byte corruption in transit (per-chunk SHA-256 mismatch rejected, resent, verified) — PASS
- **Scenario 8:** Tampered whole-file hash (rejected at assembly) — PASS
- **Scenario 9:** Non-video / corrupt payload (transfer verified, validator flags `INVALID_VIDEO`) — PASS
- **Scenario 10:** Frame anomaly detection on dropped-frame video (PTS gaps located, quantified, and classified as `capture_or_encoding`) — PASS

### 4. Technical Distinction & Limitations Note
A byte-perfect transfer cannot prove the original recording had no dropped frames. By validating cryptographic hashes prior to decoding, network corruption is eliminated as a cause for any anomalies found in the reconstructed stream. Remaining anomalies (PTS gaps, frozen frames, container mismatches) are classified as `capture_or_encoding` based on source metadata and manifest comparison. Additional limitations (VFR sampling artifacts, coarse perceptual hash false positives) are documented in `docs/ARCHITECTURE.md` and included in every generated report.

Sincerely,  
*Engineering Candidate / Team*
