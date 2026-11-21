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
- **可反向打包**：把明文 `.ntfs` / `.exfat` 镜像重新加密回 `.pack` / `.app` / `.opt` 容器
- **可从文件夹打包**：`.opt` 由本库自建 exFAT，`.pack` / `.app` 经 WSL 的 ntfs-3g 构建
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

## 重新打包（加密）

`EncryptFile` 是 `DecryptFile` 的逆操作：把明文 `.ntfs` / `.exfat` 镜像重新加密回容器。

```python
from FSDecrypt import DecryptFile, EncryptFile

Image = DecryptFile("O:/GAME.pack")          # 容器 → 明文镜像
# ... 在这里修改镜像（例如用 NTFS/exFAT 写入工具替换文件）...
Container = EncryptFile(
    Image,
    "O:/GAME.repacked.pack",
    Template="O:/GAME.pack",                 # 必须提供
)
```

**为什么需要 `Template`**：容器的头部并不只有 96 字节的 BootID。实测结构是
「10240 字节 BootID（AES-CBC 加密）+ 约 2 MB 不透明头部 + 数据区」。不透明头部以及
BootID 里几个未知字段无法从零构造，所以打包时从同游戏、同类型的原容器逐字节沿用，
只重算 `BlockCount` 与 BootID 的 CRC32。

**约束**：

- 镜像必须以该类型应有的引导头开始（exFAT 或 NTFS 的 16 字节签名）。读取端要靠它
  推导文件 IV，不匹配会直接报 `InvalidContainerError`。
- 镜像会被零填充到 262144 字节（256 KiB）的整数倍，因为容器的明文长度由
  `BlockCount` 推导，不单独存储。
- `Template` 的容器类型必须与镜像一致：`.opt` 用 exFAT，`.pack` / `.app` 用 NTFS。
- 每次只能打包「容器直接对应的那一层明文」。`.pack` 的明文是外层 NTFS（里面有
  `internal_<n>.vhd`）；要改最内层文件，需要自己在 NTFS 层重建内层 VHD 与外层
  NTFS，本库不代劳。

**能验证什么、不能验证什么**：本库能证明「打包结果能被自己完整解回原镜像」，也能
证明「拿原镜像打包会得到与原容器逐字节相同的文件」。但实机/游戏端是否接受修改过内容
的容器，取决于厂商是否还有额外校验，本库无法验证。

## 从文件夹打包

`PackFolder` 可以直接把文件夹变成容器，`.opt` 与 `.pack` / `.app` 都支持：

```python
from FSDecrypt import PackFolder

Container = PackFolder(
    "O:/Mod/Option",                        # 文件夹
    "O:/GAME.new.opt",
    Template="O:/GAME.opt",                 # 原容器，必须提供
)
```

本库自己写 exFAT（`.opt` 走这条路，不需要任何外部工具），流程是「文件夹 → exFAT 镜像
→ 用 `EncryptFile` 加密成 `.opt`」，中间镜像写在目标目录旁边并在结束后删除。细节：

- 生成的 exFAT 采用与真实镜像一致的布局：4096 字节扇区、4 KiB 簇、24 个引导扇区、
  分配位图/upcase 表/根目录依次排布，目录项校验和与名字哈希都按规范计算。
- upcase 表直接取自 `Template` 容器自带的 exFAT 镜像，不内嵌任何厂商数据。
- 镜像总大小默认对齐到 `Template` 的明文大小，因此打包结果与原容器**同尺寸**；文件夹
  超过模板容量时会自动增大 `BlockCount`。
- `Progress` 回调报告的是「写镜像」阶段（文件内容复制），这是主要耗时部分；对
  `.pack` / `.app` 只在阶段边界回调。

**`.pack` / `.app`** 的明文是外层 NTFS，里面套 `internal_<n>.vhd`，再里面才是内层
NTFS。本库不自带 NTFS 格式化器，而是把这一步交给 Linux 的 `ntfs-3g` 工具链（在 WSL 里
跑，**不需要 Windows 管理员权限**）：

```python
from FSDecrypt import InstallNtfsTools, PackFolder

InstallNtfsTools()      # 一次性：往 WSL 私有目录下载解包 ntfs-3g（约 1 MB）

PackFolder("O:/Mod/Game", "O:/GAME.new.pack", Template="O:/GAME.pack")
```

流程是「文件夹 → 内层 NTFS（mkntfs + 挂载写入）→ 固定 VHD（MBR 分区 @1 MiB + 页脚）
→ 外层 NTFS（4096 字节扇区）→ `EncryptFile` 加密」。要求：

- 装有 WSL 2 和 Debian/Ubuntu 发行版，且 `wsl -u root` 可直接进入（默认可以）。
- `InstallNtfsTools()` 只下载并解包 `.deb` 到 `~/.fsdecrypt/ntfs-tools`，不安装系统包、
  不需要 sudo 或 Windows 管理员；也可用 `FSDECRYPT_NTFS_TOOLS` 环境变量指向现成前缀。
- 中间镜像会临时占用「内容约 3 倍」的空间，结束后自动删除。

两条路径的可验证范围相同：本库能证明「打包结果被自己完整解回原文件夹」；此外 exFAT
可用 `fsck.exfat`、NTFS 可用 `ntfsfix -n` / `ntfsls` 独立校验。**实机/游戏端是否接受
修改过内容的容器无法验证**，取决于厂商是否还有额外签名校验。

如果连 WSL 也不想用：先用 `DecryptFile` 取出明文镜像，用外部工具改完后交给
`EncryptFile`，走纯镜像路径即可。

## Python API

### 只生成解密镜像

```python
from FSDecrypt import DecryptFile

ImagePath = DecryptFile("O:/GAME.pack", Overwrite=True)
print(ImagePath)
```

输出文件名根据容器 BootID 自动生成，扩展名为 `.ntfs` 或 `.exfat`。

### 重新加密镜像

```python
from FSDecrypt import EncryptFile

ContainerPath = EncryptFile(
    "O:/GAME.ntfs",
    "O:/GAME.pack",
    Template="O:/GAME_original.pack",
    Overwrite=True,
)
```

省略 `Output` 时按容器类型自动命名（`.opt` / `.app` / `.pack`）。也支持
`Key` / `Iv` / `KeyDirectory` / `Progress` 参数，含义与解密侧一致。

### 从文件夹打包容器

```python
from FSDecrypt import PackFolder

ContainerPath = PackFolder(
    "O:/Mod/Option",
    "O:/GAME.new.opt",
    Template="O:/GAME.opt",
    Overwrite=True,
)
```

只支持 `.opt`（exFAT 载荷），`.pack` / `.app` 会抛 `UnsupportedContainerError`。

需要自己控制 exFAT 生成过程时，可以只用底层函数：

```python
from FSDecrypt import BuildExfatImage, ReadUpcaseTable

with open("O:/GAME.opt", "rb") as Stream:      # 也可以用 OpenContainer(...)
    Table = ReadUpcaseTable(Stream)
ImagePath = BuildExfatImage("O:/Mod/Option", "O:/Mod.img", UpcaseTable=Table)
```

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
  Api.py       公开 API：ExtractFiles / ExtractNtfs / ExtractExfat / DecryptFile / PackFolder
  Encrypt.py   反向打包：明文镜像 + 模板容器 → 加密容器
  Reader.py    fscrypt 容器读取器，按页解密并暴露为可 seek 的流
  Crypto.py    AES-CBC 解密、页 IV 推导
  Model.py     BootID 解析与字段模型
  Keys.py      内置密钥、外部密钥文件与密钥解析
  Vhd.py       VHD 页脚/动态头/BAT/位图解析，单盘与差分链读取
  Exfat.py     exFAT 文件系统解析与提取
  ExfatBuild.py 从文件夹构建 exFAT 镜像（含 upcase 表读取）
  NtfsBuild.py 经 WSL 的 ntfs-3g 构建 NTFS 镜像 / VHD / 外层镜像
  Ntfs.py      NTFS 文件系统树与提取（基于 dissect.ntfs）
  Extract.py   共用的提取引擎（原子替换、重名处理、递归）
  Util.py      路径、命名、时间戳等公共工具
  Errors.py    异常类型
  _version.py  版本号单一来源
```

## 开发

```powershell
python -m pip install -e .
python -m ruff check FSDecrypt     # 静态检查
```

本仓库不包含测试套件。涉及字节布局的改动请用合成数据写一次性脚本自行验证，
真实的厂商容器体积大、含版权内容，请勿提交进仓库。

## 许可证

核心移植代码沿用上游项目的 0BSD 许可证，详见 [LICENSE](LICENSE)。
