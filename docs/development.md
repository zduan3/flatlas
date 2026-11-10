# 开发环境与依赖策略

> 状态：开发记录。说明本地环境、依赖与检查命令。

## 当前环境

- Python：固定 Python 3.12，由 `uv` 依据 `.python-version` 创建项目 `.venv`。
- 依赖：`pyproject.toml` 是唯一声明来源，`uv.lock` 是可复现锁文件。
- 当前运行时依赖：`blake3`（quick/full hash）、`platformdirs`（用户级配置位置）、`typer`（CLI）。
- 当前开发依赖：`pytest`、`ruff`、`pyright`。

使用：

```powershell
uv sync --locked
uv run ruff check .
uv run pytest -q
uv run pyright
git diff --check
```

`uv sync --locked` 使用已提交的锁文件，声明与锁文件不一致时直接失败。pytest 默认仅收集 `tests/`，不遍历项目中的临时输出目录；Pyright 显式使用项目 `.venv` 解析依赖。上述命令应在仓库根目录执行。

### 验证环境与平台记录

- 新检出环境先运行 `uv sync --locked`，再依次执行上述四项检查。Windows 与 Linux 分别记录通过数、跳过数及跳过原因；单一平台通过不能替代另一平台验收。
- 若 uv 无法写入缓存，先用 `uv cache dir` 确认实际位置；必要时通过当前进程的 `UV_CACHE_DIR` 指向可写目录，不将机器专属绝对路径写入项目配置。
- pytest 在 fixture 准备阶段出现临时目录权限错误时，先排查运行环境及临时目录权限；必要时用 `--basetemp` 指定专用、可写的测试目录。pytest 会清理该目录，因此不要指向已有用户数据目录。
- 受限执行环境可能阻止 uv 的缓存文件持久化或 pytest 的临时目录操作。此类错误应在具备正常文件权限的环境复核，不通过放宽文件 ACL、跳过测试或修改业务代码消除报错。
- 若 Pyright 报告依赖无法解析，先确认 `uv sync --locked` 已成功、当前目录为仓库根目录且 `.venv` 存在；可用 `uv run pyright --pythonpath .venv/Scripts/python.exe`（Windows）或 `.venv/bin/python`（Linux）诊断解释器选择。

使用 `uv add <package>` 或 `uv add --dev <package>` 变更依赖；不要向项目 `.venv` 使用裸 `pip install`，也不要让 Pixi 与 uv 同时管理该项目依赖。Pixi Global 可以继续作为日常 Python，但本项目不依赖全局环境的偶然状态。

### WSL 验证与本次记录（2026-11-10）

从 Windows 可以通过 `wsl.exe -d Ubuntu --exec bash -lc 'cd ~/code/flatlas && uv sync --locked && uv run ruff check . && uv run pytest -q -rs && uv run pyright && git diff --check'` 调用 Linux 检查。执行前确认 Linux 副本包含待验证的提交和未提交改动；两端使用各自的 `.venv`，测试语料放在 WSL 原生文件系统中。

本次 Windows 使用 Python 3.12.13，Ubuntu 22.04 / WSL2 使用 Python 3.12.14。由于 WSL 仓库落后于 Windows，本次验证将 Windows 当前源码与未提交改动复制到 WSL 原生临时目录，并独立执行 `uv sync --locked`，没有更新 WSL 仓库代码。

| 环境 | Ruff | pytest | Pyright |
|---|---|---|---|
| Windows | 通过 | 132 passed，1 skipped（无法创建 symlink） | 0 errors |
| Ubuntu / WSL2 | 通过 | 128 passed，5 skipped（原生 Windows API 用例） | 0 errors |

Windows 工作区的 `git diff --check` 通过。Linux Pyright 的 Node 运行时需要 `libatomic.so.1`；本次 Ubuntu 缺少该库，通过下载发行版 `libatomic1` 包、解包到验证临时目录，并设置该进程的 `LD_LIBRARY_PATH` 完成检查，未安装系统软件包。正常复跑前需由环境提供该运行库；这不是 Python 锁文件依赖。

## 后续 Rust 配置

仅在进入 Rust 阶段时安装：

1. Visual Studio Build Tools 的 C++ 桌面开发组件及 Windows SDK。
2. `rustup`，选择 `x86_64-pc-windows-msvc` stable toolchain。
3. `rustfmt` 与 `clippy`。

```powershell
rustup default stable-x86_64-pc-windows-msvc
rustup component add rustfmt clippy
```

届时在项目根目录加入 `rust-toolchain.toml` 与 `native/` Cargo workspace。`rustup` 管 toolchain，Cargo 管 Rust crates，uv 仍只管 Python。若采用 PyO3，`maturin` 用于本地开发和构建；若采用 Rust sidecar，则不需要 maturin。
