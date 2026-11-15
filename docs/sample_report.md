# Integrity Report — 24f6680b37464b5bb6f38d784fd3756c

## 1. Header
- **Upload ID:** 24f6680b37464b5bb6f38d784fd3756c
- **Filename:** dropped.mp4
- **Created:** 2026-11-15T17:35:56.000000+00:00
- **Report generated:** 2026-11-15T17:36:03.565000+00:00

## 2. Transfer Status
- **State:** ANALYZED
- **Chunks:** 3/3
- **SHA-256 (expected):** `ed614f346ca05ac8073e55f1c06bb51deec634ad8bf3209919e5f25444cc33d1`
- **SHA-256 (actual):** `ed614f346ca05ac8073e55f1c06bb51deec634ad8bf3209919e5f25444cc33d1`
- **Verdict:** Transfer completed and verified — SHA-256 hashes match.

## 3. Validation Status
- **Valid video:** True
- **Container:** mov,mp4,m4a,3gp,3g2,mj2
- **Codec:** h264
- **Resolution:** 640×480
- **Decode errors:** None

## 4. Frame Statistics
- **Total frames:** 880
- **Declared FPS:** 30.0
- **Measured FPS:** 29.333
- **Duration:** 30.0s
- **VFR flag:** False

## 5. Anomalies

| # | Type | Time Range | Frames | Confidence | Classification |
|---|------|-----------|--------|------------|----------------|
| 1 | timestamp_gap | 4.967s – 5.167s | 149, 150 | high | capture_or_encoding |
| 2 | timestamp_gap | 9.967s – 10.333s | 294, 295 | high | capture_or_encoding |
| 3 | timestamp_gap | 19.967s – 20.167s | 584, 585 | high | capture_or_encoding |
| 4 | frame_count_mismatch | | | medium | capture_or_encoding |

### Anomaly 1: timestamp_gap
- PTS delta 0.200000s exceeds threshold 0.050000s
- Estimated 5 missing frame(s) at nominal 30.0 FPS
- Classification: `capture_or_encoding`

### Anomaly 2: timestamp_gap
- PTS delta 0.366666s exceeds threshold 0.050000s
- Estimated 10 missing frame(s) at nominal 30.0 FPS
- Classification: `capture_or_encoding`

### Anomaly 3: timestamp_gap
- PTS delta 0.200000s exceeds threshold 0.050000s
- Estimated 5 missing frame(s) at nominal 30.0 FPS
- Classification: `capture_or_encoding`

### Anomaly 4: frame_count_mismatch
- Container duration 30.000s × 30.0 FPS → expected ~900 frames
- Actual frame count: 880 (difference: 20)
- Classification: `capture_or_encoding`

## 6. Transfer vs. Capture/Encoding Conclusion
The file was transferred with verified SHA-256 integrity. Transfer-related corruption is completely ruled out. 4 anomaly(ies) classified as capture/encoding issues. The byte-identical transfer guarantees that all frame drops occurred on the source device prior to upload (capture or encoding phase), not across the network link.

## 6b. Manifest Comparison
- **expected_frame_count:** 900
- **actual_frame_count:** 880
- **frame_count_matches:** False
- **expected_duration:** 30.0
- **actual_duration:** 30.0
- **duration_matches:** True
- **declared_fps:** 30.0
- **measured_fps:** 29.333
- **fps_matches:** True
- **source_sha256:** `ed614f346ca05ac8073e55f1c06bb51deec634ad8bf3209919e5f25444cc33d1`

## 7. Limitations
- A byte-perfect transfer cannot prove that the original recording had no dropped frames.
- Converting a video to 30 FPS may deliberately duplicate or omit frames during resampling.
- Without a capture manifest, source-side frame drops cannot be distinguished from capture-time drops.
- Frozen-content detection relies on a coarse perceptual hash; very similar (but distinct) frames may be flagged.
- VFR detection compares declared vs measured FPS — small discrepancies may be codec rounding, not true VFR.
