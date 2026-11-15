"""SYNTHETIC offline files and malicious containers; no external downloads."""
import io
from pathlib import Path
import stat
import struct
import warnings
import zipfile

import pytest

from stockapp.modules.broker_flow.domain.models import BrokerFlowError
from stockapp.modules.broker_flow.infrastructure.files import (
    FileLimits, csv_members, read_local_archive,
)


SYNTHETIC_BODY = b"SYNTHETIC,not-an-official-report\r\n"


def synthetic_zip(entries=(("SYNTHETIC.csv", SYNTHETIC_BODY),), *, compression=zipfile.ZIP_DEFLATED,
                  comment=b"", stream=None):
    stream = stream or io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # Intentional duplicate fixtures.
        with zipfile.ZipFile(stream, "w", compression=compression) as archive:
            for name, body in entries:
                archive.writestr(name, body)
            archive.comment = comment
    return stream.getvalue()


def test_read_returns_exact_original_bytes_before_any_format_validation(tmp_path):
    path = tmp_path / "SYNTHETIC.csv"
    body = b"MZ-SYNTHETIC-must-be-archived-before-format-rejection"
    path.write_bytes(body)
    raw = read_local_archive(path)
    assert raw.body == body and raw.filename == path.name and raw.content_type == "text/csv"
    with pytest.raises(BrokerFlowError, match="untrusted_broker_file_format"):
        csv_members(raw.body, raw.filename)


@pytest.mark.parametrize("name", ["SYNTHETIC.exe", "SYNTHETIC.csv.exe", "SYNTHETIC.EXE",
    "SYNTHETIC.tar", "SYNTHETIC.csv.gz", "SYNTHETIC"])
def test_non_csv_zip_paths_rejected(name, tmp_path):
    path = tmp_path / name
    path.write_bytes(SYNTHETIC_BODY)
    with pytest.raises(BrokerFlowError, match="unsupported_broker_file_type"):
        read_local_archive(path)


def test_file_size_checked_before_reading(tmp_path):
    path = tmp_path / "SYNTHETIC.csv"
    path.write_bytes(b"x" * 11)
    with pytest.raises(BrokerFlowError, match="broker_file_too_large"):
        read_local_archive(path, limits=FileLimits(max_archive_bytes=10))


@pytest.mark.parametrize("ancestor", [False, True])
def test_symlink_file_or_ancestor_is_rejected(tmp_path, ancestor):
    directory = tmp_path / "real"
    directory.mkdir()
    real = directory / "SYNTHETIC.csv"
    real.write_bytes(SYNTHETIC_BODY)
    alias = tmp_path / ("alias" if ancestor else "alias.csv")
    alias.symlink_to(directory if ancestor else real)
    with pytest.raises(BrokerFlowError, match="broker_file_unavailable"):
        read_local_archive(alias / real.name if ancestor else alias)


def test_directory_and_fifo_cannot_be_imported(tmp_path):
    import os
    directory = tmp_path / "directory.csv"
    directory.mkdir()
    fifo = tmp_path / "fifo.csv"
    os.mkfifo(fifo)
    for path in (directory, fifo):
        with pytest.raises(BrokerFlowError, match="broker_file_must_be_regular"):
            read_local_archive(path)


def test_missing_path_error_does_not_expose_local_path(tmp_path):
    with pytest.raises(BrokerFlowError) as error:
        read_local_archive(tmp_path / "SYNTHETIC-private-name.csv")
    assert error.value.code == "broker_file_unavailable"
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize("body", [b"MZ-SYNTHETIC", b"\x7fELF-SYNTHETIC", b"#!SYNTHETIC",
    b"%PDF-SYNTHETIC", b"\x1f\x8bSYNTHETIC", b"Rar!SYNTHETIC", b"BZhSYNTHETIC",
    b"\xef\xbb\xbfMZ-SYNTHETIC", b"SYNTHETIC\x00binary"])
def test_disguised_executable_or_binary_csv_is_rejected(body):
    with pytest.raises(BrokerFlowError, match="untrusted_broker_file_format"):
        csv_members(body, "SYNTHETIC.csv")


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_plain_zip_preserves_member_bytes_and_deterministic_order(compression):
    body = synthetic_zip((("z.csv", SYNTHETIC_BODY), ("a.csv", b"SYNTHETIC,a\n")), compression=compression)
    members = csv_members(body, "SYNTHETIC.zip")
    assert [(member.name, member.body) for member in members] == [
        ("a.csv", b"SYNTHETIC,a\n"), ("z.csv", SYNTHETIC_BODY)]


def test_unicode_names_and_ordinary_directory_are_safe_without_extraction():
    directory = zipfile.ZipInfo("合成/")
    directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    body = synthetic_zip(((directory, b""), ("合成/SYNTHETIC.csv", SYNTHETIC_BODY)))
    assert csv_members(body, "SYNTHETIC.zip")[0].name == "合成/SYNTHETIC.csv"


def test_streaming_zip_data_descriptor_supported_without_execution():
    class NonSeekable(io.BytesIO):
        def seek(self, *args):
            raise io.UnsupportedOperation("SYNTHETIC non-seekable writer")
    body = synthetic_zip(stream=NonSeekable())
    assert struct.unpack_from("<H", body, 6)[0] & 8
    assert csv_members(body, "SYNTHETIC.zip")[0].body == SYNTHETIC_BODY


@pytest.mark.parametrize("name", ["../SYNTHETIC.csv", "/SYNTHETIC.csv", "C:/SYNTHETIC.csv",
    "dir\\SYNTHETIC.csv", "dir/../SYNTHETIC.csv", "dir//SYNTHETIC.csv",
    "./SYNTHETIC.csv", "dir./SYNTHETIC.csv", "dir /SYNTHETIC.csv", "SYNTHETIC\n.csv",
    "e\u0301.csv"])
def test_unsafe_zip_names_rejected(name):
    with pytest.raises(BrokerFlowError, match="unsafe_broker_zip_path"):
        csv_members(synthetic_zip(((name, SYNTHETIC_BODY),)), "SYNTHETIC.zip")


def test_nul_name_cannot_hide_an_extension_or_collide_after_truncation():
    body = synthetic_zip((("synthX.csv", SYNTHETIC_BODY),)).replace(b"synthX.csv", b"synth\x00.csv")
    with pytest.raises(BrokerFlowError, match="unsafe_broker_zip_path"):
        csv_members(body, "SYNTHETIC.zip")


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600,
    stat.S_IFCHR | 0o600, stat.S_IFREG | 0o755])
def test_links_devices_and_executable_members_are_rejected(mode):
    entry = zipfile.ZipInfo("SYNTHETIC.csv")
    entry.external_attr = mode << 16
    with pytest.raises(BrokerFlowError, match="unsafe_broker_zip_member"):
        csv_members(synthetic_zip(((entry, SYNTHETIC_BODY),)), "SYNTHETIC.zip")


@pytest.mark.parametrize("name", ["nested.zip", "SYNTHETIC.exe", "SYNTHETIC.csv.exe", "README.txt"])
def test_only_csv_members_are_accepted(name):
    with pytest.raises(BrokerFlowError, match="unsupported_broker_zip_member"):
        csv_members(synthetic_zip(((name, SYNTHETIC_BODY),)), "SYNTHETIC.zip")


def test_nested_zip_disguised_as_csv_is_rejected():
    with pytest.raises(BrokerFlowError, match="untrusted_broker_file_format"):
        csv_members(synthetic_zip((("SYNTHETIC.csv", synthetic_zip()),)), "SYNTHETIC.zip")


@pytest.mark.parametrize("names", [("same.csv", "same.csv"), ("A.csv", "a.csv")])
def test_duplicate_or_case_colliding_names_are_rejected(names):
    with pytest.raises(BrokerFlowError, match="duplicate_broker_zip_path"):
        csv_members(synthetic_zip(tuple((name, SYNTHETIC_BODY) for name in names)), "SYNTHETIC.zip")


def test_encrypted_flag_is_rejected_before_member_read():
    body = bytearray(synthetic_zip())
    central = body.index(b"PK\x01\x02")
    for position in (6, central+8):
        struct.pack_into("<H", body, position, struct.unpack_from("<H", body, position)[0] | 1)
    with pytest.raises(BrokerFlowError, match="unsupported_broker_zip_flags"):
        csv_members(bytes(body), "SYNTHETIC.zip")


@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_unsupported_compression_rejected(compression):
    with pytest.raises(BrokerFlowError, match="unsupported_broker_zip_compression"):
        csv_members(synthetic_zip(compression=compression), "SYNTHETIC.zip")


def test_zip64_is_not_silently_accepted():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        with archive.open("SYNTHETIC.csv", "w", force_zip64=True) as member:
            member.write(SYNTHETIC_BODY)
    with pytest.raises(BrokerFlowError, match="unsupported_broker_zip_version"):
        csv_members(out.getvalue(), "SYNTHETIC.zip")


@pytest.mark.parametrize("limits,entries", [
    (FileLimits(max_member_bytes=10), (("SYNTHETIC.csv", b"x" * 11),)),
    (FileLimits(max_total_bytes=20), (("a.csv", b"x" * 11), ("b.csv", b"y" * 11))),
    (FileLimits(), (("SYNTHETIC.csv", b"x" * 10_000),)),
])
def test_expansion_and_compression_ratio_caps_precede_decompression(limits, entries):
    with pytest.raises(BrokerFlowError, match="broker_zip_expansion_limit"):
        csv_members(synthetic_zip(entries), "SYNTHETIC.zip", limits=limits)


def test_member_count_is_bounded_before_zipfile_allocates_entries(monkeypatch):
    body = synthetic_zip((("a.csv", SYNTHETIC_BODY), ("b.csv", SYNTHETIC_BODY)))
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: pytest.fail("Unsafe metadata allocation"))
    with pytest.raises(BrokerFlowError, match="broker_zip_member_count"):
        csv_members(body, "SYNTHETIC.zip", limits=FileLimits(max_members=1))


def test_false_member_count_cannot_hide_large_central_directory(monkeypatch):
    body = bytearray(synthetic_zip((("a.csv", SYNTHETIC_BODY), ("b.csv", SYNTHETIC_BODY))))
    end = body.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", body, end+8, 1, 1)
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: pytest.fail("Unsafe metadata allocation"))
    with pytest.raises(BrokerFlowError, match="invalid_broker_zip_directory"):
        csv_members(bytes(body), "SYNTHETIC.zip")


@pytest.mark.parametrize("transform", [lambda data: b"MZ-SYNTHETIC" + data,
    lambda data: data + b"SYNTHETIC-trailing-junk", lambda data: data[:-1]])
def test_self_extracting_polyglot_or_truncated_archive_rejected(transform):
    with pytest.raises(BrokerFlowError):
        csv_members(transform(synthetic_zip()), "SYNTHETIC.zip")


def test_zip_comments_are_explicitly_unsupported():
    with pytest.raises(BrokerFlowError, match="unsupported_broker_zip_layout"):
        csv_members(synthetic_zip(comment=b"SYNTHETIC comment"), "SYNTHETIC.zip")


def test_corrupt_crc_is_rejected():
    body = synthetic_zip(compression=zipfile.ZIP_STORED).replace(SYNTHETIC_BODY, b"X" + SYNTHETIC_BODY[1:])
    with pytest.raises(BrokerFlowError, match="invalid_broker_zip"):
        csv_members(body, "SYNTHETIC.zip")


def test_empty_or_directory_only_archives_have_no_fake_csv():
    for body in (synthetic_zip(()), synthetic_zip((("SYNTHETIC/", b""),))):
        with pytest.raises(BrokerFlowError):
            csv_members(body, "SYNTHETIC.zip")


@pytest.mark.parametrize("field,value", [("max_archive_bytes", True), ("max_members", 0),
    ("max_member_bytes", -1), ("max_total_bytes", 20_000_001), ("max_compression_ratio", 101)])
def test_file_limits_cannot_disable_resource_guards(field, value):
    with pytest.raises(BrokerFlowError):
        FileLimits(**{field: value})
