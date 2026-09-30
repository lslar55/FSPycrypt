from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
from typing import BinaryIO, cast

from .Errors import FSDecryptError, MissingBaseError
from .Exfat import ExfatVolume
from .Model import ContainerType
from .Ntfs import NtfsVolume
from .Reader import FSDecryptReader
from .Util import PathType, ProgressCallback
from .Vhd import (
    OpenChainedVhdNtfs,
    OpenVhdNtfs,
    ReadVhdGuidInfo,
    VhdGuidInfo,
)


_ContainerSuffixes = (".app", ".pack")
_ImageSuffixes = (".ntfs",)
_VhdSuffixes = (".vhd",)
_CandidateSuffixes = _ContainerSuffixes + _ImageSuffixes + _VhdSuffixes

_OptionHeader = b"EXFAT   "
_NtfsHeader = b"NTFS    "


class _VhdSource:
    """A VHD reachable from some file on disk, together with its chain info."""

    def __init__(
        self,
        PathValue: Path,
        Stream: BinaryIO,
        Info: VhdGuidInfo,
    ) -> None:
        self.Path = PathValue
        self.Stream = Stream
        self.Info = Info


def _ReadHeader(SourcePath: Path) -> bytes:
    with SourcePath.open("rb") as Stream:
        return Stream.read(11)


def _OpenContainerVhd(
    Stack: ExitStack,
    SourcePath: Path,
    *,
    Key: bytes | None,
    Iv: bytes | None,
    KeyDirectory: PathType | None,
) -> tuple[FSDecryptReader, NtfsVolume, BinaryIO | None]:
    Reader = Stack.enter_context(
        FSDecryptReader(
            SourcePath,
            Key=Key,
            Iv=Iv,
            KeyDirectory=KeyDirectory,
        )
    )
    Outer = NtfsVolume(Reader)
    Record = Outer.Find(f"internal_{Reader.BootId.SequenceNumber}.vhd")
    return Reader, Outer, (Record.open() if Record is not None else None)


def _ScanCandidates(Directory: Path, SourcePath: Path) -> list[Path]:
    try:
        Entries = sorted(Directory.iterdir())
    except OSError:
        return []
    Source = SourcePath.resolve()
    return [
        Item
        for Item in Entries
        if Item.is_file()
        and Item.suffix.lower() in _CandidateSuffixes
        and Item.resolve() != Source
    ]


def _TryOpenVhdSource(
    Stack: ExitStack,
    Candidate: Path,
    *,
    GameId: str | None,
    Key: bytes | None,
    Iv: bytes | None,
    KeyDirectory: PathType | None,
    Strict: bool = False,
) -> _VhdSource | None:
    """Open a candidate file and locate the VHD it carries, if any."""

    try:
        Suffix = Candidate.suffix.lower()
        if Suffix in _VhdSuffixes:
            Stream = Stack.enter_context(Candidate.open("rb"))
            return _VhdSource(Candidate, Stream, ReadVhdGuidInfo(Stream))

        if Suffix in _ContainerSuffixes:
            # Cheaply reject containers of other games before opening their NTFS.
            Reader = Stack.enter_context(
                FSDecryptReader(
                    Candidate,
                    Key=Key,
                    Iv=Iv,
                    KeyDirectory=KeyDirectory,
                )
            )
            BootId = Reader.BootId
            if GameId is not None and BootId.GameId.strip() != GameId.strip():
                return None
            Outer = NtfsVolume(Reader)
            Record = Outer.Find(f"internal_{BootId.SequenceNumber}.vhd")
            if Record is None:
                return None
            Stream = Record.open()
            return _VhdSource(Candidate, Stream, ReadVhdGuidInfo(Stream))

        if Suffix in _ImageSuffixes:
            Stream = Stack.enter_context(Candidate.open("rb"))
            Volume = NtfsVolume(Stream)
            Record = Volume.FindInternalVhd()
            if Record is None:
                return None
            VhdStream = Record.open()
            return _VhdSource(Candidate, VhdStream, ReadVhdGuidInfo(VhdStream))
    except (FSDecryptError, OSError, ValueError):
        if Strict:
            raise
        return None
    return None


def _FindParentSource(
    Stack: ExitStack,
    SourcePath: Path,
    ParentId: bytes,
    *,
    GameId: str | None,
    Base: PathType | None,
    Key: bytes | None,
    Iv: bytes | None,
    KeyDirectory: PathType | None,
    Cache: dict[Path, _VhdSource | None],
) -> _VhdSource | None:
    Candidates: list[Path] = []
    if Base is not None:
        Candidates.append(Path(Base).resolve())
    Candidates.extend(_ScanCandidates(SourcePath.parent, SourcePath))

    for Candidate in dict.fromkeys(Candidates):
        if Candidate not in Cache:
            Cache[Candidate] = _TryOpenVhdSource(
                Stack,
                Candidate,
                GameId=GameId,
                Key=Key,
                Iv=Iv,
                KeyDirectory=KeyDirectory,
                Strict=Base is not None and Candidate == Path(Base).resolve(),
            )
        Source = Cache[Candidate]
        if Source is not None and Source.Info.OwnId == ParentId:
            return Source
    return None


def _ExtractVhdChain(
    Stack: ExitStack,
    SourcePath: Path,
    VhdStream: BinaryIO,
    OutputPath: Path,
    *,
    GameId: str | None,
    Base: PathType | None,
    Key: bytes | None,
    Iv: bytes | None,
    KeyDirectory: PathType | None,
    Overwrite: bool,
    Progress: ProgressCallback | None,
) -> Path:
    Layers: list[BinaryIO] = [VhdStream]
    Info = ReadVhdGuidInfo(VhdStream)
    Cache: dict[Path, _VhdSource | None] = {}

    while Info.ParentId is not None:
        Source = _FindParentSource(
            Stack,
            SourcePath,
            Info.ParentId,
            GameId=GameId,
            Base=Base,
            Key=Key,
            Iv=Iv,
            KeyDirectory=KeyDirectory,
            Cache=Cache,
        )
        if Source is None:
            raise MissingBaseError(
                f"{SourcePath.name} is a differencing image, but its base VHD "
                f"was not found; put the base container next to it or pass Base="
            )
        Layers.append(Source.Stream)
        Info = Source.Info

    Layers.reverse()
    NtfsStream = (
        OpenChainedVhdNtfs(Layers)
        if len(Layers) > 1
        else OpenVhdNtfs(Layers[0])
    )
    return NtfsVolume(NtfsStream).Extract(
        OutputPath,
        Overwrite=Overwrite,
        Progress=Progress,
    )


def DecryptFile(
    Source: PathType,
    Output: PathType | None = None,
    *,
    Key: bytes | None = None,
    Iv: bytes | None = None,
    KeyDirectory: PathType | None = None,
    Overwrite: bool = False,
    ChunkSize: int = 16 * 1024 * 1024,
    Progress: ProgressCallback | None = None,
) -> Path:
    """Decrypt a container to a raw ``.ntfs``/``.exfat`` image."""

    if ChunkSize <= 0:
        raise ValueError("ChunkSize must be positive")
    SourcePath = Path(Source)
    with FSDecryptReader(
        SourcePath,
        Key=Key,
        Iv=Iv,
        KeyDirectory=KeyDirectory,
    ) as Reader:
        OutputPath = (
            Path(Output)
            if Output is not None
            else SourcePath.with_name(Reader.BootId.OutputFilename)
        )
        if OutputPath.exists() and not Overwrite:
            raise FileExistsError(f"output already exists: {OutputPath}")
        OutputPath.parent.mkdir(parents=True, exist_ok=True)
        Written = 0
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
                while True:
                    Data = Reader.read(ChunkSize)
                    if not Data:
                        break
                    Destination.write(Data)
                    Written += len(Data)
                    if Progress is not None:
                        Progress(Written, Reader.BootId.PlaintextSize)
            os.replace(TemporaryPath, OutputPath)
        except BaseException:
            if TemporaryPath is not None:
                try:
                    TemporaryPath.unlink()
                except FileNotFoundError:
                    pass
            raise
    return OutputPath.resolve()


def ExtractExfat(
    Source: PathType | BinaryIO,
    OutputDirectory: PathType | None = None,
    *,
    Overwrite: bool = False,
    Progress: ProgressCallback | None = None,
) -> Path:
    """Extract the file tree of an exFAT image or decrypted exFAT stream."""

    if hasattr(Source, "read") and hasattr(Source, "seek"):
        if OutputDirectory is None:
            raise ValueError("OutputDirectory is required for a stream")
        Volume = ExfatVolume(cast(BinaryIO, Source))
        return Volume.Extract(
            OutputDirectory,
            Overwrite=Overwrite,
            Progress=Progress,
        )

    SourcePath = Path(Source)
    OutputPath = (
        Path(OutputDirectory)
        if OutputDirectory is not None
        else SourcePath.with_suffix("")
    )
    with SourcePath.open("rb") as Stream:
        Volume = ExfatVolume(Stream)
        return Volume.Extract(
            OutputPath,
            Overwrite=Overwrite,
            Progress=Progress,
        )


def ExtractNtfs(
    Source: PathType | BinaryIO,
    OutputDirectory: PathType | None = None,
    *,
    Base: PathType | None = None,
    Overwrite: bool = False,
    Progress: ProgressCallback | None = None,
) -> Path:
    """Extract the file tree of an NTFS image, following its inner VHD if any.

    The NTFS image of an APP/OS container holds an ``internal_<n>.vhd``; when
    that VHD is a delta it is merged with its base, which is looked up next to
    ``Source`` (or taken from ``Base``).
    """

    if hasattr(Source, "read") and hasattr(Source, "seek"):
        if OutputDirectory is None:
            raise ValueError("OutputDirectory is required for a stream")
        Stream = cast(BinaryIO, Source)
        Volume = NtfsVolume(Stream)
        Record = Volume.FindInternalVhd()
        if Record is None:
            return Volume.Extract(
                OutputDirectory,
                Overwrite=Overwrite,
                Progress=Progress,
            )
        VhdStream = Record.open()
        if ReadVhdGuidInfo(VhdStream).ParentId is not None:
            raise MissingBaseError(
                "a stream source cannot be merged with its base; pass a file "
                "path instead so the base can be located"
            )
        return NtfsVolume(OpenVhdNtfs(VhdStream)).Extract(
            OutputDirectory,
            Overwrite=Overwrite,
            Progress=Progress,
        )

    SourcePath = Path(Source)
    OutputPath = (
        Path(OutputDirectory)
        if OutputDirectory is not None
        else SourcePath.with_suffix("")
    )
    with ExitStack() as Stack:
        Stream = Stack.enter_context(SourcePath.open("rb"))
        Volume = NtfsVolume(Stream)
        Record = Volume.FindInternalVhd()
        if Record is None:
            return Volume.Extract(
                OutputPath,
                Overwrite=Overwrite,
                Progress=Progress,
            )
        return _ExtractVhdChain(
            Stack,
            SourcePath,
            Record.open(),
            OutputPath,
            GameId=None,
            Base=Base,
            Key=None,
            Iv=None,
            KeyDirectory=None,
            Overwrite=Overwrite,
            Progress=Progress,
        )


def ExtractFiles(
    Source: PathType,
    OutputDirectory: PathType | None = None,
    *,
    Base: PathType | None = None,
    Key: bytes | None = None,
    Iv: bytes | None = None,
    KeyDirectory: PathType | None = None,
    Overwrite: bool = False,
    Progress: ProgressCallback | None = None,
) -> Path:
    """Extract everything a container or image holds.

    The container type is detected automatically:

    * fscrypt OPTION containers are decrypted and their exFAT tree extracted;
    * fscrypt APP/OS containers are decrypted, their ``internal_<n>.vhd``
      extracted, and the NTFS tree inside that VHD written out. Differencing
      (delta) VHDs are transparently merged with their base, which is looked up
      next to ``Source`` or taken from ``Base``;
    * raw ``.exfat``/``.ntfs`` images go straight to the matching extractor.
    """

    SourcePath = Path(Source)
    OutputPath = (
        Path(OutputDirectory)
        if OutputDirectory is not None
        else SourcePath.with_suffix("")
    )

    Header = _ReadHeader(SourcePath)
    if Header[3:11] == _OptionHeader:
        return ExtractExfat(
            SourcePath,
            OutputPath,
            Overwrite=Overwrite,
            Progress=Progress,
        )
    if Header[3:11] == _NtfsHeader:
        return ExtractNtfs(
            SourcePath,
            OutputPath,
            Base=Base,
            Overwrite=Overwrite,
            Progress=Progress,
        )

    with ExitStack() as Stack:
        Reader = Stack.enter_context(
            FSDecryptReader(
                SourcePath,
                Key=Key,
                Iv=Iv,
                KeyDirectory=KeyDirectory,
            )
        )
        BootId = Reader.BootId
        if BootId.ContainerType is ContainerType.Option:
            return ExfatVolume(Reader).Extract(
                OutputPath,
                Overwrite=Overwrite,
                Progress=Progress,
            )

        Outer = NtfsVolume(Reader)
        Record = Outer.Find(f"internal_{BootId.SequenceNumber}.vhd")
        if Record is None:
            return Outer.Extract(
                OutputPath,
                Overwrite=Overwrite,
                Progress=Progress,
            )
        return _ExtractVhdChain(
            Stack,
            SourcePath,
            Record.open(),
            OutputPath,
            GameId=BootId.GameId,
            Base=Base,
            Key=Key,
            Iv=Iv,
            KeyDirectory=KeyDirectory,
            Overwrite=Overwrite,
            Progress=Progress,
        )
