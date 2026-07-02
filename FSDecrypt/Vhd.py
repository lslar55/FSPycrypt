from __future__ import annotations

import io
import struct
from typing import BinaryIO

from dissect.hypervisor.disk.vhd import VHD

from .Errors import InvalidVhdError


NtfsMagic = b"\xEB\x52\x90\x4E"


class OffsetStream(io.BufferedIOBase):
    def __init__(self, Stream: BinaryIO, Offset: int, Size: int) -> None:
        super().__init__()
        self._Stream = Stream
        self._Offset = Offset
        self._Size = Size
        self._Position = 0

    def readable(self) -> bool:
        return True

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
            Position = self._Size + Offset
        else:
            raise ValueError(f"invalid whence: {Whence}")
        if Position < 0:
            raise ValueError("cannot seek before the start")
        self._Position = Position
        return Position

    def read(self, Size: int = -1) -> bytes:
        self._checkClosed()
        Remaining = self._Size - self._Position
        if Remaining <= 0 or Size == 0:
            return b""
        if Size is None or Size < 0:
            Size = Remaining
        else:
            Size = min(Size, Remaining)
        self._Stream.seek(self._Offset + self._Position)
        Data = self._Stream.read(Size)
        self._Position += len(Data)
        return Data

    def readinto(self, Buffer: bytearray | memoryview) -> int:
        Data = self.read(len(Buffer))
        Buffer[: len(Data)] = Data
        return len(Data)


def FindNtfsOffset(Stream: BinaryIO, Size: int) -> int:
    Stream.seek(0)
    Mbr = Stream.read(512)
    if len(Mbr) == 512 and Mbr[510:512] == b"\x55\xAA":
        for Index in range(4):
            Position = 0x1BE + Index * 16
            if Mbr[Position + 4] != 0x07:
                continue
            Lba = struct.unpack_from("<I", Mbr, Position + 8)[0]
            Offset = Lba * 512
            if Offset + 4 <= Size:
                Stream.seek(Offset)
                if Stream.read(4) == NtfsMagic:
                    return Offset

    for Offset in (0, 32256, 1048576, 512):
        if Offset + 4 > Size:
            continue
        Stream.seek(Offset)
        if Stream.read(4) == NtfsMagic:
            return Offset
    raise InvalidVhdError("no NTFS partition found in VHD")


def OpenVhdNtfs(Stream: BinaryIO) -> OffsetStream:
    try:
        Disk = VHD(Stream)
    except Exception as Error:
        raise InvalidVhdError(f"invalid VHD: {Error}") from Error
    DiskType = int(Disk.disk.footer.disk_type)
    if DiskType == 4:
        raise InvalidVhdError(
            "differencing VHD requires its base VHD and is not supported alone"
        )
    if DiskType not in (2, 3):
        raise InvalidVhdError(f"unsupported VHD type {DiskType}")
    Offset = FindNtfsOffset(Disk, Disk.size)
    return OffsetStream(Disk, Offset, Disk.size - Offset)
