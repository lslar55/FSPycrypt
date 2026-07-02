from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import BinaryIO, Callable, cast

from .Exfat import ExfatVolume
from .Model import ContainerType
from .Ntfs import NtfsVolume
from .Reader import FSDecryptReader, PathType
from .Vhd import OpenVhdNtfs


ProgressCallback = Callable[[int, int], None]


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
    Overwrite: bool = False,
    Progress: ProgressCallback | None = None,
) -> Path:
    if hasattr(Source, "read") and hasattr(Source, "seek"):
        if OutputDirectory is None:
            raise ValueError("OutputDirectory is required for a stream")
        Volume = NtfsVolume(cast(BinaryIO, Source))
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
        Volume = NtfsVolume(Stream)
        InternalRecord = Volume.FindInternalVhd()
        if InternalRecord is not None:
            VhdStream = InternalRecord.open()
            NtfsStream = OpenVhdNtfs(VhdStream)
            InnerVolume = NtfsVolume(NtfsStream)
            return InnerVolume.Extract(
                OutputPath,
                Overwrite=Overwrite,
                Progress=Progress,
            )
        return Volume.Extract(
            OutputPath,
            Overwrite=Overwrite,
            Progress=Progress,
        )


def ExtractFiles(
    Source: PathType,
    OutputDirectory: PathType | None = None,
    *,
    Key: bytes | None = None,
    Iv: bytes | None = None,
    KeyDirectory: PathType | None = None,
    Overwrite: bool = False,
    Progress: ProgressCallback | None = None,
) -> Path:
    SourcePath = Path(Source)
    OutputPath = (
        Path(OutputDirectory)
        if OutputDirectory is not None
        else SourcePath.with_suffix("")
    )
    with SourcePath.open("rb") as SourceStream:
        Header = SourceStream.read(11)
    if Header[3:11] == b"EXFAT   ":
        return ExtractExfat(
            SourcePath,
            OutputPath,
            Overwrite=Overwrite,
            Progress=Progress,
        )
    if Header[3:11] == b"NTFS    ":
        return ExtractNtfs(
            SourcePath,
            OutputPath,
            Overwrite=Overwrite,
            Progress=Progress,
        )

    with FSDecryptReader(
        SourcePath,
        Key=Key,
        Iv=Iv,
        KeyDirectory=KeyDirectory,
    ) as Reader:
        if Reader.BootId.ContainerType is ContainerType.Option:
            Volume = ExfatVolume(Reader)
            return Volume.Extract(
                OutputPath,
                Overwrite=Overwrite,
                Progress=Progress,
            )

        OuterVolume = NtfsVolume(Reader)
        InternalName = f"internal_{Reader.BootId.SequenceNumber}.vhd"
        InternalRecord = OuterVolume.Find(InternalName)
        if InternalRecord is None:
            return OuterVolume.Extract(
                OutputPath,
                Overwrite=Overwrite,
                Progress=Progress,
            )
        VhdStream = InternalRecord.open()
        NtfsStream = OpenVhdNtfs(VhdStream)
        InnerVolume = NtfsVolume(NtfsStream)
        return InnerVolume.Extract(
            OutputPath,
            Overwrite=Overwrite,
            Progress=Progress,
        )
