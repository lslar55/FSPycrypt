# FSDecrypt

FSDecrypt 是 [beerpsi/fsdecrypt](https://gitea.tendokyu.moe/beerpsi/fsdecrypt) 的 **Python 库**实现，用于读取、解密和提取 SEGA fscrypt 容器。

本仓库只提供 Python 库：安装后用 `from FSDecrypt import ...` 调用。这里没有命令行入口——需要命令行或拖放使用时请用上游 Rust 版。

| 项目 | 值 |
| --- | --- |
| 库版本 | `0.3.0` |
| 对齐的上游 Rust 版本 | **v0.1.9** |
| Python | 3.10+ |
| 许可证 | 0BSD |

## 特性

- 自动识别 `.pack` / `.app` / `.opt` 容器与裸 `.ntfs` / `.exfat` 镜像
- OPTION 容器：解密后直接解析 exFAT，不把中间镜像写到磁盘
- APP / OS 容器：解密 → 外层 NTFS → `internal_<n>.vhd` → 内层 NTFS，内层 VHD 全程流式读取，同样不落地
- **差分更新**：内层 VHD 是差分盘时，自动按 VHD GUID 向上寻找基础容器，并把多层差分合并成一条链后提取
- 内置游戏密钥库，也支持外部 `{GAME_ID}.bin` 密钥文件
- 提取先写入同卷临时目录，成功后原子替换，失败不会留下半成品
- 保留 exFAT / NTFS 的修改时间与访问时间

## 安装

在项目目录执行：

```powershell
python -m pip install .
```

## 快速开始

```python
from FSDecrypt import ExtractFiles

OutputDirectory = ExtractFiles("O:/ACA_0111.01.01_20250703090316_0.pack")
print(OutputDirectory)
```

`ExtractFiles` 会自动判断输入类型并走完整个流程。不指定输出目录时，会在输入文件旁创建同名目录。

指定输出目录并覆盖已有结果：

```python
OutputDirectory = ExtractFiles(
    "O:/GAME.opt",
    "O:/Output/GAME",
    Overwrite=True,
)
```

## 差分更新容器

游戏补丁通常以新的 `.app` 形式发布，它内部是一个差分 VHD。把补丁文件直接交给
`ExtractFiles` 即可，基础容器会在同一目录下自动找出来并合并：

```python
OutputDirectory = ExtractFiles("O:/GAME_1.01.00_..._1_1.00.00.app")
```

基础容器不在同一目录时，用 `Base=` 显式指定（可以是基础 `.app`/`.pack`，也可以是
已经抽出来的 `internal_<n>.vhd` 或 `.ntfs` 镜像）：

```python
OutputDirectory = ExtractFiles(
    "O:/Patch/GAME_1.01.00_..._1_1.00.00.app",
    Base="O:/Base/GAME_1.00.00_..._0.app",
)
```

多层补丁会沿着「差分盘的 Parent Unique ID = 父盘 Unique ID」逐级向上找全。找不到
基础盘时抛出 `MissingBaseError`。

## Python API

### 只生成解密镜像

```python
from FSDecrypt import DecryptFile

ImagePath = DecryptFile("O:/GAME.pack", Overwrite=True)
print(ImagePath)
```

输出文件名根据容器 BootID 自动生成，扩展名为 `.ntfs` 或 `.exfat`。

### 查看容器信息

```python
from FSDecrypt import InspectContainer

Information = InspectContainer("O:/GAME.pack")
print(Information.ContainerType)
print(Information.GameId)
print(Information.OptionId)
print(Information.OutputFilename)
print(Information.PlaintextSize)
```

`InspectContainer` 只读取并解密 96 字节的 BootID，不读取数据区。

### 随机读取解密数据

```python
from FSDecrypt import OpenContainer

with OpenContainer("O:/GAME.pack") as Stream:
    Stream.seek(4096)
    Data = Stream.read(512)
```

`OpenContainer` 返回的 `FSDecryptReader` 是 `io.BufferedIOBase` 子类，支持
`read` / `seek` / `tell`，且按需解密，可用于 `dissect.ntfs` 之类的库。

### 直接提取镜像

```python
from FSDecrypt import ExtractExfat, ExtractNtfs

ExtractExfat("O:/GAME.exfat", "O:/Output/Exfat")
ExtractNtfs("O:/GAME.ntfs", "O:/Output/Ntfs")
```

`ExtractNtfs` 遇到镜像里的 `internal_<n>.vhd` 时会继续进入内层 NTFS，差分盘同样会合并。

### 提取进度

`ExtractFiles`、`ExtractExfat`、`ExtractNtfs`、`DecryptFile` 都接受
`Progress=回调`，回调签名为 `(已完成字节数, 总字节数)`：

```python
from FSDecrypt import ExtractFiles

ExtractFiles(
    "O:/GAME.opt",
    Progress=lambda Done, Total: print(f"{Done}/{Total}"),
)
```

### 直接使用 VHD 层

```python
from FSDecrypt import OpenChainedVhdNtfs, OpenVhdNtfs

# 单个固定盘 / 动态盘
Stream = OpenVhdNtfs(Open("O:/internal_0.vhd", "rb"))

# 基础盘 + 差分层（顺序为基础盘在前，最上层差分在最后）
Stream = OpenChainedVhdNtfs([BaseStream, DeltaStream])
```

返回的流已经把 `internal_<n>.vhd` 内部的 NTFS 分区定位好，偏移 0 就是 NTFS 引导扇区。

## 外部密钥

未知游戏可以使用 `{GAME_ID}.bin` 密钥文件。密钥文件可以放在输入容器旁边、当前工作
目录，或通过 `KeyDirectory` 指定：

```python
ExtractFiles("O:/GAME.pack", KeyDirectory="O:/Keys")
```

| 文件大小 | 内容 |
| --- | --- |
| 16 字节 | AES key，IV 自动推导 |
| 32 字节 | AES key + 16 字节 IV |

也可以在 API 中直接传入 `Key` 和 `Iv`。

## 支持的流程

```text
.opt
→ fscrypt 解密 → exFAT → 递归提取文件

.pack / .app
→ fscrypt 解密 → 外层 NTFS → internal_<n>.vhd
→ [差分盘则向上合并基础盘] → 内层 NTFS → 递归提取文件

.ntfs
→ 检测 internal_<n>.vhd → 合并差分链 → 提取内层 NTFS
→ 没有内层 VHD 时直接提取当前 NTFS 文件树

.exfat
→ 递归提取文件
```

## 外部密钥与短名说明

- NTFS 提取会跳过 8.3 短名（`NAME~1.EXT`）索引项，避免同一文件被提取两次
- exFAT 解析按规范接受 `DataLength = 0` 的空文件（上游 v0.1.9 修的是 Rust 侧
  `exfat-fs` 库的问题，本库自己解析 exFAT，不存在该问题）
- 文件名会做 Windows 兼容处理：非法字符替换为 `_`，重名自动加序号，
  `.` / `..` 与设备名（`CON`、`LPT1` 等）会被改写

## 目录结构

```text
FSDecrypt/
  Api.py       公开 API：ExtractFiles / ExtractNtfs / ExtractExfat / DecryptFile
  Reader.py    fscrypt 容器读取器，按页解密并暴露为可 seek 的流
  Crypto.py    AES-CBC 解密、页 IV 推导
  Model.py     BootID 解析与字段模型
  Keys.py      内置密钥与外部密钥文件
  Vhd.py       VHD 页脚/动态头/BAT/位图解析，单盘与差分链读取
  Exfat.py     exFAT 文件系统解析与提取
  Ntfs.py      NTFS 文件系统树与提取（基于 dissect.ntfs）
  Extract.py   共用的提取引擎（原子替换、重名处理、递归）
  Util.py      路径、命名、时间戳等公共工具
  Errors.py    异常类型
tests/         纯逻辑回归测试（合成数据，不含任何厂商容器）
```

## 开发

```powershell
python -m pip install -e .
python -m unittest discover -s tests -v   # 回归测试
python -m ruff check FSDecrypt tests      # 静态检查
```

测试不依赖任何真实容器，全部使用合成数据。真实容器体积大且含厂商版权内容，
请勿提交进仓库。

## 许可证

核心移植代码沿用上游项目的 0BSD 许可证，详见 [LICENSE](LICENSE)。
