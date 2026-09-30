from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Callable


PathType = str | os.PathLike[str]

ProgressCallback = Callable[[int, int], None]


_InvalidCharacters = '<>:"/\\|?*'

_ReservedNames = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{Index}" for Index in range(1, 10)}
    | {f"LPT{Index}" for Index in range(1, 10)}
)


def SafeName(Name: str) -> str:
    """Turn an on-image name into a name that is safe to create on this host."""

    Result = "".join(
        "_" if ord(Character) < 32 or Character in _InvalidCharacters else Character
        for Character in Name
    ).rstrip(" .")
    if not Result:
        return "_"
    if Result.split(".", 1)[0].upper() in _ReservedNames:
        return f"_{Result}"
    return Result


def UniqueName(Name: str, UsedNames: set[str]) -> str:
    """Return a sanitized name that is unique within ``UsedNames``."""

    Candidate = SafeName(Name)
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


def RemovePath(PathValue: Path) -> None:
    if not PathValue.exists():
        return
    if PathValue.is_dir():
        shutil.rmtree(PathValue)
    else:
        PathValue.unlink()


def SetFileTimes(
    Destination: Path,
    Accessed: float | None,
    Modified: float | None,
) -> None:
    if Accessed is None and Modified is None:
        return
    Current = Destination.stat()
    os.utime(
        Destination,
        (
            Accessed if Accessed is not None else Current.st_atime,
            Modified if Modified is not None else Current.st_mtime,
        ),
    )
