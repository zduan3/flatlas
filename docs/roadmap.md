# 阶段路线图

## 阶段 0：设计与实验（当前）

- 固化术语、范围、失败语义和测试矩阵。
- 决定 SQLite 当前状态表与历史 observation 的边界。
- 设计 scan coverage、hash state、plan 与 operation 的 schema 草案。
- 创建包含非 ASCII 路径、已有 hardlink、权限错误、扫描中改名和稀疏文件的测试语料规范。

## 阶段 1：纯 Python 只读 MVP

- 全量 metadata 扫描与局部子树更新。
- SQLite 查询：`du`、`largest`、基础 `duplicates`。
- size → quick hash → BLAKE3 full hash 流水线。
- JSON/CSV 导出与 dry-run plan。

阶段验收：在中断扫描或目录遍历错误后，不会错误标记未覆盖路径为删除；重复组可在原目录离线后从数据库查询。

## 阶段 2：Python 索引完善

- 配置化 include/exclude、大小与时间过滤。
- 当前状态校验、dirty state 与历史结果保留策略。
- 基准测试与故障注入测试。

## 阶段 3：Rust 扫描与 hash 后端

- 独立 `flatlas-core` 与 sidecar executable。
- 与 Python backend 对照同一测试语料和规范化 SQLite 结果。
- 加入有界并发、取消、进度统计和性能基准。

## 阶段 4：安全去重执行器

- plan 在 apply 前重新验证文件身份、metadata 与内容。
- 支持 dry-run、跳过、重试、审计和崩溃恢复。
- 首先实现 hardlink；reflink 与 delete 在独立能力探测、测试与策略明确后加入。

## 阶段 5：持续增量与体验

- 事件驱动 dirty queue 与定期 reconcile。
- 可选 TUI、shell completion、稳定 JSON 协议与发行打包。
