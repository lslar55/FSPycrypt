# AGENTS.md

面向 AI 编码代理的仓库规范。人类贡献者请优先读 [README.md](README.md)。

## 1. 项目定位（硬性约束）

- 本仓库是上游 Rust 项目 [beerpsi/fsdecrypt](https://gitea.tendokyu.moe/beerpsi/fsdecrypt) 的
  **Python 库移植**，只提供 `import FSDecrypt` 的库接口。
- **不要重新引入命令行入口**：不添加 `argparse`/`click`/`loguru`，不添加
  `[project.scripts]`，不添加 `__main__.py`、`Cli.py` 或类似脚本。历史上的
  `Decrypt.py`、`fsdecrypt.py`、`FSDecrypt/Cli.py` 已被有意删除。
- 版本号只有一个来源：`FSDecrypt/__init__.py` 的 `__version__`，`pyproject.toml`
  通过 `dynamic = ["version"]` 读取它。不要在 `pyproject.toml` 里再写死版本。
- 破坏公开 API 前先确认：`FSDecrypt/__init__.py` 的 `__all__` 就是公开契约。

## 2. 模块职责与依赖方向

```text
Api.py      公开用例编排（ExtractFiles / ExtractNtfs / ExtractExfat / DecryptFile）
Reader.py   fscrypt 容器读取器（按页解密 + seek）
Crypto.py   AES-CBC 与页 IV 推导
Model.py    BootID 解析
Keys.py     内置密钥 + 外部密钥文件
Vhd.py      VHD 格式：页脚 / 动态头 / BAT / 扇区位图 / 单盘与差分链
Exfat.py    exFAT 解析与提取
Ntfs.py     NTFS 树与提取（基于 dissect.ntfs）
Extract.py  共用提取引擎（原子替换、重名处理、递归）
Util.py     路径、命名、时间戳等通用工具
```

依赖方向必须单向：`Api → {Reader, Exfat, Ntfs, Vhd} → {Extract, Util, Model, Crypto, Errors}`。

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

## 6. 测试与验证

回归测试位于 `tests/`，纯标准库 `unittest`，**不引入 pytest 或任何新依赖**：

```powershell
python -m unittest discover -s tests -v
python -m ruff check FSDecrypt tests
```

规范：

- 测试数据必须**合成**。真实容器（`.pack`/`.app`/`.opt`/`.vhd`/`.ntfs`）、密钥文件、
  提取产物一律不得提交进仓库，也不得写进 `tests/`。
- 涉及字节布局的逻辑（VHD、BootID、exFAT 目录项、IV 推导）必须补合成测试：
  自己构造字节流断言解析结果，而不是依赖固件样本。
- 改 `Vhd.py` 后至少跑通 `tests/test_fsdecrypt.py` 里的链式合并用例；改 `Extract.py`
  或 `Api.py` 后至少保证 `python -m unittest discover -s tests` 全绿。
- 拿真实容器做端到端验证时，输出目录要放在仓库外的临时位置或已被 `.git/info/exclude`
  排除的目录，验证完删掉；不要把它们 `git add` 进来。

## 7. Git 约定

- 提交信息用中文，类型前缀沿用现状：`chore:` / `refactor:` / `feat:` / `fix:` / `docs:`。
- 一次提交只做一件事，保持原子性；重构与功能改动不要混在同一个提交里。
- 只做本地提交，**不要** `git push`，**不要** rebase / force-push 已有历史。
- 不要把 `__pycache__/`、构建产物、临时验证目录加入版本控制；临时目录写进
  `.git/info/exclude`，不要污染 `.gitignore`。

## 8. 与上游 Rust 版对齐

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

## 9. 完成定义（DoD）

一个改动算完成，需要同时满足：

1. `python -m ruff check FSDecrypt tests` 无告警；
2. `python -m unittest discover -s tests -v` 全绿；
3. 新增/修改的字节级逻辑有对应的合成测试；
4. `README.md` 中受影响的用法示例已同步（README 面向人类读者）；
5. 仓库里没有新增多余文件：没有 CLI、没有临时目录、没有厂商容器、没有 `__pycache__`。
