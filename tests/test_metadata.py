"""Tag parsing and normalisation — the layer that decides catalogue quality."""

from __future__ import annotations

import base64
import struct

import pytest

from minimalma import metadata

from .fake import JPEG, id3_file, id3v1_file


@pytest.mark.parametrize("version", [3, 4])
def test_id3v2_round_trip(version: int) -> None:
    tags = metadata.parse(id3_file(version=version))
    assert tags.title == "Nightpost"
    assert tags.artist == "Anna Voskresenskaya"
    assert tags.album == "Stairwell"
    assert tags.year == 2024
    assert tags.genre == "Ambient"  # (26) resolved through the ID3v1 table
    assert tags.cover == JPEG
    assert tags.cover_mime == "image/jpeg"
    assert tags.container == "mp3"


def test_id3v24_uses_synchsafe_frame_sizes() -> None:
    """A v2.3 reader on a v2.4 file drops every frame after the first."""
    long_title = "A" * 200  # 0xC8 — differs between plain and synchsafe encodings
    tags = metadata.parse(id3_file(title=long_title, version=4))
    assert tags.title == long_title
    assert tags.artist == "Anna Voskresenskaya"


def test_id3v1_fallback() -> None:
    tags = metadata.parse(id3v1_file())
    assert tags.title == "Old Track"
    assert tags.artist == "Tape Artist"
    assert tags.year == 1998
    assert tags.genre == "Rock"


def test_unicode_and_cyrillic_tags() -> None:
    tags = metadata.parse(id3_file(title="Группа крови", artist="Кино"))
    assert tags.title == "Группа крови"
    assert tags.artist == "Кино"


def test_flac_vorbis_comment_and_picture() -> None:
    picture = (
        struct.pack(">I", 3)
        + struct.pack(">I", len("image/jpeg"))
        + b"image/jpeg"
        + struct.pack(">I", 0)
        + struct.pack(">IIII", 500, 500, 24, 0)
        + struct.pack(">I", len(JPEG))
        + JPEG
    )
    comments = [b"TITLE=Sleeper", b"ARTIST=Kolm", b"DATE=2021", b"TRACKNUMBER=3/9"]
    body = struct.pack("<I", 4) + b"test" + struct.pack("<I", len(comments))
    for item in comments:
        body += struct.pack("<I", len(item)) + item

    stream_info = b"\x00" * 10 + struct.pack(">I", 44100 << 12)[0:3] + b"\x00" * 5
    data = (
        b"fLaC"
        + bytes([0])
        + len(stream_info).to_bytes(3, "big")
        + stream_info
        + bytes([4])
        + len(body).to_bytes(3, "big")
        + body
        + bytes([0x80 | 6])
        + len(picture).to_bytes(3, "big")
        + picture
    )
    tags = metadata.parse(data)
    assert tags.title == "Sleeper"
    assert tags.artist == "Kolm"
    assert tags.year == 2021
    assert tags.track_no == 3
    assert tags.cover == JPEG


def test_ogg_opus_tags() -> None:
    comments = [b"TITLE=Drift", b"ARTIST=Nocturne"]
    body = b"OpusTags" + struct.pack("<I", 4) + b"test" + struct.pack("<I", len(comments))
    for item in comments:
        body += struct.pack("<I", len(item)) + item

    segments = []
    rest = body
    while len(rest) >= 255:
        segments.append(255)
        rest = rest[255:]
    segments.append(len(rest))
    page = (
        b"OggS"
        + bytes([0, 0])
        + b"\x00" * 8
        + b"\x00" * 4
        + b"\x00" * 4
        + b"\x00" * 4
        + bytes([len(segments)])
        + bytes(segments)
        + body
    )
    tags = metadata.parse(page)
    assert tags.title == "Drift"
    assert tags.artist == "Nocturne"
    assert tags.container == "opus"


def test_vorbis_picture_in_comment() -> None:
    picture = (
        struct.pack(">I", 3)
        + struct.pack(">I", len("image/jpeg"))
        + b"image/jpeg"
        + struct.pack(">I", 0)
        + struct.pack(">IIII", 1, 1, 24, 0)
        + struct.pack(">I", len(JPEG))
        + JPEG
    )
    encoded = b"METADATA_BLOCK_PICTURE=" + base64.b64encode(picture)
    comments = [b"TITLE=Encoded", encoded]
    body = struct.pack("<I", 0) + struct.pack("<I", len(comments))
    for item in comments:
        body += struct.pack("<I", len(item)) + item
    data = b"fLaC" + bytes([0x80 | 4]) + len(body).to_bytes(3, "big") + body
    tags = metadata.parse(data)
    assert tags.title == "Encoded"
    assert tags.cover == JPEG


def test_garbage_never_raises() -> None:
    for payload in (
        b"",
        b"ID3",
        b"ID3\x04\x00\x00\x7f\x7f\x7f\x7f" + b"\xff" * 50,
        b"fLaC" + b"\xff" * 30,
        b"OggS" + b"\x00" * 5,
        b"\x00\x00\x00\x18ftyp" + b"\xff" * 40,
    ):
        assert isinstance(metadata.parse(payload), metadata.Tags)


class TestNormalisation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Sunset (Official Video)", "Sunset"),
            ("Sunset [FREE] prod. by Someone", "Sunset"),
            ("Sunset — Official Audio", "Sunset"),
            ("Sunset (Lyrics)", "Sunset"),
            ("Sunset (128 kbps)", "Sunset"),
            ("Prodigy", "Prodigy"),
            ("prod. by Someone", "prod. by Someone"),
        ],
    )
    def test_clean_title(self, raw: str, expected: str) -> None:
        assert metadata.clean_title(raw) == expected

    def test_split_feature(self) -> None:
        title, guests = metadata.split_feature("Nightpost (feat. Anna V. & Kolm)")
        assert title == "Nightpost"
        assert guests == ["Anna V.", "Kolm"]

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Аквариум feat. Kolm & Anna", ["Аквариум", "Kolm", "Anna"]),
            ("Anna V. x Kolm, Someone", ["Anna V.", "Kolm", "Someone"]),
            ("Solo", ["Solo"]),
        ],
    )
    def test_split_artists(self, raw: str, expected: list) -> None:
        assert metadata.split_artists(raw) == expected

    def test_key_folds_scripts_and_case(self) -> None:
        assert metadata.key_of("Аквариум") == metadata.key_of("Akvarium")
        assert metadata.key_of("  KINO  ") == metadata.key_of("kino")
        assert metadata.key_of("Björk") == metadata.key_of("bjork")

    def test_zero_width_and_smart_quotes(self) -> None:
        # zero-width space, curly apostrophe, non-breaking space
        assert metadata.clean_text("A\u200bB\u2019C\u00a0D") == "AB'C D"

    def test_translit_is_reversible_enough_for_search(self) -> None:
        assert metadata.translit("Кино") == "Kino"
        assert metadata.translit("Щёлк") == "Shchelk"

    def test_similarity(self) -> None:
        assert metadata.similarity("Nightpost", "night post") == 1.0
        assert metadata.similarity("Nightpost", "Nightpots") > 0.7
        assert metadata.similarity("Nightpost", "Completely Other") < 0.5

    def test_parse_filename(self) -> None:
        tags = metadata.parse_filename("03. Anna V - Nightpost.mp3")
        assert tags.artist == "Anna V"
        assert tags.title == "Nightpost"

    def test_normalise_tag(self) -> None:
        assert metadata.normalise_tag("#Field Recording ") == "field recording"
        assert metadata.normalise_tag("Пост-панк") == "пост-панк"
