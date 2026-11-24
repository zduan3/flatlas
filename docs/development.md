# 开发环境与依赖策略

> 状态：开发指南。说明本地环境、依赖与检查命令。

## 当前环境

- Python：固定 Python 3.12，由 `uv` 依据 `.python-version` 创建项目 `.venv`。
- 依赖：`pyproject.toml` 是唯一声明来源，`uv.lock` 是可复现锁文件。
- 当前运行时依赖：`blake3`（quick/full hash）、`platformdirs`（用户级配置位置）、`tomlkit`（保留已有字段与注释的 TOML 配置编辑）、`typer`（CLI）。
- 当前开发依赖：`pytest`、`ruff`、`pyright`。

首次准备环境及需要全量验收时使用：

```powershell
uv sync --locked
uv run ruff check .
uv run pytest -q
uv run pyright
git diff --check
```

`uv sync --locked` 使用已提交的锁文件，声明与锁文件不一致时直接失败。pytest 默认仅收集 `tests/`，不遍历项目中的临时输出目录；Pyright 显式使用项目 `.venv` 解析依赖。上述命令应在仓库根目录执行。

### 验证环境与平台记录

- 新检出环境先运行 `uv sync --locked`；日常改动按下节选择回归范围，全量验收时执行上述四项检查。Windows 与 Linux 分别记录通过数、跳过数及跳过原因；单一平台通过不能替代另一平台验收。
- 若 uv 无法写入缓存，先用 `uv cache dir` 确认实际位置；必要时通过当前进程的 `UV_CACHE_DIR` 指向可写目录，不将机器专属绝对路径写入项目配置。
- pytest 在 fixture 准备阶段出现临时目录权限错误时，先排查运行环境及临时目录权限；必要时用 `--basetemp` 指定专用、可写的测试目录。pytest 会清理该目录，因此不要指向已有用户数据目录。
- 受限执行环境可能阻止 uv 的缓存文件持久化或 pytest 的临时目录操作。此类错误应在具备正常文件权限的环境复核，不通过放宽文件 ACL、跳过测试或修改业务代码消除报错。
- 若 Pyright 报告依赖无法解析，先确认 `uv sync --locked` 已成功、当前目录为仓库根目录且 `.venv` 存在；可用 `uv run pyright --pythonpath .venv/Scripts/python.exe`（Windows）或 `.venv/bin/python`（Linux）诊断解释器选择。

使用 `uv add <package>` 或 `uv add --dev <package>` 变更依赖；不要向项目 `.venv` 使用裸 `pip install`，也不要让 Pixi 与 uv 同时管理该项目依赖。Pixi Global 可以继续作为日常 Python，但本项目不依赖全局环境的偶然状态。

## 按改动选择测试

pytest 的默认命令仍运行全量，用显式的 `-m` 选择日常回归范围；没有默认排除任何测试。功能 marker 可重叠，一个测试可以覆盖多项职责；`--strict-markers` 会拒绝未注册的 marker，避免拼写错误造成漏选。测试实现及断言不因分组而改变。

| 改动边界 | 主要代码/内容 | 交付前 pytest 范围 |
|---|---|---|
| 版本展示 | `version.py`、`--version` | `uv run pytest -q -m version` |
| 表格、大小显示、导出格式 | `output.py`、CLI 中的显示函数 | `uv run pytest -q -m output` |
| 查询、scope、排序与状态 | `queries.py`、单个空间查询命令 | `uv run pytest -q -m "query or safety"` |
| hash、dupes 筛选/分组 | `hashing.py`、`duplicate_filters.py`、dupes CLI | `uv run pytest -q -m "hash or safety"` |
| 扫描、coverage、目录缓存、取消/续扫 | `scanning.py`、`summaries.py`、scan CLI | `uv run pytest -q -m "scan or allocation or safety"` |
| 占用空间平台接口 | `allocation.py` | `uv run pytest -q -m "allocation or safety"` |
| dry-run plan | `planning.py`、plan CLI | `uv run pytest -q -m "plan or safety"` |
| 公共 CLI 入口、参数和错误渲染 | `cli.py` 的公共流程、`errors.py` | `uv run pytest -q -m "cli or output"` |
| 数据库、身份、公共层及测试基础设施 | 见下列全量触发条件 | `uv run pytest -q` |
| 仅文档/注释 | 不影响可执行代码、配置或命令示例契约 | 无需 pytest，执行 `git diff --check` |

同一文件可能包含多种职责，应按实际修改的函数选择范围。例如只改 `cli.py` 的 `dupes` 参数时使用 hash 范围；若同时改了公共输出，再合并 output 范围。命中多个边界时用 `or` 取并集，不能用 `and` 缩成重叠子集。`db` marker 可在迁移开发中局部调试，数据库变更最终仍须全量。

开发迭代可先运行失败的 node 或测试文件，例如：

```powershell
uv run pytest -q tests/test_dupes_filters.py
uv run pytest -q tests/test_dupes_filters.py::test_local_rescan_combines_new_and_existing_hashes_under_filters
```

交付前再运行表中受影响范围，Python 改动另外运行 `uv run ruff check .`、`uv run pyright` 和 `git diff --check`。这些静态检查保持全项目范围，通常比测试便宜，也能发现调用方类型错误。范围通过后，没有新改动、失败或未解决疑点时，不额外补跑全量或重复同一检查。

以下情况必须全量：schema/migration；`paths.py` 的路径身份、namespace 和 BLOB 编码；`config.py` 的全局配置与数据库定位；跨模块重构；依赖/构建；pytest 配置、共享 fixture 或测试分组调整；发布/里程碑验收；无法可靠确定影响范围。全量命令仍是 `uv run pytest -q`，0.1 冻结的发布验收要求不变。

平台 API、metadata/身份、路径和跨平台 I/O（含 CSV 换行）变更，在 Windows 和 WSL 原生文件系统运行相同范围；平台无关的小改动通常只需当前开发平台的受影响范围。Windows 原生用例在 Linux 的跳过属于预期，不能算作 Windows 功能验证。日常无需两端重复全量，发布/里程碑验收再分别运行并记录全套结果。

现有 `test_mvp.py` 用函数级 marker 区分职责，专项文件用文件级 marker 并按需要补充交叉标记。新增测试必须至少属于一个功能范围；新增共享 fixture 时检查所有使用方。维护分组时用 `uv run pytest --collect-only -q` 与 `uv run pytest --collect-only -q -m "hash or safety"` 核对总量与选择结果，不能只改文档而遗漏实际测试归属。

### WSL 验证

从 Windows 可以通过 `wsl.exe -d Ubuntu --exec bash -lc 'cd ~/code/flatlas && uv sync --locked && uv run ruff check . && uv run pytest -q -rs && uv run pyright && git diff --check'` 调用 Linux 检查。执行前确认 Linux 副本包含待验证的提交和未提交改动；两端使用各自的 `.venv`，测试语料放在 WSL 原生文件系统中。Linux Pyright 的 Node 运行时需要 `libatomic.so.1`，应由环境提供（这不是 Python 锁文件依赖）。历次带日期的验证结果记录在 [验证记录](history/verification-log.md)。

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
