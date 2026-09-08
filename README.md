# File Atlas (`flatlas`)

跨 Windows/Linux 的持久化、只读文件系统索引工具。当前 MVP 的版本号是 0.1；本文档中的“MVP”和“0.1”是同义词。MVP（0.1）面向不跨文件系统的普通用户目录和归档目录，用于分析空间占用、定位最大文件和发现重复内容候选；扫描结果保存在 SQLite 中，可离线查询和导出，不会修改用户文件。

当前已实现的范围、已验证行为与待补齐项见 [MVP（0.1）实现状态](docs/mvp-implementation.md)。

## 安装与开发

项目使用 Python 3.12 与 uv：

```powershell
uv sync
uv run flatlas --help
```

安装为用户级命令行工具：

```powershell
uv tool install --editable .
flatlas --help
```

发布到包索引后，用户可使用 `uv tool install flatlas` 安装。

## 首次使用

`init` 仅登记当前路径所属的文件系统 namespace，不会遍历或扫描文件。配置在用户级目录，Linux 通常为 `~/.config/flatlas`，Windows 通常为 `%LOCALAPPDATA%/flatlas`；索引数据库独立于项目目录。

```powershell
flatlas init D:\Archive
flatlas scan D:\Archive
flatlas scan D:\Archive\2026\08
flatlas rm D:\Archive\2026\08
flatlas dupes D:\Archive
flatlas du D:\Archive\2026
flatlas ls D:\Archive\2026
flatlas largest D:\Archive\2026 --limit 50
flatlas export dupes --format csv --output duplicates.csv
flatlas plan create --root D:\Archive
```

`--db PATH` 仅用于测试、迁移、脚本或故障排查等需要覆盖全局索引位置的场景。未初始化时，命令只输出可读的 `error: ...` 信息并返回退出码 2。

## 当前 CLI

```text
flatlas init [PATH]
flatlas roots
flatlas df
flatlas scan PATH
flatlas paths [PATH]
flatlas rm PATH
flatlas du [PATH ...] [--format table|json|csv]
flatlas ls [PATH] [--format table|json|csv]
flatlas largest [PATH] [--limit N] [--format table|json|csv]
flatlas dupes [PATH] [--absolute] [--format table|json|csv]
flatlas export dupes [PATH] [--absolute] --format json|csv --output PATH
flatlas plan create --root PATH
flatlas plan show PLAN_ID
```

## MVP（0.1）安全边界

- MVP（0.1）的测试基线是一个不跨文件系统、使用当前平台常规 Unicode 文件名的普通目录；非 UTF-8、完整长路径/reparse tag、复杂 mount 和物理共享空间语义留给后续版本。
- 完整扫描与指定子树更新；仅在 scope 完整覆盖后将缺失路径标记为 `deleted`。
- size → quick BLAKE3 → full BLAKE3 的重复候选检测；默认不跟随 symlink 或 Windows reparse point。
- 支持路径、目录大小、最大文件、重复候选查询、JSON/CSV 导出和 dry-run plan。
- `roots` 及其别名 `df` 默认以 GNU `df` 风格表格显示 registered filesystem 的实时容量；不可访问的容量明确显示为未知。
- `du` 默认输出带表头且按列对齐的汇总，包含逻辑字节数、文件数、可用时的实际分配字节数和相对路径；紧凑表头使用 `SIZE(B)`、`N` 和 `ALLOC(B)`。
- `du` 可同时接受多个文件或目录路径，因此也能处理 shell 或启动器提前展开后的通配结果。
- 当前 `ls [PATH]` 实时枚举目标目录的直接子目录并合并持久化 scan coverage；MVP（0.1）目标还需增加直接普通文件和特殊条目。目标表格使用 `LOGICAL(B)`：文件显示自身 logical size 和 `N=1`，目录显示已知递归索引 logical size 和文件数；`ok`、`new`、`part`、`gone` 表示统计的覆盖和现场状态。
- `largest [PATH]` 只查询指定的已索引文件或目录 scope，PATH 默认为当前目录；按 logical size 降序、路径升序稳定排序，路径默认相对于 PATH。
- 不删除、移动、hardlink、reflink、symlink 或持续监听文件系统。
- rm PATH 只从 SQLite 递归移除该路径及其已索引后代，等价于对索引执行 rm -r PATH；它不访问、不验证也不改动实际文件。若子树被 dry-run plan 引用，该计划会一并移除，避免保留失效计划。

## `ls` 与 `du`

- `ls PATH` 用于浏览 PATH 的一层 live 条目、索引 coverage 和已知空间提示。它有意不显示 GNU `ls -l` 中常见但不代表内容总量的目录对象 4 KiB 大小。
- `du PATH ...` 用于查询每个显式文件或目录 scope 的递归索引汇总；默认每个参数一行，即使源目录暂时离线仍可使用。
- `largest PATH` 用于排序 PATH 子树中的最大单个普通文件。

目录的 `part` / `gone` 状态可以附带最后已知聚合，但该值可能不完整或过期；未知必须显示为 `-` / `null`，不能伪装为 0。完整字段和状态语义见 [查询命令设计](docs/query-cli-design.md)。

## 外部删除与索引

flatlas 不删除用户文件。文件或目录在外部被删除后：

```text
ls 父目录              → 可以显示 gone，但不修改数据库
scan 仍存在的父目录    → completed 后才将缺失路径标记 deleted
```

已经不存在的目标不能直接扫描，必须扫描覆盖它的现存父目录。partial、failed、cancelled 或枚举错误不会确认删除。deleted 记录保留在 SQLite，但不参与 `paths`、`du`、`largest` 或 `dupes` 的当前状态结果；同一路径重新出现并被扫描时恢复为 present，旧 hash 在 metadata basis 不匹配时变为 stale。

## 实际清理

`flatlas dupes` 输出的是基于完整 BLAKE3 的重复内容候选和理论 logical savings，不是删除授权。需要实际去重时，应选择值得处理的较小目录，再让 jdupes 重新扫描、独立验证并由用户显式执行：

```powershell
flatlas dupes D:\Archive\Photos\2025
jdupes -r D:\Archive\Photos\2025
```

不要把 `flatlas export dupes` 的结果直接管道到删除命令。dry-run plan 也只是候选建议快照；MVP（0.1）没有 `apply`。

## 开发检查

```powershell
uv run pytest -q
uv run ruff check .
uv run pyright
```

## 文档

- [当前 MVP（0.1）实现状态](docs/mvp-implementation.md)
- [架构与边界](docs/architecture.md)
- [SQLite 数据库结构草案](docs/database-schema.md)
- [MVP（0.1）范围与验收标准](docs/mvp-acceptance.md)
- [阶段路线图](docs/roadmap.md)
- [查询命令设计](docs/query-cli-design.md)
- [开发环境与依赖策略](docs/development.md)
- [维护规则](AGENTS.md)
