"""Build a spec-compliant exFAT image from a directory tree.

Only one exFAT layout is produced, mirroring the real OPTION containers:

* 4096-byte sectors, one cluster per sector (4 KiB clusters)
* 12 main + 12 backup boot sectors, then the FAT, then the cluster heap
* cluster 2 = allocation bitmap, next = up-case table, next = root directory
* directories and files are contiguous (``NoFatChain``); the FAT still carries
  a chain for every allocation so any reader can follow it
* directory entry set checksums and name hashes are computed properly, so the
  result round-trips through :class:`FSDecrypt.Exfat.ExfatVolume`

The up-case table is taken from a reference image when one is supplied (a real
container's own table is the best choice) and generated from Python's Unicode
upper-case mapping otherwise.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
import struct
from pathlib import Path
import time
from typing import BinaryIO, Iterator, Sequence

from .Errors import InvalidExfatError
from .Util import PathType, ProgressCallback


SectorShift = 12
ClusterShift = 0
SectorSize = 1 << SectorShift
ClusterSize = 1 << (SectorShift + ClusterShift)

BootRegionSectors = 12
FatOffsetSectors = 24
EndOfChain = 0xFFFFFFFF
ReservedFat = 0xFFFFFFF8

EntrySize = 32
FileEntryType = 0x85
StreamEntryType = 0xC0
FileNameEntryType = 0xC1
BitmapEntryType = 0x81
UpcaseEntryType = 0x82
VolumeLabelEntryType = 0x83

AttributeDirectory = 0x10
AttributeArchive = 0x20

NameUnitsPerEntry = 15
UtcOffsetUnknown = 0x80
CopyChunkSize = 4 * 1024 * 1024


@dataclass(slots=True)
class _Item:
    Name: str
    IsDirectory: bool
    Size: int
    Modified: float
    Source: Path | None = None
    Children: list["_Item"] = field(default_factory=list)
    FirstCluster: int = 0
    ClusterCount: int = 0
    DirectoryClusters: int = 0

    @property
    def NameUnits(self) -> int:
        return len(self.Name.encode("utf-16-le")) // 2

    @property
    def NameEntryCount(self) -> int:
        return max(1, math.ceil(self.NameUnits / NameUnitsPerEntry))

    @property
    def SecondaryCount(self) -> int:
        return 1 + self.NameEntryCount

    @property
    def EntryBytes(self) -> int:
        return EntrySize * (1 + self.SecondaryCount)


def _CeilDiv(Value: int, Divisor: int) -> int:
    return -(-Value // Divisor)


# ---------------------------------------------------------------------------
# up-case table
# ---------------------------------------------------------------------------


def UpcaseChecksum(Table: bytes) -> int:
    Value = 0
    for Byte in Table:
        Value = ((((Value & 1) << 31) + (Value >> 1)) + Byte) & 0xFFFFFFFF
    return Value


def _DecodeUpcase(Table: bytes) -> list[int]:
    Mapping = list(range(0x10000))
    Position = 0
    Code = 0
    while Position + 1 < len(Table) and Code < 0x10000:
        Value = struct.unpack_from("<H", Table, Position)[0]
        Position += 2
        if Value & 0x8000:
            Code += Value & 0x7FFF
        else:
            if Code < 0x10000:
                Mapping[Code] = Value
            Code += 1
    return Mapping


def _EncodeUpcase(Mapping: Sequence[int]) -> bytes:
    Out = bytearray()
    Index = 0
    while Index < 0x10000:
        if Mapping[Index] == Index:
            Run = 1
            while (
                Index + Run < 0x10000
                and Mapping[Index + Run] == Index + Run
                and Run < 0x7FFF
            ):
                Run += 1
            Out += struct.pack("<H", 0x8000 | Run)
            Index += Run
        else:
            Out += struct.pack("<H", Mapping[Index])
            Index += 1
    return bytes(Out)


def _GenerateUpcase() -> bytes:
    Mapping = list(range(0x10000))
    for Code in range(0x10000):
        Upper = chr(Code).upper()
        if len(Upper) == 1:
            Mapping[Code] = ord(Upper)
    return _EncodeUpcase(Mapping)


def ReadUpcaseTable(Stream: BinaryIO) -> bytes | None:
    """Read the up-case table straight out of an exFAT image stream."""

    Stream.seek(0)
    Header = Stream.read(512)
    if len(Header) != 512 or Header[3:11] != b"EXFAT   ":
        raise InvalidExfatError("stream does not contain an exFAT boot sector")

    Shift = Header[108]
    ClusterShiftValue = Header[109]
    Sector = 1 << Shift
    Cluster = 1 << (Shift + ClusterShiftValue)
    FatOffset = struct.unpack_from("<I", Header, 80)[0]
    FatLength = struct.unpack_from("<I", Header, 84)[0]
    HeapOffset = struct.unpack_from("<I", Header, 88)[0]
    RootCluster = struct.unpack_from("<I", Header, 96)[0]
    if FatOffset < 24 or FatLength == 0:
        return None

    Stream.seek(FatOffset * Sector)
    Fat = Stream.read(FatLength * Sector)
    if len(Fat) != FatLength * Sector:
        return None

    def ClusterOffset(Index: int) -> int:
        return HeapOffset * Sector + (Index - 2) * Cluster

    def ReadCluster(Index: int) -> bytes:
        Stream.seek(ClusterOffset(Index))
        return Stream.read(Cluster)

    def Next(Index: int) -> int | None:
        if (Index + 1) * 4 > len(Fat):
            return None
        Value = struct.unpack_from("<I", Fat, Index * 4)[0]
        return None if Value >= 0xFFFFFFF8 or Value < 2 else Value

    def ReadChain(First: int, Length: int | None) -> bytes:
        Data = bytearray()
        Index = First
        Seen: set[int] = set()
        while Index not in Seen and 2 <= Index:
            Seen.add(Index)
            Data += ReadCluster(Index)
            if Length is not None and len(Data) >= Length:
                break
            Following = Next(Index)
            if Following is None:
                break
            Index = Following
        return bytes(Data if Length is None else Data[:Length])

    for Position in range(0, max(0, len((Root := ReadChain(RootCluster, None))) - EntrySize + 1), EntrySize):
        if Root[Position] != UpcaseEntryType:
            continue
        First = struct.unpack_from("<I", Root, Position + 20)[0]
        Length = struct.unpack_from("<Q", Root, Position + 24)[0]
        if First < 2 or Length <= 0:
            return None
        return ReadChain(First, Length)
    return None


# ---------------------------------------------------------------------------
# directory entries
# ---------------------------------------------------------------------------


def _SetChecksum(Entries: bytes) -> int:
    Value = 0
    for Index, Byte in enumerate(Entries):
        if Index < EntrySize and Index % EntrySize in (2, 3):
            continue
        Value = ((((Value & 1) << 15) + (Value >> 1)) + Byte) & 0xFFFF
    return Value


def _NameHash(Name: str, Mapping: Sequence[int]) -> int:
    Value = 0
    Encoded = Name.encode("utf-16-le")
    for Index in range(0, len(Encoded) - 1, 2):
        Code = struct.unpack_from("<H", Encoded, Index)[0]
        Value = ((((Value & 1) << 15) + (Value >> 1)) + Mapping[Code]) & 0xFFFF
    return Value


def _DosTimestamp(Seconds: float) -> tuple[int, int]:
    Moment = time.localtime(Seconds)
    Year = max(1980, min(2107, Moment.tm_year))
    Stamp = (
        ((Year - 1980) << 25)
        | (Moment.tm_mon << 21)
        | (Moment.tm_mday << 16)
        | (Moment.tm_hour << 11)
        | (Moment.tm_min << 5)
        | (Moment.tm_sec // 2)
    )
    Fraction = Seconds - int(Seconds)
    Increment = (Moment.tm_sec % 2) * 100 + int(Fraction * 100) % 100
    return Stamp & 0xFFFFFFFF, Increment & 0xFF


def _BuildFileEntry(Item: _Item, Mapping: Sequence[int]) -> bytes:
    Stamp, Increment = _DosTimestamp(Item.Modified)
    Attributes = AttributeDirectory if Item.IsDirectory else AttributeArchive
    DataLength = Item.ClusterCount * ClusterSize if Item.IsDirectory else Item.Size
    FirstCluster = Item.FirstCluster if DataLength else 0

    Primary = bytearray(EntrySize)
    Primary[0] = FileEntryType
    Primary[1] = Item.SecondaryCount
    struct.pack_into("<H", Primary, 4, Attributes)
    struct.pack_into("<I", Primary, 8, Stamp)
    struct.pack_into("<I", Primary, 12, Stamp)
    struct.pack_into("<I", Primary, 16, Stamp)
    Primary[20] = Increment
    Primary[21] = Increment
    Primary[22] = UtcOffsetUnknown
    Primary[23] = UtcOffsetUnknown
    Primary[24] = UtcOffsetUnknown

    Stream = bytearray(EntrySize)
    Stream[0] = StreamEntryType
    Stream[1] = 0x03 if DataLength else 0x01
    Stream[3] = Item.NameUnits
    struct.pack_into("<H", Stream, 4, _NameHash(Item.Name, Mapping))
    struct.pack_into("<Q", Stream, 8, DataLength)
    struct.pack_into("<I", Stream, 20, FirstCluster)
    struct.pack_into("<Q", Stream, 24, DataLength)

    Encoded = Item.Name.encode("utf-16-le")
    Names = bytearray()
    for Index in range(Item.NameEntryCount):
        NameEntry = bytearray(EntrySize)
        NameEntry[0] = FileNameEntryType
        Chunk = Encoded[
            Index * NameUnitsPerEntry * 2 : (Index + 1) * NameUnitsPerEntry * 2
        ]
        NameEntry[2 : 2 + len(Chunk)] = Chunk
        Names += NameEntry

    Entries = bytes(Primary) + bytes(Stream) + bytes(Names)
    struct.pack_into("<H", Primary, 2, _SetChecksum(Entries))
    return bytes(Primary) + Entries[EntrySize:]


def _BuildDirectory(
    Items: Sequence[_Item],
    Extra: bytes,
    Mapping: Sequence[int],
) -> bytes:
    Body = bytearray(Extra)
    for Item in Items:
        Body += _BuildFileEntry(Item, Mapping)
    Body += bytes(EntrySize)
    return bytes(Body)


def _EntryBytesForChildren(Items: Sequence[_Item]) -> int:
    return EntrySize + sum(Item.EntryBytes for Item in Items)


def _MetadataBytes(Root: Sequence[_Item], ExtraBytes: int) -> int:
    Total = _EntryBytesForChildren(Root) + ExtraBytes
    for Item in _Flatten(Root):
        if Item.IsDirectory:
            Total += _EntryBytesForChildren(Item.Children)
    return Total


def _BitmapEntry(FirstCluster: int, Length: int) -> bytes:
    Entry = bytearray(EntrySize)
    Entry[0] = BitmapEntryType
    struct.pack_into("<I", Entry, 20, FirstCluster)
    struct.pack_into("<Q", Entry, 24, Length)
    return bytes(Entry)


def _UpcaseEntry(FirstCluster: int, Length: int, Checksum: int) -> bytes:
    Entry = bytearray(EntrySize)
    Entry[0] = UpcaseEntryType
    struct.pack_into("<I", Entry, 4, Checksum)
    struct.pack_into("<I", Entry, 20, FirstCluster)
    struct.pack_into("<Q", Entry, 24, Length)
    return bytes(Entry)


def _VolumeLabelEntry(Label: str) -> bytes:
    Entry = bytearray(EntrySize)
    Entry[0] = VolumeLabelEntryType
    Encoded = Label.encode("utf-16-le")[:22]
    Entry[1 : 1 + len(Encoded)] = Encoded
    return bytes(Entry)


# ---------------------------------------------------------------------------
# tree preparation
# ---------------------------------------------------------------------------


def _Flatten(Items: Sequence[_Item]) -> Iterator[_Item]:
    for Item in Items:
        yield Item
        yield from _Flatten(Item.Children)


def _BuildTree(Source: Path) -> list[_Item]:
    if not Source.is_dir():
        raise NotADirectoryError(f"not a directory: {Source}")

    def Load(Directory: Path) -> list[_Item]:
        try:
            Entries = sorted(
                Directory.iterdir(), key=lambda Entry: Entry.name.casefold()
            )
        except OSError as Error:
            raise InvalidExfatError(f"cannot read {Directory}: {Error}") from Error
        Children: list[_Item] = []
        for Entry in Entries:
            try:
                if Entry.is_symlink():
                    continue
                if Entry.is_dir():
                    Children.append(
                        _Item(
                            Name=Entry.name,
                            IsDirectory=True,
                            Size=0,
                            Modified=Entry.stat().st_mtime,
                            Source=Entry,
                            Children=Load(Entry),
                        )
                    )
                elif Entry.is_file():
                    Children.append(
                        _Item(
                            Name=Entry.name,
                            IsDirectory=False,
                            Size=Entry.stat().st_size,
                            Modified=Entry.stat().st_mtime,
                            Source=Entry,
                        )
                    )
            except OSError as Error:
                raise InvalidExfatError(
                    f"cannot inspect {Entry}: {Error}"
                ) from Error
        return Children

    return Load(Source)


def _PlanDirectoryClusters(Root: list[_Item], ExtraBytes: int) -> int:
    RootBytes = _EntryBytesForChildren(Root) + ExtraBytes
    RootClusters = max(1, _CeilDiv(RootBytes, ClusterSize))
    for Item in _Flatten(Root):
        if Item.IsDirectory:
            Item.DirectoryClusters = max(
                1, _CeilDiv(_EntryBytesForChildren(Item.Children), ClusterSize)
            )
    return RootClusters


def _PlanItemClusters(Root: list[_Item]) -> int:
    Total = 0
    for Item in _Flatten(Root):
        if Item.IsDirectory:
            Item.ClusterCount = Item.DirectoryClusters
        else:
            Item.ClusterCount = _CeilDiv(Item.Size, ClusterSize)
        Total += Item.ClusterCount
    return Total


def _AssignClusters(Root: list[_Item], FirstFree: int) -> int:
    Cluster = FirstFree
    for Item in _Flatten(Root):
        if Item.ClusterCount:
            Item.FirstCluster = Cluster
            Cluster += Item.ClusterCount
    return Cluster


def _WriteChain(Fat: bytearray, FirstCluster: int, Count: int) -> None:
    for Offset in range(Count):
        Cluster = FirstCluster + Offset
        Value = EndOfChain if Offset == Count - 1 else Cluster + 1
        struct.pack_into("<I", Fat, Cluster * 4, Value)


# ---------------------------------------------------------------------------
# image writing
# ---------------------------------------------------------------------------


def _ClusterOffset(HeapOffset: int, Cluster: int) -> int:
    return (HeapOffset + (Cluster - 2) * (ClusterSize // SectorSize)) * SectorSize


def _WriteClusters(Stream: BinaryIO, HeapOffset: int, First: int, Data: bytes) -> None:
    Stream.seek(_ClusterOffset(HeapOffset, First))
    Stream.write(Data)


def _BuildBootRegion(
    *,
    VolumeLength: int,
    FatOffset: int,
    FatLength: int,
    HeapOffset: int,
    ClusterCount: int,
    RootFirst: int,
    Serial: int,
    PercentInUse: int,
) -> bytes:
    Sector = bytearray(SectorSize)
    Sector[0:3] = b"\xEB\x76\x90"
    Sector[3:11] = b"EXFAT   "
    struct.pack_into("<Q", Sector, 72, VolumeLength)
    struct.pack_into("<I", Sector, 80, FatOffset)
    struct.pack_into("<I", Sector, 84, FatLength)
    struct.pack_into("<I", Sector, 88, HeapOffset)
    struct.pack_into("<I", Sector, 92, ClusterCount)
    struct.pack_into("<I", Sector, 96, RootFirst)
    struct.pack_into("<I", Sector, 100, Serial)
    struct.pack_into("<H", Sector, 104, 0x0100)
    struct.pack_into("<H", Sector, 106, 0)
    Sector[108] = SectorShift
    Sector[109] = ClusterShift
    Sector[110] = 1
    Sector[111] = 0x80
    Sector[112] = PercentInUse
    Sector[510:512] = b"\x55\xAA"

    Region = bytearray(BootRegionSectors * SectorSize)
    Region[0:SectorSize] = Sector

    Checksum = 0
    for Index in range(11 * SectorSize):
        if Index in (106, 107, 112):
            continue
        Checksum = ((((Checksum & 1) << 31) + (Checksum >> 1)) + Region[Index]) & 0xFFFFFFFF
    for Index in range(11 * SectorSize, BootRegionSectors * SectorSize, 4):
        struct.pack_into("<I", Region, Index, Checksum)
    return bytes(Region)


def _ClusterCountFor(Needed: int, VolumeSize: int | None) -> int:
    """Pick the cluster count, honouring a requested total volume size."""

    if VolumeSize is None:
        return Needed + max(2, Needed // 20)

    TargetSectors = VolumeSize // SectorSize
    ClusterCount = Needed
    for _ in range(16):
        FatLength = _CeilDiv((ClusterCount + 2) * 4, SectorSize)
        Candidate = TargetSectors - FatOffsetSectors - FatLength
        if Candidate == ClusterCount:
            break
        ClusterCount = max(Needed, Candidate)
    FatLength = _CeilDiv((ClusterCount + 2) * 4, SectorSize)
    if FatOffsetSectors + FatLength + ClusterCount > TargetSectors:
        return Needed + max(2, Needed // 20)
    return ClusterCount


def BuildExfatImage(
    SourceDirectory: PathType,
    Output: PathType | None = None,
    *,
    UpcaseTable: bytes | None = None,
    VolumeLabel: str | None = None,
    VolumeSize: int | None = None,
    Overwrite: bool = False,
    Progress: ProgressCallback | None = None,
) -> Path:
    """Write an exFAT image holding everything inside ``SourceDirectory``.

    ``VolumeSize`` requests a total image size in bytes (it must be a multiple
    of the sector size and large enough for the content); it is used to make a
    rebuilt image match the geometry of the container it will be packed into.
    """

    SourcePath = Path(SourceDirectory).resolve()
    OutputPath = (
        Path(Output) if Output is not None else SourcePath.with_suffix(".exfat")
    )
    if OutputPath.exists() and not Overwrite:
        raise FileExistsError(f"output already exists: {OutputPath}")

    Table = UpcaseTable if UpcaseTable else _GenerateUpcase()
    Mapping = _DecodeUpcase(Table)
    Root = _BuildTree(SourcePath)

    ExtraBytes = EntrySize * 2 + (EntrySize if VolumeLabel else 0)
    RootClusters = _PlanDirectoryClusters(Root, ExtraBytes)
    ItemClusters = _PlanItemClusters(Root)
    FileClusters = sum(
        Item.ClusterCount for Item in _Flatten(Root) if not Item.IsDirectory
    )
    DirectoryClusters = ItemClusters - FileClusters
    UpcaseClusters = max(1, _CeilDiv(len(Table), ClusterSize))

    BitmapClusters = 1
    while True:
        Needed = (
            BitmapClusters
            + UpcaseClusters
            + RootClusters
            + DirectoryClusters
            + FileClusters
        )
        ClusterCount = _ClusterCountFor(Needed, VolumeSize)
        BitmapBytes = _CeilDiv(ClusterCount, 8)
        Required = max(1, _CeilDiv(BitmapBytes, ClusterSize))
        if Required == BitmapClusters:
            break
        BitmapClusters = Required

    BitmapBytes = _CeilDiv(ClusterCount, 8)
    FatLength = _CeilDiv((ClusterCount + 2) * 4, SectorSize)
    FatOffset = FatOffsetSectors
    HeapOffset = FatOffset + FatLength
    VolumeLength = HeapOffset + ClusterCount

    BitmapFirst = 2
    UpcaseFirst = BitmapFirst + BitmapClusters
    RootFirst = UpcaseFirst + UpcaseClusters
    FirstFree = _AssignClusters(Root, RootFirst + RootClusters)

    Fat = bytearray(FatLength * SectorSize)
    struct.pack_into("<I", Fat, 0, ReservedFat)
    struct.pack_into("<I", Fat, 4, EndOfChain)
    _WriteChain(Fat, BitmapFirst, BitmapClusters)
    _WriteChain(Fat, UpcaseFirst, UpcaseClusters)
    _WriteChain(Fat, RootFirst, RootClusters)
    for Item in _Flatten(Root):
        if Item.ClusterCount:
            _WriteChain(Fat, Item.FirstCluster, Item.ClusterCount)

    Bitmap = bytearray(BitmapBytes)
    for Cluster in range(2, FirstFree):
        Offset = Cluster - 2
        Bitmap[Offset // 8] |= 1 << (Offset % 8)
    PercentInUse = min(100, (FirstFree - 2) * 100 // ClusterCount)

    Extra = b""
    if VolumeLabel:
        Extra += _VolumeLabelEntry(VolumeLabel)
    Extra += _BitmapEntry(BitmapFirst, BitmapBytes)
    Extra += _UpcaseEntry(UpcaseFirst, len(Table), UpcaseChecksum(Table))
    RootEntries = _BuildDirectory(Root, Extra, Mapping)

    Boot = _BuildBootRegion(
        VolumeLength=VolumeLength,
        FatOffset=FatOffset,
        FatLength=FatLength,
        HeapOffset=HeapOffset,
        ClusterCount=ClusterCount,
        RootFirst=RootFirst,
        Serial=struct.unpack("<I", os.urandom(4))[0],
        PercentInUse=PercentInUse,
    )

    TotalBytes = sum(Item.Size for Item in _Flatten(Root)) or 1
    Written = 0
    OutputPath.parent.mkdir(parents=True, exist_ok=True)
    with OutputPath.open("wb") as Stream:
        Stream.truncate(VolumeLength * SectorSize)
        Stream.write(Boot)
        Stream.write(Boot)
        Stream.seek(FatOffset * SectorSize)
        Stream.write(Fat)
        _WriteClusters(Stream, HeapOffset, BitmapFirst, Bitmap)
        _WriteClusters(Stream, HeapOffset, UpcaseFirst, Table)
        _WriteClusters(Stream, HeapOffset, RootFirst, RootEntries)
        for Item in _Flatten(Root):
            if Item.IsDirectory:
                Entries = _BuildDirectory(Item.Children, b"", Mapping)
                _WriteClusters(Stream, HeapOffset, Item.FirstCluster, Entries)
            elif Item.Size:
                Stream.seek(_ClusterOffset(HeapOffset, Item.FirstCluster))
                with Item.Source.open("rb") as Source:
                    Remaining = Item.Size
                    while Remaining > 0:
                        Chunk = Source.read(min(Remaining, CopyChunkSize))
                        if not Chunk:
                            break
                        Stream.write(Chunk)
                        Remaining -= len(Chunk)
                        Written += len(Chunk)
                        if Progress is not None:
                            Progress(Written, TotalBytes)
    return OutputPath.resolve()
