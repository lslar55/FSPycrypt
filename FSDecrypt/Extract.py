from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Callable, Iterator, Protocol, Sequence
import uuid

from .Util import ProgressCallback, PathType, RemovePath, UniqueName


class ExtractNode(Protocol):
    """Shared view over the exFAT and NTFS tree nodes used while extracting."""

    @property
    def Name(self) -> str: ...

    @property
    def IsDirectory(self) -> bool: ...

    @property
    def Size(self) -> int: ...

    @property
    def Children(self) -> Sequence["ExtractNode"]: ...


WriteFileCallback = Callable[
    [ExtractNode, Path, "list[int]", int, ProgressCallback | None],
    None,
]

SetTimesCallback = Callable[[Path, ExtractNode], None]


def Flatten(Nodes: Sequence[ExtractNode]) -> Iterator[ExtractNode]:
    for Node in Nodes:
        yield Node
        yield from Flatten(Node.Children)


def _ExtractNodes(
    Nodes: Sequence[ExtractNode],
    Directory: Path,
    *,
    WriteFile: WriteFileCallback,
    SetTimes: SetTimesCallback,
    State: list[int],
    Total: int,
    Progress: ProgressCallback | None,
) -> None:
    UsedNames: set[str] = set()
    for Node in Nodes:
        Destination = Directory / UniqueName(Node.Name, UsedNames)
        if Node.IsDirectory:
            Destination.mkdir()
            _ExtractNodes(
                Node.Children,
                Destination,
                WriteFile=WriteFile,
                SetTimes=SetTimes,
                State=State,
                Total=Total,
                Progress=Progress,
            )
            SetTimes(Destination, Node)
        else:
            WriteFile(Node, Destination, State, Total, Progress)


def ExtractTree(
    OutputDirectory: PathType,
    Nodes: Sequence[ExtractNode],
    *,
    WriteFile: WriteFileCallback,
    SetTimes: SetTimesCallback,
    Overwrite: bool = False,
    Progress: ProgressCallback | None = None,
) -> Path:
    """Extract a parsed tree to ``OutputDirectory``, atomically.

    The tree is written to a temporary directory next to the destination and
    only swapped into place once every file has been written, so a failed or
    interrupted extraction never leaves a half-written output behind.
    """

    OutputPath = Path(OutputDirectory).resolve()
    if OutputPath.parent == OutputPath:
        raise ValueError("cannot extract to a filesystem root")
    if OutputPath.exists() and not Overwrite:
        raise FileExistsError(f"output already exists: {OutputPath}")
    OutputPath.parent.mkdir(parents=True, exist_ok=True)

    Total = sum(Node.Size for Node in Flatten(Nodes) if not Node.IsDirectory)
    TemporaryPath = Path(
        tempfile.mkdtemp(
            dir=OutputPath.parent,
            prefix=f".{OutputPath.name}.",
        )
    )
    BackupPath: Path | None = None
    try:
        _ExtractNodes(
            Nodes,
            TemporaryPath,
            WriteFile=WriteFile,
            SetTimes=SetTimes,
            State=[0],
            Total=Total,
            Progress=Progress,
        )
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
            RemovePath(BackupPath)
    except BaseException:
        RemovePath(TemporaryPath)
        raise
    return OutputPath
