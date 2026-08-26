# 阶段路线图

## 产品优先级

File Atlas 首先服务于寻找大文件、大目录和重复内容，以减少存储占用。它不以复刻 ncdu 的交互式空间浏览为首要目标，而是用持久化索引补充重复文件识别、离线查询和局部子树更新。名称/路径查找属于附加能力，不能优先于 `du`、`largest`、`dupes`、局部扫描正确性和结果可信度。

## 阶段 0：设计与实验（当前）

- 固化术语、范围、失败语义和测试矩阵。
- 决定 SQLite 当前状态表与历史 observation 的边界。
- 设计 scan coverage、hash state、plan 与 operation 的 schema 草案。
- 创建包含非 ASCII 路径、已有 hardlink、权限错误、扫描中改名和稀疏文件的测试语料规范。

## 阶段 1：纯 Python 只读 MVP

- 全量 metadata 扫描与局部子树更新。
- SQLite/文件系统查询：`roots` / `df`、`ls`、`du`、`largest`、基础 `dupes`。
- size → quick hash → BLAKE3 full hash 流水线。
- JSON/CSV 导出与 dry-run plan。

阶段验收：在中断扫描或目录遍历错误后，不会错误标记未覆盖路径为删除；重复组可在原目录离线后从数据库查询。

## 阶段 2：Python 索引完善

- 优先强化 `dupes` 的理论可节省量排序/汇总、范围和阈值过滤、hardlink 去重、completeness 与稳定导出。
- 强化 `du` / `largest` 的 metric、top N、排序、深度和阈值能力，服务大目录与大文件定位。
- 完善局部子树更新、当前状态校验和 stale hash 重算，并以最小的 `coverage`、`errors`、scan 摘要解释结果可信度。
- 配置化 include/exclude、大小与时间过滤。
- 按 [查询命令设计](query-cli-design.md) 逐步增加 GNU 风格常用参数；`stat`、`find`、`locate` 等通用查找便利功能后置。
- dirty state 与历史结果保留策略。
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
