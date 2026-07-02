import argparse
import sys
from pathlib import Path

from FSDecrypt import (
    BootId,
    ContainerType,
    DecryptFile,
    ExfatEntry,
    ExfatNode,
    ExfatVolume,
    ExtractExfat,
    ExtractFiles,
    ExtractNtfs,
    FSDecryptError,
    FSDecryptReader,
    InspectContainer,
    InvalidContainerError,
    InvalidExfatError,
    InvalidKeyFileError,
    InvalidNtfsError,
    InvalidVhdError,
    MissingKeyError,
    NtfsNode,
    NtfsVolume,
    OpenContainer,
    Timestamp,
    Version,
)


def FormatSize(Size: int) -> str:
    Value = float(Size)
    for Unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if Value < 1024 or Unit == "TiB":
            return f"{Value:.2f} {Unit}"
        Value /= 1024
    return f"{Value:.2f} TiB"


def CreateProgress(Label: str):
    LastPercent = -1

    def Progress(Done: int, Total: int) -> None:
        nonlocal LastPercent
        Percent = Done * 100 // Total if Total else 100
        if Percent != LastPercent:
            print(f"\r{Label}: {Percent:3d}%", end="", flush=True)
            LastPercent = Percent

    return Progress


def PrintResult(Result: Path) -> None:
    print(f"输出位置：{Result}")
    if not Result.is_dir():
        print(f"文件大小：{FormatSize(Result.stat().st_size)}")
        return
    Directories = sum(1 for Item in Result.rglob("*") if Item.is_dir())
    Files = [Item for Item in Result.rglob("*") if Item.is_file()]
    TotalSize = sum(Item.stat().st_size for Item in Files)
    print(f"目录数量：{Directories}")
    print(f"文件数量：{len(Files)}")
    print(f"文件总大小：{FormatSize(TotalSize)}")


def CreateParser() -> argparse.ArgumentParser:
    Parser = argparse.ArgumentParser(prog="fsdecrypt.py")
    Parser.add_argument("InputFile", type=Path)
    Parser.add_argument("-o", "--output", dest="OutputPath", type=Path)
    Parser.add_argument("-f", "--overwrite", dest="Overwrite", action="store_true")
    Parser.add_argument("--no-extract", dest="NoExtract", action="store_true")
    Parser.add_argument("--key-directory", dest="KeyDirectory", type=Path)
    return Parser


def Main(Arguments: list[str] | None = None) -> int:
    Values = CreateParser().parse_args(Arguments)
    InputFile = Values.InputFile.resolve()
    if not InputFile.is_file():
        print(f"找不到输入文件：{InputFile}", file=sys.stderr)
        return 1
    OutputPath = Values.OutputPath.resolve() if Values.OutputPath else None
    try:
        if Values.NoExtract:
            Result = DecryptFile(
                InputFile,
                OutputPath,
                KeyDirectory=Values.KeyDirectory,
                Overwrite=Values.Overwrite,
                Progress=CreateProgress("解密镜像"),
            )
        else:
            Result = ExtractFiles(
                InputFile,
                OutputPath,
                KeyDirectory=Values.KeyDirectory,
                Overwrite=Values.Overwrite,
                Progress=CreateProgress("提取文件"),
            )
        print()
        PrintResult(Result)
        return 0
    except (FSDecryptError, OSError, ValueError) as Error:
        print(f"处理失败：{Error}", file=sys.stderr)
        return 1


__all__ = [
    "BootId",
    "ContainerType",
    "DecryptFile",
    "ExfatEntry",
    "ExfatNode",
    "ExfatVolume",
    "ExtractExfat",
    "ExtractFiles",
    "ExtractNtfs",
    "FSDecryptError",
    "FSDecryptReader",
    "InspectContainer",
    "InvalidContainerError",
    "InvalidExfatError",
    "InvalidKeyFileError",
    "InvalidNtfsError",
    "InvalidVhdError",
    "Main",
    "MissingKeyError",
    "NtfsNode",
    "NtfsVolume",
    "OpenContainer",
    "Timestamp",
    "Version",
]


if __name__ == "__main__":
    raise SystemExit(Main())
