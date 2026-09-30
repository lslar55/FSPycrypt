"""Regression tests for the parts that are pure logic: no real container needed.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

import io
import struct
import unittest

from Crypto.Cipher import AES

from FSDecrypt.Crypto import CalculateFileIv, CalculatePageIv
from FSDecrypt.Model import BootId, ContainerType
from FSDecrypt.Util import SafeName, UniqueName
from FSDecrypt.Vhd import (
    ChainedVhdDisk,
    FindNtfsOffset,
    OpenChainedVhdNtfs,
    ReadVhdGuidInfo,
    VhdDisk,
)


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------


class CryptoTest(unittest.TestCase):
    Key = bytes(range(16))
    Header = b"\xEB\x52\x90\x4E" + bytes(12)

    def test_page_iv_is_the_file_iv_for_the_first_page(self):
        FileIv = bytes(range(16))
        self.assertEqual(CalculatePageIv(0, FileIv), FileIv)

    def test_page_iv_xors_the_offset_into_every_eight_bytes(self):
        Iv = bytes(16)
        # 0x0807060504030201 little-endian in the low 8 bytes.
        self.assertEqual(
            CalculatePageIv(0x0807060504030201, Iv),
            bytes([1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 6, 7, 8]),
        )

    def test_file_iv_round_trips(self):
        Expected = bytes(range(16, 32))
        Encrypted = AES.new(self.Key, AES.MODE_CBC, self.Header).encrypt(Expected)
        self.assertEqual(CalculateFileIv(self.Key, self.Header, Encrypted), Expected)


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


class NamingTest(unittest.TestCase):
    def test_invalid_characters_are_replaced(self):
        self.assertEqual(SafeName('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")

    def test_dots_and_dot_dots_cannot_escape_the_output_directory(self):
        self.assertEqual(SafeName("."), "_")
        self.assertEqual(SafeName(".."), "_")

    def test_reserved_device_names_are_escaped(self):
        self.assertEqual(SafeName("CON.txt"), "_CON.txt")
        self.assertEqual(SafeName("lpt1"), "_lpt1")

    def test_duplicate_names_are_numbered(self):
        Used: set[str] = set()
        self.assertEqual(UniqueName("Game.exe", Used), "Game.exe")
        self.assertEqual(UniqueName("game.EXE", Used), "game (2).EXE")
        self.assertEqual(UniqueName("game.EXE", Used), "game (3).EXE")


# ---------------------------------------------------------------------------
# BootId
# ---------------------------------------------------------------------------


def BuildBootId(ContainerTypeValue, SequenceNumber, GameId, OptionOrVersion):
    Data = bytearray(96)
    struct.pack_into("<I", Data, 0, 0xDEADBEEF)
    struct.pack_into("<I", Data, 4, 96)
    Data[8:12] = b"BOOT"
    Data[13] = ContainerTypeValue
    Data[14] = SequenceNumber
    Data[15] = 1
    Data[16:20] = GameId
    struct.pack_into("<H6B", Data, 20, 2025, 4, 1, 7, 45, 3, 0)
    Data[28:32] = OptionOrVersion
    struct.pack_into("<Q", Data, 32, 252)
    struct.pack_into("<Q", Data, 40, 262144)
    struct.pack_into("<Q", Data, 48, 8)
    Data[64:67] = b"ACA"
    Data[67] = 1
    struct.pack_into("<H6B", Data, 68, 2025, 1, 2, 3, 4, 5, 0)
    struct.pack_into("<BBH", Data, 76, 0, 0, 0)
    struct.pack_into("<BBH", Data, 80, 1, 1, 111)
    return bytes(Data)


class BootIdTest(unittest.TestCase):
    def test_option_container_fields(self):
        Info = BootId.FromPlaintext(BuildBootId(2, 0, b"SDGA", b"A031"))
        self.assertIs(Info.ContainerType, ContainerType.Option)
        self.assertEqual(Info.GameId, "SDGA")
        self.assertEqual(Info.OptionId, "A031")
        self.assertEqual(str(Info.TargetTimestamp), "20250401074503")
        self.assertEqual(Info.BlockCount, 252)
        self.assertEqual(Info.HeaderBlockCount, 8)
        self.assertEqual(Info.PlaintextSize, 262144 * (252 - 8))
        self.assertEqual(Info.DataOffset, 262144 * 8)
        self.assertTrue(Info.OutputFilename.endswith(".exfat"))

    def test_os_container_output_name(self):
        Info = BootId.FromPlaintext(BuildBootId(0, 0, b"----", b"\x01\x01o\x00"))
        self.assertIs(Info.ContainerType, ContainerType.Os)
        self.assertEqual(Info.OsId, "ACA")
        self.assertEqual(str(Info.OsVersion), "111.01.01")
        self.assertEqual(
            Info.OutputFilename,
            "ACA_111.01.01_20250401074503_0.ntfs",
        )

    def test_wrong_size_is_rejected(self):
        with self.assertRaises(Exception):
            BootId.FromPlaintext(bytes(95))


# ---------------------------------------------------------------------------
# VHD: synthetic fixed-size blocks, no proprietary data involved
# ---------------------------------------------------------------------------


Sector = 512
BlockSize = 2 * 1024 * 1024
BlockSectors = BlockSize // Sector
HeaderOffset = Sector
BatOffset = HeaderOffset + 1024
FirstBlockSector = 4
NtfsMagic = b"\xEB\x52\x90\x4E"


def _Footer(DiskType, DataOffset, CurrentSize, UniqueId):
    Data = bytearray(Sector)
    Data[0:8] = b"conectix"
    struct.pack_into(">I", Data, 8, 0x00000002)
    struct.pack_into(">I", Data, 12, 0x00010000)
    struct.pack_into(">Q", Data, 16, DataOffset)
    struct.pack_into(">Q", Data, 40, CurrentSize)
    struct.pack_into(">Q", Data, 48, CurrentSize)
    struct.pack_into(">I", Data, 60, DiskType)
    Data[68:84] = UniqueId
    return bytes(Data)


def _DynamicHeader(BatOffsetValue, MaxEntries, ParentId):
    Data = bytearray(1024)
    Data[0:8] = b"cxsparse"
    struct.pack_into(">Q", Data, 8, 0xFFFFFFFFFFFFFFFF)
    struct.pack_into(">Q", Data, 16, BatOffsetValue)
    struct.pack_into(">I", Data, 24, 0x00010000)
    struct.pack_into(">I", Data, 28, MaxEntries)
    struct.pack_into(">I", Data, 32, BlockSize)
    Data[40:56] = ParentId
    return bytes(Data)


def BuildVhd(DiskType, UniqueId, ParentId, Owned, BlockCount, Fill):
    """Build a dynamic/differencing VHD.

    ``Owned`` maps a block index to the sectors that block owns; a block that is
    absent is left unallocated in the BAT.
    """

    Allocated = sorted(Owned)
    BlockStartSectors = {
        Index: FirstBlockSector + Position * (1 + BlockSectors)
        for Position, Index in enumerate(Allocated)
    }

    Data = bytearray(bytes(HeaderOffset))
    Data += _DynamicHeader(BatOffset, BlockCount, ParentId)
    for Index in range(BlockCount):
        Data += struct.pack(">I", BlockStartSectors.get(Index, 0xFFFFFFFF))
    First = (
        BlockStartSectors[Allocated[0]] if Allocated else FirstBlockSector
    )
    Data += bytes(First * Sector - len(Data))

    for Index in Allocated:
        Bitmap = bytearray(Sector)
        for SectorIndex in Owned[Index]:
            Bitmap[SectorIndex // 8] |= 1 << (7 - SectorIndex % 8)
        Block = bytearray(bytes([Fill + Index]) * BlockSize)
        if Index == 0:
            Block[0:4] = NtfsMagic
        Data += bytes(Bitmap)
        Data += Block

    Data += _Footer(DiskType, HeaderOffset, BlockCount * BlockSize, UniqueId)
    return io.BytesIO(bytes(Data))


BaseId = bytes(range(16))
Delta1Id = bytes(range(16, 32))
Delta2Id = bytes(range(32, 48))


def BuildThreeLayerChain():
    Base = BuildVhd(
        3,
        BaseId,
        bytes(16),
        {0: set(range(BlockSectors)), 1: set(range(BlockSectors))},
        2,
        0x10,
    )
    Delta1 = BuildVhd(
        4,
        Delta1Id,
        BaseId,
        {0: set(range(BlockSectors))},
        2,
        0x20,
    )
    Delta2 = BuildVhd(
        4,
        Delta2Id,
        Delta1Id,
        {0: {3, 6}, 1: set(range(BlockSectors))},
        2,
        0x30,
    )
    return Base, Delta1, Delta2


def FirstByte(Disk, SectorIndex, Count=1):
    Data = Disk.ReadAt(SectorIndex * Sector, Count * Sector)
    return Data[0], len({Data[Index] for Index in range(len(Data))}) == 1


class VhdTest(unittest.TestCase):
    def test_guid_info_links_a_delta_to_its_parent(self):
        Base, Delta1, Delta2 = BuildThreeLayerChain()
        self.assertEqual(ReadVhdGuidInfo(Base).OwnId, BaseId)
        self.assertIsNone(ReadVhdGuidInfo(Base).ParentId)
        self.assertEqual(ReadVhdGuidInfo(Delta1).ParentId, BaseId)
        self.assertEqual(ReadVhdGuidInfo(Delta2).ParentId, Delta1Id)

    def test_single_dynamic_disk_reads_its_allocated_block(self):
        Base, _, _ = BuildThreeLayerChain()
        Disk = VhdDisk(Base)
        self.assertEqual(Disk.Size, 2 * BlockSize)
        self.assertEqual(Disk.ReadAt(0, 4), NtfsMagic)
        self.assertEqual(FirstByte(Disk, 1), (0x10, True))
        self.assertEqual(FirstByte(Disk, BlockSectors + 1), (0x11, True))

    def test_the_top_most_layer_that_owns_a_sector_wins(self):
        Chain = ChainedVhdDisk(list(BuildThreeLayerChain()))
        self.assertEqual(FindNtfsOffset(Chain), 0)
        self.assertEqual(FirstByte(Chain, 3), (0x30, True))
        self.assertEqual(FirstByte(Chain, 6), (0x30, True))
        self.assertEqual(FirstByte(Chain, 5), (0x20, True))
        self.assertEqual(FirstByte(Chain, BlockSectors + 7), (0x31, True))

    def test_a_mixed_bitmap_is_resolved_sector_by_sector(self):
        # Delta 2 owns only sectors 3 and 6 of block 0, so a single spanning
        # read must still fall through to delta 1 for the sectors in between.
        Chain = ChainedVhdDisk(list(BuildThreeLayerChain()))
        Span = Chain.ReadAt(0, 8 * Sector)
        self.assertEqual(Span[0:4], NtfsMagic)
        self.assertEqual(Span[4:Sector], bytes([0x20]) * (Sector - 4))
        Expected = {1: 0x20, 2: 0x20, 3: 0x30, 4: 0x20, 5: 0x20, 6: 0x30, 7: 0x20}
        for Index, Byte in Expected.items():
            self.assertEqual(
                Span[Index * Sector : (Index + 1) * Sector],
                bytes([Byte]) * Sector,
            )

    def test_reads_clamp_at_the_virtual_end_of_the_disk(self):
        Chain = ChainedVhdDisk(list(BuildThreeLayerChain()))
        self.assertEqual(
            Chain.ReadAt(BlockSize, 3 * Sector),
            bytes([0x31]) * 3 * Sector,
        )
        self.assertEqual(len(Chain.ReadAt(2 * BlockSize - Sector, 8 * Sector)), Sector)

    def test_partition_stream_exposes_only_the_ntfs_partition(self):
        Base, Delta1, Delta2 = BuildThreeLayerChain()
        Stream = OpenChainedVhdNtfs([Base, Delta1, Delta2])
        Stream.seek(3 * Sector)
        self.assertEqual(Stream.read(Sector), bytes([0x30]) * Sector)
        self.assertEqual(Stream.seek(0, io.SEEK_END), 2 * BlockSize)
        self.assertEqual(Stream.read(), b"")

    def test_layers_must_agree_about_the_disk_size(self):
        Base, Delta1, _ = BuildThreeLayerChain()
        Short = BuildVhd(4, Delta2Id, Delta1Id, {0: {3}}, 1, 0x30)
        with self.assertRaises(Exception):
            ChainedVhdDisk([Base, Delta1, Short])


if __name__ == "__main__":
    unittest.main()
