"""Audio inspection.

Two layers, both optional:

* ``ffprobe``/``ffmpeg`` when they are on ``PATH`` — authoritative duration,
  bitrate, EBU R128 loudness and a decoded PCM stream.
* A pure-Python DSP pass over that PCM — spectral shape, tempo and the
  frequency cutoff that betrays a low-bitrate re-encode.

When ffmpeg is absent every function returns empty results and the caller
falls back to what Telegram itself reported. Nothing here is load-bearing for
the service; it only makes the curator's job easier and the recommendations
sharper.
"""

from __future__ import annotations

import array
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("minimalma.audio")

#: Sample rate we decode to. Low enough to be cheap, high enough that a
#: 128 kbps cutoff at ~16 kHz is still visible below Nyquist.
ANALYSIS_RATE = 22050
#: Seconds of audio we look at, taken from the middle of the track.
ANALYSIS_WINDOW = 90
#: FFT size and hop for the spectral pass.
FFT_SIZE = 1024
FFT_HOP = 4096
#: Hop for the cheap energy envelope used by tempo estimation.
ENVELOPE_HOP = 512

_TIMEOUT = 90


def _which(name: str) -> str | None:
    return shutil.which(name)


def have_ffmpeg() -> bool:
    return _which("ffmpeg") is not None


def have_ffprobe() -> bool:
    return _which("ffprobe") is not None


def toolchain() -> dict[str, str | None]:
    return {"ffmpeg": _which("ffmpeg"), "ffprobe": _which("ffprobe")}


def _run(args: list[str], timeout: int = _TIMEOUT) -> tuple[int, bytes, bytes]:
    """Run a subprocess with no shell, bounded time and no inherited stdin."""
    try:
        proc = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("subprocess failed: %s", exc)
        return 1, b"", str(exc).encode()
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------
# ffprobe / ffmpeg
# --------------------------------------------------------------------------


def probe(path: Path) -> dict[str, Any]:
    """Container and stream facts. Empty dict when ffprobe is unavailable."""
    exe = _which("ffprobe")
    if not exe:
        return {}
    code, out, _ = _run(
        [
            exe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=30,
    )
    if code != 0 or not out:
        return {}
    try:
        raw = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        return {}

    fmt = raw.get("format") or {}
    streams = raw.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    info: dict[str, Any] = {
        "duration": _float(fmt.get("duration")) or _float(audio.get("duration")),
        "bitrate": _int(fmt.get("bit_rate")) or _int(audio.get("bit_rate")),
        "sample_rate": _int(audio.get("sample_rate")),
        "channels": _int(audio.get("channels")),
        "codec": audio.get("codec_name") or "",
        "format": fmt.get("format_name") or "",
        "size": _int(fmt.get("size")),
        # An "attached picture" video stream is the embedded cover.
        "has_cover": bool((video.get("disposition") or {}).get("attached_pic")),
    }
    tags = {}
    for source in (fmt.get("tags") or {}, audio.get("tags") or {}):
        for key, value in source.items():
            tags.setdefault(str(key).lower(), str(value))
    info["tags"] = tags
    return info


def loudness(path: Path) -> dict[str, float]:
    """EBU R128 measurement via the ``loudnorm`` filter's JSON report."""
    exe = _which("ffmpeg")
    if not exe:
        return {}
    code, _, err = _run(
        [
            exe,
            "-nostdin",
            "-hide_banner",
            "-i",
            str(path),
            "-map",
            "a:0",
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    if code != 0:
        return {}
    text = err.decode("utf-8", "replace")
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        raw = json.loads(text[start : end + 1])
    except ValueError:
        return {}
    out: dict[str, float] = {}
    for src, dst in (
        ("input_i", "lufs"),
        ("input_tp", "true_peak"),
        ("input_lra", "lra"),
        ("input_thresh", "threshold"),
    ):
        value = _float(raw.get(src))
        # loudnorm reports -inf as "-inf" for digital silence.
        if value is not None and math.isfinite(value):
            out[dst] = round(value, 2)
    return out


def extract_cover(path: Path) -> bytes | None:
    """Pull the embedded cover image out of a file with ffmpeg."""
    exe = _which("ffmpeg")
    if not exe:
        return None
    code, out, _ = _run(
        [
            exe,
            "-nostdin",
            "-v",
            "quiet",
            "-i",
            str(path),
            "-an",
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-f",
            "image2pipe",
            "-",
        ],
        timeout=30,
    )
    return out if code == 0 and len(out) > 100 else None


def normalise_cover(blob: bytes, max_side: int = 1000) -> bytes:
    """Re-encode a cover to a Telegram-friendly JPEG when ffmpeg is present.

    Telegram rejects photos whose width plus height exceeds 10000 or that are
    larger than 10 MB. Rather than parse image headers we simply hand oversized
    blobs to ffmpeg; without ffmpeg we pass small images through untouched and
    give up on large ones (the caller then falls back to Telegram's own
    thumbnail).
    """
    if not blob:
        return b""
    exe = _which("ffmpeg")
    if not exe:
        return blob if len(blob) <= 5 * 1024 * 1024 else b""
    if len(blob) <= 200 * 1024:
        return blob
    with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as handle:
        handle.write(blob)
        temp = Path(handle.name)
    try:
        code, out, _ = _run(
            [
                exe,
                "-nostdin",
                "-v",
                "quiet",
                "-i",
                str(temp),
                "-vf",
                f"scale='min({max_side},iw)':-2",
                "-q:v",
                "3",
                "-f",
                "mjpeg",
                "-",
            ],
            timeout=30,
        )
        if code == 0 and len(out) > 100:
            return out
    finally:
        _unlink(temp)
    return blob if len(blob) <= 5 * 1024 * 1024 else b""


def _decode_pcm(path: Path, duration: float | None) -> array.array:
    """Decode a mono 16-bit slice from the middle of the track."""
    exe = _which("ffmpeg")
    if not exe:
        return array.array("h")
    start = 0.0
    if duration and duration > ANALYSIS_WINDOW:
        start = max(0.0, duration / 2 - ANALYSIS_WINDOW / 2)
    args = [exe, "-nostdin", "-v", "quiet"]
    if start:
        args += ["-ss", f"{start:.2f}"]
    args += [
        "-i",
        str(path),
        "-map",
        "a:0",
        "-t",
        str(ANALYSIS_WINDOW),
        "-ac",
        "1",
        "-ar",
        str(ANALYSIS_RATE),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    code, out, _ = _run(args)
    if code != 0 or len(out) < 4096:
        return array.array("h")
    samples = array.array("h")
    samples.frombytes(out[: len(out) - (len(out) % 2)])
    return samples


# --------------------------------------------------------------------------
# Pure-Python DSP
# --------------------------------------------------------------------------

_TWIDDLE_CACHE: dict[int, tuple[list[float], list[float]]] = {}
_WINDOW_CACHE: dict[int, list[float]] = {}


def _twiddles(size: int) -> tuple[list[float], list[float]]:
    cached = _TWIDDLE_CACHE.get(size)
    if cached is None:
        cos = [math.cos(-2.0 * math.pi * i / size) for i in range(size // 2)]
        sin = [math.sin(-2.0 * math.pi * i / size) for i in range(size // 2)]
        cached = (cos, sin)
        _TWIDDLE_CACHE[size] = cached
    return cached


def _hann(size: int) -> list[float]:
    cached = _WINDOW_CACHE.get(size)
    if cached is None:
        cached = [0.5 - 0.5 * math.cos(2.0 * math.pi * i / (size - 1)) for i in range(size)]
        _WINDOW_CACHE[size] = cached
    return cached


def fft_magnitudes(frame: list[float]) -> list[float]:
    """Magnitude spectrum of a real frame whose length is a power of two.

    Iterative radix-2 Cooley-Tukey over parallel real/imaginary lists; using
    two float lists instead of ``complex`` keeps the inner loop free of object
    allocation, which matters in CPython.
    """
    size = len(frame)
    if size & (size - 1):
        raise ValueError("frame length must be a power of two")
    real = list(frame)
    imag = [0.0] * size

    # Bit-reversal permutation.
    j = 0
    for i in range(1, size):
        bit = size >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            real[i], real[j] = real[j], real[i]
            imag[i], imag[j] = imag[j], imag[i]

    cos_table, sin_table = _twiddles(size)
    length = 2
    while length <= size:
        step = size // length
        half = length // 2
        for start in range(0, size, length):
            angle = 0
            for offset in range(start, start + half):
                partner = offset + half
                wr = cos_table[angle]
                wi = sin_table[angle]
                pr = real[partner]
                pi = imag[partner]
                tr = pr * wr - pi * wi
                ti = pr * wi + pi * wr
                real[partner] = real[offset] - tr
                imag[partner] = imag[offset] - ti
                real[offset] += tr
                imag[offset] += ti
                angle += step
        length <<= 1

    half = size // 2
    return [math.hypot(real[i], imag[i]) for i in range(half)]


def spectral_features(samples: array.array, rate: int = ANALYSIS_RATE) -> dict[str, float]:
    """Average spectral descriptors over the decoded slice."""
    if len(samples) < FFT_SIZE * 4:
        return {}
    window = _hann(FFT_SIZE)
    bin_hz = rate / FFT_SIZE
    frames = 0
    mean_spectrum = [0.0] * (FFT_SIZE // 2)
    centroid_sum = 0.0
    rolloff_sum = 0.0
    flatness_sum = 0.0
    bandwidth_sum = 0.0

    for start in range(0, len(samples) - FFT_SIZE, FFT_HOP):
        frame = [samples[start + i] * window[i] / 32768.0 for i in range(FFT_SIZE)]
        mags = fft_magnitudes(frame)
        total = sum(mags)
        if total <= 1e-9:
            continue
        frames += 1
        for i, value in enumerate(mags):
            mean_spectrum[i] += value

        centroid = sum(i * value for i, value in enumerate(mags)) / total
        centroid_sum += centroid * bin_hz

        # Roll-off: the bin below which 85% of the energy sits.
        target = total * 0.85
        running = 0.0
        rolloff_bin = len(mags) - 1
        for i, value in enumerate(mags):
            running += value
            if running >= target:
                rolloff_bin = i
                break
        rolloff_sum += rolloff_bin * bin_hz

        # Flatness: geometric over arithmetic mean. Tonal material tends to 0,
        # noise towards 1.
        log_sum = 0.0
        for value in mags:
            log_sum += math.log(value + 1e-12)
        geometric = math.exp(log_sum / len(mags))
        flatness_sum += geometric / (total / len(mags))

        spread = sum(((i - centroid) ** 2) * value for i, value in enumerate(mags)) / total
        bandwidth_sum += math.sqrt(max(spread, 0.0)) * bin_hz

    if not frames:
        return {}

    mean_spectrum = [value / frames for value in mean_spectrum]
    peak = max(mean_spectrum) or 1.0
    cutoff_bin = 0
    for i in range(len(mean_spectrum) - 1, -1, -1):
        if mean_spectrum[i] > peak * 0.0015:  # about -56 dB below the peak bin
            cutoff_bin = i
            break

    return {
        "centroid": round(centroid_sum / frames, 1),
        "rolloff": round(rolloff_sum / frames, 1),
        "flatness": round(flatness_sum / frames, 4),
        "bandwidth": round(bandwidth_sum / frames, 1),
        "cutoff_hz": round(cutoff_bin * bin_hz, 1),
        "frames": frames,
    }


def envelope_features(samples: array.array, rate: int = ANALYSIS_RATE) -> dict[str, float]:
    """Level statistics and tempo from a cheap short-time energy envelope."""
    if len(samples) < ENVELOPE_HOP * 8:
        return {}
    envelope: list[float] = []
    peak = 0
    square_sum = 0.0
    crossings = 0
    previous = 0
    dc_sum = 0.0
    clipped = 0

    for start in range(0, len(samples) - ENVELOPE_HOP, ENVELOPE_HOP):
        block = samples[start : start + ENVELOPE_HOP]
        energy = 0.0
        for value in block:
            energy += value * value
            square_sum += value * value
            dc_sum += value
            if value > peak:
                peak = value
            elif -value > peak:
                peak = -value
            if value >= 32700 or value <= -32700:
                clipped += 1
            if (value >= 0) != (previous >= 0):
                crossings += 1
            previous = value
        envelope.append(math.sqrt(energy / len(block)))

    count = len(envelope) * ENVELOPE_HOP
    rms = math.sqrt(square_sum / count) if count else 0.0
    out: dict[str, float] = {
        "rms_db": round(20 * math.log10(rms / 32768.0), 2) if rms > 0 else -120.0,
        "peak_db": round(20 * math.log10(peak / 32768.0), 2) if peak > 0 else -120.0,
        "crest": round(peak / rms, 2) if rms > 0 else 0.0,
        "zcr": round(crossings / count, 5) if count else 0.0,
        "dc_offset": round(abs(dc_sum / count) / 32768.0, 5) if count else 0.0,
        "clip_ratio": round(clipped / count, 6) if count else 0.0,
    }
    tempo = _estimate_tempo(envelope, rate / ENVELOPE_HOP)
    if tempo:
        out["tempo"] = tempo
    return out


def _estimate_tempo(envelope: list[float], frame_rate: float) -> float | None:
    """Autocorrelation of the onset strength curve, refined parabolically.

    Accurate to roughly a couple of BPM on percussive material and unreliable
    on ambient or rubato music — which is why the value is only ever consumed
    as a coarse band, never as a number shown to anyone.
    """
    if len(envelope) < 32 or frame_rate <= 0:
        return None
    # Onset strength: positive part of the differentiated log envelope.
    logs = [math.log(value + 1e-6) for value in envelope]
    onset = [max(0.0, logs[i] - logs[i - 1]) for i in range(1, len(logs))]
    mean = sum(onset) / len(onset)
    onset = [value - mean for value in onset]
    if not any(onset):
        return None

    min_lag = max(2, int(round(frame_rate * 60.0 / 200.0)))  # 200 BPM
    max_lag = min(len(onset) - 2, int(round(frame_rate * 60.0 / 55.0)))  # 55 BPM
    if max_lag <= min_lag + 1:
        return None

    scores: dict[int, float] = {}
    for lag in range(min_lag, max_lag + 1):
        total = 0.0
        for i in range(len(onset) - lag):
            total += onset[i] * onset[i + lag]
        scores[lag] = total / (len(onset) - lag)

    best = max(scores, key=lambda lag: scores[lag])
    if scores[best] <= 0:
        return None
    # Parabolic interpolation around the peak for sub-lag resolution.
    left = scores.get(best - 1, scores[best])
    right = scores.get(best + 1, scores[best])
    denominator = left - 2 * scores[best] + right
    shift = 0.5 * (left - right) / denominator if denominator else 0.0
    lag = best + max(-0.5, min(0.5, shift))
    bpm = 60.0 * frame_rate / lag
    while bpm < 70:
        bpm *= 2
    while bpm > 190:
        bpm /= 2
    return round(bpm, 1)


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def analyse(data: bytes, filename: str = "audio") -> dict[str, Any]:
    """Measure an audio file held in memory.

    The result feeds the recommender and nothing else. It is never shown to a
    curator and never scores a submission: whether a record belongs on the
    station is a judgement made by ear, and a number cannot help with it.

    An earlier version derived "quality flags" from these same measurements —
    low bitrate, dull top end, over-compression, mono — and put them on the
    review card. That was a mistake worth recording. Most of those readings
    describe an aesthetic rather than a defect: a cassette, a lo-fi mix or a
    field recording on a cheap microphone all trip them, which on a station for
    niche music means the warnings fire hardest on exactly the material the
    station exists for. A panel that reads like a verdict quietly pushes a
    curator away from it.
    """
    if not data or not have_ffmpeg():
        return {}
    suffix = Path(filename).suffix[:8] or ".bin"
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(data)
            temp = Path(handle.name)
        info = probe(temp)
        duration = info.get("duration")
        features: dict[str, Any] = {
            k: v
            for k, v in info.items()
            if k in ("duration", "bitrate", "sample_rate", "channels", "codec")
        }
        features.update(loudness(temp))
        samples = _decode_pcm(temp, duration)
        if samples:
            features.update(spectral_features(samples))
            features.update(envelope_features(samples))
        return features
    except Exception as exc:  # pragma: no cover - never let analysis break intake
        log.warning("analysis failed for %s: %s", filename, exc)
        return {}
    finally:
        if temp is not None:
            _unlink(temp)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:  # pragma: no cover
        pass


def feature_tokens(features: dict[str, Any]) -> list[str]:
    """Bucket numeric features into tokens so content similarity can use them.

    Turning continuous values into coarse bands lets tags and acoustics share
    one sparse vector space, which keeps the similarity code to a single dot
    product instead of two weighted halves.
    """
    tokens: list[str] = []
    tempo = features.get("tempo")
    if tempo:
        for name, low, high in (
            ("slow", 0, 90),
            ("mid", 90, 115),
            ("up", 115, 135),
            ("fast", 135, 160),
            ("veryfast", 160, 1000),
        ):
            if low <= tempo < high:
                tokens.append(f"~tempo:{name}")
                break
    centroid = features.get("centroid")
    if centroid:
        for name, low, high in (
            ("dark", 0, 1200),
            ("warm", 1200, 2200),
            ("bright", 2200, 3600),
            ("harsh", 3600, 99999),
        ):
            if low <= centroid < high:
                tokens.append(f"~tone:{name}")
                break
    lufs = features.get("lufs")
    if lufs is not None:
        tokens.append(
            "~level:loud" if lufs > -11 else "~level:quiet" if lufs < -17 else "~level:mid"
        )
    flatness = features.get("flatness")
    if flatness is not None:
        tokens.append("~texture:noisy" if flatness > 0.25 else "~texture:tonal")
    crest = features.get("crest")
    if crest:
        tokens.append("~dynamics:flat" if crest < 3.5 else "~dynamics:open")
    duration = features.get("duration")
    if duration:
        if duration < 120:
            tokens.append("~length:short")
        elif duration > 420:
            tokens.append("~length:long")
    return tokens
