"""Self-contained VHD reader, including differencing (delta) chains.

The layout intentionally mirrors the upstream Rust implementation so both ports
agree on how a VHD footer, dynamic header, block allocation table and sector
bitmap are interpreted.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from typing import BinaryIO, Sequence

from .Errors import InvalidVhdError


SectorSize = 512
DynamicHeaderSize = 1024

VhdCookie = b"conectix"
DynamicHeaderCookie = b"cxsparse"

VhdTypeFixed = 2
VhdTypeDynamic = 3
VhdTypeDifferencing = 4

FooterDataOffsetPosition = 0x10
FooterCurrentSizePosition = 0x30
FooterDiskTypePosition = 0x3C
FooterUniqueIdPosition = 0x44

DynamicBatOffsetPosition = 0x10
DynamicMaxEntriesPosition = 0x18
DynamicBlockSizePosition = 0x20
DynamicParentUniqueIdPosition = 0x28

BatUnused = 0xFFFFFFFF

MbrSignature = b"\x55\xAA"
MbrPartitionTableOffset = 0x1BE
MbrPartitionEntrySize = 16
MbrMaxPartitions = 4
NtfsPartitionType = 0x07
NtfsMagic = b"\xEB\x52\x90\x4E"
NtfsProbeOffsets = (0, 32256, 1048576, 512)


def _ReadAt(Stream: BinaryIO, Offset: int, Size: int) -> bytes:
    if Size <= 0:
        return b""
    Stream.seek(Offset)
    return Stream.read(Size)


def _ReadExact(Stream: BinaryIO, Offset: int, Size: int) -> bytes:
    Data = _ReadAt(Stream, Offset, Size)
    if len(Data) != Size:
        raise InvalidVhdError("unexpected end of VHD data")
    return Data


@dataclass(frozen=True, slots=True)
class VhdFooter:
    FileSize: int
    DiskType: int
    DataOffset: int
    UniqueId: bytes
    CurrentSize: int


@dataclass(frozen=True, slots=True)
class VhdGuidInfo:
    """Chain-linking info: ``ParentId`` is set only for differencing VHDs."""

    OwnId: bytes
    ParentId: bytes | None
    DiskType: int


def _ReadFooter(Stream: BinaryIO) -> VhdFooter:
    Stream.seek(0, io.SEEK_END)
    FileSize = Stream.tell()
    if FileSize < SectorSize:
        raise InvalidVhdError("VHD is too small to hold a footer")

    Footer = _ReadExact(Stream, FileSize - SectorSize, SectorSize)
    if Footer[:8] != VhdCookie:
        # Versions before Virtual PC 2004 can carry a 511-byte footer.
        Footer = _ReadExact(Stream, FileSize - 511, SectorSize)
        if Footer[:8] != VhdCookie:
            raise InvalidVhdError("not a VHD file: missing conectix footer")

    return VhdFooter(
        FileSize=FileSize,
        DiskType=struct.unpack_from(">I", Footer, FooterDiskTypePosition)[0],
        DataOffset=struct.unpack_from(">Q", Footer, FooterDataOffsetPosition)[0],
        UniqueId=bytes(Footer[FooterUniqueIdPosition : FooterUniqueIdPosition + 16]),
        CurrentSize=struct.unpack_from(">Q", Footer, FooterCurrentSizePosition)[0],
    )


def _ReadDynamicHeader(Stream: BinaryIO, Offset: int) -> bytes:
    if Offset <= 0 or Offset > 0xFFFFFFFF:
        raise InvalidVhdError("invalid VHD dynamic header offset")
    Header = _ReadExact(Stream, Offset, DynamicHeaderSize)
    if Header[:8] != DynamicHeaderCookie:
        raise InvalidVhdError("invalid VHD dynamic header")
    return Header


def ReadVhdGuidInfo(Stream: BinaryIO) -> VhdGuidInfo:
    """Read a VHD's own Unique Id and, for differencing VHDs, its parent's."""

    Footer = _ReadFooter(Stream)
    ParentId: bytes | None = None
    if Footer.DiskType == VhdTypeDifferencing:
        Header = _ReadDynamicHeader(Stream, Footer.DataOffset)
        ParentId = bytes(
            Header[
                DynamicParentUniqueIdPosition : DynamicParentUniqueIdPosition + 16
            ]
        )
    return VhdGuidInfo(Footer.UniqueId, ParentId, Footer.DiskType)


@dataclass(frozen=True, slots=True)
class _SparseLayout:
    Bat: tuple[int, ...]
    BlockSize: int
    SectorsPerBlock: int


def _ParseSparseLayout(Stream: BinaryIO, Footer: VhdFooter) -> _SparseLayout:
    Header = _ReadDynamicHeader(Stream, Footer.DataOffset)
    BatOffset = struct.unpack_from(">Q", Header, DynamicBatOffsetPosition)[0]
    MaxEntries = struct.unpack_from(">I", Header, DynamicMaxEntriesPosition)[0]
    BlockSize = struct.unpack_from(">I", Header, DynamicBlockSizePosition)[0]

    if BlockSize == 0 or BlockSize % SectorSize:
        raise InvalidVhdError("invalid VHD block size")
    if MaxEntries == 0:
        raise InvalidVhdError("VHD block allocation table is empty")
    SectorsPerBlock = BlockSize // SectorSize
    if SectorsPerBlock > SectorSize * 8:
        raise InvalidVhdError("VHD block size exceeds a single bitmap sector")

    Raw = _ReadExact(Stream, BatOffset, MaxEntries * 4)
    return _SparseLayout(
        Bat=struct.unpack(f">{MaxEntries}I", Raw),
        BlockSize=BlockSize,
        SectorsPerBlock=SectorsPerBlock,
    )


def _IsSectorPresent(Bitmap: bytes, Sector: int) -> bool:
    return bool(Bitmap[Sector // 8] >> (7 - Sector % 8) & 1)


class VhdLayer:
    """One VHD file, mapped to its virtual disk."""

    def __init__(self, Stream: BinaryIO) -> None:
        Footer = _ReadFooter(Stream)
        self.Stream = Stream
        self.DiskType = Footer.DiskType
        self.Sparse: _SparseLayout | None = None
        self._BitmapBlock = -1
        self._Bitmap = b""

        if Footer.DiskType == VhdTypeFixed:
            DataSize = Footer.FileSize - SectorSize
            self.VirtualSize = (
                Footer.CurrentSize
                if 0 < Footer.CurrentSize <= DataSize
                else DataSize
            )
        elif Footer.DiskType in (VhdTypeDynamic, VhdTypeDifferencing):
            self.Sparse = _ParseSparseLayout(Stream, Footer)
            Capacity = len(self.Sparse.Bat) * self.Sparse.BlockSize
            self.VirtualSize = Footer.CurrentSize or Capacity
            if not 0 < self.VirtualSize <= Capacity:
                raise InvalidVhdError("invalid VHD virtual size")
        else:
            raise InvalidVhdError(f"unsupported VHD type {Footer.DiskType}")

    def ReadAt(self, Offset: int, Size: int) -> bytes:
        """Read as the disk itself: unallocated blocks read back as zeros."""

        if Size <= 0:
            return b""
        if self.Sparse is None:
            return _ReadAt(self.Stream, Offset, Size)

        BlockSize = self.Sparse.BlockSize
        Result = bytearray()
        while len(Result) < Size:
            Position = Offset + len(Result)
            BlockIndex, BlockOffset = divmod(Position, BlockSize)
            if BlockIndex >= len(self.Sparse.Bat):
                break
            Count = min(Size - len(Result), BlockSize - BlockOffset)
            Entry = self.Sparse.Bat[BlockIndex]
            if Entry == BatUnused:
                Result += bytes(Count)
            else:
                Result += _ReadExact(
                    self.Stream,
                    Entry * SectorSize + SectorSize + BlockOffset,
                    Count,
                )
        return bytes(Result)

    def OwnsSector(self, Offset: int) -> bool:
        """Whether this layer holds its own data for the sector at ``Offset``."""

        Sparse = self.Sparse
        if Sparse is None:
            return False
        BlockIndex, BlockOffset = divmod(Offset, Sparse.BlockSize)
        if BlockIndex >= len(Sparse.Bat):
            return False
        Entry = Sparse.Bat[BlockIndex]
        if Entry == BatUnused:
            return False
        Bitmap = self._BlockBitmap(BlockIndex, Entry)
        return _IsSectorPresent(Bitmap, BlockOffset // SectorSize)

    def ReadOwned(self, Offset: int, Size: int) -> bytes:
        """Read a range this layer owns; the caller must have verified it."""

        if Size <= 0:
            return b""
        Sparse = self.Sparse
        if Sparse is None:
            raise InvalidVhdError("fixed VHDs never own blocks of their own")

        Result = bytearray()
        while len(Result) < Size:
            Position = Offset + len(Result)
            BlockIndex, BlockOffset = divmod(Position, Sparse.BlockSize)
            Entry = Sparse.Bat[BlockIndex]
            Count = min(Size - len(Result), Sparse.BlockSize - BlockOffset)
            Result += _ReadExact(
                self.Stream,
                Entry * SectorSize + SectorSize + BlockOffset,
                Count,
            )
        return bytes(Result)

    def _BlockBitmap(self, BlockIndex: int, Entry: int) -> bytes:
        if self._BitmapBlock != BlockIndex:
            self._Bitmap = _ReadExact(self.Stream, Entry * SectorSize, SectorSize)
            self._BitmapBlock = BlockIndex
        return self._Bitmap


class VhdDisk:
    """A single fixed, dynamic or differencing VHD seen as a flat disk."""

    def __init__(self, Stream: BinaryIO) -> None:
        self.Layer = VhdLayer(Stream)
        self.Size = self.Layer.VirtualSize

    def ReadAt(self, Offset: int, Size: int) -> bytes:
        return self.Layer.ReadAt(Offset, min(Size, max(0, self.Size - Offset)))


class ChainedVhdDisk:
    """A base VHD with any number of differencing VHDs overlaid on top.

    ``Streams`` must be ordered base first, top-most delta last. A read is
    resolved sector by sector: the top-most layer whose sector bitmap marks a
    sector as present owns it, and everything else falls through to the layer
    below, ending at the base. Consecutive sectors owned by the same layer are
    read in one go, which keeps sequential reads fast.
    """

    def __init__(self, Streams: Sequence[BinaryIO]) -> None:
        if not Streams:
            raise InvalidVhdError("VHD chain is empty")
        self.Layers = [VhdLayer(Stream) for Stream in Streams]
        self.Size = self.Layers[0].VirtualSize
        if len(self.Layers) > 1:
            if self.Layers[0].DiskType == VhdTypeDifferencing:
                raise InvalidVhdError("VHD chain base must not be differencing")
            for Layer in self.Layers[1:]:
                if Layer.DiskType != VhdTypeDifferencing:
                    raise InvalidVhdError(
                        "VHD chain layers above the base must be differencing"
                    )
                if Layer.VirtualSize != self.Size:
                    raise InvalidVhdError(
                        "VHD chain layers disagree about the disk size"
                    )

    def OwnerOf(self, Offset: int) -> int:
        """Index of the top-most layer owning the sector at ``Offset``."""

        for Index in range(len(self.Layers) - 1, 0, -1):
            if self.Layers[Index].OwnsSector(Offset):
                return Index
        return 0

    def ReadAt(self, Offset: int, Size: int) -> bytes:
        Size = min(Size, max(0, self.Size - Offset))
        if Size <= 0:
            return b""

        End = Offset + Size
        Result = bytearray()
        while len(Result) < Size:
            Position = Offset + len(Result)
            Owner = self.OwnerOf(Position)

            # Extend the read while the same layer keeps owning whole sectors.
            Stop = Position - Position % SectorSize + SectorSize
            while Stop < End and self.OwnerOf(Stop) == Owner:
                Stop += SectorSize
            Stop = min(Stop, End)

            Layer = self.Layers[Owner]
            Count = Stop - Position
            Result += (
                Layer.ReadOwned(Position, Count)
                if Owner
                else Layer.ReadAt(Position, Count)
            )
        return bytes(Result)


class _PartitionStream(io.BufferedIOBase):
    """Presents the NTFS partition inside a disk as a seekable stream."""

    def __init__(self, Disk: VhdDisk | ChainedVhdDisk, Offset: int) -> None:
        super().__init__()
        self._Disk = Disk
        self._Offset = Offset
        self._Size = Disk.Size - Offset
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
        Data = self._Disk.ReadAt(self._Offset + self._Position, Size)
        self._Position += len(Data)
        return Data

    def readinto(self, Buffer: bytearray | memoryview) -> int:
        Data = self.read(len(Buffer))
        Buffer[: len(Data)] = Data
        return len(Data)


def FindNtfsOffset(Disk: VhdDisk | ChainedVhdDisk) -> int:
    Mbr = Disk.ReadAt(0, SectorSize)
    if len(Mbr) == SectorSize and Mbr[510:512] == MbrSignature:
        for Index in range(MbrMaxPartitions):
            Position = MbrPartitionTableOffset + Index * MbrPartitionEntrySize
            if Mbr[Position + 4] != NtfsPartitionType:
                continue
            Lba = struct.unpack_from("<I", Mbr, Position + 8)[0]
            Offset = Lba * SectorSize
            if Offset + 4 <= Disk.Size and Disk.ReadAt(Offset, 4) == NtfsMagic:
                return Offset

    for Offset in NtfsProbeOffsets:
        if Offset + 4 <= Disk.Size and Disk.ReadAt(Offset, 4) == NtfsMagic:
            return Offset
    raise InvalidVhdError("no NTFS partition found in VHD")


def OpenVhdNtfs(Stream: BinaryIO) -> BinaryIO:
    """Open the NTFS partition of a single fixed or dynamic VHD."""

    return OpenDiskNtfs(VhdDisk(Stream))


def OpenChainedVhdNtfs(Streams: Sequence[BinaryIO]) -> BinaryIO:
    """Open the NTFS partition of a merged base + differencing VHD chain."""

    return OpenDiskNtfs(ChainedVhdDisk(Streams))


def OpenDiskNtfs(Disk: VhdDisk | ChainedVhdDisk) -> BinaryIO:
    return _PartitionStream(Disk, FindNtfsOffset(Disk))
