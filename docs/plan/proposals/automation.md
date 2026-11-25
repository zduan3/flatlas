# 自动化存储评估：0.2.1 后续入口

> 状态：提案（0.2.1 未实现）。0.2.0 的机器输出约定、`find --format`、`FLATLAS_DB` 与 `--no-config` 已发布，见 [查询命令](../../design/queries.md#机器输出)、[配置](../../design/config.md) 和 [0.2.0 发布记录](../../history/0.2.0-release.md)。0.2.1 不变更 schema（沿用 v7）。

## 0.2.1 计划

### 删除计数与 `rm` 帮助

- `scan` 输出和 `scan.stats_json` 增加 `paths_deleted`，统计最终事务实际从 `present` 转为 `deleted` 并提交的路径数（包含缺失目录的后代，排除已有 tombstone）。旧扫描缺少该字段时表示未知，新扫描确认零条时报告 `0`。
- `entries_per_s` 继续按本次观测到的条目计算，说明总耗时包含删除确认。
- `rm --help` 明确单个 PATH 同样立即移除索引子树，并提示先用 `du PATH` 查看规模。

### 只读 Python API

- `flatlas.api` 作为 `queries.py` 之上的薄只读门面，提供 `with open_index(db) as idx:` 一次打开索引、多次查询，取代报告脚本反复启动子进程或直接引用内部模块。
- 默认以 SQLite 只读模式打开已有索引（复用现有 `open_database_readonly`）；索引不存在或 schema 落后需要迁移时抛出 `FlatlasError`。
- 首批覆盖 `find`、`du`、`largest`、`roots`、`scans`、`scan_info`。字段、单位与状态沿用 CLI 的机器输出约定，`find` 保持流式迭代；实时读取的 `ls`、`df` 与写入命令留在各自 CLI 入口。
- 与 CLI 共用底层查询函数和记录转换，不需要额外的跨查询读快照抽象或反向重构既有 CLI 的打开兼容逻辑。

### 批量浅扫描

- `scan --shallow --files0-from PARENTS` 在一次调用中复用连接，顺序对清单中的每个父目录执行单目录浅扫描。
- 保持既有删除安全不变量：每个父目录各自记录 `scan_id` / `scope_id` 并独立提交；只有 `completed` scope 才确认直接缺失条目及其已索引后代为 `deleted`；`partial`、`failed` 或 `cancelled` 不确认删除，且不影响其他已提交目录。
- 已知删除清单且不需要保留 tombstone 时，仍优先使用 `rm --files0-from`。

## 不在本提案内

- 交互参数（GNU `ls`/`du`/`df` 选项）按 [查询命令 GNU 兼容规划](query-cli.md) 后置。
- 扫描前后两次结果的差异报告、`find -printf`、非 UTF-8 路径在机器输出中的无损表示：见 [待决策清单](../decisions.md)。
