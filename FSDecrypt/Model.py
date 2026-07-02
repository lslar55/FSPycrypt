from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import struct

from .Errors import InvalidContainerError


BootIdSize = 96


class ContainerType(IntEnum):
    Os = 0
    App = 1
    Option = 2


@dataclass(frozen=True, slots=True)
class Timestamp:
    Year: int
    Month: int
    Day: int
    Hour: int
    Minute: int
    Second: int

    def __str__(self) -> str:
        return (
            f"{self.Year:04d}{self.Month:02d}{self.Day:02d}"
            f"{self.Hour:02d}{self.Minute:02d}{self.Second:02d}"
        )


@dataclass(frozen=True, slots=True)
class Version:
    Major: int
    Minor: int
    Release: int

    def __str__(self) -> str:
        return f"{self.Major}.{self.Minor:02d}.{self.Release:02d}"


def _Ascii(Value: bytes, Field: str) -> str:
    try:
        return Value.decode("ascii").rstrip("\x00 ")
    except UnicodeDecodeError as Error:
        raise InvalidContainerError(f"{Field} is not ASCII") from Error


def _Timestamp(Data: bytes, Offset: int) -> Timestamp:
    Year, Month, Day, Hour, Minute, Second, _ = struct.unpack_from(
        "<H6B", Data, Offset
    )
    return Timestamp(Year, Month, Day, Hour, Minute, Second)


def _Version(Data: bytes, Offset: int) -> Version:
    Release, Minor, Major = struct.unpack_from("<BBH", Data, Offset)
    return Version(Major, Minor, Release)


@dataclass(frozen=True, slots=True)
class BootId:
    Crc32: int
    Length: int
    Signature: bytes
    ContainerType: ContainerType
    SequenceNumber: int
    UseCustomIv: bool
    GameId: str
    TargetTimestamp: Timestamp
    TargetVersion: Version
    OptionId: str
    BlockCount: int
    BlockSize: int
    HeaderBlockCount: int
    OsId: str
    OsGeneration: int
    SourceTimestamp: Timestamp
    SourceVersion: Version
    OsVersion: Version

    @classmethod
    def FromPlaintext(Class, Data: bytes) -> "BootId":
        if len(Data) != BootIdSize:
            raise InvalidContainerError(
                f"BootID must be {BootIdSize} bytes, got {len(Data)}"
            )
        ContainerValue = Data[13]
        try:
            ParsedContainerType = ContainerType(ContainerValue)
        except ValueError as Error:
            raise InvalidContainerError(
                f"unsupported container type {ContainerValue}"
            ) from Error

        TargetRaw = Data[28:32]
        return Class(
            Crc32=struct.unpack_from("<I", Data, 0)[0],
            Length=struct.unpack_from("<I", Data, 4)[0],
            Signature=Data[8:12],
            ContainerType=ParsedContainerType,
            SequenceNumber=Data[14],
            UseCustomIv=bool(Data[15]),
            GameId=_Ascii(Data[16:20], "GameId"),
            TargetTimestamp=_Timestamp(Data, 20),
            TargetVersion=_Version(Data, 28),
            OptionId=_Ascii(TargetRaw, "OptionId"),
            BlockCount=struct.unpack_from("<Q", Data, 32)[0],
            BlockSize=struct.unpack_from("<Q", Data, 40)[0],
            HeaderBlockCount=struct.unpack_from("<Q", Data, 48)[0],
            OsId=_Ascii(Data[64:67], "OsId"),
            OsGeneration=Data[67],
            SourceTimestamp=_Timestamp(Data, 68),
            SourceVersion=_Version(Data, 76),
            OsVersion=_Version(Data, 80),
        )

    @property
    def DataOffset(self) -> int:
        return self.HeaderBlockCount * self.BlockSize

    @property
    def PlaintextSize(self) -> int:
        if self.BlockCount < self.HeaderBlockCount:
            raise InvalidContainerError("header block count exceeds total block count")
        return self.BlockSize * (self.BlockCount - self.HeaderBlockCount)

    @property
    def OutputFilename(self) -> str:
        Stamp = str(self.TargetTimestamp)
        if self.ContainerType is ContainerType.Os:
            return f"{self.OsId}_{self.OsVersion}_{Stamp}_{self.SequenceNumber}.ntfs"
        if self.ContainerType is ContainerType.App:
            Suffix = ""
            if self.SequenceNumber > 0:
                Suffix = f"_{self.SourceVersion}"
            return (
                f"{self.GameId}_{self.TargetVersion}_{Stamp}_"
                f"{self.SequenceNumber}{Suffix}.ntfs"
            )
        return (
            f"{self.GameId}_{self.OptionId}_{Stamp}_"
            f"{self.SequenceNumber}.exfat"
        )

    def AsDictionary(self) -> dict[str, object]:
        return {
            "ContainerType": self.ContainerType.name,
            "GameId": self.GameId,
            "OsId": self.OsId,
            "OptionId": self.OptionId,
            "SequenceNumber": self.SequenceNumber,
            "TargetTimestamp": str(self.TargetTimestamp),
            "TargetVersion": str(self.TargetVersion),
            "SourceVersion": str(self.SourceVersion),
            "OsVersion": str(self.OsVersion),
            "BlockSize": self.BlockSize,
            "BlockCount": self.BlockCount,
            "HeaderBlockCount": self.HeaderBlockCount,
            "PlaintextSize": self.PlaintextSize,
            "OutputFilename": self.OutputFilename,
        }
