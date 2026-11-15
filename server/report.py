"""JSON and Markdown report generation."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from .models import AnalysisResult, UploadMetadata, UploadState


def generate_report_json(meta: UploadMetadata, analysis: Optional[AnalysisResult]) -> str:
    """Return a pretty-printed JSON report string."""
    report = _build_report_dict(meta, analysis)
    return json.dumps(report, indent=2, default=str)


def generate_report_md(meta: UploadMetadata, analysis: Optional[AnalysisResult]) -> str:
    """Return a Markdown report string."""
    r = _build_report_dict(meta, analysis)
    lines: list[str] = []
    lines.append(f"# Integrity Report — {r['header']['upload_id']}")
    lines.append("")

    # Header
    h = r["header"]
    lines.append("## 1. Header")
    lines.append(f"- **Upload ID:** {h['upload_id']}")
    lines.append(f"- **Filename:** {h['filename']}")
    lines.append(f"- **Created:** {h['created_at']}")
    lines.append(f"- **Report generated:** {h['report_generated']}")
    lines.append("")

    # Transfer status
    t = r["transfer_status"]
    lines.append("## 2. Transfer Status")
    lines.append(f"- **State:** {t['state']}")
    if t.get("fail_reason"):
        lines.append(f"- **Fail reason:** {t['fail_reason']}")
    lines.append(f"- **Chunks:** {t['received_chunks']}/{t['total_chunks']}")
    lines.append(f"- **SHA-256 (expected):** `{t['sha256_expected']}`")
    lines.append(f"- **SHA-256 (actual):** `{t.get('sha256_actual', 'N/A')}`")
    lines.append(f"- **Verdict:** {t['verdict']}")
    lines.append("")

    # Validation
    v = r.get("validation_status", {})
    lines.append("## 3. Validation Status")
    if v:
        lines.append(f"- **Valid video:** {v.get('valid_video')}")
        lines.append(f"- **Container:** {v.get('container_format', 'N/A')}")
        lines.append(f"- **Codec:** {v.get('codec', 'N/A')}")
        lines.append(f"- **Resolution:** {v.get('width', '?')}×{v.get('height', '?')}")
        if v.get("decode_errors"):
            lines.append("- **Decode errors:**")
            for e in v["decode_errors"]:
                lines.append(f"  - `{e}`")
    else:
        lines.append("_Analysis was not performed (upload not verified)._")
    lines.append("")

    # Frame statistics
    f = r.get("frame_statistics", {})
    lines.append("## 4. Frame Statistics")
    if f:
        lines.append(f"- **Total frames:** {f.get('total_frames', 'N/A')}")
        lines.append(f"- **Declared FPS:** {f.get('declared_fps', 'N/A')}")
        lines.append(f"- **Measured FPS:** {f.get('measured_fps', 'N/A')}")
        lines.append(f"- **Duration:** {f.get('duration', 'N/A')}s")
        lines.append(f"- **VFR flag:** {f.get('vfr_flag', False)}")
    else:
        lines.append("_No frame statistics available._")
    lines.append("")

    # Anomalies
    anomalies = r.get("anomalies", [])
    lines.append("## 5. Anomalies")
    if anomalies:
        lines.append("")
        lines.append("| # | Type | Time Range | Frames | Confidence | Classification |")
        lines.append("|---|------|-----------|--------|------------|----------------|")
        for i, a in enumerate(anomalies, 1):
            time_range = ""
            if a.get("start_time") is not None:
                time_range = f"{a['start_time']:.3f}s"
                if a.get("end_time") is not None:
                    time_range += f" – {a['end_time']:.3f}s"
            frame_str = ", ".join(str(fi) for fi in (a.get("frame_indices") or [])[:5])
            if len(a.get("frame_indices", [])) > 5:
                frame_str += "…"
            lines.append(
                f"| {i} | {a['type']} | {time_range} | {frame_str} "
                f"| {a['confidence']} | {a['classification']} |"
            )
        lines.append("")
        # Detail per anomaly
        for i, a in enumerate(anomalies, 1):
            lines.append(f"### Anomaly {i}: {a['type']}")
            for ev in a.get("evidence", []):
                lines.append(f"- {ev}")
            lines.append("")
    else:
        lines.append("_No anomalies detected._")
    lines.append("")

    # Classification conclusion
    lines.append("## 6. Transfer vs. Capture/Encoding Conclusion")
    lines.append(r.get("classification_summary", "_N/A_"))
    lines.append("")

    # Manifest comparison
    mc = r.get("manifest_comparison")
    if mc:
        lines.append("## 6b. Manifest Comparison")
        for k, v in mc.items():
            lines.append(f"- **{k}:** {v}")
        lines.append("")

    # Limitations
    lims = r.get("limitations", [])
    lines.append("## 7. Limitations")
    for lim in lims:
        lines.append(f"- {lim}")
    lines.append("")

    return "\n".join(lines)


def _build_report_dict(meta: UploadMetadata, analysis: Optional[AnalysisResult]) -> dict:
    now = datetime.now(timezone.utc).isoformat()

    # Transfer verdict
    if meta.state == UploadState.VERIFIED or meta.state == UploadState.ANALYZED:
        verdict = "Transfer completed and verified — SHA-256 hashes match."
    elif meta.state == UploadState.FAILED:
        verdict = f"Transfer FAILED: {meta.fail_reason}"
    elif meta.state == UploadState.INCOMPLETE:
        verdict = "Transfer INCOMPLETE: upload stalled or abandoned."
    else:
        verdict = f"Transfer in progress (state: {meta.state.value})."

    report: dict = {
        "header": {
            "upload_id": meta.upload_id,
            "filename": meta.filename,
            "created_at": datetime.fromtimestamp(meta.created_at, tz=timezone.utc).isoformat(),
            "report_generated": now,
        },
        "transfer_status": {
            "state": meta.state.value,
            "fail_reason": meta.fail_reason,
            "received_chunks": len(meta.received_chunks),
            "total_chunks": meta.chunk_count,
            "sha256_expected": meta.sha256,
            "sha256_actual": meta.assembled_sha256,
            "verdict": verdict,
        },
    }

    if analysis:
        report["validation_status"] = {
            "valid_video": analysis.valid_video,
            "container_format": analysis.container_format,
            "codec": analysis.codec,
            "width": analysis.width,
            "height": analysis.height,
            "decode_errors": analysis.decode_errors,
        }
        report["frame_statistics"] = {
            "total_frames": analysis.total_frames,
            "declared_fps": analysis.declared_fps,
            "measured_fps": analysis.measured_fps,
            "r_frame_rate": analysis.r_frame_rate,
            "duration": analysis.duration,
            "vfr_flag": analysis.vfr_flag,
        }
        report["anomalies"] = [a.model_dump() for a in analysis.anomalies]
        report["classification_summary"] = analysis.classification_summary
        report["manifest_comparison"] = analysis.manifest_comparison
        report["limitations"] = analysis.limitations
    else:
        report["validation_status"] = None
        report["frame_statistics"] = None
        report["anomalies"] = []
        report["classification_summary"] = "Analysis was not performed."
        report["manifest_comparison"] = None
        report["limitations"] = [
            "A byte-perfect transfer cannot prove the original recording had no dropped frames.",
            "Analysis was not performed because the upload was not in VERIFIED state.",
        ]

    return report
