"""DSP and the ffmpeg layer.

The pure-Python half is tested unconditionally against synthetic signals with
known answers. The ffmpeg half is skipped when the binary is absent — which is
itself the point: everything must degrade rather than fail.
"""

from __future__ import annotations

import array
import math
import subprocess
import tempfile
from pathlib import Path

import pytest

from minimalma import audio

needs_ffmpeg = pytest.mark.skipif(not audio.have_ffmpeg(), reason="ffmpeg is not installed")


def _tone(freq: float, seconds: float = 4.0, rate: int = audio.ANALYSIS_RATE, amp: float = 0.4):
    return array.array(
        "h",
        (
            int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))
            for i in range(int(rate * seconds))
        ),
    )


class TestFFT:
    def test_matches_a_naive_dft(self) -> None:
        size = 64
        signal = [
            math.sin(2 * math.pi * 5 * i / size) + 0.5 * math.cos(2 * math.pi * 11 * i / size)
            for i in range(size)
        ]
        fast = audio.fft_magnitudes(signal)
        naive: list[float] = []
        for k in range(size // 2):
            real = sum(signal[n] * math.cos(-2 * math.pi * k * n / size) for n in range(size))
            imag = sum(signal[n] * math.sin(-2 * math.pi * k * n / size) for n in range(size))
            naive.append(math.hypot(real, imag))
        assert max(abs(a - b) for a, b in zip(fast, naive)) < 1e-9

    def test_finds_the_right_bin(self) -> None:
        size = 1024
        rate = 22050
        freq = 1000.0
        signal = [math.sin(2 * math.pi * freq * i / rate) for i in range(size)]
        mags = audio.fft_magnitudes(signal)
        peak = max(range(len(mags)), key=lambda i: mags[i])
        assert abs(peak * rate / size - freq) < rate / size

    def test_rejects_a_non_power_of_two(self) -> None:
        with pytest.raises(ValueError):
            audio.fft_magnitudes([0.0] * 100)


class TestSpectralFeatures:
    def test_a_low_tone_reads_as_dark(self) -> None:
        features = audio.spectral_features(_tone(200))
        assert features["centroid"] < 400

    def test_a_high_tone_reads_as_bright(self) -> None:
        features = audio.spectral_features(_tone(6000))
        assert features["centroid"] > 4000

    def test_cutoff_detects_a_band_limited_signal(self) -> None:
        # A 3 kHz tone has no content above itself; the cutoff must reflect that.
        features = audio.spectral_features(_tone(3000))
        assert features["cutoff_hz"] < 6000

    def test_too_little_audio_returns_nothing_rather_than_garbage(self) -> None:
        assert audio.spectral_features(array.array("h", [0] * 100)) == {}


class TestEnvelopeAndTempo:
    def _click_track(self, bpm: float, seconds: int = 20) -> array.array:
        rate = audio.ANALYSIS_RATE
        samples = array.array("h", [0] * (rate * seconds))
        period = int(rate * 60.0 / bpm)
        for start in range(0, len(samples) - 1000, period):
            for i in range(400):
                samples[start + i] = int(
                    12000 * math.exp(-i / 40.0) * math.sin(2 * math.pi * 180 * i / rate)
                )
        return samples

    @pytest.mark.parametrize("bpm", [90, 120, 145])
    def test_tempo_estimation(self, bpm: int) -> None:
        features = audio.envelope_features(self._click_track(bpm))
        assert abs(features["tempo"] - bpm) <= 4

    def test_levels_are_sane(self) -> None:
        features = audio.envelope_features(_tone(440, amp=0.5))
        # A 0.5-amplitude sine: RMS = 0.5/sqrt(2) -> -9.03 dBFS, peak -> -6.02 dBFS.
        assert -9.5 < features["rms_db"] < -8.5
        assert -6.5 < features["peak_db"] < -5.5
        assert 1.3 < features["crest"] < 1.6  # sqrt(2) for a sine
        assert features["dc_offset"] < 0.01

    def test_clipping_is_detected(self) -> None:
        samples = array.array(
            "h", [32767 if i % 2 else -32767 for i in range(audio.ANALYSIS_RATE * 3)]
        )
        assert audio.envelope_features(samples)["clip_ratio"] > 0.9

    def test_silence_does_not_produce_a_tempo(self) -> None:
        features = audio.envelope_features(array.array("h", [0] * (audio.ANALYSIS_RATE * 5)))
        assert "tempo" not in features


class TestFeatureTokens:
    def test_numbers_become_coarse_bands(self) -> None:
        tokens = audio.feature_tokens(
            {"tempo": 128.0, "centroid": 2600.0, "lufs": -12.0, "flatness": 0.1, "crest": 5.0}
        )
        assert "~tempo:up" in tokens
        assert "~tone:bright" in tokens
        assert "~level:mid" in tokens
        assert "~texture:tonal" in tokens

    def test_empty_features_yield_no_tokens(self) -> None:
        assert audio.feature_tokens({}) == []


class TestGracefulDegradation:
    def test_analysis_without_ffmpeg_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(audio, "have_ffmpeg", lambda: False)
        assert audio.analyse(b"whatever", "x.mp3") == {}

    def test_probing_a_missing_binary_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(audio, "_which", lambda name: None)
        assert audio.probe(Path("/nonexistent")) == {}
        assert audio.loudness(Path("/nonexistent")) == {}
        assert audio.extract_cover(Path("/nonexistent")) is None

    def test_hostile_bytes_do_not_raise(self) -> None:
        assert isinstance(audio.analyse(b"\x00\xff" * 5000, "evil.mp3"), dict)


@needs_ffmpeg
class TestWithFfmpeg:
    @pytest.fixture()
    def real_mp3(self, tmp_path: Path) -> bytes:
        """A genuine 6-second 220 Hz MP3 with real tags, made by ffmpeg."""
        target = tmp_path / "real.mp3"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "quiet",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:duration=6",
                "-metadata",
                "title=Real Title",
                "-metadata",
                "artist=Real Artist",
                "-metadata",
                "album=Real Album",
                "-metadata",
                "date=2023",
                "-b:a",
                "192k",
                str(target),
            ],
            check=True,
        )
        return target.read_bytes()

    def test_probe_reads_the_stream(self, real_mp3: bytes, tmp_path: Path) -> None:
        path = tmp_path / "p.mp3"
        path.write_bytes(real_mp3)
        info = audio.probe(path)
        assert 5.5 < info["duration"] < 6.5
        assert info["codec"] == "mp3"
        assert info["channels"] == 1
        assert info["tags"]["title"] == "Real Title"

    def test_analysis_produces_a_usable_feature_set(self, real_mp3: bytes) -> None:
        """The measurements exist to feed the recommender, not to judge."""
        features = audio.analyse(real_mp3, "real.mp3")
        assert features["codec"] == "mp3"
        assert "lufs" in features
        assert features["centroid"] < 600  # a 220 Hz sine is dark
        assert audio.feature_tokens(features)

    def test_our_own_parser_agrees_with_ffprobe(self, real_mp3: bytes, tmp_path: Path) -> None:
        from minimalma import metadata

        path = tmp_path / "p.mp3"
        path.write_bytes(real_mp3)
        ours = metadata.parse(real_mp3)
        theirs = audio.probe(path)["tags"]
        assert ours.title == theirs["title"] == "Real Title"
        assert ours.artist == theirs["artist"] == "Real Artist"
        assert ours.album == theirs["album"] == "Real Album"
        assert ours.year == 2023

    def test_flac_round_trip_through_our_parser(self, tmp_path: Path) -> None:
        from minimalma import metadata

        target = tmp_path / "real.flac"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "quiet",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=3",
                "-metadata",
                "title=Flac Title",
                "-metadata",
                "artist=Flac Artist",
                str(target),
            ],
            check=True,
        )
        tags = metadata.parse(target.read_bytes())
        assert tags.title == "Flac Title"
        assert tags.artist == "Flac Artist"
        assert tags.container == "flac"

    def test_m4a_round_trip_through_our_parser(self, tmp_path: Path) -> None:
        from minimalma import metadata

        target = tmp_path / "real.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "quiet",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=3",
                "-metadata",
                "title=M4A Title",
                "-metadata",
                "artist=M4A Artist",
                "-c:a",
                "aac",
                str(target),
            ],
            check=True,
        )
        tags = metadata.parse(target.read_bytes())
        assert tags.title == "M4A Title"
        assert tags.artist == "M4A Artist"
        assert tags.container == "mp4"

    def test_ogg_round_trip_through_our_parser(self, tmp_path: Path) -> None:
        from minimalma import metadata

        target = tmp_path / "real.ogg"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "quiet",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=3",
                "-metadata",
                "title=Ogg Title",
                "-metadata",
                "artist=Ogg Artist",
                "-c:a",
                "libvorbis",
                str(target),
            ],
            check=False,
        )
        if not target.exists() or target.stat().st_size < 1000:
            pytest.skip("this ffmpeg build has no vorbis encoder")
        tags = metadata.parse(target.read_bytes())
        assert tags.title == "Ogg Title"
        assert tags.artist == "Ogg Artist"

    def test_embedded_cover_survives_the_whole_pipeline(self, tmp_path: Path) -> None:
        from minimalma import metadata

        cover = tmp_path / "cover.jpg"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "quiet",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=gray:s=300x300:d=1",
                "-frames:v",
                "1",
                str(cover),
            ],
            check=True,
        )
        target = tmp_path / "withcover.mp3"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "quiet",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=3",
                "-i",
                str(cover),
                "-map",
                "0:a",
                "-map",
                "1:v",
                "-c:v",
                "copy",
                "-id3v2_version",
                "3",
                "-metadata:s:v",
                "title=Album cover",
                "-metadata:s:v",
                "comment=Cover (front)",
                "-metadata",
                "title=Covered",
                str(target),
            ],
            check=True,
        )
        data = target.read_bytes()
        tags = metadata.parse(data)
        assert tags.title == "Covered"
        assert tags.cover and tags.cover[:3] == b"\xff\xd8\xff"
        assert audio.probe(target)["has_cover"] is True
        assert audio.extract_cover(target)[:3] == b"\xff\xd8\xff"
        assert audio.normalise_cover(tags.cover)[:2] == b"\xff\xd8"

    def test_a_truncated_file_is_survived(self, real_mp3: bytes) -> None:
        assert isinstance(audio.analyse(real_mp3[: len(real_mp3) // 3], "trunc.mp3"), dict)

    def test_subprocess_arguments_are_never_shell_interpreted(self, tmp_path: Path) -> None:
        # A filename full of shell metacharacters must be harmless.
        nasty = tmp_path / "a; rm -rf $HOME `x`.mp3"
        nasty.write_bytes(b"not audio")
        assert audio.probe(nasty) == {}
        assert (tmp_path / "a; rm -rf $HOME `x`.mp3").exists()


def test_run_times_out_rather_than_hanging() -> None:
    code, out, err = audio._run(["sleep", "5"], timeout=1)
    assert code == 1


def test_temporary_files_are_cleaned_up(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []
    original = tempfile.NamedTemporaryFile

    def spy(*args, **kwargs):
        handle = original(*args, **kwargs)
        created.append(handle.name)
        return handle

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", spy)
    audio.analyse(b"\x00" * 1000, "x.mp3")
    assert all(not Path(name).exists() for name in created)
