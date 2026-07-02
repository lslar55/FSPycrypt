# FSDecrypt

FSDecrypt 是 [beerpsi/fsdecrypt](https://gitea.tendokyu.moe/beerpsi/fsdecrypt) 的 Python 库实现，用于读取、解密和提取 SEGA fscrypt 容器。

支持作为 Python 库导入，也支持直接在 CMD 或 PowerShell 中运行。

## 安装

在项目目录执行：

```powershell
python -m pip install .
```

安装完成后可以在其他 Python 项目中导入：

```python
from Decrypt import DecryptFile, ExtractFiles, InspectContainer, OpenContainer
```

## Python 库用法

### 自动提取文件

`ExtractFiles` 会自动识别加密容器、NTFS 镜像或 exFAT 镜像，并执行对应的完整提取流程。

```python
from pathlib import Path

from Decrypt import ExtractFiles

InputFile = Path("O:/ACA_0111.01.01_20250703090316_0.pack")
OutputDirectory = ExtractFiles(InputFile)
print(OutputDirectory)
```

不指定输出目录时，会在输入文件旁创建同名目录。

指定输出目录：

```python
from Decrypt import ExtractFiles

OutputDirectory = ExtractFiles(
    "O:/GAME.opt",
    "O:/Output/GAME",
    Overwrite=True,
)
```

### 只生成解密镜像

```python
from Decrypt import DecryptFile

ImagePath = DecryptFile(
    "O:/GAME.pack",
    Overwrite=True,
)
print(ImagePath)
```

输出文件名根据容器 BootID 自动生成，扩展名为 `.ntfs` 或 `.exfat`。

### 查看容器信息

```python
from Decrypt import InspectContainer

Information = InspectContainer("O:/GAME.pack")
print(Information.ContainerType)
print(Information.GameId)
print(Information.OptionId)
print(Information.OutputFilename)
print(Information.PlaintextSize)
```

### 随机读取解密数据

```python
from Decrypt import OpenContainer

with OpenContainer("O:/GAME.pack") as Stream:
    Stream.seek(4096)
    Data = Stream.read(512)
```

### 直接提取原始镜像

```python
from Decrypt import ExtractExfat, ExtractNtfs

ExtractExfat("O:/GAME.exfat", "O:/Output/Exfat")
ExtractNtfs("O:/GAME.ntfs", "O:/Output/Ntfs")
```

## CMD 与 PowerShell 用法

基本格式：

```powershell
python fsdecrypt.py <inputFile> -o <outputPath>
```

`-o` 可以省略：

```powershell
python fsdecrypt.py "O:\GAME.opt"
```

指定输出目录：

```powershell
python fsdecrypt.py "O:\GAME.pack" -o "O:\Output\GAME"
```

提取 NTFS 镜像：

```powershell
python fsdecrypt.py "O:\GAME.ntfs" -o "O:\Output\NTFS"
```

覆盖已有输出：

```powershell
python fsdecrypt.py "O:\GAME.pack" -o "O:\Output\GAME" -f
```

只生成 `.ntfs` 或 `.exfat` 镜像，不提取其中的文件：

```powershell
python fsdecrypt.py "O:\GAME.pack" --no-extract
```

指定外部密钥目录：

```powershell
python fsdecrypt.py "O:\GAME.app" --key-directory "O:\Keys"
```

安装项目后，也可以直接使用命令：

```powershell
fsdecrypt "O:\GAME.opt" -o "O:\Output\GAME"
```

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `inputFile` | 输入的 `.pack`、`.app`、`.opt`、`.ntfs` 或 `.exfat` 文件 |
| `-o`, `--output` | 输出文件或目录；省略时自动生成 |
| `-f`, `--overwrite` | 覆盖已有输出 |
| `--no-extract` | 只生成解密镜像 |
| `--key-directory` | 指定外部密钥文件目录 |

## 支持的处理流程

### OPTION 容器

```text
.opt
→ fscrypt 解密
→ exFAT
→ 递归提取文件
```

### APP 与 OS 容器

```text
.pack/.app
→ fscrypt 解密
→ 外层 NTFS
→ internal_*.vhd
→ 固定或动态 VHD
→ 内层 NTFS
→ 递归提取文件
```

### 原始镜像

```text
.exfat
→ 递归提取文件

.ntfs
→ 检测 internal_*.vhd
→ 提取内层 NTFS 或当前 NTFS 文件树
```

差分 VHD 不能单独使用，需要同时提供对应的基础 VHD。

## 外部密钥

未知游戏可以使用 `{GAME_ID}.bin` 密钥文件。

密钥文件可以放在输入容器旁边、当前工作目录，或通过 `KeyDirectory`、`--key-directory` 指定。

| 文件大小 | 内容 |
| --- | --- |
| 16 字节 | AES key，IV 自动推导 |
| 32 字节 | AES key + 16 字节 IV |

也可以在 Python API 中直接传入 `Key` 和 `Iv`。

## 许可证

核心移植代码沿用上游项目的 0BSD 许可证，详见 [LICENSE](LICENSE)。
