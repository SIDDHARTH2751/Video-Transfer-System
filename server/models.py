"""Pydantic models and upload state machine."""
from __future__ import annotations

import enum
import time
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field


class UploadState(str, enum.Enum):
    CREATED = "CREATED"
    UPLOADING = "UPLOADING"
    ASSEMBLED = "ASSEMBLED"
    VERIFIED = "VERIFIED"
    ANALYZED = "ANALYZED"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"


# Valid transitions: current_state -> set of allowed next states
VALID_TRANSITIONS: dict[UploadState, set[UploadState]] = {
    UploadState.CREATED: {UploadState.UPLOADING, UploadState.FAILED, UploadState.INCOMPLETE},
    UploadState.UPLOADING: {UploadState.ASSEMBLED, UploadState.FAILED, UploadState.INCOMPLETE},
    UploadState.ASSEMBLED: {UploadState.VERIFIED, UploadState.FAILED},
    UploadState.VERIFIED: {UploadState.ANALYZED, UploadState.FAILED},
    UploadState.ANALYZED: set(),
    UploadState.FAILED: set(),
    UploadState.INCOMPLETE: set(),
}


class CaptureManifest(BaseModel):
    """Optional manifest generated client-side from ffprobe."""
    source_sha256: str
    source_size: int
    source_filename: str
    expected_duration: float
    declared_fps: float
    expected_frame_count: int
    pts_summary_hash: Optional[str] = None  # hash of the full PTS list
    pts_list: Optional[list[float]] = None  # compact list of source PTS values


class CreateUploadRequest(BaseModel):
    filename: str
    total_size: int
    chunk_size: int
    chunk_count: int
    sha256: str  # whole-file SHA-256 hex digest
    manifest: Optional[CaptureManifest] = None


class CreateUploadResponse(BaseModel):
    upload_id: str


class UploadMetadata(BaseModel):
    """Persisted upload state — serialised to JSON on disk."""
    upload_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    filename: str = ""
    total_size: int = 0
    chunk_size: int = 0
    chunk_count: int = 0
    sha256: str = ""
    state: UploadState = UploadState.CREATED
    fail_reason: Optional[str] = None
    received_chunks: list[int] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    last_activity: float = Field(default_factory=time.time)
    manifest: Optional[CaptureManifest] = None
    assembled_sha256: Optional[str] = None  # computed after assembly

    # --- state machine helpers ---------------------------------------------------

    def transition(self, target: UploadState, reason: Optional[str] = None) -> None:
        allowed = VALID_TRANSITIONS.get(self.state, set())
        if target not in allowed:
            raise ValueError(
                f"Invalid state transition {self.state.value} → {target.value}"
            )
        self.state = target
        self.last_activity = time.time()
        if target == UploadState.FAILED:
            self.fail_reason = reason or "unspecified"

    def touch(self) -> None:
        self.last_activity = time.time()


class UploadStatusResponse(BaseModel):
    upload_id: str
    state: UploadState
    fail_reason: Optional[str] = None
    received_chunks: list[int]
    chunk_count: int
    filename: str


class CompleteResponse(BaseModel):
    upload_id: str
    state: UploadState
    fail_reason: Optional[str] = None
    sha256_expected: str
    sha256_actual: Optional[str] = None


class AnomalyEntry(BaseModel):
    type: str  # e.g. "timestamp_gap", "duplicate_content", "vfr"
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    frame_indices: list[int] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    confidence: str = "medium"  # high / medium / low
    classification: str = "undetermined"  # transfer / capture_or_encoding / undetermined


class FrameInfo(BaseModel):
    frame_index: int
    pts: Optional[int] = None
    dts: Optional[int] = None
    pts_time: Optional[float] = None
    pkt_duration: Optional[float] = None
    key_frame: bool = False


class AnalysisResult(BaseModel):
    upload_id: str
    # validation
    valid_video: bool = True
    container_format: Optional[str] = None
    codec: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    declared_fps: Optional[float] = None
    measured_fps: Optional[float] = None
    r_frame_rate: Optional[str] = None
    duration: Optional[float] = None
    total_frames: int = 0
    vfr_flag: bool = False
    decode_errors: list[str] = Field(default_factory=list)
    # frames
    frames: list[FrameInfo] = Field(default_factory=list)
    # anomalies
    anomalies: list[AnomalyEntry] = Field(default_factory=list)
    # manifest comparison
    manifest_comparison: Optional[dict[str, Any]] = None
    # summary
    classification_summary: str = ""
    limitations: list[str] = Field(default_factory=list)
