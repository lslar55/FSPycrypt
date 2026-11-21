# AGENTS.md

面向 AI 编码代理的仓库规范。人类贡献者请优先读 [README.md](README.md)。

## 1. 项目定位（硬性约束）

- 本仓库是上游 Rust 项目 [beerpsi/fsdecrypt](https://gitea.tendokyu.moe/beerpsi/fsdecrypt) 的
  **Python 库移植**，只提供 `import FSDecrypt` 的库接口。
- **不要重新引入命令行入口**：不添加 `argparse`/`click`/`loguru`，不添加
  `[project.scripts]`，不添加 `__main__.py`、`Cli.py` 或类似脚本。历史上的
  `Decrypt.py`、`fsdecrypt.py`、`FSDecrypt/Cli.py` 已被有意删除。
- 版本号只有一个来源：`FSDecrypt/_version.py` 的 `__version__`（刻意保持零 import，
  以便 setuptools 在建包时静态读取），`pyproject.toml` 通过 `dynamic = ["version"]`
  指向它，`FSDecrypt/__init__.py` 只做 re-export。不要在 `pyproject.toml` 或
  `__init__.py` 里再写死版本。
- 破坏公开 API 前先确认：`FSDecrypt/__init__.py` 的 `__all__` 就是公开契约。

## 2. 模块职责与依赖方向

```text
Api.py      公开用例编排（ExtractFiles / ExtractNtfs / ExtractExfat / DecryptFile / PackFolder）
Encrypt.py  反向打包（EncryptFile）：明文镜像 + 模板容器 → 加密容器
Reader.py   fscrypt 容器读取器（按页解密 + seek）
Crypto.py   AES-CBC 与页 IV 推导
Model.py    BootID 解析
Keys.py     内置密钥、外部密钥文件与密钥解析（ResolveGameKeys）
Vhd.py      VHD 格式：页脚 / 动态头 / BAT / 扇区位图 / 单盘与差分链
Exfat.py    exFAT 解析与提取
ExfatBuild.py 从文件夹构建 exFAT 镜像（BuildExfatImage / ReadUpcaseTable）
NtfsBuild.py 经 WSL 的 ntfs-3g 构建 NTFS 镜像 / 固定 VHD / 外层镜像
Ntfs.py     NTFS 树与提取（基于 dissect.ntfs）
Extract.py  共用提取引擎（原子替换、重名处理、递归）
Util.py     路径、命名、时间戳等通用工具
```

依赖方向必须单向：`Api → {Reader, Exfat, ExfatBuild, NtfsBuild, Encrypt, Ntfs, Vhd} → {Extract, Util, Model, Crypto, Keys, Errors}`，
`Encrypt → {Keys, Model, Crypto, Util, Errors}`，`ExfatBuild → {Util, Errors}`（不依赖 Reader），
`NtfsBuild → {Util, Errors}`（不依赖 Reader，也不认识容器格式）。

- `Exfat.py` 与 `Ntfs.py` **不得**互相 import。两者共用的东西放 `Util.py` 或 `Extract.py`。
  历史上 `Ntfs.py` 反向 import `Exfat._UniqueName` / `Exfat._RemovePath`，已修正，别改回去。
- `Vhd.py` 只管 VHD 格式，不认识 fscrypt 容器、密钥或 NTFS。容器层面的「找基础盘」
  逻辑属于 `Api.py`。
- `dissect.hypervisor` 已移除，VHD 由本仓库自己实现（原因是差分链需要按扇区位图
  逐层合并，上游库不支持）。**不要**为了读 VHD 再把它加回依赖。

## 3. 代码风格

本仓库有意采用 **PascalCase 标识符**（含局部变量、参数、模块级常量），这是既有
代码库的统一风格，新代码必须保持一致，不要混入 PEP 8 的 snake_case：

```python
def ExtractTree(OutputDirectory: PathType, Nodes: Sequence[ExtractNode], ...) -> Path:
    OutputPath = Path(OutputDirectory).resolve()
    UsedNames: set[str] = set()
```

其余约定：

- 每个模块首行 `from __future__ import annotations`，类型注解写全。
- 数据结构优先 `@dataclass(frozen=True, slots=True)`。
- 模块私有函数/属性用单下划线前缀（`_ReadFooter`、`self._Position`）。
- 行宽 ≤ 88，长表达式按现有风格折行。
- 常量与字段名用描述性英文全称，不用缩写（`HeaderBlockCount` 而非 `HdrBlkCnt`）。
- 不写 `# type: ignore`；确实需要时先在评审里说明原因。

## 4. 错误处理

- 所有面向用户的异常继承 `FSDecryptError`（见 `Errors.py`）。新增语义化异常要同时
  加进 `Errors.py` 与 `__init__.py` 的 `__all__`。
- 参数非法用 `ValueError`；输出已存在用 `FileExistsError`；格式不符用
  `Invalid*Error`；找不到依赖文件用 `MissingKeyError` / `MissingBaseError`。
- 解析外部数据时不要 `assert`，一律显式校验并抛语义化异常。
- 在「搜索候选文件」这类探索性路径里可以捕获并跳过异常，但要局限在
  `Api.py` 的候选扫描函数内，并保留 `Strict` 语义让显式传入的 `Base=` 失败可见。

## 5. 提取管线约定

- 任何文件树提取都必须走 `Extract.ExtractTree`。它负责：输出根校验 → 临时目录 →
  递归写入 → 备份旧目录 → `os.replace` 原子替换 → 清理。**不要**在各文件系统模块里
  自己实现这套流程，也不要直接 `os.utime`（用 `Util.SetFileTimes`）。
- `ExtractTree` 通过 `WriteFile` / `SetTimes` 回调解耦：各文件系统模块只负责「怎么读
  一个文件」和「怎么取时间戳」。
- 节点类型只要满足 `Extract.ExtractNode` 协议（`Name` / `IsDirectory` / `Size` /
  `Children`）即可，新增文件系统请照此实现。
- 提取过程中任何失败都必须让临时目录被清理，且**不得**破坏已存在的旧输出。

## 6. 容器格式与加密约定

以下事实已用真实容器逐字节验证过，改 `Reader.py` / `Encrypt.py` 前必须保持：

- 容器布局：`[0:Length)` = BootID（AES-CBC，`BootIdKey` / `BootIdIv`）；
  `[Length:DataOffset)` = 不透明头部，打包时从模板逐字节复制；
  `[DataOffset:end)` = 明文镜像的加密数据区。
- `Length` 实测为 10240，但它是 BootID 明文里的字段，不要写死成常量假设。
- BootID 明文的 CRC32 在偏移 0，等于 `zlib.crc32(明文[4:Length])`，不含前 4 字节自身。
- 数据区按 4096 字节分页，每页独立 AES-CBC，页 IV =
  `CalculatePageIv(页在明文中的偏移, FileIv)`，页与页之间**不**串联。
- `FileIv` 来自密钥表；没有配置 IV 时读取端从首 16 字节明文推导，因此**明文镜像的
  前 16 字节必须等于 exFAT/NTFS 引导签名**，否则打包会破坏镜像开头。
- 明文长度不单独存储，由 `BlockCount * BlockSize` 推导，所以打包必须把镜像零填充到
  块大小的整数倍，并重算 `BlockCount`。
- 不透明头部（约 2 MB）的内容未知，打包只能复制模板，**不要**试图自行生成。
- 改动上述任何一条之前，先做「原镜像打包 == 原容器」的逐字节校验。

### exFAT 构建器（`ExfatBuild.py`）

- 生成布局固定镜像真实 OPTION 镜像：4096 字节扇区、4 KiB 簇、24 个引导扇区、
  簇 2 = 分配位图、随后 upcase 表、再根目录。
- 目录项（0x85/0xC0/0xC1）的 **条目集校验和**（16 位轮转，跳过主项第 2、3 字节）和
  **名字哈希**（16 位轮转 + upcase 映射）必须正确，读取端会校验。
- upcase 表的存储形式是**游程编码**（bit15 置位 = 该数量个恒等映射，否则 = 单个映射），
  其校验和是**逐字节 32 位轮转**（不是 16 位）。
- upcase 表优先从模板容器自带的 exFAT 镜像读取（`ReadUpcaseTable`），不要内嵌任何
  厂商镜像里抠出来的数据块。
- **不要**自己手写 NTFS 格式化器（mkntfs 是几万行 C 的成熟实现，且本机没有挂载验证
  手段时无法判断产物是否可挂载）。`.pack`/`.app` 的从零构造走 `NtfsBuild.py`：
  把 `mkntfs` / `ntfs-3g` 委托给 WSL（`wsl -u root`，不需要 Windows 管理员），
  NTFS 结构正确性由参考实现保证。

### NTFS 构建器（`NtfsBuild.py`）

- 工具链是 WSL 里私有前缀下的 `mkntfs`/`ntfs-3g`（`InstallNtfsTools()` 下载解包，
  不装系统包、不动 Windows）。工具版本是**固定配对**，别改成"取最新"——曾经因为
  `sort -V` 选中旧版 `libntfs-3g883` 导致 SONAME 不匹配。
- 工具脚本通过 `wsl -u root bash <script> <action> ...` 调用；注意 WSL 会重新解析
  命令行，**不要**把带空格的环境变量拼进内联命令，写成脚本文件传参。
- 安装前缀是「默认 WSL 用户的 home」（`~/.fsdecrypt/ntfs-tools`），但挂载是以 root
  执行的，所以前缀必须是**绝对路径**，`~` 在 root 下会指向 `/root`。
- 外层 NTFS 必须用 `mkntfs -s 4096 -c 4096`：容器读取端要靠开头 16 字节（含 4096
  字节扇区标志）推导文件 IV，512 字节扇区会导致 `InvalidContainerError`。
- 内层 VHD 用**固定盘**（type 2）：MBR 分区从 1 MiB 开始、类型 0x07，页脚
  `conectix` + CHS + 校验和；`internal_<n>.vhd` 的序号必须等于模板 BootID 的
  `SequenceNumber`。
- 这些约定改动后，验证必须是「文件夹 → PackFolder(.pack) → ExtractFiles → 与原文件夹
  逐文件比对一致」。

## 7. 测试与验证

本仓库**不提交测试套件**，`tests/` 曾被有意删除，不要重新引入测试目录、测试框架
或 `pytest` / `unittest` 依赖。

```powershell
python -m ruff check FSDecrypt
```

规范：

- 需要验证时，写一次性的临时脚本，跑完即删；脚本不得提交进仓库。
- 涉及字节布局的逻辑（VHD、BootID、exFAT 目录项、IV 推导）必须用**合成字节流**
  验证：自己构造字节流并断言解析结果，不要依赖固件样本。
- 真实容器（`.pack`/`.app`/`.opt`/`.vhd`/`.ntfs`）、密钥文件、提取产物一律不得提交
  进仓库。做端到端验证时，输出目录放在仓库外或已被 `.git/info/exclude` 排除的目录，
  验证完删掉，不要 `git add` 进来。
- 改 `Vhd.py` 时，重点验证差分链：父子 GUID 匹配、多层优先级、部分扇区位图的跨扇区
  读、跨块连续读、虚拟容量截断。
- 改 `Encrypt.py` 时，最低验证标准是「用原镜像打包得到与原容器逐字节相同的文件」，
  并再覆盖：改动内容后回读一致、尺寸非块整数倍时的填充、派生 IV（只给 Key）路径。
- 改 `ExfatBuild.py` 时，验证闭环是「文件夹 → `BuildExfatImage` → 用 `ExfatVolume`
  提取回来内容一致」，以及「文件夹 → `PackFolder` → `ExtractFiles` 一致」；两条都要
  覆盖：普通文件、空文件、多级目录、文件名接近 15 字符边界的情况。
- 改 `NtfsBuild.py` 时，验证闭环是「文件夹 → `PackFolder(.pack)` → `ExtractFiles`
  → 与原文件夹逐文件哈希一致」，并用 `ntfsfix -n` / `ntfsls` 独立校验中间镜像。
  需要 WSL；Windows 管理员权限不是必需的，也不要要求用户开管理员。

## 8. Git 约定

- 提交信息用中文，类型前缀沿用现状：`chore:` / `refactor:` / `feat:` / `fix:` / `docs:`。
- 一次提交只做一件事，保持原子性；重构与功能改动不要混在同一个提交里。
- 除非用户明确要求，否则只做本地提交，**不要** `git push`；任何情况下都**不要**
  rebase 或 force-push 已有历史。
- 不要把 `__pycache__/`、构建产物、临时验证目录加入版本控制；临时目录写进
  `.git/info/exclude`，不要污染 `.gitignore`。

## 9. 与上游 Rust 版对齐

- 上游仓库：`https://gitea.tendokyu.moe/beerpsi/fsdecrypt`，主分支名为 `trunk`。
- 取上游源码用 raw 接口，例如：

  ```text
  https://gitea.tendokyu.moe/beerpsi/fsdecrypt/raw/branch/trunk/src/main.rs
  https://gitea.tendokyu.moe/beerpsi/fsdecrypt/raw/branch/trunk/Cargo.toml
  ```

  `Cargo.toml` 的 `version` 是上游版本号；`src/` 下文件与本地模块的对应关系：
  `bootid.rs → Model.py`、`crypto.rs → Crypto.py`、`stream.rs → Reader.py`、
  `vhd.rs → Vhd.py`、`main.rs → Api.py`（提取流程部分）。
- 当前对齐到上游 **v0.1.9**。已知差异（不要为了「对齐」而抹平）：
  - 本库**没有** CLI，上游的 `--no-extract` 对应 `DecryptFile`，多文件参数对应用
    多次调用，输出目录参数是本库扩展。
  - 本库 APP/OS 的内层 VHD 是**流式读取**，不写中间 `.vhd` 文件；上游会落地再删除。
  - 本库保留了上游没有的裸 `.ntfs` / `.exfat` 镜像支持。
  - 链式读取的扇区位图合并比上游更严格：上游在「差分盘不拥有首个扇区」时会一次读回
    整段，从而覆盖掉后续本应由差分盘提供的扇区；本库在 `ChainedVhdDisk.ReadAt` 里按
    归属层逐段解析，**这是有意为之的修正，不要改回上游写法**。
- BootID 字段偏移（96 字节）：`0 crc32 / 4 length / 8 signature / 13 container_type /
  14 sequence_number / 15 use_custom_iv / 16 game_id / 20 target_timestamp(8) /
  28 target_version 或 option_id(4) / 32 block_count / 40 block_size /
  48 header_block_count / 64 os_id(3) / 67 os_generation / 68 source_timestamp(8) /
  76 source_version / 80 os_version`。`option_id` 与 `target_version` 是同一段字节的
  两种解释（上游用 union）。

## 10. 完成定义（DoD）

一个改动算完成，需要同时满足：

1. `python -m ruff check FSDecrypt` 无告警；
2. 改动的字节级逻辑已用一次性合成脚本验证过（脚本本身不留仓库）；
3. `README.md` 中受影响的用法示例已同步（README 面向人类读者）；
4. 仓库里没有新增多余文件：没有 CLI、没有测试目录、没有临时脚本或目录、
   没有厂商容器、没有 `__pycache__`。
