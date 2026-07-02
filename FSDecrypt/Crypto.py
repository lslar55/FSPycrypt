from Crypto.Cipher import AES


PageSize = 4096


def CalculatePageIv(FileOffset: int, FileIv: bytes) -> bytes:
    if FileOffset < 0:
        raise ValueError("file_offset cannot be negative")
    if len(FileIv) != AES.block_size:
        raise ValueError("file IV must be 16 bytes")
    return bytes(
        Byte ^ ((FileOffset >> (8 * (Index % 8))) & 0xFF)
        for Index, Byte in enumerate(FileIv)
    )


def CalculateFileIv(Key: bytes, ExpectedHeader: bytes, FirstPage: bytes) -> bytes:
    if len(Key) != AES.block_size:
        raise ValueError("AES-128 key must be 16 bytes")
    if len(ExpectedHeader) != AES.block_size:
        raise ValueError("expected header must be 16 bytes")
    if len(FirstPage) < AES.block_size:
        raise ValueError("encrypted payload is too short to derive the IV")
    return AES.new(Key, AES.MODE_CBC, ExpectedHeader).decrypt(
        FirstPage[: AES.block_size]
    )


def DecryptCbc(Data: bytes, Key: bytes, Iv: bytes) -> bytes:
    if len(Data) % AES.block_size:
        raise ValueError("encrypted data length must be a multiple of 16")
    return AES.new(Key, AES.MODE_CBC, Iv).decrypt(Data)
