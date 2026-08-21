# File Atlas (`flatlas`)

跨平台的持久化文件系统索引工具。它将目录扫描结果保存在 SQLite 中，以便离线浏览、查询和导出；重复文件检测、去重计划与实际文件系统修改建立在该索引之上。

## 当前状态

项目处于纯 Python 3.12 的设计与原型准备阶段，尚未包含功能代码、CLI 实现或数据库 schema。

当前阶段的目标：

- 建立可复现的 `uv` 项目环境。
- 明确索引、扫描覆盖、重复检测与安全操作计划的设计。
- 先实现只读的 Python MVP；不实现文件修改、监听、TUI 或 Rust 后端。

## 快速开始（环境）

```powershell
uv sync
uv run python --version
```

预期解释器为 Python 3.12。依赖将在功能设计确定后通过 `uv add` 加入；不要向项目环境直接使用 `pip install`。

## 文档

- [架构与边界](docs/architecture.md)
- [阶段路线图](docs/roadmap.md)
- [SQLite 数据库结构草案](docs/database-schema.md)
- [MVP 范围与验收标准](docs/mvp-acceptance.md)
- [同类工具与 MVP 功能对照](docs/tool-comparison.md)
- [开发环境与依赖策略](docs/development.md)
- [待决策清单](docs/decisions.md)

## 目录

```text
src/flatlas/  # 未来的纯 Python 应用包；当前无功能代码
tests/        # 未来的测试语料与测试代码；当前无功能代码
docs/         # 设计、决策与开发说明
```
