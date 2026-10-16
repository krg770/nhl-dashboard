"""
Parallel transcription (shared by the nhl, ff and stock workers - identical copy
in each repo).

One faster-whisper process on this machine uses only ~2-3 of its 9 cores
(decoding is largely sequential), so an hour-long episode runs at roughly
real-time speed while ~70% of the CPU sits idle. This splits the audio into
chunks and transcribes several at the same time, each with its own model
instance (3 threads each).

Chunk boundaries are placed in the nearest quiet moment (ffmpeg silencedetect)
so a sentence - and a bet - is unlikely to be cut in half; if no silence is
found within a minute of the ideal split point it cuts at the exact point.
Timestamps are shifted back onto the original timeline, so the output has the
same "[123s] text" shape as a single-process transcript.

Two modes:
  - default (NHL): N_CHUNKS equal chunks transcribed all at once.
  - waves (football / stock): pass chunk_seconds (e.g. 360) and a pause_hook.
    The episode is cut into ~6-minute chunks run N_WORKERS at a time; pause_hook
    is called between waves so a lower-priority worker can hand the transcriber
    to the NHL worker (see the NHL priority flag) and take it back afterwards.
"""
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from faster_whisper import WhisperModel

N_CHUNKS = 3
N_WORKERS = 3
THREADS_PER_CHUNK = 3          # 3 workers x 3 threads = the 9 cores
SEARCH_WINDOW_SEC = 60         # how far from the ideal split point to look for silence


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def _silences(path: Path):
    """(start, end) of every quiet stretch of at least 0.4s."""
    err = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    starts = [float(m) for m in re.findall(r"silence_start: ([0-9.]+)", err)]
    ends = [float(m) for m in re.findall(r"silence_end: ([0-9.]+)", err)]
    return list(zip(starts, ends))


def split_points(path: Path, duration: float, n: int):
    ideal = [duration * i / n for i in range(1, n)]
    silences = _silences(path)
    points = []
    for target in ideal:
        best, best_dist = target, SEARCH_WINDOW_SEC + 1
        for s, e in silences:
            mid = (s + e) / 2
            if abs(mid - target) < best_dist:
                best, best_dist = mid, abs(mid - target)
        points.append(best if best_dist <= SEARCH_WINDOW_SEC else target)
    return points


def _cut(path: Path, start: float, end: float, out: Path):
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", str(path),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
        check=True, capture_output=True,
    )


def _transcribe_chunk(model, chunk_path, offset):
    segments, _ = model.transcribe(str(chunk_path))
    return [(seg.start + offset, seg.text.strip()) for seg in segments]


def transcribe_parallel(audio_path: Path, model_size: str = "small", n_chunks: int = N_CHUNKS,
                        pause_hook=None, chunk_seconds=None) -> str:
    duration = _duration(audio_path)
    if chunk_seconds and duration > chunk_seconds * 1.5:
        n_chunks = max(1, round(duration / chunk_seconds))
    elif duration < 240:  # too short to be worth splitting
        n_chunks = 1
    bounds = [0.0] + (split_points(audio_path, duration, n_chunks) if n_chunks > 1 else []) + [duration]

    chunk_files, jobs = [], []
    try:
        for i in range(len(bounds) - 1):
            out = audio_path.with_name(f"{audio_path.stem}_chunk{i}.wav")
            _cut(audio_path, bounds[i], bounds[i + 1], out)
            chunk_files.append(out)
            jobs.append((out, bounds[i]))

        workers = min(N_WORKERS, len(jobs))
        results = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            models = list(pool.map(
                lambda _: WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=THREADS_PER_CHUNK),
                range(workers),
            ))
            for start in range(0, len(jobs), workers):
                if pause_hook and start > 0:
                    pause_hook()  # a chance to hand the transcriber to a higher-priority worker
                wave = jobs[start:start + workers]
                results += list(pool.map(
                    lambda args: _transcribe_chunk(models[args[0]], *args[1]), enumerate(wave)
                ))
    finally:
        for f in chunk_files:
            f.unlink(missing_ok=True)

    lines = [(start, text) for chunk in results for start, text in chunk]
    lines.sort(key=lambda x: x[0])
    return "\n".join(f"[{start:.0f}s] {text}" for start, text in lines)
