# 验证记录

> 状态：历史记录。按日期保存跨平台验证结果，只追加，不修改既有记录。验证方法见 [开发环境与依赖策略](../development.md)。

## WSL 验证与本次记录（2026-11-10）

从 Windows 可以通过 `wsl.exe -d Ubuntu --exec bash -lc 'cd ~/code/flatlas && uv sync --locked && uv run ruff check . && uv run pytest -q -rs && uv run pyright && git diff --check'` 调用 Linux 检查。执行前确认 Linux 副本包含待验证的提交和未提交改动；两端使用各自的 `.venv`，测试语料放在 WSL 原生文件系统中。

本次 Windows 使用 Python 3.12.13，Ubuntu 22.04 / WSL2 使用 Python 3.12.14。由于 WSL 仓库落后于 Windows，本次验证将 Windows 当前源码与未提交改动复制到 WSL 原生临时目录，并独立执行 `uv sync --locked`，没有更新 WSL 仓库代码。

| 环境 | Ruff | pytest | Pyright |
|---|---|---|---|
| Windows | 通过 | 132 passed，1 skipped（无法创建 symlink） | 0 errors |
| Ubuntu / WSL2 | 通过 | 128 passed，5 skipped（原生 Windows API 用例） | 0 errors |

Windows 工作区的 `git diff --check` 通过。Linux Pyright 的 Node 运行时需要 `libatomic.so.1`；本次 Ubuntu 缺少该库，通过下载发行版 `libatomic1` 包、解包到验证临时目录，并设置该进程的 `LD_LIBRARY_PATH` 完成检查，未安装系统软件包。正常复跑前需由环境提供该运行库；这不是 Python 锁文件依赖。

## 0.2.0 发布验收（2026-11-25）

受测提交为 `c4ec106094fa19fdbc8a2e8666b34dd599874230`。Windows 原生在当前工作区验证，未提交变动仅涉及文档。WSL 在原生文件系统的临时目录独立检出同一提交。两端均使用提交中的锁文件执行 `uv sync --locked`，并完整运行 Ruff、pytest（`-q -rs`）、Pyright 和 `git diff --check`。

| 环境 | Python | Ruff | pytest | Pyright | `git diff --check` |
|---|---|---|---|---|---|
| Windows 原生（10.0.26200.9911） | 3.12.13 | 通过 | 303 通过，52 跳过（15.01 s） | 0 错误，0 警告 | 通过 |
| Ubuntu 22.04.3 LTS / WSL2（内核 5.15.167.4） | 3.12.14 | 通过 | 350 通过，5 跳过（10.68 s） | 0 错误，0 警告 | 通过 |

- Windows 的 52 项跳过包括 28 项因本次进程无法创建 symlink，以及 24 项需要 Linux 或 POSIX 特性。其中包含 POSIX hardlink 语料、FIFO、挂载与 `/proc`、`statx`、原始字节文件名、POSIX 权限和信号。
- WSL 的 5 项跳过均需要 Windows 原生 API，涉及占用空间查询、稀疏或压缩控制及驻留文件元数据。
- WSL 首次运行 Pyright 时，Node 因缺少 `libatomic.so.1` 启动失败。将 Ubuntu 的 `libatomic1` 包解包到临时目录，仅为复跑进程设置 `LD_LIBRARY_PATH`，随后类型检查通过。

Windows 归档机械硬盘的性能基线待实测。发布背景见 [0.2.0 发布记录](0.2.0-release.md)。
