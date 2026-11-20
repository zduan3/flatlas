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
