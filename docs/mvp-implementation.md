# 当前 MVP 实现状态

更新时间：2026-08-24。

本文描述仓库中已落地的 Python MVP，不替代 [架构与边界](architecture.md)、[数据库结构草案](database-schema.md) 和 [验收标准](mvp-acceptance.md)；后三者仍是后续功能的设计约束。

## 已实现

### 安装、配置与入口

- `pyproject.toml` 定义 `flatlas = flatlas.cli:main` 命令行入口，可通过 `uv run flatlas ...` 开发运行，或用 `uv tool install --editable .` 安装为用户级工具。
- 通过 `platformdirs` 保存用户级配置和索引位置：Linux 通常使用 `~/.config/flatlas` 与 `~/.local/share/flatlas`，Windows 通常使用 `%LOCALAPPDATA%/flatlas`。
- `flatlas init [PATH]` 会创建全局配置和 SQLite 数据库，并仅登记 PATH 所在的文件系统 namespace；它不会扫描目录或创建 path 记录。
- `--db PATH` 可覆盖全局数据库位置，适用于测试、脚本和故障排查。
- 未初始化或 namespace 未登记等领域错误仅输出 `error: ...` 并以退出码 2 结束，不输出 Python traceback。

### 数据库与扫描

- 首次打开数据库时创建 schema migration、root、scan、scan_scope、path、scan_error、file_hash、plan 和 plan_operation 表及必要索引。
- SQLite 连接启用 foreign keys、WAL 和 busy timeout。
- `flatlas scan PATH` 支持完整 namespace 扫描或任意已登记 namespace 内的 subtree 扫描。
- 每次扫描记录 scan、scope、路径 metadata 与错误；只有 scope 成功完成，才将该 scope 内未再次发现的旧 path 标记为 `deleted`。
- 目录枚举、stat 和 hash 失败会令 scope 变为 `partial` 或 `failed`，不会据此推断旧路径被删除。
- 默认不递归进入 symlink；Windows reparse point（包括 junction）记录为 `reparse`，不递归进入。
- Windows 的无符号文件身份值在写入 SQLite 前会映射到其有符号 64 位表示，保证同次及后续比较稳定。

### Hash、查询与计划

- `scan --hash {none,quick,full}` 默认使用 `full`；重复检测先按 size 缩小候选集，再计算 quick BLAKE3 和 full BLAKE3。
- full hash 仅在 size 与 quick hash 匹配后生成；同一 full digest、算法和大小的当前文件构成重复组。
- 局部扫描会将新候选与已有索引中的同大小文件一并补齐 hash，从而发现新旧目录之间的重复项。
- 可用命令：`roots`、`paths [PATH]`、`du [PATH ...]`、`ls [PATH]`、`largest`、`duplicates`，其中路径参数均为可选索引范围。`du` 默认以空格对齐的紧凑汇总表输出每个目标的逻辑字节数、文件数、可用时的 allocated 字节数和相对路径，并接受多个文件或目录路径以兼容调用层已经展开的通配结果；`ls` 以相同统计列列出目标目录的直接子目录和普通文件，以 `d`/`f` 标识类型，并为目录递归汇总后代普通文件；JSON/CSV 仍可显式选择。
- 查询可用 `--format json|csv` 与 `--output PATH` 导出；`flatlas export duplicates` 提供快捷导出。
- `flatlas plan create --root PATH` 生成不可变 `dry_run` hardlink plan。canonical 以字典序最小路径确定；operation 带 size、full digest 与“不执行”的前置条件。MVP 没有 `apply` 命令。

## 当前代码布局

```text
src/flatlas/
  cli.py       # Typer 命令与用户错误边界
  config.py    # 用户级配置、数据库定位
  core.py      # SQLite、扫描、hash、查询、导出、dry-run plan
  errors.py    # 用户可见领域异常
  __main__.py  # python -m flatlas
```

`core.py` 目前集中实现 MVP 以降低初期复杂度。后续功能增长时，可按 architecture.md 的边界拆分为 `db/`、`scanning/`、`hashing/`、`queries/` 和 `planning/`，但不得改变现有数据库语义。

## 已验证的行为

`tests/test_mvp.py` 覆盖：

- init 仅登记 namespace，未创建 scan/path。
- 完整扫描发现同内容文件，并生成不可变 dry-run plan。
- 局部扫描可与已有索引文件形成重复组；成功重扫局部 scope 后，已删除文件被标记 deleted，scope 外文件仍为 present。

当前检查命令：

```powershell
uv run ruff check .
uv run pytest -q
uv run pyright
```

## 尚未完成

这不是对完整验收矩阵“已通过”的声明。以下仍需后续实现或扩展测试：Linux 非 UTF-8 路径的集成语料、权限/I/O/取消故障注入、Windows 长路径与 reparse tag 细节、allocated size 的平台校准、复杂过滤、历史 snapshot、监听、并发/Rust 后端，以及任何修改文件系统的执行器。
