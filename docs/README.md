# 开发文档导航

根目录 [README](../README.md) 是面向用户的使用说明：安装、首次扫描、查询命令、安全边界和实际清理建议。本目录面向开发与维护，按“现状、计划、历史、调研”分类。

## design/：当前设计

只描述当前行为与不变量，按主题组织，随代码一起更新。

- [架构与边界](design/architecture.md)：产品定位、分层流水线、代码组织、核心模型、跨平台边界与不可破坏的语义。
- [数据库结构](design/schema.md)：schema v8 的表、索引、扫描与删除语义、migration 历史与执行机制。
- [扫描与目录汇总](design/scanning.md)：metadata 扫描、元数据成本、目录汇总与 freshness、`--refresh-before`、中断与续扫、删除确认。
- [惰性 Hash 与重复候选](design/hashing.md)：size → quick → full 流水线、basis 校验、dupes 筛选、分组与输出。
- [查询命令](design/queries.md)：`ls`、`du`、`find`、`largest`、`roots` / `df`、`rm`、`scans` / `scan-info`、`plan` 与只读 Python API。
- [命令默认参数配置](design/config.md)：`config.toml` 与 `flatlas config`。
- [平台差异](design/platform.md)：Windows 占用空间采集；Linux / Lustre 元数据实测。

## plan/：未来工作

计划内容只在这里出现，不代表当前 CLI 已支持。

- [路线图](plan/roadmap.md)：版本级目标（唯一副本）。
- [backlog](plan/backlog.md)：当前阶段按优先级排列的待办。
- [待决策清单](plan/decisions.md)：尚未定案的产品与技术选择。
- [proposals/incremental-refresh.md](plan/proposals/incremental-refresh.md)：浅扫描与按目录 mtime 的增量刷新。
- [proposals/lazy-hash.md](plan/proposals/lazy-hash.md)：惰性 hash 的并发、缓存控制、审计与过滤。
- [proposals/query-cli.md](plan/proposals/query-cli.md)：查询命令 GNU 兼容参数、后续子命令与默认行为迁移。

## history/：冻结记录

发布记录与验证结果。已发布版本的记录只修正链接。

- [0.1 功能范围与验收基线](history/0.1-acceptance.md)
- [0.1 实现状态](history/0.1-implementation.md)
- [0.2.0 发布记录](history/0.2.0-release.md)
- [验证记录](history/verification-log.md)：按日期追加的跨平台验证结果。

## research/：调研

- [同类工具与 MVP（0.1）功能对照](research/tool-comparison.md)：产品范围与外部工具对照记录。

## 开发指南

- [开发环境与依赖策略](development.md)：Python、uv、依赖、按改动选择测试与检查命令。

## 文档维护规则

- **放哪里**：当前行为与不变量写入 `design/`；未来工作写入 `plan/`（版本目标进 `roadmap.md`，当前阶段待办进 `backlog.md`，未实现的设计进 `proposals/`，未定案问题进 `decisions.md`）；已发布记录与带日期的验证结果进 `history/`；外部调研进 `research/`。
- **design 不写计划，plan 不写现状**：design 文档用现在时描述行为，末尾可以有一行“相关提案”链接；提案只用一两行指向对应 design 文档，不复述当前行为。
- **功能落地时**：把提案内容合并进相应 design 文档并改写为现状，删除该提案，从 `backlog.md` 移除条目，并更新本导航与入站链接。
- **0.1 冻结基线**：代码由 git tag `v0.1.0` 固定，文档在 `history/`；`history/` 只修正链接。
- **不重复**：路线图只在 `roadmap.md` 保留一份，其他文档链接它；不变量以 [架构与边界](design/architecture.md#不可破坏的语义) 为准。
- 用户可通过根 README 完成正常使用；README 只写已实现行为，使用说明增长时再拆出专用使用指南并从 README 链接。
- 保持文件路径稳定；需要迁移时，先更新本导航和所有入站链接，再执行移动。
