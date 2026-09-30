from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from dissect.ntfs import NTFS
from dissect.ntfs.c_ntfs import ATTRIBUTE_TYPE_CODE

from .Errors import InvalidNtfsError
from .Extract import ExtractTree
from .Util import PathType, ProgressCallback, SetFileTimes


@dataclass(frozen=True, slots=True)
class NtfsNode:
    Name: str
    IsDirectory: bool
    Size: int
    ModifiedTime: float | None
    AccessedTime: float | None
    Record: Any
    Children: tuple["NtfsNode", ...]


def _Times(Record: Any) -> tuple[float | None, float | None]:
    try:
        Attribute = Record.attributes[
            ATTRIBUTE_TYPE_CODE.STANDARD_INFORMATION
        ][0].attribute
        return (
            Attribute.last_modification_time.timestamp(),
            Attribute.last_access_time.timestamp(),
        )
    except (KeyError, IndexError, AttributeError, ValueError, OSError):
        return None, None


def _Skipped(Name: str) -> bool:
    return (
        Name.startswith("$")
        or Name in (".", "..")
        or Name == "System Volume Information"
    )


class NtfsVolume:
    def __init__(self, Stream: BinaryIO) -> None:
        Position = Stream.tell()
        Stream.seek(0)
        Header = Stream.read(11)
        Stream.seek(Position)
        if len(Header) < 11 or Header[3:11] != b"NTFS    ":
            raise InvalidNtfsError("image does not contain an NTFS boot sector")
        self._Stream = Stream
        try:
            self._Volume = NTFS(Stream)
        except Exception as Error:
            raise InvalidNtfsError(f"invalid NTFS image: {Error}") from Error

    @property
    def RootRecord(self):
        return self._Volume.mft.root

    def Find(self, Name: str):
        try:
            return self.RootRecord.get(Name)
        except Exception:
            return None

    def FindInternalVhd(self):
        try:
            Entries = self.RootRecord.listdir(
                dereference=True,
                ignore_dos=True,
            )
        except Exception as Error:
            raise InvalidNtfsError(f"cannot list NTFS root: {Error}") from Error
        Candidates = sorted(
            (
                Name,
                Record,
            )
            for Name, Record in Entries.items()
            if Name.lower().startswith("internal_")
            and Name.lower().endswith(".vhd")
            and Record.is_file()
        )
        return Candidates[0][1] if Candidates else None

    def BuildTree(self) -> tuple[NtfsNode, ...]:
        SeenDirectories = {self.RootRecord.segment}

        def Load(Name: str, Record: Any) -> NtfsNode:
            IsDirectory = Record.is_dir()
            ModifiedTime, AccessedTime = _Times(Record)
            if not IsDirectory:
                try:
                    Size = Record.size()
                except Exception as Error:
                    raise InvalidNtfsError(
                        f"cannot read size of {Name!r}: {Error}"
                    ) from Error
                return NtfsNode(
                    Name,
                    False,
                    Size,
                    ModifiedTime,
                    AccessedTime,
                    Record,
                    (),
                )

            if Record.segment in SeenDirectories:
                raise InvalidNtfsError("duplicate or cyclic NTFS directory")
            SeenDirectories.add(Record.segment)
            try:
                Entries = Record.listdir(dereference=True, ignore_dos=True)
            except Exception as Error:
                raise InvalidNtfsError(
                    f"cannot list NTFS directory {Name!r}: {Error}"
                ) from Error
            Children = tuple(
                Load(ChildName, ChildRecord)
                for ChildName, ChildRecord in Entries.items()
                if not _Skipped(ChildName)
            )
            return NtfsNode(
                Name,
                True,
                0,
                ModifiedTime,
                AccessedTime,
                Record,
                Children,
            )

        try:
            Entries = self.RootRecord.listdir(
                dereference=True,
                ignore_dos=True,
            )
        except Exception as Error:
            raise InvalidNtfsError(f"cannot list NTFS root: {Error}") from Error
        return tuple(
            Load(Name, Record)
            for Name, Record in Entries.items()
            if not _Skipped(Name)
        )

    def _SetTimes(self, Destination: Path, Node: NtfsNode) -> None:
        SetFileTimes(Destination, Node.AccessedTime, Node.ModifiedTime)

    def _WriteFile(
        self,
        Node: NtfsNode,
        Destination: Path,
        State: list[int],
        Total: int,
        Progress: ProgressCallback | None,
    ) -> None:
        try:
            with Node.Record.open() as Source, Destination.open("xb") as Output:
                while True:
                    Data = Source.read(4 * 1024 * 1024)
                    if not Data:
                        break
                    Output.write(Data)
                    State[0] += len(Data)
                    if Progress is not None:
                        Progress(State[0], Total)
        except Exception as Error:
            raise InvalidNtfsError(
                f"cannot extract NTFS file {Node.Name!r}: {Error}"
            ) from Error
        self._SetTimes(Destination, Node)

    def Extract(
        self,
        OutputDirectory: PathType,
        *,
        Overwrite: bool = False,
        Progress: ProgressCallback | None = None,
    ) -> Path:
        return ExtractTree(
            OutputDirectory,
            self.BuildTree(),
            WriteFile=self._WriteFile,
            SetTimes=self._SetTimes,
            Overwrite=Overwrite,
            Progress=Progress,
        )

