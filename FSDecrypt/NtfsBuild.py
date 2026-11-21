"""Build NTFS images and APP/OS containers through the Linux ntfs-3g toolchain.

Windows cannot format an NTFS image file without administrator rights, and this
library does not carry an NTFS formatter.  Instead this module drives a WSL
distro (WSL 2 is enough, no Windows admin needed):

* ``mkntfs`` creates an empty NTFS volume in a plain file;
* ``ntfs-3g`` mounts that file inside WSL (as WSL *root*, still no Windows
  admin), the tree is copied in, and the volume is unmounted;
* the result is a normal image file, which is then wrapped in a fixed VHD and
  finally encrypted into a container.

Every step is delegated to the reference implementation, and ``ntfsfix`` /
``ntfsls`` / ``ntfscat`` can validate the result independently.
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from typing import Sequence
import uuid

from .Errors import FSDecryptError, NtfsBuildError
from .Util import PathType, ProgressCallback


PartitionOffset = 1024 * 1024
DiskSectorSize = 512
Mebibyte = 1024 * 1024

DefaultToolsPrefix = "~/.fsdecrypt/ntfs-tools"
ToolsVariable = "FSDECRYPT_NTFS_TOOLS"

DebianPool = "https://deb.debian.org/debian/pool/main"
# Fixed pair, verified to work together (ntfs-3g 2022.10.3 has integrated FUSE,
# so no libfuse3 is needed).  Pinning avoids picking a libntfs-3g build whose
# SONAME does not match the ntfs-3g binary.
DebianPackages = (
    ("n/ntfs-3g", "ntfs-3g_2022.10.3-5+deb13u3_amd64.deb"),
    ("n/ntfs-3g", "libntfs-3g89t64_2022.10.3-5+deb13u3_amd64.deb"),
)

HelperTemplate = """#!/bin/bash
set -eu
TOOLS=__TOOLS__
if [ -n "$TOOLS" ]; then
	case "$TOOLS" in
		"~/"*) TOOLS="$HOME/${TOOLS#\\~/}" ;;
	esac
	export LD_LIBRARY_PATH="$TOOLS/usr/lib/x86_64-linux-gnu:$TOOLS/lib/x86_64-linux-gnu"
	export PATH="$TOOLS/usr/bin:$TOOLS/usr/sbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
fi
Action="${1:-}"
shift || true
case "$Action" in
	make)
		Image="$1"
		Sector="${2:-}"
		Cluster="${3:-}"
		if [ -n "$Sector" ] && [ -n "$Cluster" ]; then
			mkntfs -F -q -s "$Sector" -c "$Cluster" -L FSDSYS "$Image"
		elif [ -n "$Sector" ]; then
			mkntfs -F -q -s "$Sector" -L FSDSYS "$Image"
		else
			mkntfs -F -q -L FSDSYS "$Image"
		fi
		;;
	fill)
		Image="$1"
		Source="$2"
		Mount=$(mktemp -d)
		ntfs-3g "$Image" "$Mount"
		cp -a "$Source"/. "$Mount"/
		sync
		umount "$Mount"
		rmdir "$Mount"
		;;
	put)
		Image="$1"
		Source="$2"
		Target="$3"
		Mount=$(mktemp -d)
		ntfs-3g "$Image" "$Mount"
		cp -a "$Source" "$Mount/$Target"
		sync
		umount "$Mount"
		rmdir "$Mount"
		;;
	probe)
		for Tool in mkntfs ntfs-3g; do
			command -v "$Tool" >/dev/null 2>&1 || exit 3
		done
		;;
	check)
		Image="$1"
		ntfsfix -n "$Image" 2>&1 | tail -3
		ntfsls -l -p / "$Image" 2>&1 | head -20
		;;
	*)
		echo "unknown action: $Action" >&2
		exit 2
		;;
esac
"""

InstallScript = """#!/bin/bash
set -eu
Prefix="$1"
case "$Prefix" in
	"~/"*) Prefix="$HOME/${Prefix#\\~/}" ;;
esac
Arch=$(uname -m)
if [ "$Arch" != "x86_64" ]; then
	echo "unsupported architecture: $Arch" >&2
	exit 4
fi
Pool="https://deb.debian.org/debian/pool/main"
mkdir -p "$Prefix/debs" "$Prefix/root"
cd "$Prefix/debs"
rm -f ./*.deb
__DOWNLOADS__
for Archive in ./*.deb; do
	dpkg-deb -x "$Archive" "$Prefix/root"
done
for Binary in "$Prefix/root/usr/bin/ntfs-3g" "$Prefix/root/usr/bin/fusermount3" \
	"$Prefix/root/usr/sbin/mkntfs" "$Prefix/root/usr/sbin/ntfscp"; do
	[ -e "$Binary" ] && chmod u-s "$Binary" || true
done
    echo "$Prefix/root"
"""


@dataclass(frozen=True, slots=True)
class NtfsToolchain:
    """Where the NTFS tools live: a private prefix, or the WSL system paths."""

    Prefix: str | None

    @property
    def IsSystem(self) -> bool:
        return self.Prefix is None

    def Describe(self) -> str:
        return "WSL system ntfs-3g" if self.IsSystem else f"private tools at {self.Prefix}"


def _Run(WslArguments: Sequence[str], *, Check: bool, Timeout: int = 7200):
    try:
        Result = subprocess.run(
            ["wsl", *WslArguments],
            capture_output=True,
            timeout=Timeout,
        )
    except FileNotFoundError as Error:
        raise NtfsBuildError(
            "WSL is required to build NTFS images but was not found; install "
            "WSL 2 and a Debian/Ubuntu distro, or use EncryptFile with a "
            "prebuilt .ntfs image instead"
        ) from Error
    except subprocess.TimeoutExpired as Error:
        raise NtfsBuildError("WSL command timed out") from Error
    Raw = (Result.stdout or b"") + (Result.stderr or b"")
    for Encoding in ("utf-8", "utf-16-le"):
        try:
            Text = Raw.decode(Encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        Text = Raw.decode("utf-8", "replace")
    if Check and Result.returncode != 0:
        raise NtfsBuildError(
            f"WSL command failed ({Result.returncode}): {' '.join(WslArguments)}\n"
            f"{Text.strip()[:2000]}"
        )
    return Result.returncode, Text


def _WindowsToWsl(PathValue: PathType) -> str:
    Text = str(Path(PathValue).resolve()).replace("\\", "/")
    if len(Text) > 1 and Text[1] == ":":
        return "/mnt/" + Text[0].lower() + Text[2:]
    return Text


def _HelperPath(Toolchain: NtfsToolchain) -> Path:
    Digest = hashlib.sha256(
        f"{Toolchain.Prefix}|{HelperTemplate}".encode()
    ).hexdigest()[:16]
    Directory = Path(tempfile.gettempdir()) / "fsdecrypt"
    Directory.mkdir(parents=True, exist_ok=True)
    Helper = Directory / f"ntfs_helper_{Digest}.sh"
    Content = HelperTemplate.replace("__TOOLS__", Toolchain.Prefix or "")
    if not Helper.is_file() or Helper.read_text(encoding="utf-8") != Content:
        Helper.write_text(Content, encoding="utf-8", newline="\n")
    return Helper


def _RunHelper(Toolchain: NtfsToolchain, *Arguments, Check=True):
    Helper = _HelperPath(Toolchain)
    return _Run(
        ["-u", "root", "bash", _WindowsToWsl(Helper), *Arguments],
        Check=Check,
    )


def _DefaultUserHome() -> str:
    """Absolute home of the *default* WSL user (tools are installed there)."""

    global _CachedHome
    if _CachedHome is None:
        try:
            _, Output = _Run(["bash", "-lc", 'printf %s "$HOME"'], Check=False)
            _CachedHome = Output.strip().splitlines()[-1].strip() if Output.strip() else ""
        except NtfsBuildError:
            _CachedHome = ""
    return _CachedHome


_CachedHome: str | None = None


def _DefaultToolsBase() -> str:
    Home = _DefaultUserHome()
    return f"{Home}/.fsdecrypt/ntfs-tools" if Home else DefaultToolsPrefix


def _DefaultToolsPrefix() -> str:
    return f"{_DefaultToolsBase()}/root"


def _NormalizeToolsBase(Prefix: str) -> str:
    Value = Prefix.rstrip("/")
    if Value.endswith("/root"):
        return Value[: -len("/root")]
    return Value


def FindNtfsToolchain(Prefix: str | None = None) -> NtfsToolchain | None:
    """Locate a usable ntfs-3g toolchain; return ``None`` when unavailable."""

    Candidates: list[NtfsToolchain] = []
    if Prefix:
        Candidates.append(NtfsToolchain(Prefix))
    Environment = os.environ.get(ToolsVariable)
    if Environment:
        Candidates.append(NtfsToolchain(Environment))
    Candidates.append(NtfsToolchain(_DefaultToolsPrefix()))
    Candidates.append(NtfsToolchain(None))

    for Candidate in Candidates:
        try:
            Code, _ = _RunHelper(Candidate, "probe", Check=False)
        except NtfsBuildError:
            return None
        if Code == 0:
            return Candidate
    return None


def InstallNtfsTools(Prefix: str | None = None) -> NtfsToolchain:
    """Download and unpack ntfs-3g into a private prefix inside WSL.

    The Debian ``.deb`` packages are fetched with the distro's own ``curl`` and
    unpacked with ``dpkg-deb -x``; nothing is installed system-wide, no root is
    needed, and no Windows administrator rights are involved.
    """

    Target = _NormalizeToolsBase(Prefix or _DefaultToolsBase())
    Downloads: list[str] = []
    for Directory, Filename in DebianPackages:
        Url = f"{DebianPool}/{Directory}/{Filename}"
        Downloads.append(
            f'[ -f "{Filename}" ] || curl -sSfLO "{Url}" || '
            f'{{ echo "cannot download {Url}" >&2; exit 5; }}\n'
        )
    Script = InstallScript.replace("__DOWNLOADS__", "\n".join(Downloads))
    Directory = Path(tempfile.gettempdir()) / "fsdecrypt"
    Directory.mkdir(parents=True, exist_ok=True)
    ScriptPath = Directory / "ntfs_install.sh"
    ScriptPath.write_text(Script, encoding="utf-8", newline="\n")
    Code, Output = _Run(
        [
            "bash",
            _WindowsToWsl(ScriptPath),
            Target,
        ],
        Check=False,
    )
    if Code != 0:
        raise NtfsBuildError(
            "installing the ntfs-3g toolchain failed; make sure a Debian/Ubuntu "
            f"WSL distro with curl and dpkg-deb is available\n{Output.strip()[:2000]}"
        )
    Lines = [Line.strip() for Line in Output.strip().splitlines() if Line.strip()]
    Absolute = Lines[-1] if Lines and Lines[-1].startswith("/") else f"{Target}/root"
    Toolchain = NtfsToolchain(Absolute)
    if FindNtfsToolchain(Toolchain.Prefix) is None:
        raise NtfsBuildError(
            f"ntfs-3g was unpacked to {Toolchain.Prefix} but does not run"
        )
    return Toolchain


def _RequireToolchain(Toolchain: NtfsToolchain | None) -> NtfsToolchain:
    Resolved = Toolchain if Toolchain is not None else FindNtfsToolchain()
    if Resolved is None:
        raise NtfsBuildError(
            "building .pack containers from a folder needs the Linux ntfs-3g "
            "toolchain running under WSL. Call FSDecrypt.InstallNtfsTools() "
            "once (downloads ~1 MB into a private prefix inside the WSL distro, "
            "no administrator rights needed), or pass a prebuilt .ntfs image to "
            "EncryptFile instead."
        )
    return Resolved


def DirectorySize(Directory: PathType) -> int:
    Total = 0
    Root = Path(Directory)
    for Item in Root.rglob("*"):
        try:
            if Item.is_file() and not Item.is_symlink():
                Total += Item.stat().st_size
        except OSError:
            continue
    return Total


def PlanVolumeSize(ContentBytes: int, *, Overhead: float, Floor: int = 64 * Mebibyte) -> int:
    """Pick an NTFS volume size generous enough for the content."""

    Raw = int(ContentBytes * Overhead) + Floor
    return max(16 * Mebibyte, math.ceil(Raw / Mebibyte) * Mebibyte)


def MakeNtfsImage(
    Output: PathType,
    *,
    Size: int,
    SectorSize: int = DiskSectorSize,
    ClusterSize: int | None = None,
    Toolchain: NtfsToolchain | None = None,
) -> Path:
    """Create an empty NTFS volume of ``Size`` bytes in a new file."""

    Toolchain = _RequireToolchain(Toolchain)
    OutputPath = Path(Output)
    OutputPath.parent.mkdir(parents=True, exist_ok=True)
    with OutputPath.open("wb") as Stream:
        Stream.truncate(Size)
    Arguments: list[str] = ["make", _WindowsToWsl(OutputPath), str(SectorSize)]
    if ClusterSize is not None:
        Arguments.append(str(ClusterSize))
    _RunHelper(Toolchain, *Arguments)
    return OutputPath


def FillNtfsImage(
    Image: PathType,
    Directory: PathType,
    *,
    Toolchain: NtfsToolchain | None = None,
) -> Path:
    """Copy the contents of ``Directory`` into a mounted NTFS image."""

    Toolchain = _RequireToolchain(Toolchain)
    _RunHelper(
        Toolchain,
        "fill",
        _WindowsToWsl(Image),
        _WindowsToWsl(Directory),
    )
    return Path(Image)


def PutNtfsFile(
    Image: PathType,
    Source: PathType,
    TargetName: str,
    *,
    Toolchain: NtfsToolchain | None = None,
) -> Path:
    """Copy a single file into the root of a mounted NTFS image."""

    Toolchain = _RequireToolchain(Toolchain)
    if "/" in TargetName or "\\" in TargetName:
        raise ValueError("TargetName must be a plain file name")
    _RunHelper(
        Toolchain,
        "put",
        _WindowsToWsl(Image),
        _WindowsToWsl(Source),
        TargetName,
    )
    return Path(Image)


def _ChsGeometry(TotalSectors: int) -> tuple[int, int, int]:
    if TotalSectors > 65535 * 16 * 63:
        Sectors, Heads = 255, 16
    else:
        Sectors, Heads = 17, 4
        if TotalSectors // (Sectors * Heads) > 65535:
            Sectors, Heads = 31, 16
            if TotalSectors // (Sectors * Heads) > 65535:
                Sectors, Heads = 63, 16
    return min(TotalSectors // (Sectors * Heads), 65535), Heads, Sectors


def BuildFixedVhd(
    NtfsImage: PathType,
    Output: PathType,
    *,
    PartitionOffset: int = PartitionOffset,
    DiskId: bytes | None = None,
    Progress: ProgressCallback | None = None,
) -> Path:
    """Wrap an NTFS volume in a fixed VHD with an MBR partition at 1 MiB."""

    ImagePath = Path(NtfsImage)
    OutputPath = Path(Output)
    NtfsBytes = ImagePath.stat().st_size
    if NtfsBytes % DiskSectorSize:
        raise FSDecryptError("NTFS image size is not a multiple of 512 bytes")
    if PartitionOffset % DiskSectorSize:
        raise FSDecryptError("partition offset is not sector aligned")

    DiskSize = PartitionOffset + NtfsBytes
    Cylinders, Heads, Sectors = _ChsGeometry(DiskSize // DiskSectorSize)

    Mbr = bytearray(DiskSectorSize)
    Entry = 0x1BE
    Mbr[Entry] = 0x80
    Mbr[Entry + 1 : Entry + 4] = b"\xFF\xFF\xFF"
    Mbr[Entry + 4] = 0x07
    Mbr[Entry + 5 : Entry + 8] = b"\xFF\xFF\xFF"
    struct.pack_into("<I", Mbr, Entry + 8, PartitionOffset // DiskSectorSize)
    struct.pack_into("<I", Mbr, Entry + 12, NtfsBytes // DiskSectorSize)
    Mbr[510:512] = b"\x55\xAA"

    Footer = bytearray(DiskSectorSize)
    Footer[0:8] = b"conectix"
    struct.pack_into(">I", Footer, 8, 0x00000002)
    struct.pack_into(">I", Footer, 12, 0x00010000)
    struct.pack_into(">Q", Footer, 16, 0xFFFFFFFFFFFFFFFF)
    Footer[28:32] = b"fsdc"
    struct.pack_into(">I", Footer, 32, 0x00010000)
    Footer[36:40] = b"Wi2k"
    struct.pack_into(">Q", Footer, 40, DiskSize)
    struct.pack_into(">Q", Footer, 48, DiskSize)
    struct.pack_into(">I", Footer, 56, (Cylinders << 16) | (Heads << 8) | Sectors)
    struct.pack_into(">I", Footer, 60, 2)
    Footer[68:84] = DiskId if DiskId is not None else uuid.uuid4().bytes
    Checksum = sum(Footer[:64]) + sum(Footer[84:])
    struct.pack_into(">I", Footer, 64, (~Checksum) & 0xFFFFFFFF)

    OutputPath.parent.mkdir(parents=True, exist_ok=True)
    Written = 0
    with OutputPath.open("wb") as Stream:
        Stream.write(Mbr)
        Stream.truncate(PartitionOffset)
        Stream.seek(PartitionOffset)
        with ImagePath.open("rb") as Source:
            while True:
                Chunk = Source.read(4 * 1024 * 1024)
                if not Chunk:
                    break
                Stream.write(Chunk)
                Written += len(Chunk)
                if Progress is not None:
                    Progress(Written, NtfsBytes)
        Stream.write(Footer)
    return OutputPath


def CheckNtfsImage(Image: PathType, *, Toolchain: NtfsToolchain | None = None) -> str:
    """Run ``ntfsfix -n``/``ntfsls`` on an image and return their output."""

    Toolchain = _RequireToolchain(Toolchain)
    _, Output = _RunHelper(
        Toolchain, "check", _WindowsToWsl(Image), Check=False
    )
    return Output


def BuildAppImage(
    SourceDirectory: PathType,
    Output: PathType,
    *,
    SequenceNumber: int,
    Toolchain: NtfsToolchain | None = None,
    Progress: ProgressCallback | None = None,
) -> Path:
    """Build the outer NTFS image that an APP/OS container wraps.

    The result is the container's plaintext: an NTFS volume (4096-byte sectors,
    which the container format's IV derivation requires) holding exactly one
    ``internal_<SequenceNumber>.vhd``.  That VHD is a fixed VHD whose NTFS
    volume contains the source directory.
    """

    Toolchain = _RequireToolchain(Toolchain)
    SourcePath = Path(SourceDirectory)
    if not SourcePath.is_dir():
        raise NotADirectoryError(f"not a directory: {SourcePath}")
    OutputPath = Path(Output)
    OutputPath.parent.mkdir(parents=True, exist_ok=True)

    ContentBytes = DirectorySize(SourcePath)
    WorkDirectory = Path(
        tempfile.mkdtemp(dir=OutputPath.parent, prefix=f".{OutputPath.name}.")
    )
    try:
        InnerImage = WorkDirectory / "inner.ntfs"
        MakeNtfsImage(
            InnerImage,
            Size=PlanVolumeSize(ContentBytes, Overhead=1.5, Floor=64 * Mebibyte),
            Toolchain=Toolchain,
        )
        FillNtfsImage(InnerImage, SourcePath, Toolchain=Toolchain)
        if Progress is not None:
            Progress(ContentBytes, ContentBytes)

        VhdPath = WorkDirectory / f"internal_{SequenceNumber}.vhd"
        BuildFixedVhd(InnerImage, VhdPath)
        InnerImage.unlink(missing_ok=True)

        OuterImage = WorkDirectory / "outer.ntfs"
        MakeNtfsImage(
            OuterImage,
            Size=PlanVolumeSize(
                VhdPath.stat().st_size, Overhead=1.2, Floor=64 * Mebibyte
            ),
            SectorSize=4096,
            ClusterSize=4096,
            Toolchain=Toolchain,
        )
        PutNtfsFile(
            OuterImage,
            VhdPath,
            f"internal_{SequenceNumber}.vhd",
            Toolchain=Toolchain,
        )
        VhdPath.unlink(missing_ok=True)

        shutil.move(str(OuterImage), str(OutputPath))
    finally:
        shutil.rmtree(WorkDirectory, ignore_errors=True)
    return OutputPath
