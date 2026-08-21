# MVP 范围与验收标准

本文是 File Atlas MVP 的范围基线和完成验收标准。MVP 的定位是跨 Windows/Linux 的只读、持久化文件索引与重复内容分析工具；它不在本阶段修改文件系统。

## 已确定的功能范围

### 扫描命名空间与索引

- 管理多个内部 scan namespace：Windows 的卷/盘符或挂载点，Linux 的 `/` 或独立挂载点。
- 用户查询默认可以跨全部 namespace 聚合；内部仍保留 namespace 边界，以保证 scan coverage、挂载策略和未来同文件系统操作的正确性。
- SQLite schema 与 migration 保存当前路径状态、扫描、scope coverage、扫描错误、metadata、hash 和 dry-run plan。
- 保存的是当前状态与扫描审计，不保存可查询的完整 immutable scan snapshot。

### 扫描与一致性

- 支持完整扫描，以及任意指定子树的局部更新。
- 只有某个 `scan_scope` 完整完成，才将该 scope 内本次未再次观测到的路径标记为 `deleted`。
- 取消、权限错误、I/O 错误、过滤排除和扫描中改名都不得被解释为路径删除。
- 源目录之后不可访问时，已索引的当前状态仍可从 SQLite 查询。

### 跨平台文件处理

- Windows：索引原生 Unicode 路径，处理长路径；默认不跟随 symlink、junction 或其他 reparse point。
- Linux：以原始字节保存非 UTF-8 路径；默认不跟随 symlink；FIFO、socket、device 等记录为 `other`，不计算内容 hash。
- hash 复用仅在文件身份、size、mtime 及全部可用的 change/birth 时间一致时进行；任何字段缺失、变化或无法可靠比较时重新 hash。

### 查询、查重与导出

- 提供路径查询、目录大小（`du`）、最大文件（`largest`）和基础重复组（`duplicates`）查询。
- 以 `size → quick hash → BLAKE3 full hash` 分级识别内容重复文件。
- 支持 JSON 与 CSV 导出。
- 支持生成不可变 dry-run plan，记录 canonical/replacement、预期 full hash 和同卷/同文件系统等前置条件。

### MVP 明确不包含

- 删除、移动或替换文件。
- 创建 hardlink、reflink 或 symlink。
- USN Journal、inotify 或任何持续监听。
- TUI、图形界面、Rust/PyO3 后端。
- 完整历史 snapshot、复杂 include/exclude 配置和高级过滤。

## 验收矩阵

| 类别 | 测试场景 | 通过标准 |
|---|---|---|
| 数据库 | 新库初始化、重复打开、schema 升级 | migration 可重复执行；外键与必要索引生效；既有数据不被意外丢弃。 |
| 全量扫描 | 包含普通文件、空目录、嵌套目录的固定语料 | 路径、类型、逻辑大小和可用 metadata 与文件系统一致。 |
| 局部更新 | 初次扫描后新增子目录，只扫描该子目录 | 新文件进入索引，且可与旧索引文件形成重复组；未扫描的旧子树不被重新标记。 |
| 安全删除语义 | 模拟取消、权限错误、I/O 错误、扫描中改名 | 对应 scope 为 `partial`、`failed` 或 `cancelled`；任何未覆盖旧路径仍为 `present`。 |
| 已确认删除 | 对完整成功的 scope 删除一个既有文件后重扫该 scope | 已删除路径标为 `deleted`，scope 外路径不受影响。 |
| 分级 hash | 完全相同、同大小但不同内容、文件修改后重扫 | 仅完全相同内容进入同一重复组；修改使旧 hash 失效并重算。 |
| 离线查询 | 完成扫描后使源目录不可访问 | 仍可查询此前索引的 `path`、`du`、`largest` 和重复组。 |
| 查询与导出 | 固定语料运行所有基础查询、JSON/CSV 导出 | 查询结果、排序和统计符合预期；JSON 可解析，CSV 列名与编码稳定。 |
| dry-run | 生成 plan 前后对比文件系统快照 | 文件系统无新增、删除、改名、内容或 metadata 修改；plan 含预期 precondition。 |
| 全局查询 | 两个 namespace 中各存在相同内容文件 | 全局 `duplicates` 能返回该组；plan 报告跨文件系统 hardlink 不可执行的前置条件。 |
| Windows | Unicode、长路径、Access Denied、junction/reparse point | 不发生路径截断或越界遍历；错误被记录；reparse point 不被默认跟随。 |
| Linux | 非 UTF-8 路径、`EACCES`、symlink、FIFO/socket/device、稀疏文件 | 原始路径可 round-trip；错误不导致假删除；特殊类型不 hash；logical/allocated size 分开报告。 |

## 完成门槛

MVP 完成须同时满足以下条件：

1. Windows 与 Linux 均运行完整测试语料，并通过其对应的平台测试项。
2. 任一中断或错误注入测试均不会产生假删除记录。
3. 局部扫描新增目录后，可准确找出它与既有索引之间的重复内容。
4. dry-run 和所有 MVP 命令均不修改用户文件系统。
5. 静态检查、单元测试和集成测试均通过；每项已知平台差异都有回归测试。

## 与路线图的关系

本文覆盖 [roadmap.md](roadmap.md) 的阶段 1。阶段 2 以后才引入配置化过滤、dirty state、历史策略和性能基准；阶段 4 以后才允许实际执行 hardlink 或其他修改操作。
