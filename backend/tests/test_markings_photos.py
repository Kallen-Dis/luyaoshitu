"""照片：只认文件头、去掉 EXIF 等元数据、校验结构与尺寸。"""

import struct
import zlib

import pytest

from app.markings.photos import PhotoError, sanitize

GPS = b"GPSLatitude31.25"


def _seg(marker: int, body: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", len(body) + 2) + body


def jpeg(width=640, height=480, extra=b"") -> bytes:
    app0 = _seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    exif = _seg(0xE1, b"Exif\x00\x00" + GPS)
    comment = _seg(0xFE, b"shot by someone")
    sof = _seg(0xC0, b"\x08" + struct.pack(">HH", height, width) + b"\x01\x01\x11\x00")
    sos = _seg(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    # 熵编码数据里的 FF00（字节填充）与 RST 标记都不是段边界
    scan = b"\x12\x34\xff\x00\x56\xff\xd0\x78"
    return b"\xff\xd8" + app0 + exif + comment + sof + sos + scan + b"\xff\xd9" + extra


def _chunk(ctype: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + ctype
        + body
        + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)
    )


def png(width=320, height=200, corrupt=False) -> bytes:
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    text = _chunk(b"tEXt", b"Comment\x00" + GPS)
    idat = _chunk(b"IDAT", zlib.compress(b"\x00" * 16))
    if corrupt:
        idat = idat[:-1] + bytes([idat[-1] ^ 0xFF])
    return b"\x89PNG\r\n\x1a\n" + ihdr + text + idat + _chunk(b"IEND", b"")


def webp(width=300, height=150) -> bytes:
    def chunk(fourcc: bytes, body: bytes) -> bytes:
        return fourcc + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) & 1 else b"")

    flags = 0x08 | 0x04  # EXIF + XMP
    vp8x = (
        bytes((flags, 0, 0, 0))
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    vp8 = b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"\x00" * 7
    body = b"WEBP" + chunk(b"VP8X", vp8x) + chunk(b"VP8 ", vp8) + chunk(b"EXIF", GPS)
    body += chunk(b"XMP ", b"<x:xmpmeta/>")
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_jpeg_metadata_is_stripped_and_size_parsed():
    out = sanitize(jpeg(extra=b"hidden trailing payload"))
    assert out.mime == "image/jpeg" and (out.width, out.height) == (640, 480)
    assert GPS not in out.data and b"shot by someone" not in out.data
    assert b"JFIF" in out.data  # JFIF 头保留
    assert out.data.endswith(b"\xff\xd9")  # EOI 之后夹带的数据丢弃
    assert b"\x12\x34\xff\x00\x56\xff\xd0\x78" in out.data  # 图像数据原样保留
    assert len(out.sha256) == 64


def test_png_text_chunks_are_dropped_and_crc_checked():
    out = sanitize(png())
    assert out.mime == "image/png" and (out.width, out.height) == (320, 200)
    assert GPS not in out.data and b"IDAT" in out.data
    with pytest.raises(PhotoError, match="损坏"):
        sanitize(png(corrupt=True))


def test_webp_exif_and_xmp_are_removed_and_flags_cleared():
    out = sanitize(webp())
    assert out.mime == "image/webp" and (out.width, out.height) == (300, 150)
    assert GPS not in out.data and b"xmpmeta" not in out.data
    flags = out.data[20]  # RIFF(4) size(4) WEBP(4) VP8X(4) size(4) → 载荷首字节
    assert flags & 0x0C == 0
    assert struct.unpack("<I", out.data[4:8])[0] == len(out.data) - 8


def test_declared_type_is_ignored_and_junk_is_rejected():
    with pytest.raises(PhotoError, match="只支持"):
        sanitize(b"GIF89a....")
    with pytest.raises(PhotoError, match="没有收到"):
        sanitize(b"")
    with pytest.raises(PhotoError, match="损坏"):
        sanitize(jpeg()[:40])


def test_dimensions_are_bounded():
    with pytest.raises(PhotoError, match="太小"):
        sanitize(png(width=8, height=8))
    with pytest.raises(PhotoError, match="过大"):
        sanitize(png(width=9000, height=100))
