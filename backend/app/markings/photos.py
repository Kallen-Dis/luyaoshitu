"""标注照片：格式校验、尺寸解析、去除元数据。

照片是公开给其他用户和管理员看的，而手机照片的 EXIF 里通常带着拍摄时的 GPS 坐标、
设备型号和时间——把它原样存下来再分发，等于公开了上传者的行踪。前端上传前会用 canvas
重新编码（这一步本身就会丢掉 EXIF），但服务端不能信任客户端，这里再按容器格式逐段过一遍：

- JPEG：去掉 APP1（EXIF / XMP）、APP3~APP13、APP15 与注释段，保留 JFIF、ICC 色彩（APP2）
  与 Adobe（APP14，个别 CMYK 图解码要用）；EOI 之后夹带的数据一并丢弃；
- PNG：逐块校验 CRC，去掉 eXIf、tEXt、zTXt、iTXt、tIME；
- WebP：去掉 EXIF、XMP 块，并清掉 VP8X 里对应的标志位，重算 RIFF 长度。

不解码像素，所以没有解压炸弹的风险；但会限制宽高，免得一张超大图拖垮查看者的浏览器。
不依赖 Pillow：它是本项目唯一会因此引入的原生依赖。
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass

MAX_BYTES = 4 * 1024 * 1024
MIN_SIDE = 16
MAX_SIDE = 8000
MAX_PIXELS = 40_000_000

EXTENSIONS: dict[str, str] = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_JPEG_KEEP_APP = {0xE0, 0xE2, 0xEE}
_PNG_DROP = {b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME"}
_WEBP_DROP = {b"EXIF", b"XMP "}


class PhotoError(ValueError):
    """图片不合格。消息直接给用户看。"""


@dataclass(frozen=True)
class CleanImage:
    data: bytes
    mime: str
    width: int
    height: int
    sha256: str

    @property
    def ext(self) -> str:
        return EXTENSIONS[self.mime]


def sniff(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _broken(kind: str) -> PhotoError:
    return PhotoError(f"{kind} 文件不完整或已损坏，请换一张或重新拍摄")


def _jpeg(data: bytes) -> tuple[bytes, int, int]:
    n = len(data)
    out = bytearray(b"\xff\xd8")
    pos = 2
    width = height = 0
    saw_scan = False
    while True:
        if pos >= n or data[pos] != 0xFF:
            raise _broken("JPEG")
        while pos < n and data[pos] == 0xFF:
            pos += 1  # 标记前允许有填充字节
        if pos >= n:
            raise _broken("JPEG")
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # EOI：之后夹带的任何数据都丢掉
            out += b"\xff\xd9"
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            out += bytes((0xFF, marker))
            continue
        if marker == 0xD8:
            raise _broken("JPEG")
        if pos + 2 > n:
            raise _broken("JPEG")
        length = int.from_bytes(data[pos : pos + 2], "big")
        if length < 2 or pos + length > n:
            raise _broken("JPEG")
        segment = data[pos : pos + length]
        pos += length
        if marker in _SOF:
            if length < 8:
                raise _broken("JPEG")
            height = int.from_bytes(segment[3:5], "big")
            width = int.from_bytes(segment[5:7], "big")
        drop = (0xE1 <= marker <= 0xEF and marker not in _JPEG_KEEP_APP) or marker == 0xFE
        if not drop:
            out += bytes((0xFF, marker)) + segment
        if marker == 0xDA:
            saw_scan = True
            # 熵编码数据：一直读到下一个真正的标记（FF 后面不是 00、复位标记或填充）
            start = pos
            while True:
                idx = data.find(b"\xff", pos)
                if idx < 0 or idx + 1 >= n:
                    raise _broken("JPEG")
                nxt = data[idx + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                    pos = idx + 2
                    continue
                if nxt == 0xFF:
                    pos = idx + 1
                    continue
                out += data[start:idx]
                pos = idx
                break
    if not saw_scan or not width or not height:
        raise _broken("JPEG")
    return bytes(out), width, height


def _png(data: bytes) -> tuple[bytes, int, int]:
    n = len(data)
    out = bytearray(data[:8])
    pos = 8
    width = height = 0
    first = True
    saw_idat = saw_end = False
    while pos + 12 <= n:
        length = int.from_bytes(data[pos : pos + 4], "big")
        ctype = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if length > MAX_BYTES or end > n or not ctype.isalpha():
            raise _broken("PNG")
        body = data[pos + 8 : pos + 8 + length]
        crc = int.from_bytes(data[pos + 8 + length : end], "big")
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != crc:
            raise _broken("PNG")
        if first:
            if ctype != b"IHDR" or length != 13:
                raise _broken("PNG")
            width = int.from_bytes(body[0:4], "big")
            height = int.from_bytes(body[4:8], "big")
            first = False
        if ctype == b"IDAT":
            saw_idat = True
        if ctype not in _PNG_DROP:
            out += data[pos:end]
        pos = end
        if ctype == b"IEND":
            saw_end = True
            break
    if not (saw_idat and saw_end):
        raise _broken("PNG")
    return bytes(out), width, height


def _webp(data: bytes) -> tuple[bytes, int, int]:
    riff_size = int.from_bytes(data[4:8], "little")
    end = 8 + riff_size
    if riff_size < 4 or end > len(data):
        raise _broken("WebP")
    chunks: list[tuple[bytes, bytes]] = []
    pos = 12
    while pos + 8 <= end:
        fourcc = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        body_end = pos + 8 + size
        if body_end > end:
            raise _broken("WebP")
        chunks.append((fourcc, data[pos + 8 : body_end]))
        pos = body_end + (size & 1)
    if not chunks:
        raise _broken("WebP")

    width = height = 0
    kept: list[tuple[bytes, bytes]] = []
    for fourcc, body in chunks:
        if fourcc in _WEBP_DROP:
            continue
        if fourcc == b"VP8X":
            if len(body) < 10:
                raise _broken("WebP")
            flags = body[0] & ~0x0C  # 去掉 EXIF(0x08) 与 XMP(0x04) 标志
            body = bytes((flags,)) + body[1:]
            width = int.from_bytes(body[4:7], "little") + 1
            height = int.from_bytes(body[7:10], "little") + 1
        elif fourcc == b"VP8 " and not width:
            if len(body) < 10 or body[3:6] != b"\x9d\x01\x2a":
                raise _broken("WebP")
            width = int.from_bytes(body[6:8], "little") & 0x3FFF
            height = int.from_bytes(body[8:10], "little") & 0x3FFF
        elif fourcc == b"VP8L" and not width:
            if len(body) < 5 or body[0] != 0x2F:
                raise _broken("WebP")
            bits = int.from_bytes(body[1:5], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
        kept.append((fourcc, body))

    payload = bytearray(b"WEBP")
    for fourcc, body in kept:
        payload += fourcc + len(body).to_bytes(4, "little") + body
        if len(body) & 1:
            payload += b"\x00"
    return b"RIFF" + len(payload).to_bytes(4, "little") + bytes(payload), width, height


def sanitize(data: bytes) -> CleanImage:
    """校验并去除元数据。格式以文件头为准，不信任请求里声明的 Content-Type。"""
    if not data:
        raise PhotoError("没有收到图片内容")
    if len(data) > MAX_BYTES:
        raise PhotoError(f"图片超过 {MAX_BYTES // (1024 * 1024)} MB，请压缩后再上传")
    mime = sniff(data)
    if mime is None:
        raise PhotoError("只支持 JPEG、PNG、WebP 格式的照片")
    if mime == "image/jpeg":
        clean, width, height = _jpeg(data)
    elif mime == "image/png":
        clean, width, height = _png(data)
    else:
        clean, width, height = _webp(data)
    if min(width, height) < MIN_SIDE:
        raise PhotoError(f"图片太小（{width}×{height}），看不清现场")
    if max(width, height) > MAX_SIDE or width * height > MAX_PIXELS:
        raise PhotoError(f"图片尺寸过大（{width}×{height}），请缩小后再上传")
    return CleanImage(
        data=clean,
        mime=mime,
        width=width,
        height=height,
        sha256=hashlib.sha256(clean).hexdigest(),
    )
