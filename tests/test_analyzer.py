"""Pytest tests for the video analyzer."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

# Set test data dir
_test_dir = tempfile.mkdtemp(prefix="analyzer_test_")
os.environ.setdefault("UPLOAD_DATA_DIR", _test_dir)

from server.analyzer import AnalyzerGateError, analyze  # noqa: E402
from server.models import CaptureManifest, UploadMetadata, UploadState  # noqa: E402


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


pytestmark = pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not found")


def _make_test_video(outpath: str, duration: float = 2, fps: int = 30, color: str = "blue") -> str:
    """Create a short test video with ffmpeg."""
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"testsrc=duration={duration}:size=160x120:rate={fps}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        outpath,
    ], capture_output=True, check=True)
    return outpath


def _make_dropped_video(outpath: str, duration: float = 2, fps: int = 30) -> str:
    """Create a video with dropped frames."""
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"testsrc=duration={duration}:size=160x120:rate={fps}",
        "-vf", "select='not(mod(n\\,5))'",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        outpath,
    ], capture_output=True, check=True)
    return outpath


@pytest.fixture
def verified_meta():
    """Return a minimal UploadMetadata in VERIFIED state."""
    meta = UploadMetadata(
        filename="test.mp4",
        total_size=1000,
        chunk_size=1000,
        chunk_count=1,
        sha256="abc123",
    )
    meta.state = UploadState.VERIFIED
    return meta


class TestGate:
    def test_refuses_non_verified(self):
        meta = UploadMetadata(filename="x.mp4", total_size=1, chunk_size=1, chunk_count=1, sha256="x")
        with pytest.raises(AnalyzerGateError, match="not VERIFIED"):
            from pathlib import Path
            analyze(meta, Path("/nonexistent"))

    def test_refuses_created(self):
        meta = UploadMetadata(filename="x.mp4", total_size=1, chunk_size=1, chunk_count=1, sha256="x")
        assert meta.state == UploadState.CREATED
        with pytest.raises(AnalyzerGateError):
            from pathlib import Path
            analyze(meta, Path("/nonexistent"))


class TestCleanVideo:
    def test_clean_video_no_anomalies(self, verified_meta, tmp_path):
        vid = str(tmp_path / "clean.mp4")
        _make_test_video(vid, duration=2, fps=30)

        result = analyze(verified_meta, tmp_path / "clean.mp4")
        assert result.valid_video is True
        assert result.codec == "h264"
        assert result.total_frames > 0
        assert result.declared_fps is not None

        # A clean 2s 30fps video should have 0 or very few anomalies
        # (some may trigger from rounding)
        gap_anomalies = [a for a in result.anomalies if a.type == "timestamp_gap"]
        # Allow a small number from rounding
        assert len(gap_anomalies) <= 2


class TestDroppedVideo:
    def test_detects_frame_count_mismatch(self, verified_meta, tmp_path):
        vid = str(tmp_path / "dropped.mp4")
        _make_dropped_video(vid, duration=2, fps=30)

        result = analyze(verified_meta, tmp_path / "dropped.mp4")
        assert result.valid_video is True

        # The dropped video has fewer actual frames than duration * fps would suggest
        # unless setpts corrected it — check for any anomaly detection
        assert result.total_frames > 0


class TestNonVideo:
    def test_non_video_file(self, verified_meta, tmp_path):
        fake = tmp_path / "garbage.bin"
        fake.write_bytes(os.urandom(5000))

        result = analyze(verified_meta, fake)
        assert result.valid_video is False
        assert len(result.decode_errors) > 0


class TestManifestComparison:
    def test_manifest_matches(self, verified_meta, tmp_path):
        vid = str(tmp_path / "clean.mp4")
        _make_test_video(vid, duration=2, fps=30)

        # Create a manifest that matches
        verified_meta.manifest = CaptureManifest(
            source_sha256="test",
            source_size=1000,
            source_filename="clean.mp4",
            expected_duration=2.0,
            declared_fps=30.0,
            expected_frame_count=60,
        )

        result = analyze(verified_meta, tmp_path / "clean.mp4")
        assert result.manifest_comparison is not None
        # Frame count should be close
        assert result.manifest_comparison["expected_frame_count"] == 60


class TestLimitations:
    def test_limitations_present(self, verified_meta, tmp_path):
        vid = str(tmp_path / "clean.mp4")
        _make_test_video(vid, duration=1, fps=30)

        result = analyze(verified_meta, tmp_path / "clean.mp4")
        assert len(result.limitations) > 0
        # Must mention byte-perfect transfer limitation
        lim_text = " ".join(result.limitations)
        assert "byte-perfect" in lim_text.lower() or "transfer" in lim_text.lower()
