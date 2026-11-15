"""Video validation, frame-table construction, and anomaly detection.

Hard-gated: refuses to run unless the upload state is VERIFIED.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import statistics
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .models import (
    AnalysisResult,
    AnomalyEntry,
    CaptureManifest,
    FrameInfo,
    UploadMetadata,
    UploadState,
)

logger = logging.getLogger("server.analyzer")


class AnalyzerGateError(Exception):
    """Raised when analysis is attempted on a non-VERIFIED upload."""


def _run_ffprobe(args: list[str], video_path: str) -> dict:
    """Run ffprobe with JSON output and return parsed dict."""
    cmd = [
        "ffprobe", "-v", "error",
        *args,
        "-of", "json",
        video_path,
    ]
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _get_stream_info(video_path: str) -> dict:
    """Get container and video stream info via ffprobe."""
    data = _run_ffprobe(
        ["-show_format", "-show_streams", "-select_streams", "v:0"],
        video_path,
    )
    return data


def _get_frame_table(video_path: str) -> list[dict]:
    """Get per-frame info via ffprobe -show_frames."""
    data = _run_ffprobe(
        ["-show_frames", "-select_streams", "v:0"],
        video_path,
    )
    return data.get("frames", [])


def _frame_hash(frame_bytes: bytes, width: int = 16, height: int = 16) -> str:
    """Compute a perceptual hash: downscale to grayscale 16x16 then SHA-256."""
    try:
        import cv2
        arr = np.frombuffer(frame_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return ""
        thumb = cv2.resize(img, (width, height), interpolation=cv2.INTER_AREA)
        return hashlib.md5(thumb.tobytes()).hexdigest()
    except Exception:
        return ""


def _decode_frames_pyav(video_path: str) -> list[tuple[int, str]]:
    """Decode all video frames with PyAV, returning (index, content_hash) pairs.
    
    The content hash is an MD5 of a small grayscale thumbnail so we can detect
    duplicate/frozen content cheaply.
    """
    import av
    import cv2

    hashes: list[tuple[int, str]] = []
    try:
        container = av.open(video_path)
        stream = container.streams.video[0]
        for i, frame in enumerate(container.decode(stream)):
            arr = frame.to_ndarray(format="gray")
            thumb = cv2.resize(arr, (16, 16), interpolation=cv2.INTER_AREA)
            h = hashlib.md5(thumb.tobytes()).hexdigest()
            hashes.append((i, h))
        container.close()
    except Exception as exc:
        logger.warning("PyAV decode failed: %s", exc)
    return hashes


def _parse_fraction(s: str) -> Optional[float]:
    """Parse '30/1' or '30000/1001' style fraction strings."""
    if not s or s == "0/0":
        return None
    parts = s.split("/")
    if len(parts) == 2:
        num, den = float(parts[0]), float(parts[1])
        return num / den if den != 0 else None
    try:
        return float(s)
    except ValueError:
        return None


# ── Main analysis entry point ────────────────────────────────────────────────


def analyze(meta: UploadMetadata, video_path: Path) -> AnalysisResult:
    """Run the full analysis pipeline.  Raises AnalyzerGateError if not VERIFIED."""

    # ── Gate ──────────────────────────────────────────────────────────────
    if meta.state != UploadState.VERIFIED:
        raise AnalyzerGateError(
            f"Analysis refused: upload {meta.upload_id} is in state "
            f"{meta.state.value}, not VERIFIED."
        )

    result = AnalysisResult(upload_id=meta.upload_id)
    result.limitations = [
        "A byte-perfect transfer cannot prove that the original recording had no dropped frames.",
        "Converting a video to 30 FPS may deliberately duplicate or omit frames during resampling.",
        "Without a capture manifest, source-side frame drops cannot be distinguished from capture-time drops.",
        "Frozen-content detection relies on a coarse perceptual hash; very similar (but distinct) frames may be flagged.",
        "VFR detection compares declared vs measured FPS — small discrepancies may be codec rounding, not true VFR.",
    ]

    # ── Step 1: ffprobe validation ────────────────────────────────────────
    try:
        info = _get_stream_info(str(video_path))
    except Exception as exc:
        result.valid_video = False
        result.decode_errors.append(f"ffprobe stream info failed: {exc}")
        result.classification_summary = (
            "The file could not be parsed as a valid video container. "
            "No frame-level analysis was performed."
        )
        return result

    streams = info.get("streams", [])
    fmt = info.get("format", {})

    if not streams:
        result.valid_video = False
        result.decode_errors.append("No video stream found in the file.")
        result.classification_summary = (
            "The file contains no video stream. Marked as INVALID_VIDEO."
        )
        return result

    vs = streams[0]  # first video stream
    result.container_format = fmt.get("format_name")
    result.codec = vs.get("codec_name")
    result.width = int(vs.get("width", 0)) or None
    result.height = int(vs.get("height", 0)) or None
    result.duration = float(fmt.get("duration", 0)) or float(vs.get("duration", 0)) or None
    result.r_frame_rate = vs.get("r_frame_rate")
    result.declared_fps = _parse_fraction(vs.get("r_frame_rate", ""))

    nb_frames = vs.get("nb_frames")
    if nb_frames and nb_frames != "N/A":
        result.total_frames = int(nb_frames)

    # ── Step 2: full decode check ─────────────────────────────────────────
    try:
        decode_check = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(video_path), "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
        if decode_check.stderr.strip():
            for line in decode_check.stderr.strip().splitlines():
                result.decode_errors.append(line.strip())
    except Exception as exc:
        result.decode_errors.append(f"Full decode check failed: {exc}")

    # ── Step 3: frame table from ffprobe ──────────────────────────────────
    try:
        raw_frames = _get_frame_table(str(video_path))
    except Exception as exc:
        result.decode_errors.append(f"Frame table extraction failed: {exc}")
        raw_frames = []

    frames: list[FrameInfo] = []
    for idx, rf in enumerate(raw_frames):
        if rf.get("media_type") != "video":
            continue
        fi = FrameInfo(
            frame_index=idx,
            pts=int(rf["pts"]) if rf.get("pts") and rf["pts"] != "N/A" else None,
            dts=int(rf["dts"]) if rf.get("dts") and rf["dts"] != "N/A" else None,
            pts_time=float(rf["pts_time"]) if rf.get("pts_time") and rf["pts_time"] != "N/A" else None,
            pkt_duration=float(rf.get("pkt_duration_time", 0)) if rf.get("pkt_duration_time") and rf["pkt_duration_time"] != "N/A" else None,
            key_frame=rf.get("key_frame", 0) == 1,
        )
        frames.append(fi)

    result.frames = frames
    if not result.total_frames:
        result.total_frames = len(frames)

    # measured fps
    if frames and result.duration and result.duration > 0:
        result.measured_fps = round(len(frames) / result.duration, 3)

    # ── Step 4: anomaly detection ─────────────────────────────────────────
    anomalies: list[AnomalyEntry] = []

    # 4a. Timestamp gap analysis
    pts_times = [f.pts_time for f in frames if f.pts_time is not None]
    if pts_times and result.declared_fps and result.declared_fps > 0:
        nominal_interval = 1.0 / result.declared_fps
        gap_threshold = nominal_interval * 1.5
        for i in range(1, len(pts_times)):
            delta = pts_times[i] - pts_times[i - 1]
            if delta > gap_threshold:
                estimated_missing = round(delta / nominal_interval) - 1
                anomalies.append(AnomalyEntry(
                    type="timestamp_gap",
                    start_time=pts_times[i - 1],
                    end_time=pts_times[i],
                    frame_indices=[i - 1, i],
                    evidence=[
                        f"PTS delta {delta:.6f}s exceeds threshold {gap_threshold:.6f}s",
                        f"Estimated {estimated_missing} missing frame(s) at nominal {result.declared_fps} FPS",
                    ],
                    confidence="high",
                    classification="capture_or_encoding" if meta.state == UploadState.VERIFIED else "undetermined",
                ))
            elif delta <= 0:
                anomalies.append(AnomalyEntry(
                    type="non_monotonic_pts",
                    start_time=pts_times[i - 1],
                    end_time=pts_times[i],
                    frame_indices=[i - 1, i],
                    evidence=[f"PTS went from {pts_times[i-1]:.6f} to {pts_times[i]:.6f} (delta={delta:.6f})"],
                    confidence="high",
                    classification="capture_or_encoding",
                ))

    # 4b. Duplicate/frozen content detection via PyAV frame hashes
    try:
        content_hashes = _decode_frames_pyav(str(video_path))
        frozen_runs: list[tuple[int, int]] = []
        if content_hashes:
            run_start = 0
            for i in range(1, len(content_hashes)):
                if content_hashes[i][1] != content_hashes[i - 1][1]:
                    if i - run_start >= 3:  # 3+ consecutive identical frames
                        frozen_runs.append((run_start, i - 1))
                    run_start = i
            if len(content_hashes) - run_start >= 3:
                frozen_runs.append((run_start, len(content_hashes) - 1))

        for start, end in frozen_runs:
            start_time = pts_times[start] if start < len(pts_times) else None
            end_time = pts_times[end] if end < len(pts_times) else None
            anomalies.append(AnomalyEntry(
                type="frozen_content",
                start_time=start_time,
                end_time=end_time,
                frame_indices=list(range(start, end + 1)),
                evidence=[
                    f"Frames {start}-{end} have identical perceptual hash",
                    f"Duration: {end - start + 1} consecutive frames",
                ],
                confidence="medium",
                classification="capture_or_encoding",
            ))
    except Exception as exc:
        result.decode_errors.append(f"Content hash analysis failed: {exc}")

    # 4c. VFR detection
    if result.declared_fps and result.measured_fps:
        ratio = result.measured_fps / result.declared_fps
        if abs(ratio - 1.0) > 0.05:
            result.vfr_flag = True
            anomalies.append(AnomalyEntry(
                type="vfr_detected",
                evidence=[
                    f"Declared FPS: {result.declared_fps}, Measured FPS: {result.measured_fps}",
                    f"Ratio: {ratio:.4f} (>5% deviation)",
                    f"r_frame_rate: {result.r_frame_rate}",
                ],
                confidence="medium",
                classification="capture_or_encoding",
            ))

    # 4d. Duration / frame-count mismatch
    if result.duration and result.declared_fps and result.total_frames:
        expected_frames = round(result.duration * result.declared_fps)
        diff = abs(result.total_frames - expected_frames)
        if diff > 2:  # allow small rounding margin
            anomalies.append(AnomalyEntry(
                type="frame_count_mismatch",
                evidence=[
                    f"Container duration {result.duration:.3f}s × {result.declared_fps} FPS → expected ~{expected_frames} frames",
                    f"Actual frame count: {result.total_frames} (difference: {diff})",
                ],
                frame_indices=[],
                confidence="medium",
                classification="capture_or_encoding",
            ))

    # 4e. Pattern detection – group adjacent timestamp gaps
    gap_anomalies = [a for a in anomalies if a.type == "timestamp_gap"]
    if len(gap_anomalies) >= 3:
        intervals = []
        for i in range(1, len(gap_anomalies)):
            if gap_anomalies[i].start_time and gap_anomalies[i - 1].start_time:
                intervals.append(gap_anomalies[i].start_time - gap_anomalies[i - 1].start_time)
        if intervals:
            mean_int = statistics.mean(intervals)
            if all(abs(iv - mean_int) < mean_int * 0.3 for iv in intervals):
                anomalies.append(AnomalyEntry(
                    type="periodic_drop_pattern",
                    evidence=[
                        f"Detected {len(gap_anomalies)} timestamp gaps with ~{mean_int:.3f}s interval",
                        "This suggests a periodic frame-drop pattern in the source.",
                    ],
                    confidence="medium",
                    classification="capture_or_encoding",
                ))

    result.anomalies = anomalies

    # ── Step 5: manifest comparison ───────────────────────────────────────
    if meta.manifest:
        result.manifest_comparison = _compare_manifest(meta.manifest, result)
        # If manifest shows source already had issues, upgrade classification
        if result.manifest_comparison.get("frame_count_matches") is False:
            for a in result.anomalies:
                if a.classification == "undetermined":
                    a.classification = "capture_or_encoding"
                    a.evidence.append("Manifest comparison confirms source discrepancy.")
    else:
        result.limitations.append(
            "No capture manifest was supplied. Source-side frame drops cannot "
            "be distinguished from capture-time drops; confidence is downgraded."
        )
        # downgrade undetermined anomalies
        for a in result.anomalies:
            if a.confidence == "high" and a.classification != "transfer":
                a.confidence = "medium"

    # ── Step 6: classification summary ────────────────────────────────────
    transfer_issues = [a for a in anomalies if a.classification == "transfer"]
    capture_issues = [a for a in anomalies if a.classification == "capture_or_encoding"]
    undetermined = [a for a in anomalies if a.classification == "undetermined"]

    parts: list[str] = []
    if meta.state == UploadState.VERIFIED:
        parts.append(
            "The file was transferred with verified SHA-256 integrity. "
            "Transfer-related corruption is ruled out."
        )
    if transfer_issues:
        parts.append(f"{len(transfer_issues)} anomaly(ies) classified as transfer issues.")
    if capture_issues:
        parts.append(f"{len(capture_issues)} anomaly(ies) classified as capture/encoding issues.")
    if undetermined:
        parts.append(f"{len(undetermined)} anomaly(ies) with undetermined classification.")
    if not anomalies:
        parts.append("No frame-level anomalies detected.")

    result.classification_summary = " ".join(parts)

    return result


def _compare_manifest(manifest: CaptureManifest, result: AnalysisResult) -> dict[str, Any]:
    """Compare analysis results against the client-supplied capture manifest."""
    comparison: dict[str, Any] = {}

    # Frame count
    comparison["expected_frame_count"] = manifest.expected_frame_count
    comparison["actual_frame_count"] = result.total_frames
    comparison["frame_count_matches"] = (
        abs(manifest.expected_frame_count - result.total_frames) <= 1
    )

    # Duration
    if result.duration:
        comparison["expected_duration"] = manifest.expected_duration
        comparison["actual_duration"] = result.duration
        comparison["duration_matches"] = abs(manifest.expected_duration - result.duration) < 0.1

    # FPS
    comparison["declared_fps"] = manifest.declared_fps
    comparison["measured_fps"] = result.measured_fps
    if result.measured_fps:
        comparison["fps_matches"] = abs(manifest.declared_fps - result.measured_fps) < 1.0

    # SHA-256 (source vs assembled — they should match for a clean transfer)
    comparison["source_sha256"] = manifest.source_sha256

    return comparison
