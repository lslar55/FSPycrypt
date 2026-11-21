"""Rebuild an fscrypt container from a plaintext image.

The container layout, as verified byte-for-byte against real containers:

* ``[0 : Length)``            -- the BootID, AES-CBC with the BootID key/IV.
                                 ``Length`` is 10240 in practice: a 96-byte
                                 struct followed by zero padding.
* ``[Length : DataOffset)``    -- opaque header tail, copied verbatim from a
                                 template container.
* ``[DataOffset : end)``       -- the plaintext image, encrypted page by page:
                                 every 4096-byte page is AES-CBC encrypted with
                                 the game key and the page IV
                                 ``FileIv XOR page_offset``.

``BootId.Crc32`` is ``zlib.crc32(bootid_plaintext[4:Length])``.

Re-encrypting therefore needs the fields we do not model (and the opaque header
tail) to come from a template container of the same game and container type.
"""

from __future__ import annotations

import os
from pathlib import Path
import struct
import tempfile
from typing import BinaryIO
import zlib

from Crypto.Cipher import AES

from .Crypto import CalculatePageIv, PageSize
from .Errors import InvalidContainerError, MissingKeyError
from .Keys import (
    AesBlockSize,
    BootIdIv,
    BootIdKey,
    ExfatHeader,
    GameKeys,
    NtfsHeader,
    ResolveGameKeys,
)
from .Model import BootId, BootIdSize, ContainerType
from .Util import PathType, ProgressCallback


CrcOffset = 0
LengthOffset = 4
BlockCountOffset = 32
BlockSizeOffset = 40
HeaderBlockCountOffset = 48

_ExpectedHeaders = {
    ContainerType.Option: ExfatHeader,
}
_DefaultExpectedHeader = NtfsHeader

_ContainerExtensions = {
    ContainerType.Os: ".pack",
    ContainerType.App: ".app",
    ContainerType.Option: ".opt",
}


def _ExpectedHeader(ContainerTypeValue: ContainerType) -> bytes:
    return _ExpectedHeaders.get(ContainerTypeValue, _DefaultExpectedHeader)


def ContainerExtension(ContainerTypeValue: ContainerType) -> str:
    """File extension a container of this type would carry."""

    return _ContainerExtensions[ContainerTypeValue]


def _DecryptBootId(Encrypted: bytes) -> bytes:
    if len(Encrypted) % AesBlockSize:
        raise InvalidContainerError("BootID length is not a multiple of 16")
    return AES.new(BootIdKey, AES.MODE_CBC, BootIdIv).decrypt(Encrypted)


def _EncryptBootId(Plaintext: bytes) -> bytes:
    if len(Plaintext) % AesBlockSize:
        raise InvalidContainerError("BootID length is not a multiple of 16")
    return AES.new(BootIdKey, AES.MODE_CBC, BootIdIv).encrypt(Plaintext)


def EncryptPages(
    Data: bytes,
    Key: bytes,
    Iv: bytes,
    Offset: int = 0,
) -> bytes:
    """Encrypt plaintext image bytes the way a container stores them.

    ``Data`` must start on a page boundary and be a multiple of the page size;
    ``Offset`` is the position of ``Data`` within the plaintext image. Each
    4096-byte page is encrypted independently with a fresh CBC chain whose IV
    depends on that page's offset.
    """

    if len(Data) % PageSize:
        raise ValueError("plaintext length must be a multiple of the page size")
    Result = bytearray(len(Data))
    for Start in range(0, len(Data), PageSize):
        Page = Data[Start : Start + PageSize]
        Result[Start : Start + len(Page)] = AES.new(
            Key,
            AES.MODE_CBC,
            CalculatePageIv(Offset + Start, Iv),
        ).encrypt(Page)
    return bytes(Result)


def _ReadTemplate(
    SourcePath: Path,
) -> tuple[bytearray, int, int, int, bytes, BootId]:
    """Read a template container's BootID and opaque header tail."""

    with SourcePath.open("rb") as Stream:
        Head = Stream.read(BootIdSize)
        if len(Head) != BootIdSize:
            raise InvalidContainerError("template is too short to be a container")
        Length = struct.unpack_from("<I", _DecryptBootId(Head), LengthOffset)[0]
        if Length < BootIdSize or Length % AesBlockSize:
            raise InvalidContainerError(
                f"template BootID length {Length} is not supported"
            )

        Stream.seek(0)
        Encrypted = Stream.read(Length)
        if len(Encrypted) != Length:
            raise InvalidContainerError("template BootID is truncated")
        Plaintext = bytearray(_DecryptBootId(Encrypted))
        BootIdValue = BootId.FromPlaintext(bytes(Plaintext[:BootIdSize]))

        DataOffset = BootIdValue.DataOffset
        BlockSize = BootIdValue.BlockSize
        HeaderBlockCount = BootIdValue.HeaderBlockCount
        if BlockSize <= 0 or HeaderBlockCount <= 0:
            raise InvalidContainerError("template has an invalid block layout")
        if DataOffset < Length:
            raise InvalidContainerError("template header is smaller than its BootID")
        if DataOffset % PageSize:
            raise InvalidContainerError("template data offset is not page aligned")

        Stream.seek(Length)
        Tail = Stream.read(DataOffset - Length)
        if len(Tail) != DataOffset - Length:
            raise InvalidContainerError("template header is truncated")

    return (
        Plaintext,
        Length,
        BlockSize,
        HeaderBlockCount,
        Tail,
        BootIdValue,
    )


def _BuildBootId(Plaintext: bytearray, BlockCount: int) -> bytes:
    struct.pack_into("<Q", Plaintext, BlockCountOffset, BlockCount)
    struct.pack_into("<I", Plaintext, CrcOffset, 0)
    Checksum = zlib.crc32(bytes(Plaintext[4:])) & 0xFFFFFFFF
    struct.pack_into("<I", Plaintext, CrcOffset, Checksum)
    return bytes(Plaintext)


def _ResolveEncryptKeys(
    BootIdValue: BootId,
    TemplatePath: Path,
    SourcePath: Path,
    Key: bytes | None,
    Iv: bytes | None,
    KeyDirectory: PathType | None,
) -> GameKeys:
    Resolved = ResolveGameKeys(
        BootIdValue,
        Key=Key,
        Iv=Iv,
        KeyDirectory=KeyDirectory,
        SearchPaths=(TemplatePath.parent, SourcePath.parent),
    )
    if BootIdValue.UseCustomIv and Resolved.Iv is None:
        raise MissingKeyError(
            "template uses a custom IV; pass Iv= or provide a key file that "
            "contains it"
        )
    return Resolved


def EncryptFile(
    Source: PathType,
    Output: PathType | None = None,
    *,
    Template: PathType | None = None,
    Key: bytes | None = None,
    Iv: bytes | None = None,
    KeyDirectory: PathType | None = None,
    Overwrite: bool = False,
    ChunkSize: int = 16 * 1024 * 1024,
    Progress: ProgressCallback | None = None,
) -> Path:
    """Encrypt a plaintext ``.ntfs``/``.exfat`` image back into a container.

    This is the inverse of :func:`FSDecrypt.DecryptFile`. ``Template`` must be a
    container of the same game and container type: its BootID fields and opaque
    header tail are reused, because only part of the header is understood.
    ``BootId.BlockCount`` and the BootID CRC are recomputed for the new image.

    The image must start with the expected exFAT/NTFS boot header (the reader
    derives the file IV from it) and is zero-padded to a whole block.
    """

    if ChunkSize <= 0 or ChunkSize % PageSize:
        raise ValueError("ChunkSize must be a positive multiple of the page size")
    if Template is None:
        raise ValueError(
            "Template is required: repacking reuses the template's header fields"
        )

    SourcePath = Path(Source)
    TemplatePath = Path(Template)
    if not SourcePath.is_file():
        raise FileNotFoundError(f"image not found: {SourcePath}")
    if not TemplatePath.is_file():
        raise FileNotFoundError(f"template not found: {TemplatePath}")

    (
        BootIdPlaintext,
        Length,
        BlockSize,
        HeaderBlockCount,
        HeaderTail,
        BootIdValue,
    ) = _ReadTemplate(TemplatePath)

    if BlockSize % PageSize:
        raise InvalidContainerError("template block size is not page aligned")
    if not 0 < Length <= BlockSize * HeaderBlockCount:
        raise InvalidContainerError("template BootID length is out of range")

    Resolved = _ResolveEncryptKeys(
        BootIdValue,
        TemplatePath,
        SourcePath,
        Key,
        Iv,
        KeyDirectory,
    )
    FileIv = Resolved.Iv if Resolved.Iv is not None else bytes(AesBlockSize)

    ExpectedHeader = _ExpectedHeader(BootIdValue.ContainerType)
    ImageSize = SourcePath.stat().st_size
    if ImageSize < len(ExpectedHeader):
        raise InvalidContainerError("image is too short to be a volume image")
    with SourcePath.open("rb") as Stream:
        if Stream.read(len(ExpectedHeader)) != ExpectedHeader:
            Expected = ExpectedHeader.hex()
            raise InvalidContainerError(
                f"image does not start with the expected {Expected} boot header"
            )

    BlockCount = HeaderBlockCount + max(1, -(-ImageSize // BlockSize))
    PlaintextSize = (BlockCount - HeaderBlockCount) * BlockSize
    BootIdEncrypted = _EncryptBootId(_BuildBootId(BootIdPlaintext, BlockCount))

    OutputPath = (
        Path(Output)
        if Output is not None
        else SourcePath.with_suffix(ContainerExtension(BootIdValue.ContainerType))
    )
    if OutputPath.exists() and not Overwrite:
        raise FileExistsError(f"output already exists: {OutputPath}")
    OutputPath.parent.mkdir(parents=True, exist_ok=True)

    TemporaryPath: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=OutputPath.parent,
            prefix=f".{OutputPath.name}.",
            suffix=".part",
            delete=False,
        ) as Destination:
            TemporaryPath = Path(Destination.name)
            Destination.write(BootIdEncrypted)
            Destination.write(HeaderTail)
            _WriteEncryptedImage(
                SourcePath,
                Destination,
                FileIv=FileIv,
                Key=Resolved.Key,
                PlaintextSize=PlaintextSize,
                ChunkSize=ChunkSize,
                Progress=Progress,
            )
        os.replace(TemporaryPath, OutputPath)
    except BaseException:
        if TemporaryPath is not None:
            try:
                TemporaryPath.unlink()
            except FileNotFoundError:
                pass
        raise
    return OutputPath.resolve()


def _WriteEncryptedImage(
    SourcePath: Path,
    Destination: BinaryIO,
    *,
    FileIv: bytes,
    Key: bytes,
    PlaintextSize: int,
    ChunkSize: int,
    Progress: ProgressCallback | None,
) -> int:
    Written = 0
    Remaining = PlaintextSize
    with SourcePath.open("rb") as Stream:
        while Remaining > 0:
            Want = min(ChunkSize, Remaining)
            Data = Stream.read(Want)
            if len(Data) < Want:
                Data += bytes(Want - len(Data))
            Destination.write(EncryptPages(Data, Key, FileIv, Written))
            Written += Want
            Remaining -= Want
            if Progress is not None:
                Progress(Written, PlaintextSize)
    return Written
