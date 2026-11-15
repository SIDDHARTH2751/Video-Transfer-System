"""Generate test videos for the scenario suite using ffmpeg without font dependencies.

Produces:
  sample_videos/clean.mp4          — 30s, 30fps, testsrc pattern
  sample_videos/dropped.mp4        — 30s, 30fps with missing frames (PTS gaps)
  sample_videos/duplicated.mp4     — 30s with 3s frozen segment
  sample_videos/vfr.mp4            — variable frame rate (25→30 fps)
  sample_videos/truncated.mp4      — partial file (~5s)
  sample_videos/not_a_video.bin    — random non-video bytes

Requires: ffmpeg and ffprobe on PATH.
"""
from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "sample_videos"


def _run(cmd: list[str]) -> None:
    print(f"  → {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  STDERR: {result.stderr.strip()}")
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")


def ensure_ffmpeg():
    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found on PATH.")
        sys.exit(1)
    if not shutil.which("ffprobe"):
        print("ERROR: ffprobe not found on PATH.")
        sys.exit(1)
    print("✓ ffmpeg and ffprobe found")


def generate_clean(outdir: Path) -> Path:
    """30s, 30fps test pattern."""
    out = outdir / "clean.mp4"
    _run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "testsrc=duration=30:size=640x480:rate=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(out),
    ])
    return out


def generate_dropped(outdir: Path) -> Path:
    """30fps video with periodic dropped frames creating real PTS gaps."""
    out = outdir / "dropped.mp4"
    _run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "testsrc=duration=30:size=640x480:rate=30",
        "-vf", "select='not(between(n\\,150\\,154))*not(between(n\\,300\\,309))*not(between(n\\,600\\,604))'",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(out),
    ])
    return out


def generate_duplicated(outdir: Path) -> Path:
    """Video with frozen segment (3 seconds of static frame)."""
    out = outdir / "duplicated.mp4"
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=duration=5:size=640x480:rate=30",
        "-f", "lavfi", "-i", "color=c=blue:s=640x480:r=30:d=3",
        "-f", "lavfi", "-i", "testsrc=duration=22:size=640x480:rate=30",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(out),
    ])
    return out


def generate_vfr(outdir: Path) -> Path:
    """Variable frame rate video (15s @ 25fps + 15s @ 30fps)."""
    tmp1 = outdir / "_vfr_25.mp4"
    tmp2 = outdir / "_vfr_30.mp4"
    out = outdir / "vfr.mp4"

    _run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "testsrc=duration=15:size=640x480:rate=25",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(tmp1),
    ])
    _run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "testsrc=duration=15:size=640x480:rate=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(tmp2),
    ])

    concat_file = outdir / "_concat.txt"
    concat_file.write_text(f"file '{tmp1.name}'\nfile '{tmp2.name}'\n")
    _run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-c:v", "copy",
        str(out),
    ])

    for f in (tmp1, tmp2, concat_file):
        if f.exists():
            f.unlink()

    return out


def generate_truncated(outdir: Path) -> Path:
    """First fraction of clean.mp4 to simulate a corrupt/truncated transfer."""
    clean = outdir / "clean.mp4"
    out = outdir / "truncated.mp4"
    if not clean.exists():
        generate_clean(outdir)

    data = clean.read_bytes()
    truncated = data[:len(data) // 6]
    out.write_bytes(truncated)
    return out


def generate_non_video(outdir: Path) -> Path:
    """Random bytes — not a video."""
    out = outdir / "not_a_video.bin"
    out.write_bytes(random.randbytes(50000))
    return out


def main():
    ensure_ffmpeg()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {OUTPUT_DIR}\n")

    generators = [
        ("clean.mp4", generate_clean),
        ("dropped.mp4", generate_dropped),
        ("duplicated.mp4", generate_duplicated),
        ("vfr.mp4", generate_vfr),
        ("truncated.mp4", generate_truncated),
        ("not_a_video.bin", generate_non_video),
    ]

    for name, gen_fn in generators:
        print(f"Generating {name}...")
        try:
            p = gen_fn(OUTPUT_DIR)
            size = p.stat().st_size
            print(f"  ✓ {p} ({size:,} bytes)\n")
        except Exception as exc:
            print(f"  ✗ Failed: {exc}\n")

    # Clean up test_sample.mp4 if present
    test_sample = Path("test_sample.mp4")
    if test_sample.exists():
        test_sample.unlink()

    print("Done! All test videos generated.")


if __name__ == "__main__":
    main()
