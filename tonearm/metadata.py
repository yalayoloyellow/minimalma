"""Byte-level tag reading and text normalisation.

Everything here is pure Python and reads from an in-memory ``bytes`` object,
because that is the shape Telegram hands us. Four containers are understood:
ID3v2/ID3v1 (MP3), FLAC, Ogg (Vorbis and Opus) and MP4/M4A. Anything else
degrades to whatever Telegram itself reported about the file.

The second half of the module is the normalisation layer. It exists because a
catalogue is only as good as its keys: ``Аквариум``, ``AKVARIUM`` and
``Akvarium `` must collapse to one artist, and ``Song (Official Video) [FREE]``
must become ``Song``.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import difflib
import re
import struct
import unicodedata
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Container parsing
# --------------------------------------------------------------------------

ID3V1_GENRES = (
    "Blues",
    "Classic Rock",
    "Country",
    "Dance",
    "Disco",
    "Funk",
    "Grunge",
    "Hip-Hop",
    "Jazz",
    "Metal",
    "New Age",
    "Oldies",
    "Other",
    "Pop",
    "R&B",
    "Rap",
    "Reggae",
    "Rock",
    "Techno",
    "Industrial",
    "Alternative",
    "Ska",
    "Death Metal",
    "Pranks",
    "Soundtrack",
    "Euro-Techno",
    "Ambient",
    "Trip-Hop",
    "Vocal",
    "Jazz+Funk",
    "Fusion",
    "Trance",
    "Classical",
    "Instrumental",
    "Acid",
    "House",
    "Game",
    "Sound Clip",
    "Gospel",
    "Noise",
    "AlternRock",
    "Bass",
    "Soul",
    "Punk",
    "Space",
    "Meditative",
    "Instrumental Pop",
    "Instrumental Rock",
    "Ethnic",
    "Gothic",
    "Darkwave",
    "Techno-Industrial",
    "Electronic",
    "Pop-Folk",
    "Eurodance",
    "Dream",
    "Southern Rock",
    "Comedy",
    "Cult",
    "Gangsta",
    "Top 40",
    "Christian Rap",
    "Pop/Funk",
    "Jungle",
    "Native American",
    "Cabaret",
    "New Wave",
    "Psychadelic",
    "Rave",
    "Showtunes",
    "Trailer",
    "Lo-Fi",
    "Tribal",
    "Acid Punk",
    "Acid Jazz",
    "Polka",
    "Retro",
    "Musical",
    "Rock & Roll",
    "Hard Rock",
    "Folk",
    "Folk-Rock",
    "National Folk",
    "Swing",
    "Fast Fusion",
    "Bebob",
    "Latin",
    "Revival",
    "Celtic",
    "Bluegrass",
    "Avantgarde",
    "Gothic Rock",
    "Progressive Rock",
    "Psychedelic Rock",
    "Symphonic Rock",
    "Slow Rock",
    "Big Band",
    "Chorus",
    "Easy Listening",
    "Acoustic",
    "Humour",
    "Speech",
    "Chanson",
    "Opera",
    "Chamber Music",
    "Sonata",
    "Symphony",
    "Booty Bass",
    "Primus",
    "Porn Groove",
    "Satire",
    "Slow Jam",
    "Club",
    "Tango",
    "Samba",
    "Folklore",
    "Ballad",
    "Power Ballad",
    "Rhythmic Soul",
    "Freestyle",
    "Duet",
    "Punk Rock",
    "Drum Solo",
    "A capella",
    "Euro-House",
    "Dance Hall",
    "Goa",
    "Drum & Bass",
    "Club-House",
    "Hardcore",
    "Terror",
    "Indie",
    "BritPop",
    "Negerpunk",
    "Polsk Punk",
    "Beat",
    "Christian Gangsta Rap",
    "Heavy Metal",
    "Black Metal",
    "Crossover",
    "Contemporary Christian",
    "Christian Rock",
    "Merengue",
    "Salsa",
    "Trash Metal",
    "Anime",
    "Jpop",
    "Synthpop",
)

#: Picture types we accept, best first. 3 is "cover (front)".
COVER_PRIORITY = (3, 0, 2, 4, 18)


@dataclass
class Tags:
    """Everything we managed to read out of a file."""

    title: str = ""
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    genre: str = ""
    comment: str = ""
    year: int | None = None
    track_no: int | None = None
    bpm: float | None = None
    duration: float | None = None
    cover: bytes | None = None
    cover_mime: str = ""
    container: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def merged_with(self, other: Tags) -> Tags:
        """Fill blanks in ``self`` from ``other`` without overwriting."""
        for name in ("title", "artist", "album_artist", "album", "genre", "comment", "container"):
            if not getattr(self, name) and getattr(other, name):
                setattr(self, name, getattr(other, name))
        for name in ("year", "track_no", "bpm", "duration"):
            if getattr(self, name) in (None, 0) and getattr(other, name):
                setattr(self, name, getattr(other, name))
        if not self.cover and other.cover:
            self.cover = other.cover
            self.cover_mime = other.cover_mime
        for key, value in other.extra.items():
            self.extra.setdefault(key, value)
        return self


def parse(data: bytes) -> Tags:
    """Read tags from a whole audio file held in memory.

    Never raises on malformed input; a corrupt file simply yields empty tags.
    """
    if not data:
        return Tags()
    try:
        if data[:3] == b"ID3":
            tags = _parse_id3v2(data)
            tags.container = "mp3"
            return tags.merged_with(_parse_id3v1(data))
        if data[:4] == b"fLaC":
            tags = _parse_flac(data)
            tags.container = "flac"
            return tags
        if data[:4] == b"OggS":
            tags = _parse_ogg(data)
            tags.container = tags.container or "ogg"
            return tags
        if len(data) > 12 and data[4:8] == b"ftyp":
            tags = _parse_mp4(data)
            tags.container = "mp4"
            return tags
        if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            tags = _parse_wav(data)
            tags.container = "wav"
            return tags
        # A bare MPEG stream may still carry a trailing ID3v1 block.
        v1 = _parse_id3v1(data)
        if v1.title or v1.artist:
            v1.container = "mp3"
        return v1
    except Exception:  # pragma: no cover - defensive: any file may be hostile
        return Tags()


# ------------------------------------------------------------------ ID3v2


def _synchsafe(raw: bytes) -> int:
    """Decode a synchsafe integer: seven significant bits per byte."""
    value = 0
    for byte in raw:
        value = (value << 7) | (byte & 0x7F)
    return value


def _decode_text(payload: bytes) -> list[str]:
    """Decode an ID3v2 text frame body into its (possibly multiple) values."""
    if not payload:
        return []
    encoding = payload[0]
    body = payload[1:]
    if encoding == 0:
        text = body.decode("latin-1", "replace")
        sep = "\x00"
    elif encoding == 1:
        text = body.decode("utf-16", "replace")
        sep = "\x00"
    elif encoding == 2:
        text = body.decode("utf-16-be", "replace")
        sep = "\x00"
    else:
        text = body.decode("utf-8", "replace")
        sep = "\x00"
    text = text.replace("﻿", "")
    parts = [p.strip() for p in text.split(sep)]
    return [p for p in parts if p]


def _split_encoded(body: bytes, encoding: int) -> tuple[str, bytes]:
    """Split a null-terminated string in the given ID3 encoding from ``body``."""
    if encoding in (1, 2):
        # UTF-16 terminators are two zero bytes on an even boundary.
        index = 0
        while index + 1 < len(body):
            if body[index] == 0 and body[index + 1] == 0:
                break
            index += 2
        raw, rest = body[:index], body[index + 2 :]
        codec = "utf-16" if encoding == 1 else "utf-16-be"
        return raw.decode(codec, "replace").replace("﻿", ""), rest
    index = body.find(b"\x00")
    if index < 0:
        return body.decode("utf-8" if encoding == 3 else "latin-1", "replace"), b""
    raw, rest = body[:index], body[index + 1 :]
    return raw.decode("utf-8" if encoding == 3 else "latin-1", "replace"), rest


def _parse_id3v2(data: bytes) -> Tags:
    tags = Tags()
    if len(data) < 10:
        return tags
    major = data[3]
    flags = data[5]
    size = _synchsafe(data[6:10])
    body = data[10 : 10 + size]
    if flags & 0x80:  # unsynchronisation applied to the whole tag (v2.3)
        body = body.replace(b"\xff\x00", b"\xff")
    if flags & 0x40:  # extended header
        if major >= 4 and len(body) >= 4:
            body = body[_synchsafe(body[:4]) :]
        elif len(body) >= 4:
            body = body[struct.unpack(">I", body[:4])[0] + 4 :]

    id_len, size_len, flag_len = (3, 3, 0) if major == 2 else (4, 4, 2)
    covers: list[tuple[int, str, bytes]] = []
    pos = 0
    while pos + id_len + size_len + flag_len <= len(body):
        frame_id = body[pos : pos + id_len]
        if not frame_id.strip(b"\x00"):
            break  # padding
        raw_size = body[pos + id_len : pos + id_len + size_len]
        if major >= 4:
            frame_size = _synchsafe(raw_size)
        elif size_len == 3:
            frame_size = int.from_bytes(raw_size, "big")
        else:
            frame_size = struct.unpack(">I", raw_size)[0]
        frame_flags = (
            struct.unpack(">H", body[pos + id_len + size_len : pos + id_len + size_len + 2])[0]
            if flag_len
            else 0
        )
        start = pos + id_len + size_len + flag_len
        payload = body[start : start + frame_size]
        pos = start + frame_size
        if frame_size <= 0 or not payload:
            continue
        if major >= 4:
            if frame_flags & 0x0002:  # frame-level unsynchronisation
                payload = payload.replace(b"\xff\x00", b"\xff")
            if frame_flags & 0x0001 and len(payload) >= 4:  # data length indicator
                payload = payload[4:]
        name = frame_id.decode("latin-1", "replace")
        _absorb_id3_frame(tags, name, payload, covers)

    if covers:
        covers.sort(key=lambda item: _cover_rank(item[0]))
        _, mime, blob = covers[0]
        tags.cover, tags.cover_mime = blob, mime
    return tags


def _cover_rank(picture_type: int) -> int:
    try:
        return COVER_PRIORITY.index(picture_type)
    except ValueError:
        return len(COVER_PRIORITY) + picture_type


_ID3_TEXT_MAP = {
    "TIT2": "title",
    "TT2": "title",
    "TPE1": "artist",
    "TP1": "artist",
    "TPE2": "album_artist",
    "TP2": "album_artist",
    "TALB": "album",
    "TAL": "album",
    "TCON": "genre",
    "TCO": "genre",
}


def _absorb_id3_frame(
    tags: Tags, name: str, payload: bytes, covers: list[tuple[int, str, bytes]]
) -> None:
    if name in _ID3_TEXT_MAP:
        values = _decode_text(payload)
        if values:
            value = values[0]
            if name in ("TCON", "TCO"):
                value = _expand_genre(value)
            setattr(tags, _ID3_TEXT_MAP[name], value)
        return
    if name in ("TRCK", "TRK"):
        values = _decode_text(payload)
        if values:
            tags.track_no = _first_int(values[0])
        return
    if name in ("TYER", "TYE", "TDRC", "TDRL", "TDAT", "TORY"):
        values = _decode_text(payload)
        if values and tags.year is None:
            tags.year = _year_from(values[0])
        return
    if name in ("TBPM", "TBP"):
        values = _decode_text(payload)
        if values:
            with contextlib.suppress(ValueError):
                tags.bpm = float(values[0].replace(",", "."))
        return
    if name in ("TLEN",):
        values = _decode_text(payload)
        if values:
            ms = _first_int(values[0])
            if ms:
                tags.duration = ms / 1000.0
        return
    if name in ("COMM", "COM") and not tags.comment:
        # encoding(1) + language(3) + short description + text
        if len(payload) > 4:
            encoding = payload[0]
            _, rest = _split_encoded(payload[4:], encoding)
            text = _decode_text(bytes([encoding]) + rest)
            if text:
                tags.comment = text[0]
        return
    if name == "TXXX" and payload:
        encoding = payload[0]
        key, rest = _split_encoded(payload[1:], encoding)
        values = _decode_text(bytes([encoding]) + rest)
        if key and values:
            tags.extra[key.upper()] = values[0]
        return
    if name in ("APIC", "PIC") and payload:
        entry = _parse_picture_frame(name, payload)
        if entry:
            covers.append(entry)


def _parse_picture_frame(name: str, payload: bytes) -> tuple[int, str, bytes] | None:
    encoding = payload[0]
    rest = payload[1:]
    if name == "PIC":
        # ID3v2.2 stores a three-character image format instead of a MIME type.
        if len(rest) < 4:
            return None
        fmt = rest[:3].decode("latin-1", "replace").upper()
        mime = "image/png" if fmt == "PNG" else "image/jpeg"
        picture_type = rest[3]
        rest = rest[4:]
    else:
        mime, rest = _split_encoded(rest, 0)
        if not rest:
            return None
        picture_type = rest[0]
        rest = rest[1:]
        mime = mime.strip() or "image/jpeg"
        if mime == "-->":  # a URL, not image data
            return None
    _description, blob = _split_encoded(rest, encoding)
    if len(blob) < 100:
        return None
    return picture_type, _sniff_image_mime(blob) or mime, blob


def _parse_id3v1(data: bytes) -> Tags:
    tags = Tags()
    if len(data) < 128 or data[-128:-125] != b"TAG":
        return tags
    block = data[-128:]

    def field_at(start: int, end: int) -> str:
        return block[start:end].split(b"\x00")[0].decode("latin-1", "replace").strip()

    tags.title = field_at(3, 33)
    tags.artist = field_at(33, 63)
    tags.album = field_at(63, 93)
    tags.year = _year_from(field_at(93, 97))
    if block[125] == 0 and block[126] != 0:
        tags.track_no = block[126]
    genre = block[127]
    if genre < len(ID3V1_GENRES):
        tags.genre = ID3V1_GENRES[genre]
    return tags


# -------------------------------------------------------------------- FLAC


def _parse_flac(data: bytes) -> Tags:
    tags = Tags()
    pos = 4
    covers: list[tuple[int, str, bytes]] = []
    while pos + 4 <= len(data):
        header = data[pos]
        block_type = header & 0x7F
        last = bool(header & 0x80)
        length = int.from_bytes(data[pos + 1 : pos + 4], "big")
        payload = data[pos + 4 : pos + 4 + length]
        pos += 4 + length
        if block_type == 0 and len(payload) >= 18:  # STREAMINFO
            rate = int.from_bytes(payload[10:13], "big") >> 4
            total = int.from_bytes(payload[13:18], "big") & 0x0FFFFFFFFF
            if rate:
                tags.duration = total / rate
        elif block_type == 4:
            _absorb_vorbis_comment(tags, payload, covers)
        elif block_type == 6:
            entry = _parse_flac_picture(payload)
            if entry:
                covers.append(entry)
        if last:
            break
    if covers:
        covers.sort(key=lambda item: _cover_rank(item[0]))
        _, mime, blob = covers[0]
        tags.cover, tags.cover_mime = blob, mime
    return tags


def _parse_flac_picture(payload: bytes) -> tuple[int, str, bytes] | None:
    if len(payload) < 32:
        return None
    picture_type = struct.unpack(">I", payload[0:4])[0]
    mime_len = struct.unpack(">I", payload[4:8])[0]
    pos = 8 + mime_len
    mime = payload[8:pos].decode("latin-1", "replace")
    desc_len = struct.unpack(">I", payload[pos : pos + 4])[0]
    pos += 4 + desc_len
    pos += 16  # width, height, depth, colour count
    data_len = struct.unpack(">I", payload[pos : pos + 4])[0]
    pos += 4
    blob = payload[pos : pos + data_len]
    if len(blob) < 100:
        return None
    return picture_type, _sniff_image_mime(blob) or mime or "image/jpeg", blob


_VORBIS_MAP = {
    "TITLE": "title",
    "ARTIST": "artist",
    "ALBUMARTIST": "album_artist",
    "ALBUM": "album",
    "GENRE": "genre",
    "COMMENT": "comment",
    "DESCRIPTION": "comment",
}


def _absorb_vorbis_comment(
    tags: Tags, payload: bytes, covers: list[tuple[int, str, bytes]]
) -> None:
    if len(payload) < 8:
        return
    vendor_len = struct.unpack("<I", payload[0:4])[0]
    pos = 4 + vendor_len
    if pos + 4 > len(payload):
        return
    count = struct.unpack("<I", payload[pos : pos + 4])[0]
    pos += 4
    for _ in range(min(count, 4096)):
        if pos + 4 > len(payload):
            return
        length = struct.unpack("<I", payload[pos : pos + 4])[0]
        pos += 4
        item = payload[pos : pos + length].decode("utf-8", "replace")
        pos += length
        if "=" not in item:
            continue
        key, _, value = item.partition("=")
        key = key.upper().strip()
        value = value.strip()
        if not value:
            continue
        if key in _VORBIS_MAP:
            if not getattr(tags, _VORBIS_MAP[key]):
                setattr(tags, _VORBIS_MAP[key], value)
        elif key == "DATE" and tags.year is None:
            tags.year = _year_from(value)
        elif key in ("TRACKNUMBER", "TRACK") and tags.track_no is None:
            tags.track_no = _first_int(value)
        elif key == "BPM" and tags.bpm is None:
            with contextlib.suppress(ValueError):
                tags.bpm = float(value.replace(",", "."))
        elif key == "METADATA_BLOCK_PICTURE":
            try:
                entry = _parse_flac_picture(base64.b64decode(value, validate=False))
            except (binascii.Error, ValueError):
                entry = None
            if entry:
                covers.append(entry)
        else:
            tags.extra.setdefault(key, value)


# --------------------------------------------------------------------- Ogg


def _ogg_packets(data: bytes, max_packets: int = 4) -> list[bytes]:
    """Reassemble the first few logical packets from an Ogg bitstream."""
    packets: list[bytes] = []
    current = bytearray()
    pos = 0
    while pos + 27 <= len(data) and len(packets) < max_packets:
        if data[pos : pos + 4] != b"OggS":
            break
        segments = data[pos + 26]
        table_at = pos + 27
        table = data[table_at : table_at + segments]
        body_at = table_at + segments
        offset = body_at
        for length in table:
            current += data[offset : offset + length]
            offset += length
            if length < 255:
                packets.append(bytes(current))
                current = bytearray()
                if len(packets) >= max_packets:
                    break
        pos = body_at + sum(table)
    return packets


def _parse_ogg(data: bytes) -> Tags:
    tags = Tags()
    covers: list[tuple[int, str, bytes]] = []
    for packet in _ogg_packets(data):
        if packet[:7] == b"\x03vorbis":
            tags.container = "ogg"
            _absorb_vorbis_comment(tags, packet[7:], covers)
        elif packet[:8] == b"OpusTags":
            tags.container = "opus"
            _absorb_vorbis_comment(tags, packet[8:], covers)
        elif packet[:1] == b"\x01" and packet[1:7] == b"vorbis":
            tags.container = "ogg"
        elif packet[:8] == b"OpusHead":
            tags.container = "opus"
    if covers:
        covers.sort(key=lambda item: _cover_rank(item[0]))
        _, mime, blob = covers[0]
        tags.cover, tags.cover_mime = blob, mime
    return tags


# --------------------------------------------------------------------- MP4

_MP4_MAP = {
    "©nam": "title",
    "©ART": "artist",
    "aART": "album_artist",
    "©alb": "album",
    "©gen": "genre",
    "©cmt": "comment",
}


def _mp4_children(data: bytes, start: int, end: int) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", data[pos : pos + 4])[0]
        name = data[pos + 4 : pos + 8].decode("latin-1", "replace")
        header = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            break
        out.append((name, pos + header, pos + size))
        pos += size
    return out


def _parse_mp4(data: bytes) -> Tags:
    tags = Tags()
    ilst: tuple[int, int] | None = None
    for name, start, end in _mp4_children(data, 0, len(data)):
        if name != "moov":
            continue
        for name2, s2, e2 in _mp4_children(data, start, end):
            if name2 == "mvhd" and e2 - s2 >= 20:
                version = data[s2]
                if version == 0:
                    scale, units = struct.unpack(">II", data[s2 + 12 : s2 + 20])
                else:
                    scale, units = struct.unpack(">IQ", data[s2 + 20 : s2 + 32])
                if scale:
                    tags.duration = units / scale
            if name2 != "udta":
                continue
            for name3, s3, e3 in _mp4_children(data, s2, e2):
                if name3 != "meta":
                    continue
                # 'meta' is a full box: four bytes of version/flags first.
                for name4, s4, e4 in _mp4_children(data, s3 + 4, e3):
                    if name4 == "ilst":
                        ilst = (s4, e4)
    if not ilst:
        return tags
    covers: list[tuple[int, str, bytes]] = []
    for name, start, end in _mp4_children(data, ilst[0], ilst[1]):
        for inner, s2, e2 in _mp4_children(data, start, end):
            if inner != "data" or e2 - s2 < 8:
                continue
            flag = struct.unpack(">I", data[s2 : s2 + 4])[0] & 0x00FFFFFF
            payload = data[s2 + 8 : e2]
            if name == "covr":
                if len(payload) >= 100:
                    mime = "image/png" if flag == 14 else "image/jpeg"
                    covers.append((3, _sniff_image_mime(payload) or mime, payload))
                continue
            if name == "trkn" and len(payload) >= 4:
                tags.track_no = struct.unpack(">H", payload[2:4])[0] or None
                continue
            text = payload.decode("utf-8", "replace").strip()
            if not text:
                continue
            if name in _MP4_MAP and not getattr(tags, _MP4_MAP[name]):
                setattr(tags, _MP4_MAP[name], text)
            elif name == "©day" and tags.year is None:
                tags.year = _year_from(text)
            elif name == "tmpo" and len(payload) >= 2:
                tags.bpm = float(struct.unpack(">H", payload[:2])[0])
    if covers:
        _, mime, blob = covers[0]
        tags.cover, tags.cover_mime = blob, mime
    return tags


# --------------------------------------------------------------------- WAV

_WAV_MAP = {"INAM": "title", "IART": "artist", "IPRD": "album", "IGNR": "genre"}


def _parse_wav(data: bytes) -> Tags:
    tags = Tags()
    pos = 12
    while pos + 8 <= len(data):
        chunk = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = data[pos + 8 : pos + 8 + size]
        if chunk == b"LIST" and body[:4] == b"INFO":
            inner = 4
            while inner + 8 <= len(body):
                key = body[inner : inner + 4].decode("latin-1", "replace")
                klen = struct.unpack("<I", body[inner + 4 : inner + 8])[0]
                value = body[inner + 8 : inner + 8 + klen].split(b"\x00")[0]
                text = value.decode("utf-8", "replace").strip()
                if key in _WAV_MAP and text and not getattr(tags, _WAV_MAP[key]):
                    setattr(tags, _WAV_MAP[key], text)
                elif key == "ICRD" and text and tags.year is None:
                    tags.year = _year_from(text)
                inner += 8 + klen + (klen & 1)
        pos += 8 + size + (size & 1)
    return tags


def _sniff_image_mime(blob: bytes) -> str:
    """Trust the bytes, not the declared MIME type."""
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def image_extension(mime: str) -> str:
    return {
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime, ".jpg")


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys([0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF, 0x00AD], None)
_PUNCT_FOLD = {
    ord("‘"): "'",
    ord("’"): "'",
    ord("‛"): "'",
    ord("ʼ"): "'",
    ord("“"): '"',
    ord("”"): '"',
    ord("«"): '"',
    ord("»"): '"',
    ord("–"): "-",
    ord("—"): "-",
    ord("−"): "-",
    ord("‐"): "-",
    ord(" "): " ",
    ord("…"): "...",
}

#: Bracketed noise that says nothing about the music.
_JUNK_PATTERNS = re.compile(
    r"""
    \s*[\(\[\{]\s*
    (?:
        official(?:\s+(?:music\s+)?(?:video|audio|lyric[s]?|visualizer))?
      | lyric[s]?(?:\s+video)?
      | audio | video | visuali[sz]er | hd | hq | 4k | full\s+album
      | free(?:\s*(?:dl|download|beat|for\s+profit))? | no\s*tags?
      | explicit | clean | radio\s+edit | album\s+version
      | out\s+now | premiere | exclusive
      | prod(?:\.|uced)?\s*(?:by)?[^\)\]\}]*
      | \d{2,4}\s*(?:kbps|bpm)
    )
    \s*[\)\]\}]
    """,
    re.IGNORECASE | re.VERBOSE,
)
_TRAILING_JUNK = re.compile(
    r"\s*(?:[|/-]\s*)?(?:official\s+(?:music\s+)?video|official\s+audio|lyric\s+video|"
    r"free\s+download|out\s+now)\s*$",
    re.IGNORECASE,
)
_FEAT = re.compile(
    r"\s*[\(\[]?\s*\b(?:feat|ft|featuring|with)\b\.?\s*(?P<who>[^\)\]]+)[\)\]]?\s*$",
    re.IGNORECASE,
)
_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|;|&|\+|/|\b(?:x|vs|versus|feat|ft|featuring|and|with)\b\.?)\s*",
    re.IGNORECASE,
)
#: A trailing producer credit belongs in the notes, not in the title.
_PRODUCER = re.compile(
    r"\s*[\(\[]?\s*\bprod(?:\.|uced)?\b\.?\s*(?:by\b)?[^\)\]]*[\)\]]?\s*$",
    re.IGNORECASE,
)

_CYRILLIC = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "ґ": "g",
    "є": "ie",
    "і": "i",
    "ї": "i",
    "ў": "u",
}


def clean_text(value: str) -> str:
    """NFKC, unify punctuation, drop zero-width characters, collapse spaces."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = text.translate(_ZERO_WIDTH).translate(_PUNCT_FOLD)
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch)[0] != "C")
    return re.sub(r"\s+", " ", text).strip()


def clean_title(value: str) -> str:
    """Strip promotional debris from a track title."""
    text = clean_text(value)
    previous = None
    while previous != text:
        previous = text
        text = _JUNK_PATTERNS.sub(" ", text)
        text = _TRAILING_JUNK.sub("", text)
        stripped = _PRODUCER.sub("", text)
        # Only accept the producer strip if something recognisable survives.
        if stripped.strip(" -–—_|·"):
            text = stripped
        text = re.sub(r"\s+", " ", text).strip(" -–—_|·")
    return text.strip()


def split_feature(title: str) -> tuple[str, list[str]]:
    """Separate a trailing ``feat. X`` from a title."""
    text = clean_title(title)
    match = _FEAT.search(text)
    if not match:
        return text, []
    guests = [g for g in (p.strip() for p in _ARTIST_SPLIT.split(match.group("who"))) if g]
    return text[: match.start()].strip(" -–—_|·"), guests


def split_artists(value: str) -> list[str]:
    """Split a credit string into individual artist names, order preserved."""
    text = clean_text(value)
    if not text:
        return []
    parts = [p.strip(" -–—_|·") for p in _ARTIST_SPLIT.split(text)]
    seen: dict[str, None] = {}
    for part in parts:
        if part and part.lower() not in seen:
            seen[part.lower()] = None
            seen[part] = None
    return [p for p in parts if p][:8]


def fold(value: str) -> str:
    """Aggressive case/diacritic folding used for equality, never for display."""
    text = clean_text(value).casefold()
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def key_of(value: str) -> str:
    """Stable identity key: folded, transliterated, alphanumerics only."""
    text = translit(fold(value))
    return re.sub(r"[^0-9a-z]+", "", text)


def translit(value: str) -> str:
    """Cyrillic to Latin, so a mixed-script catalogue has one search space."""
    out: list[str] = []
    for ch in value:
        lower = ch.lower()
        mapped = _CYRILLIC.get(lower)
        if mapped is None:
            out.append(ch)
        elif ch == lower:
            out.append(mapped)
        else:
            out.append(mapped.capitalize() if len(mapped) > 1 else mapped.upper())
    return "".join(out)


def has_cyrillic(value: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in value)


def similarity(left: str, right: str) -> float:
    """0..1 similarity over folded strings; used for duplicate detection."""
    a, b = key_of(left), key_of(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _first_int(value: str) -> int | None:
    match = re.search(r"\d+", value or "")
    return int(match.group()) if match else None


def _year_from(value: str) -> int | None:
    match = re.search(r"(1[89]\d{2}|20\d{2}|21\d{2})", value or "")
    return int(match.group()) if match else None


def _expand_genre(value: str) -> str:
    """Resolve ID3's ``(17)`` and ``17`` numeric genre references."""
    text = clean_text(value)
    match = re.fullmatch(r"\(?(\d{1,3})\)?", text)
    if match:
        index = int(match.group(1))
        if index < len(ID3V1_GENRES):
            return ID3V1_GENRES[index]
    return re.sub(r"^\((\d{1,3})\)\s*", "", text)


def parse_filename(name: str) -> Tags:
    """Last-resort metadata from ``Artist - Title.mp3``-style filenames."""
    tags = Tags()
    if not name:
        return tags
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", clean_text(name))
    stem = re.sub(r"^\s*\d{1,3}\s*[-._)]\s*", "", stem)
    for sep in (" - ", " – ", " — ", " _ ", "-"):
        if sep in stem:
            left, _, right = stem.partition(sep)
            if left.strip() and right.strip():
                tags.artist = left.strip()
                tags.title = right.strip()
                return tags
    tags.title = stem.strip()
    return tags


def normalise_tag(value: str) -> str:
    """Canonical form of a curator tag: lowercase, single spaces, no hashes."""
    text = clean_text(value).lstrip("#").casefold()
    text = re.sub(r"[^0-9a-zЀ-ӿ\- ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:32]
