"""Bounded offline reads and strict in-memory CSV/ZIP containers.

The caller archives ``LocalArchive.body`` before calling ``csv_members``. No
archive member is ever written to disk or executed. These helpers do not grant
permission to acquire, publish or derive information from an input file.
"""
from dataclasses import dataclass
import io
import os
from pathlib import Path
import stat
import struct
import unicodedata
import zipfile
from zlib import crc32, error as ZlibError

from ..domain.models import BrokerFlowError


@dataclass(frozen=True, kw_only=True)
class FileLimits:
    max_archive_bytes: int = 20_000_000
    max_member_bytes: int = 20_000_000
    max_total_bytes: int = 20_000_000
    max_members: int = 32
    max_compression_ratio: int = 100

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise BrokerFlowError("invalid_broker_file_limits")
        if (self.max_archive_bytes > 20_000_000 or self.max_member_bytes > 20_000_000
                or self.max_total_bytes > 20_000_000 or self.max_members > 128
                or self.max_compression_ratio > 100):
            raise BrokerFlowError("unsupported_broker_file_limits")


DEFAULT_LIMITS = FileLimits()


@dataclass(frozen=True, kw_only=True)
class LocalArchive:
    filename: str
    body: bytes
    content_type: str


@dataclass(frozen=True, kw_only=True)
class CsvMember:
    name: str
    body: bytes


def _extension(filename: str) -> str:
    if (not isinstance(filename, str) or not filename or len(filename) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)):
        raise BrokerFlowError("invalid_broker_filename")
    try:
        filename.encode("utf-8")
    except UnicodeError:
        raise BrokerFlowError("invalid_broker_filename") from None
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".zip"}:
        raise BrokerFlowError("unsupported_broker_file_type")
    return suffix


def read_local_archive(path: str | Path, *, limits: FileLimits = DEFAULT_LIMITS) -> LocalArchive:
    """Read a regular file with no symlink traversal or unbounded allocation.

    Format checking is deliberately deferred until after Raw Archive commits.
    The size and path checks protect the act of acquiring the original bytes.
    """
    try:
        text = os.fspath(path)
        if not isinstance(text, str) or "\x00" in text:
            raise BrokerFlowError("invalid_broker_file_path")
        absolute = os.path.abspath(text)
        name = os.path.basename(absolute)
        suffix = _extension(name)
        parts = Path(absolute).parts
        # Opening each directory prevents a symlink ancestor from redirecting
        # the read between a path check and open(). Linux/WSL supplies these flags.
        directory = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                | os.O_CLOEXEC, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW
                                 | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise BrokerFlowError("broker_file_must_be_regular")
            if before.st_size > limits.max_archive_bytes:
                raise BrokerFlowError("broker_file_too_large")
            stream = os.fdopen(descriptor, "rb")
            descriptor = None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        with stream:
            body = stream.read(limits.max_archive_bytes + 1)
            after = os.fstat(stream.fileno())
            if len(body) > limits.max_archive_bytes:
                raise BrokerFlowError("broker_file_too_large")
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise BrokerFlowError("broker_file_changed_during_read")
    except BrokerFlowError:
        raise
    except (OSError, ValueError, TypeError):
        raise BrokerFlowError("broker_file_unavailable") from None
    return LocalArchive(filename=name, body=body,
                        content_type="application/zip" if suffix == ".zip" else "text/csv")


def _csv_bytes(body: bytes) -> None:
    candidate = body[3:] if body.startswith(b"\xef\xbb\xbf") else body
    if (b"\x00" in candidate or candidate.startswith((
            b"MZ", b"\x7fELF", b"#!", b"PK\x03\x04", b"PK\x05\x06", b"%PDF",
            b"\x1f\x8b", b"Rar!", b"7z\xbc\xaf\x27\x1c", b"BZh", b"\xfd7zXZ",
            b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
            b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"))):
        raise BrokerFlowError("untrusted_broker_file_format")


def _member_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if (name != info.orig_filename or not name or len(name.encode("utf-8")) > 512
            or name != unicodedata.normalize("NFC", name)
            or "\\" in name or ":" in name or name.startswith("/")
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise BrokerFlowError("unsafe_broker_zip_path")
    trimmed = name[:-1] if info.is_dir() else name
    parts = trimmed.split("/")
    if any(not part or part in {".", ".."} or part.endswith((" ", ".")) for part in parts):
        raise BrokerFlowError("unsafe_broker_zip_path")
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or (not info.is_dir() and mode & 0o111):
        raise BrokerFlowError("unsafe_broker_zip_member")
    if (kind == stat.S_IFDIR) != info.is_dir() and kind != 0:
        raise BrokerFlowError("unsafe_broker_zip_member")
    if info.flag_bits & ~0x80e or info.flag_bits & 1:
        raise BrokerFlowError("unsupported_broker_zip_flags")
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise BrokerFlowError("unsupported_broker_zip_compression")
    if info.extract_version > 20 or info.file_size < 0 or info.compress_size < 0:
        raise BrokerFlowError("unsupported_broker_zip_version")
    if not info.is_dir() and Path(name).suffix.lower() != ".csv":
        raise BrokerFlowError("unsupported_broker_zip_member")
    return name


def _zip_metadata(body: bytes, max_members: int) -> tuple[int, int, int]:
    """Bound central-directory parsing before ZipFile allocates member objects."""
    end = body.rfind(b"PK\x05\x06", max(0, len(body) - 65_557))
    if end < 0 or end + 22 > len(body):
        raise BrokerFlowError("invalid_broker_zip")
    _, disk, central_disk, count_disk, count, central_size, central_offset, comment_size = struct.unpack_from(
        "<4s4H2IH", body, end)
    if (disk or central_disk or count_disk != count
            or count == 0xffff or central_offset == 0xffffffff or central_size == 0xffffffff
            or central_offset + central_size != end or end + 22 + comment_size != len(body)
            or comment_size):
        raise BrokerFlowError("unsupported_broker_zip_layout")
    if not 0 < count <= max_members:
        raise BrokerFlowError("broker_zip_member_count")
    cursor, actual_count = central_offset, 0
    while cursor < end:
        if cursor + 46 > end or body[cursor:cursor+4] != b"PK\x01\x02":
            raise BrokerFlowError("invalid_broker_zip_directory")
        name_length, extra_length, comment_length = struct.unpack_from("<3H", body, cursor+28)
        if name_length > 512:
            raise BrokerFlowError("unsafe_broker_zip_path")
        cursor += 46 + name_length + extra_length + comment_length
        actual_count += 1
        if cursor > end or actual_count > max_members:
            raise BrokerFlowError("broker_zip_member_count")
    if actual_count != count:
        raise BrokerFlowError("invalid_broker_zip_directory")
    return count, central_offset, end


def _zip_layout(body: bytes, entries: list[zipfile.ZipInfo], central_offset: int) -> None:
    """Reject executable prefixes, concatenated archives and hidden trailing data."""
    cursor = 0
    for info in sorted(entries, key=lambda item: item.header_offset):
        if info.header_offset != cursor or cursor + 30 > central_offset:
            raise BrokerFlowError("unsafe_broker_zip_layout")
        (signature, version, flags, compression, _, _, local_crc, compressed, expanded,
         name_length, extra_length) = struct.unpack_from("<4s5H3I2H", body, cursor)
        if (signature != b"PK\x03\x04" or flags != info.flag_bits
                or compression != info.compress_type or version > 20):
            raise BrokerFlowError("invalid_broker_zip_header")
        cursor += 30 + name_length + extra_length + info.compress_size
        if cursor > central_offset:
            raise BrokerFlowError("invalid_broker_zip_header")
        if flags & 8:
            # Streaming ZIP writers may put CRC/sizes after the compressed data.
            if body[cursor:cursor+4] == b"PK\x07\x08":
                cursor += 4
            if cursor + 12 > central_offset:
                raise BrokerFlowError("invalid_broker_zip_descriptor")
            values = struct.unpack_from("<3I", body, cursor)
            if values != (info.CRC, info.compress_size, info.file_size):
                raise BrokerFlowError("invalid_broker_zip_descriptor")
            cursor += 12
        elif (local_crc, compressed, expanded) != (info.CRC, info.compress_size, info.file_size):
            raise BrokerFlowError("invalid_broker_zip_header")
    if cursor != central_offset:
        raise BrokerFlowError("unsafe_broker_zip_layout")


def csv_members(body: bytes, filename: str, *, limits: FileLimits = DEFAULT_LIMITS) -> tuple[CsvMember, ...]:
    """Validate an archived container and return original member bytes in memory."""
    suffix = _extension(filename)
    if not isinstance(body, bytes) or len(body) > limits.max_archive_bytes:
        raise BrokerFlowError("broker_file_too_large")
    if suffix == ".csv":
        _csv_bytes(body)
        if len(body) > limits.max_member_bytes or len(body) > limits.max_total_bytes:
            raise BrokerFlowError("broker_csv_too_large")
        return (CsvMember(name=Path(filename).name, body=body),)
    if not body.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        raise BrokerFlowError("untrusted_broker_file_format")
    try:
        count, central_offset, _ = _zip_metadata(body, limits.max_members)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) != count:
                raise BrokerFlowError("broker_zip_member_count")
            names = [_member_name(info) for info in entries]
            if len({name.rstrip("/").casefold() for name in names}) != len(names):
                raise BrokerFlowError("duplicate_broker_zip_path")
            total = 0
            for info in entries:
                if info.is_dir():
                    if info.file_size or info.compress_size:
                        raise BrokerFlowError("unsafe_broker_zip_directory")
                    continue
                total += info.file_size
                if (info.file_size > limits.max_member_bytes or total > limits.max_total_bytes
                        or info.file_size > max(info.compress_size, 1) * limits.max_compression_ratio):
                    raise BrokerFlowError("broker_zip_expansion_limit")
            _zip_layout(body, entries, central_offset)
            members = []
            for info in sorted(entries, key=lambda item: item.filename):
                if info.is_dir():
                    continue
                with archive.open(info, "r") as stream:
                    content = stream.read(limits.max_member_bytes + 1)
                if len(content) != info.file_size or len(content) > limits.max_member_bytes:
                    raise BrokerFlowError("broker_zip_size_mismatch")
                if crc32(content) & 0xffffffff != info.CRC:
                    raise BrokerFlowError("broker_zip_crc_mismatch")
                _csv_bytes(content)
                members.append(CsvMember(name=info.filename, body=content))
            if not members:
                raise BrokerFlowError("broker_zip_no_csv")
            return tuple(members)
    except BrokerFlowError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError, ValueError,
            NotImplementedError, EOFError, UnicodeError, struct.error, ZlibError):
        raise BrokerFlowError("invalid_broker_zip") from None
