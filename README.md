# File Atlas (`flatlas`)

跨 Windows/Linux 的持久化、只读文件系统索引工具。扫描结果保存在 SQLite 中，可离线查询、分析重复内容、导出结果并生成不可变 dry-run 去重计划；MVP 不会修改用户文件。

当前已实现的范围、已验证行为与待补齐项见 [MVP 实现状态](docs/mvp-implementation.md)。

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
flatlas dupes D:\Archive
flatlas du D:\Archive\2026
flatlas ls D:\Archive\2026
flatlas largest --limit 50
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
flatlas du [PATH ...] [--format table|json|csv]
flatlas ls [PATH] [--format table|json|csv]
flatlas largest [--limit N]
flatlas dupes [PATH] [--absolute] [--format table|json|csv]
flatlas export dupes [PATH] [--absolute] --format json|csv --output PATH
flatlas plan create --root PATH
flatlas plan show PLAN_ID
```

## MVP 安全边界

- 完整扫描与指定子树更新；仅在 scope 完整覆盖后将缺失路径标记为 `deleted`。
- size → quick BLAKE3 → full BLAKE3 的重复检测；默认不跟随 symlink 或 Windows reparse point。
- 支持路径、目录大小、最大文件、重复组查询、JSON/CSV 导出和 dry-run plan。
- `roots` 及其别名 `df` 默认以 GNU `df` 风格表格显示 registered filesystem 的实时容量；不可访问的容量明确显示为未知。
- `du` 默认输出带表头且按列对齐的汇总，包含逻辑字节数、文件数、可用时的实际分配字节数和相对路径；紧凑表头使用 `SIZE(B)`、`N` 和 `ALLOC(B)`。
- `du` 可同时接受多个文件或目录路径，因此也能处理 shell 或启动器提前展开后的通配结果。
- `ls [PATH]` 实时枚举目标目录的直接子目录并只显示目录名，类似 Linux `ls`，同时与持久化 scan coverage 合并：`ok` 表示完整扫描，`new` 表示尚未扫描，`part` 表示最近覆盖未完成，`gone` 表示索引曾观察到但本次枚举已不存在。状态查询只读，不会修改索引中的删除状态。
- 不删除、移动、hardlink、reflink、symlink 或持续监听文件系统。

## 开发检查

```powershell
uv run pytest -q
uv run ruff check .
uv run pyright
```

## 文档

- [当前 MVP 实现状态](docs/mvp-implementation.md)
- [架构与边界](docs/architecture.md)
- [SQLite 数据库结构草案](docs/database-schema.md)
- [MVP 范围与验收标准](docs/mvp-acceptance.md)
- [阶段路线图](docs/roadmap.md)
- [查询命令设计](docs/query-cli-design.md)
- [开发环境与依赖策略](docs/development.md)
- [维护规则](AGENTS.md)
