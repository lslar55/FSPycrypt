from __future__ import annotations

import io
import os
from pathlib import Path
from typing import BinaryIO, cast

from .Crypto import PageSize, CalculateFileIv, CalculatePageIv, DecryptCbc
from .Errors import InvalidContainerError, MissingKeyError
from .Keys import (
    BootIdIv,
    BootIdKey,
    BuiltinKeys,
    ExfatHeader,
    NtfsHeader,
    OptionIv,
    OptionKey,
    GameKeys,
    ReadKeyFile,
)
from .Model import BootIdSize, BootId, ContainerType


PathType = str | os.PathLike[str]


def _ReadBootId(Stream: BinaryIO) -> BootId:
    Stream.seek(0)
    Encrypted = Stream.read(BootIdSize)
    if len(Encrypted) != BootIdSize:
        raise InvalidContainerError("input is too short to contain a BootID")
    Plaintext = DecryptCbc(Encrypted, BootIdKey, BootIdIv)
    return BootId.FromPlaintext(Plaintext)


def InspectContainer(Source: PathType | BinaryIO) -> BootId:
    if hasattr(Source, "read") and hasattr(Source, "seek"):
        Stream = cast(BinaryIO, Source)
        OldPosition = Stream.tell()
        try:
            return _ReadBootId(Stream)
        finally:
            Stream.seek(OldPosition)
    with Path(Source).open("rb") as Stream:
        return _ReadBootId(Stream)


class FSDecryptReader(io.BufferedIOBase):
    def __init__(
        self,
        Source: PathType | BinaryIO,
        *,
        Key: bytes | None = None,
        Iv: bytes | None = None,
        KeyDirectory: PathType | None = None,
    ) -> None:
        super().__init__()
        self._OwnsStream = not (
            hasattr(Source, "read") and hasattr(Source, "seek")
        )
        self._Path = Path(Source).resolve() if self._OwnsStream else None
        self._Stream = (
            self._Path.open("rb")
            if self._OwnsStream
            else cast(BinaryIO, Source)
        )
        self._Position = 0
        self._PageIndex: int | None = None
        self._Page = b""
        try:
            self.BootId = _ReadBootId(self._Stream)
            Resolved = self._ResolveKeys(Key, Iv, KeyDirectory)
            self._Key = Resolved.Key
            self._Iv = self._ResolveIv(Resolved.Iv, Iv is not None)

            self._Stream.seek(0, io.SEEK_END)
            PhysicalSize = self._Stream.tell()
            RequiredSize = self.BootId.DataOffset + self.BootId.PlaintextSize
            if PhysicalSize < RequiredSize:
                raise InvalidContainerError(
                    f"container is truncated: expected at least {RequiredSize} bytes, "
                    f"got {PhysicalSize}"
                )
        except BaseException:
            self.close()
            raise

    def _ResolveKeys(
        self,
        Key: bytes | None,
        Iv: bytes | None,
        KeyDirectory: PathType | None,
    ) -> GameKeys:
        if Iv is not None and len(Iv) != 16:
            raise ValueError("IV must be 16 bytes")
        if Key is not None:
            if len(Key) != 16:
                raise ValueError("key must be 16 bytes")
            return GameKeys(Key, Iv)

        if self.BootId.ContainerType is ContainerType.Option:
            return GameKeys(OptionKey, Iv if Iv is not None else OptionIv)

        KeyId = (
            self.BootId.OsId
            if self.BootId.ContainerType is ContainerType.Os
            else self.BootId.GameId
        )
        if KeyId in BuiltinKeys:
            Resolved = BuiltinKeys[KeyId]
            return GameKeys(Resolved.Key, Iv if Iv is not None else Resolved.Iv)

        Directories: list[Path] = []
        if KeyDirectory is not None:
            Directories.append(Path(KeyDirectory))
        if self._Path is not None:
            Directories.append(self._Path.parent)
        Directories.append(Path.cwd())
        for Directory in dict.fromkeys(Item.resolve() for Item in Directories):
            Candidate = Directory / f"{KeyId}.bin"
            if Candidate.is_file():
                Resolved = ReadKeyFile(Candidate)
                return GameKeys(
                    Resolved.Key,
                    Iv if Iv is not None else Resolved.Iv,
                )
        raise MissingKeyError(
            f"no key for {KeyId!r}; provide Key, KeyDirectory, or {KeyId}.bin"
        )

    def _ResolveIv(self, ConfiguredIv: bytes | None, ExplicitIv: bool) -> bytes:
        if ConfiguredIv is not None and (
            ExplicitIv or not self.BootId.UseCustomIv
        ):
            return ConfiguredIv
        self._Stream.seek(self.BootId.DataOffset)
        FirstPage = self._Stream.read(PageSize)
        Header = (
            ExfatHeader
            if self.BootId.ContainerType is ContainerType.Option
            else NtfsHeader
        )
        return CalculateFileIv(self._Key, Header, FirstPage)

    def _LoadPage(self, PageIndex: int) -> bytes:
        if self._PageIndex == PageIndex:
            return self._Page
        PageOffset = PageIndex * PageSize
        Remaining = self.BootId.PlaintextSize - PageOffset
        if Remaining <= 0:
            return b""
        EncryptedSize = min(PageSize, Remaining)
        self._Stream.seek(self.BootId.DataOffset + PageOffset)
        Encrypted = self._Stream.read(EncryptedSize)
        if len(Encrypted) != EncryptedSize:
            raise InvalidContainerError("container ended inside an encrypted page")
        try:
            PageIv = CalculatePageIv(PageOffset, self._Iv)
            Plaintext = DecryptCbc(Encrypted, self._Key, PageIv)
        except ValueError as Error:
            raise InvalidContainerError(str(Error)) from Error
        self._PageIndex = PageIndex
        self._Page = Plaintext
        return Plaintext

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        self._checkClosed()
        return self._Position

    def seek(self, Offset: int, Whence: int = io.SEEK_SET) -> int:
        self._checkClosed()
        if Whence == io.SEEK_SET:
            Position = Offset
        elif Whence == io.SEEK_CUR:
            Position = self._Position + Offset
        elif Whence == io.SEEK_END:
            Position = self.BootId.PlaintextSize + Offset
        else:
            raise ValueError(f"invalid whence: {Whence}")
        if Position < 0:
            raise ValueError("cannot seek before the start")
        self._Position = Position
        return Position

    def read(self, Size: int = -1) -> bytes:
        self._checkClosed()
        Remaining = self.BootId.PlaintextSize - self._Position
        if Remaining <= 0 or Size == 0:
            return b""
        if Size is None or Size < 0:
            Size = Remaining
        else:
            Size = min(Size, Remaining)

        Chunks: list[bytes] = []
        Left = Size
        while Left:
            PageIndex, PageOffset = divmod(self._Position, PageSize)
            Page = self._LoadPage(PageIndex)
            Count = min(Left, len(Page) - PageOffset)
            if Count <= 0:
                raise InvalidContainerError("decrypted page is unexpectedly short")
            Chunks.append(Page[PageOffset : PageOffset + Count])
            self._Position += Count
            Left -= Count
        return b"".join(Chunks)

    def readinto(self, Buffer: bytearray | memoryview) -> int:
        Data = self.read(len(Buffer))
        Buffer[: len(Data)] = Data
        return len(Data)

    def close(self) -> None:
        if not self.closed and getattr(self, "_OwnsStream", False):
            self._Stream.close()
        super().close()


def OpenContainer(
    Source: PathType | BinaryIO,
    *,
    Key: bytes | None = None,
    Iv: bytes | None = None,
    KeyDirectory: PathType | None = None,
) -> FSDecryptReader:
    return FSDecryptReader(
        Source,
        Key=Key,
        Iv=Iv,
        KeyDirectory=KeyDirectory,
    )
