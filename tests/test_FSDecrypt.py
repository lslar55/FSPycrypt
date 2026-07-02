from __future__ import annotations

import io
from pathlib import Path
import struct
import tempfile
import unittest

from Crypto.Cipher import AES

from FSDecrypt import (
    FSDecryptReader,
    DecryptFile,
    ExfatVolume,
    ExtractExfat,
    InspectContainer,
)
from FSDecrypt.Crypto import PageSize, CalculatePageIv
from FSDecrypt.Keys import BootIdIv, BootIdKey, NtfsHeader
from FSDecrypt.Vhd import FindNtfsOffset, OffsetStream


TestKey = bytes.fromhex("00112233445566778899aabbccddeeff")
TestIv = bytes.fromhex("ffeeddccbbaa99887766554433221100")


def MakeBootId(CustomIv: bool, Pages: int) -> bytes:
    BootIdData = bytearray(96)
    struct.pack_into("<II", BootIdData, 0, 0, 96)
    BootIdData[8:12] = b"FSC2"
    BootIdData[13] = 1
    BootIdData[14] = 0
    BootIdData[15] = CustomIv
    BootIdData[16:20] = b"TEST"
    struct.pack_into("<H6B", BootIdData, 20, 2024, 1, 2, 12, 0, 0, 0)
    struct.pack_into("<BBH", BootIdData, 28, 0, 0, 1)
    struct.pack_into("<QQQQ", BootIdData, 32, Pages + 1, PageSize, 1, 0)
    BootIdData[64:67] = b"ACA"
    struct.pack_into("<H6B", BootIdData, 68, 2024, 1, 1, 12, 0, 0, 0)
    struct.pack_into("<BBH", BootIdData, 76, 0, 0, 0)
    struct.pack_into("<BBH", BootIdData, 80, 0, 0, 0)
    return bytes(BootIdData)


def MakeContainer(Plaintext: bytes, CustomIv: bool = False) -> bytes:
    if len(Plaintext) % PageSize:
        raise ValueError("test plaintext must contain complete pages")
    BootIdData = MakeBootId(CustomIv, len(Plaintext) // PageSize)
    EncryptedBootId = AES.new(BootIdKey, AES.MODE_CBC, BootIdIv).encrypt(
        BootIdData
    )
    Header = EncryptedBootId + bytes(PageSize - len(EncryptedBootId))
    Pages = []
    for Offset in range(0, len(Plaintext), PageSize):
        PageIv = CalculatePageIv(Offset, TestIv)
        Pages.append(
            AES.new(TestKey, AES.MODE_CBC, PageIv).encrypt(
                Plaintext[Offset : Offset + PageSize]
            )
        )
    return Header + b"".join(Pages)


def ExfatSet(
    Name: str,
    FirstCluster: int,
    Data: bytes,
    Directory: bool,
    NoFatChain: bool = True,
) -> bytes:
    NameBytes = Name.encode("utf-16-le")
    NameCount = (len(Name) + 14) // 15
    Primary = bytearray(32)
    Primary[0] = 0x85
    Primary[1] = NameCount + 1
    struct.pack_into("<H", Primary, 4, 0x10 if Directory else 0x20)

    Stream = bytearray(32)
    Stream[0] = 0xC0
    Stream[1] = 3 if NoFatChain else 1
    Stream[3] = len(Name)
    struct.pack_into("<Q", Stream, 8, len(Data))
    struct.pack_into("<I", Stream, 20, FirstCluster)
    struct.pack_into("<Q", Stream, 24, len(Data))

    Names = []
    for Index in range(NameCount):
        Entry = bytearray(32)
        Entry[0] = 0xC1
        Part = NameBytes[Index * 30 : (Index + 1) * 30]
        Entry[2 : 2 + len(Part)] = Part
        Names.append(Entry)

    Entries = [Primary, Stream, *Names]
    Checksum = 0
    for ByteIndex, Value in enumerate(b"".join(Entries)):
        if ByteIndex in (2, 3):
            continue
        Checksum = (((Checksum & 1) << 15) + (Checksum >> 1) + Value) & 0xFFFF
    struct.pack_into("<H", Primary, 2, Checksum)
    return b"".join(Entries)


def MakeExfatImage() -> bytes:
    SectorSize = 512
    ClusterCount = 5
    FatOffset = 24
    HeapOffset = 25
    VolumeLength = HeapOffset + ClusterCount
    Image = bytearray(VolumeLength * SectorSize)
    Image[0:3] = b"\xEB\x76\x90"
    Image[3:11] = b"EXFAT   "
    struct.pack_into("<Q", Image, 72, VolumeLength)
    struct.pack_into("<I", Image, 80, FatOffset)
    struct.pack_into("<I", Image, 84, 1)
    struct.pack_into("<I", Image, 88, HeapOffset)
    struct.pack_into("<I", Image, 92, ClusterCount)
    struct.pack_into("<I", Image, 96, 2)
    struct.pack_into("<H", Image, 104, 0x0100)
    Image[108] = 9
    Image[109] = 0
    Image[110] = 1
    Image[510:512] = b"\x55\xAA"
    struct.pack_into("<I", Image, FatOffset * SectorSize + 2 * 4, 0xFFFFFFFF)
    struct.pack_into("<I", Image, FatOffset * SectorSize + 3 * 4, 6)
    struct.pack_into("<I", Image, FatOffset * SectorSize + 6 * 4, 0xFFFFFFFF)

    DirectoryData = bytes(SectorSize)
    HelloData = b"A" * SectorSize + b"B" * 10
    Root = (
        ExfatSet("Hello.txt", 3, HelloData, False, False)
        + ExfatSet("Folder", 4, DirectoryData, True)
        + bytes(32)
    )
    Folder = ExfatSet("World.bin", 5, b"XYZ", False) + bytes(32)
    Image[HeapOffset * SectorSize : HeapOffset * SectorSize + len(Root)] = Root
    Image[(HeapOffset + 1) * SectorSize : (HeapOffset + 2) * SectorSize] = b"A" * SectorSize
    Image[(HeapOffset + 2) * SectorSize : (HeapOffset + 2) * SectorSize + len(Folder)] = Folder
    Image[(HeapOffset + 3) * SectorSize : (HeapOffset + 3) * SectorSize + 3] = b"XYZ"
    Image[(HeapOffset + 4) * SectorSize : (HeapOffset + 4) * SectorSize + 10] = b"B" * 10
    return bytes(Image)


class FSDecryptTests(unittest.TestCase):
    def setUp(self) -> None:
        First = NtfsHeader + bytes(
            (Index % 251 for Index in range(PageSize - 16))
        )
        Second = bytes(((Index + 17) % 251 for Index in range(PageSize)))
        self.Plaintext = First + Second

    def test_PageIvMatchesUpstreamAlgorithm(self) -> None:
        Value = CalculatePageIv(0x0102030405060708, bytes(16))
        self.assertEqual(Value, bytes([8, 7, 6, 5, 4, 3, 2, 1] * 2))

    def test_RandomAccessReader(self) -> None:
        Container = MakeContainer(self.Plaintext)
        with FSDecryptReader(
            io.BytesIO(Container),
            Key=TestKey,
            Iv=TestIv,
        ) as Reader:
            self.assertEqual(Reader.BootId.GameId, "TEST")
            self.assertEqual(Reader.read(31), self.Plaintext[:31])
            Reader.seek(PageSize - 7)
            self.assertEqual(
                Reader.read(29),
                self.Plaintext[PageSize - 7 : PageSize + 22],
            )
            Reader.seek(-16, io.SEEK_END)
            self.assertEqual(Reader.read(), self.Plaintext[-16:])

    def test_AutomaticIvDerivation(self) -> None:
        Container = MakeContainer(self.Plaintext, True)
        with FSDecryptReader(
            io.BytesIO(Container),
            Key=TestKey,
        ) as Reader:
            self.assertEqual(Reader.read(), self.Plaintext)
        with FSDecryptReader(
            io.BytesIO(Container),
            Key=TestKey,
            Iv=TestIv,
        ) as Reader:
            self.assertEqual(Reader.read(), self.Plaintext)

    def test_FileApiAndExternalKey(self) -> None:
        with tempfile.TemporaryDirectory() as Directory:
            Root = Path(Directory)
            Source = Root / "Input.app"
            Source.write_bytes(MakeContainer(self.Plaintext))
            (Root / "TEST.bin").write_bytes(TestKey + TestIv)
            Information = InspectContainer(Source)
            self.assertEqual(
                Information.OutputFilename,
                "TEST_1.00.00_20240102120000_0.ntfs",
            )
            Output = DecryptFile(Source)
            self.assertEqual(Output.read_bytes(), self.Plaintext)

    def test_FailedOverwritePreservesExistingOutput(self) -> None:
        with tempfile.TemporaryDirectory() as Directory:
            Root = Path(Directory)
            Source = Root / "Input.app"
            Output = Root / "Existing.ntfs"
            Source.write_bytes(MakeContainer(self.Plaintext))
            Output.write_bytes(b"keep me")

            def FailProgress(Done: int, Total: int) -> None:
                raise RuntimeError(f"stop {Done}/{Total}")

            with self.assertRaisesRegex(RuntimeError, "stop"):
                DecryptFile(
                    Source,
                    Output,
                    Key=TestKey,
                    Iv=TestIv,
                    Overwrite=True,
                    Progress=FailProgress,
                )
            self.assertEqual(Output.read_bytes(), b"keep me")
            self.assertEqual(list(Root.glob("*.part")), [])

    def test_ExfatTreeAndExtraction(self) -> None:
        Image = MakeExfatImage()
        Volume = ExfatVolume(io.BytesIO(Image))
        Tree = Volume.BuildTree()
        self.assertEqual([Node.Entry.Name for Node in Tree], ["Hello.txt", "Folder"])
        self.assertEqual(Tree[1].Children[0].Entry.Name, "World.bin")

        with tempfile.TemporaryDirectory() as Directory:
            Output = Path(Directory) / "Extracted"
            Result = ExtractExfat(io.BytesIO(Image), Output)
            self.assertEqual(Result, Output.resolve())
            self.assertEqual(
                (Output / "Hello.txt").read_bytes(),
                b"A" * (PageSize // 8) + b"B" * 10,
            )
            self.assertEqual((Output / "Folder" / "World.bin").read_bytes(), b"XYZ")

    def test_VhdPartitionOffsetAndOffsetStream(self) -> None:
        Disk = bytearray(4096)
        Disk[510:512] = b"\x55\xAA"
        Disk[0x1BE + 4] = 0x07
        struct.pack_into("<I", Disk, 0x1BE + 8, 2)
        Disk[1024:1028] = b"\xEB\x52\x90\x4E"
        Stream = io.BytesIO(Disk)
        self.assertEqual(FindNtfsOffset(Stream, len(Disk)), 1024)
        Partition = OffsetStream(Stream, 1024, 1024)
        self.assertEqual(Partition.read(4), b"\xEB\x52\x90\x4E")
        Partition.seek(-4, io.SEEK_END)
        self.assertEqual(Partition.tell(), 1020)


if __name__ == "__main__":
    unittest.main()
