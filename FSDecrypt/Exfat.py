from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import math
import os
from pathlib import Path
import shutil
import struct
import tempfile
from typing import BinaryIO, Callable
import uuid

from .Errors import InvalidExfatError


ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True, slots=True)
class ExfatEntry:
    Name: str
    Attributes: int
    FirstCluster: int
    DataLength: int
    ValidDataLength: int
    NoFatChain: bool
    ModifiedTime: float | None
    AccessedTime: float | None

    @property
    def IsDirectory(self) -> bool:
        return bool(self.Attributes & 0x10)


@dataclass(frozen=True, slots=True)
class ExfatNode:
    Entry: ExfatEntry
    Children: tuple["ExfatNode", ...]


def _ReadExact(Stream: BinaryIO, Offset: int, Size: int) -> bytes:
    Stream.seek(Offset)
    Data = Stream.read(Size)
    if len(Data) != Size:
        raise InvalidExfatError(
            f"unexpected end of exFAT image at offset {Offset}"
        )
    return Data


def _Timestamp(Value: int, Increment: int, UtcOffset: int) -> float | None:
    if Value == 0:
        return None
    Second = (Value & 0x1F) * 2 + Increment // 100
    Minute = (Value >> 5) & 0x3F
    Hour = (Value >> 11) & 0x1F
    Day = (Value >> 16) & 0x1F
    Month = (Value >> 21) & 0x0F
    Year = ((Value >> 25) & 0x7F) + 1980
    Microsecond = (Increment % 100) * 10000
    OffsetSeconds = 0
    if UtcOffset & 0x80:
        QuarterHours = UtcOffset & 0x7F
        if QuarterHours & 0x40:
            QuarterHours -= 0x80
        OffsetSeconds = QuarterHours * 15 * 60
    try:
        Zone = timezone(timedelta(seconds=OffsetSeconds))
        return datetime(
            Year,
            Month,
            Day,
            Hour,
            Minute,
            Second,
            Microsecond,
            Zone,
        ).timestamp()
    except ValueError:
        return None


def _SetChecksum(Data: bytes) -> int:
    Checksum = 0
    for Index, Value in enumerate(Data):
        if Index in (2, 3):
            continue
        Checksum = (
            ((Checksum & 1) << 15) + (Checksum >> 1) + Value
        ) & 0xFFFF
    return Checksum


def _SafeName(Name: str) -> str:
    if Name in (".", ".."):
        raise InvalidExfatError(f"unsafe exFAT name: {Name!r}")
    InvalidCharacters = '<>:"/\\|?*'
    Result = "".join(
        "_" if ord(Character) < 32 or Character in InvalidCharacters else Character
        for Character in Name
    ).rstrip(" .")
    if not Result:
        Result = "_"
    Stem = Result.split(".", 1)[0].upper()
    Reserved = {"CON", "PRN", "AUX", "NUL"}
    Reserved.update(f"COM{Index}" for Index in range(1, 10))
    Reserved.update(f"LPT{Index}" for Index in range(1, 10))
    if Stem in Reserved:
        Result = f"_{Result}"
    return Result


def _UniqueName(Name: str, UsedNames: set[str]) -> str:
    Candidate = _SafeName(Name)
    Key = Candidate.casefold()
    if Key not in UsedNames:
        UsedNames.add(Key)
        return Candidate
    PathValue = Path(Candidate)
    Counter = 2
    while True:
        Candidate = f"{PathValue.stem} ({Counter}){PathValue.suffix}"
        Key = Candidate.casefold()
        if Key not in UsedNames:
            UsedNames.add(Key)
            return Candidate
        Counter += 1


def _RemovePath(PathValue: Path) -> None:
    if not PathValue.exists():
        return
    if PathValue.is_dir():
        shutil.rmtree(PathValue)
    else:
        PathValue.unlink()


class ExfatVolume:
    def __init__(self, Stream: BinaryIO) -> None:
        self._Stream = Stream
        Header = _ReadExact(Stream, 0, 512)
        if Header[3:11] != b"EXFAT   ":
            raise InvalidExfatError("image does not contain an exFAT boot sector")
        if Header[510:512] != b"\x55\xAA":
            raise InvalidExfatError("invalid exFAT boot signature")

        SectorShift = Header[108]
        ClusterShift = Header[109]
        if not 9 <= SectorShift <= 12:
            raise InvalidExfatError("invalid exFAT sector size")
        if ClusterShift > 25 - SectorShift:
            raise InvalidExfatError("invalid exFAT cluster size")

        self.SectorSize = 1 << SectorShift
        self.SectorsPerCluster = 1 << ClusterShift
        self.ClusterSize = self.SectorSize * self.SectorsPerCluster
        self.VolumeLength = struct.unpack_from("<Q", Header, 72)[0]
        self.FatOffset = struct.unpack_from("<I", Header, 80)[0]
        self.FatLength = struct.unpack_from("<I", Header, 84)[0]
        self.ClusterHeapOffset = struct.unpack_from("<I", Header, 88)[0]
        self.ClusterCount = struct.unpack_from("<I", Header, 92)[0]
        self.RootCluster = struct.unpack_from("<I", Header, 96)[0]
        self.VolumeFlags = struct.unpack_from("<H", Header, 106)[0]
        self.NumberOfFats = Header[110]

        if self.NumberOfFats not in (1, 2):
            raise InvalidExfatError("invalid number of exFAT allocation tables")
        if self.VolumeLength == 0:
            raise InvalidExfatError("invalid exFAT volume length")
        if self.FatOffset < 24 or self.FatLength == 0:
            raise InvalidExfatError("invalid exFAT allocation table location")
        FatEnd = self.FatOffset + self.FatLength * self.NumberOfFats
        if self.ClusterHeapOffset < FatEnd:
            raise InvalidExfatError("invalid exFAT cluster heap location")
        if self.ClusterCount == 0:
            raise InvalidExfatError("exFAT image has no clusters")
        HeapEnd = self.ClusterHeapOffset + (
            self.ClusterCount * self.SectorsPerCluster
        )
        if HeapEnd > self.VolumeLength:
            raise InvalidExfatError("exFAT cluster heap exceeds volume length")
        self._ValidateCluster(self.RootCluster)
        ActiveFat = self.VolumeFlags & 1 if self.NumberOfFats == 2 else 0
        self._FatByteOffset = (
            self.FatOffset + ActiveFat * self.FatLength
        ) * self.SectorSize

        Stream.seek(0, io.SEEK_END)
        PhysicalSize = Stream.tell()
        ExpectedSize = self.VolumeLength * self.SectorSize
        if ExpectedSize > PhysicalSize:
            raise InvalidExfatError(
                f"truncated exFAT image: expected {ExpectedSize} bytes, got {PhysicalSize}"
            )
        MinimumFatBytes = (self.ClusterCount + 2) * 4
        if self.FatLength * self.SectorSize < MinimumFatBytes:
            raise InvalidExfatError("exFAT allocation table is too small")

    def _ValidateCluster(self, Cluster: int) -> None:
        if not 2 <= Cluster <= self.ClusterCount + 1:
            raise InvalidExfatError(f"invalid exFAT cluster {Cluster}")

    def _ClusterOffset(self, Cluster: int) -> int:
        self._ValidateCluster(Cluster)
        Sector = self.ClusterHeapOffset + (
            Cluster - 2
        ) * self.SectorsPerCluster
        return Sector * self.SectorSize

    def _ReadCluster(self, Cluster: int) -> bytes:
        return _ReadExact(
            self._Stream,
            self._ClusterOffset(Cluster),
            self.ClusterSize,
        )

    def _FatValue(self, Cluster: int) -> int:
        self._ValidateCluster(Cluster)
        Data = _ReadExact(
            self._Stream,
            self._FatByteOffset + Cluster * 4,
            4,
        )
        return struct.unpack("<I", Data)[0]

    def _Clusters(
        self,
        FirstCluster: int,
        DataLength: int | None,
        NoFatChain: bool,
    ):
        if DataLength == 0:
            return
        self._ValidateCluster(FirstCluster)
        if NoFatChain:
            if DataLength is None:
                raise InvalidExfatError("contiguous allocation has no length")
            Count = math.ceil(DataLength / self.ClusterSize)
            for Index in range(Count):
                Cluster = FirstCluster + Index
                self._ValidateCluster(Cluster)
                yield Cluster
            return

        Seen: set[int] = set()
        Cluster = FirstCluster
        RequiredCount = (
            math.ceil(DataLength / self.ClusterSize)
            if DataLength is not None
            else None
        )
        while True:
            if Cluster in Seen:
                raise InvalidExfatError("cycle in exFAT cluster chain")
            self._ValidateCluster(Cluster)
            Seen.add(Cluster)
            yield Cluster
            if RequiredCount is not None and len(Seen) >= RequiredCount:
                return
            NextCluster = self._FatValue(Cluster)
            if NextCluster >= 0xFFFFFFF8:
                if RequiredCount is not None and len(Seen) < RequiredCount:
                    raise InvalidExfatError("exFAT cluster chain is too short")
                return
            if NextCluster in (0, 1, 0xFFFFFFF7):
                raise InvalidExfatError("invalid value in exFAT cluster chain")
            Cluster = NextCluster
            if len(Seen) > self.ClusterCount:
                raise InvalidExfatError("exFAT cluster chain exceeds volume size")

    def _ReadAllocation(
        self,
        FirstCluster: int,
        DataLength: int | None,
        NoFatChain: bool,
    ) -> bytes:
        Chunks = [
            self._ReadCluster(Cluster)
            for Cluster in self._Clusters(
                FirstCluster,
                DataLength,
                NoFatChain,
            )
        ]
        Data = b"".join(Chunks)
        return Data if DataLength is None else Data[:DataLength]

    def _ParseEntries(self, Data: bytes) -> tuple[ExfatEntry, ...]:
        Entries: list[ExfatEntry] = []
        Index = 0
        while Index + 32 <= len(Data):
            Primary = Data[Index : Index + 32]
            EntryType = Primary[0]
            if EntryType == 0:
                break
            if EntryType != 0x85:
                Index += 32
                continue

            SecondaryCount = Primary[1]
            SetSize = (SecondaryCount + 1) * 32
            SetData = Data[Index : Index + SetSize]
            if SecondaryCount < 2 or len(SetData) != SetSize:
                raise InvalidExfatError("incomplete exFAT file entry set")
            StoredChecksum = struct.unpack_from("<H", Primary, 2)[0]
            if _SetChecksum(SetData) != StoredChecksum:
                raise InvalidExfatError("invalid exFAT file entry checksum")

            StreamEntry = SetData[32:64]
            if StreamEntry[0] != 0xC0 or not StreamEntry[1] & 1:
                raise InvalidExfatError("invalid exFAT stream extension")
            NameLength = StreamEntry[3]
            NameEntryCount = math.ceil(NameLength / 15)
            if NameLength == 0 or NameEntryCount + 1 > SecondaryCount:
                raise InvalidExfatError("invalid exFAT file name length")

            NameData = bytearray()
            for NameIndex in range(NameEntryCount):
                NameEntry = SetData[
                    64 + NameIndex * 32 : 96 + NameIndex * 32
                ]
                if NameEntry[0] != 0xC1:
                    raise InvalidExfatError("missing exFAT file name entry")
                NameData.extend(NameEntry[2:32])
            try:
                Name = bytes(NameData[: NameLength * 2]).decode("utf-16-le")
            except UnicodeDecodeError as Error:
                raise InvalidExfatError("invalid UTF-16 exFAT file name") from Error

            Attributes = struct.unpack_from("<H", Primary, 4)[0]
            ModifiedTimestamp = struct.unpack_from("<I", Primary, 12)[0]
            AccessedTimestamp = struct.unpack_from("<I", Primary, 16)[0]
            ValidDataLength = struct.unpack_from("<Q", StreamEntry, 8)[0]
            FirstCluster = struct.unpack_from("<I", StreamEntry, 20)[0]
            DataLength = struct.unpack_from("<Q", StreamEntry, 24)[0]
            if ValidDataLength > DataLength:
                raise InvalidExfatError("valid data length exceeds data length")
            if DataLength:
                self._ValidateCluster(FirstCluster)

            Entries.append(
                ExfatEntry(
                    Name=Name,
                    Attributes=Attributes,
                    FirstCluster=FirstCluster,
                    DataLength=DataLength,
                    ValidDataLength=ValidDataLength,
                    NoFatChain=bool(StreamEntry[1] & 2),
                    ModifiedTime=_Timestamp(
                        ModifiedTimestamp,
                        Primary[21],
                        Primary[23],
                    ),
                    AccessedTime=_Timestamp(
                        AccessedTimestamp,
                        0,
                        Primary[24],
                    ),
                )
            )
            Index += SetSize
        return tuple(Entries)

    def ReadDirectory(
        self,
        Entry: ExfatEntry | None = None,
    ) -> tuple[ExfatEntry, ...]:
        if Entry is None:
            Data = self._ReadAllocation(self.RootCluster, None, False)
        else:
            if not Entry.IsDirectory:
                raise InvalidExfatError(f"{Entry.Name!r} is not a directory")
            Data = self._ReadAllocation(
                Entry.FirstCluster,
                Entry.DataLength,
                Entry.NoFatChain,
            )
        return self._ParseEntries(Data)

    def BuildTree(self) -> tuple[ExfatNode, ...]:
        SeenDirectories = {self.RootCluster}

        def Load(Entry: ExfatEntry) -> ExfatNode:
            if not Entry.IsDirectory:
                return ExfatNode(Entry, ())
            if Entry.DataLength == 0:
                return ExfatNode(Entry, ())
            if Entry.FirstCluster in SeenDirectories:
                raise InvalidExfatError("duplicate or cyclic exFAT directory")
            SeenDirectories.add(Entry.FirstCluster)
            Children = tuple(Load(Child) for Child in self.ReadDirectory(Entry))
            return ExfatNode(Entry, Children)

        return tuple(Load(Entry) for Entry in self.ReadDirectory())

    def _WriteFile(
        self,
        Entry: ExfatEntry,
        Destination: Path,
        State: list[int],
        Total: int,
        Progress: ProgressCallback | None,
    ) -> None:
        RemainingValid = Entry.ValidDataLength
        RemainingTotal = Entry.DataLength
        with Destination.open("xb") as Output:
            for Cluster in self._Clusters(
                Entry.FirstCluster,
                Entry.DataLength,
                Entry.NoFatChain,
            ):
                ClusterData = self._ReadCluster(Cluster)
                Count = min(len(ClusterData), RemainingTotal)
                ValidCount = min(Count, RemainingValid)
                if ValidCount:
                    Output.write(ClusterData[:ValidCount])
                if Count > ValidCount:
                    Output.write(bytes(Count - ValidCount))
                RemainingValid -= ValidCount
                RemainingTotal -= Count
                State[0] += Count
                if Progress is not None:
                    Progress(State[0], Total)
                if RemainingTotal == 0:
                    break
        self._SetTimes(Destination, Entry)

    def _SetTimes(self, Destination: Path, Entry: ExfatEntry) -> None:
        Modified = Entry.ModifiedTime
        Accessed = Entry.AccessedTime
        if Modified is None and Accessed is None:
            return
        Current = Destination.stat()
        os.utime(
            Destination,
            (
                Accessed if Accessed is not None else Current.st_atime,
                Modified if Modified is not None else Current.st_mtime,
            ),
        )

    def _ExtractNodes(
        self,
        Nodes: tuple[ExfatNode, ...],
        Directory: Path,
        State: list[int],
        Total: int,
        Progress: ProgressCallback | None,
    ) -> None:
        UsedNames: set[str] = set()
        for Node in Nodes:
            Name = _UniqueName(Node.Entry.Name, UsedNames)
            Destination = Directory / Name
            if Node.Entry.IsDirectory:
                Destination.mkdir()
                self._ExtractNodes(
                    Node.Children,
                    Destination,
                    State,
                    Total,
                    Progress,
                )
                self._SetTimes(Destination, Node.Entry)
            else:
                self._WriteFile(
                    Node.Entry,
                    Destination,
                    State,
                    Total,
                    Progress,
                )

    def Extract(
        self,
        OutputDirectory: str | os.PathLike[str],
        *,
        Overwrite: bool = False,
        Progress: ProgressCallback | None = None,
    ) -> Path:
        OutputPath = Path(OutputDirectory).resolve()
        if OutputPath.parent == OutputPath:
            raise ValueError("cannot extract to a filesystem root")
        if OutputPath.exists() and not Overwrite:
            raise FileExistsError(f"output already exists: {OutputPath}")
        OutputPath.parent.mkdir(parents=True, exist_ok=True)
        Nodes = self.BuildTree()
        Total = sum(
            Node.Entry.DataLength
            for Node in self._Flatten(Nodes)
            if not Node.Entry.IsDirectory
        )
        TemporaryPath = Path(
            tempfile.mkdtemp(
                dir=OutputPath.parent,
                prefix=f".{OutputPath.name}.",
            )
        )
        BackupPath: Path | None = None
        try:
            self._ExtractNodes(Nodes, TemporaryPath, [0], Total, Progress)
            if OutputPath.exists():
                BackupPath = OutputPath.with_name(
                    f".{OutputPath.name}.{uuid.uuid4().hex}.backup"
                )
                os.replace(OutputPath, BackupPath)
            try:
                os.replace(TemporaryPath, OutputPath)
            except BaseException:
                if BackupPath is not None and BackupPath.exists():
                    os.replace(BackupPath, OutputPath)
                raise
            if BackupPath is not None:
                _RemovePath(BackupPath)
        except BaseException:
            _RemovePath(TemporaryPath)
            raise
        return OutputPath

    def _Flatten(self, Nodes: tuple[ExfatNode, ...]):
        for Node in Nodes:
            yield Node
            yield from self._Flatten(Node.Children)
