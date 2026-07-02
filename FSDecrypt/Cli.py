from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .Api import DecryptFile, ExtractFiles
from .Errors import FSDecryptError
from .Reader import InspectContainer


def _Parser() -> argparse.ArgumentParser:
    Parser = argparse.ArgumentParser(
        prog="FSDecrypt",
        description="Decrypt SEGA fscrypt containers from Python",
    )
    Parser.add_argument("Files", nargs="+", type=Path)
    Parser.add_argument("-o", "--output", dest="Output", type=Path)
    Parser.add_argument("--key-directory", dest="KeyDirectory", type=Path)
    Parser.add_argument("--overwrite", dest="Overwrite", action="store_true")
    Parser.add_argument(
        "--info",
        dest="Info",
        action="store_true",
        help="Print container metadata without decrypting",
    )
    Parser.add_argument(
        "--no-extract",
        dest="NoExtract",
        action="store_true",
        help="Decrypt to a raw image without extracting files",
    )
    return Parser


def Main(Arguments: list[str] | None = None) -> int:
    Parser = _Parser()
    Values = Parser.parse_args(Arguments)
    if Values.Output is not None and len(Values.Files) != 1:
        Parser.error("--output can only be used with one input file")
    try:
        for Source in Values.Files:
            if Values.Info:
                print(
                    json.dumps(
                        InspectContainer(Source).AsDictionary(),
                        ensure_ascii=False,
                    )
                )
                continue

            LastPercent = -1

            def Report(Done: int, Total: int) -> None:
                nonlocal LastPercent
                Percent = Done * 100 // Total if Total else 100
                if Percent != LastPercent and (
                    Percent % 5 == 0 or Done == Total
                ):
                    print(
                        f"\r{Source.name}: {Percent:3d}%",
                        end="",
                        file=sys.stderr,
                    )
                    LastPercent = Percent

            if Values.NoExtract:
                Output = DecryptFile(
                    Source,
                    Values.Output,
                    KeyDirectory=Values.KeyDirectory,
                    Overwrite=Values.Overwrite,
                    Progress=Report,
                )
            else:
                Output = ExtractFiles(
                    Source,
                    Values.Output,
                    KeyDirectory=Values.KeyDirectory,
                    Overwrite=Values.Overwrite,
                    Progress=Report,
                )
            print(file=sys.stderr)
            print(Output)
    except (FSDecryptError, OSError, ValueError) as Error:
        print(f"FSDecrypt: {Error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(Main())
