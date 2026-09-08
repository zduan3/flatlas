# 阶段路线图

> 状态：路线图。描述后续阶段与优先级，不代表当前已实现功能。

## 产品优先级

File Atlas 首先服务于寻找大文件、大目录和重复内容，以减少存储占用。它不以复刻 ncdu 的交互式空间浏览为首要目标，而是用持久化索引补充重复文件识别、离线查询和局部子树更新。名称/路径查找属于附加能力，不能优先于 `du`、`largest`、`dupes`、局部扫描正确性和结果可信度。

## MVP（版本 0.1）：只读清理指导基线（当前）

本路线图中的“MVP”和“0.1”指同一发布范围，不是两个阶段；后续版本从 0.2 继续演进。

- 面向不跨文件系统的普通用户目录或归档目录，提供全量 metadata 扫描与局部子树更新。
- SQLite/文件系统查询：`roots` / `df`、`paths`、`ls`、`du`、带 PATH scope 的 `largest` 和基础 `dupes`。
- `ls` 完成一层直接文件/目录/特殊条目视图：文件显示自身 logical size，目录显示递归 indexed logical size，`part` / `gone` 保留状态和可用的最后已知聚合；`du` 保持显式 scope 的递归汇总职责。
- metadata-only scan；`dupes` 在查询范围内执行 size → quick hash → BLAKE3 full hash 惰性流水线并复用缓存。
- JSON/CSV 导出；可保留仅供审查的 dry-run plan，但没有 apply。
- 输出用于定位空间大户和重复内容候选，不构成删除或替换授权。
- 实际去重由用户使用 jdupes 对选定目录重新扫描、逐字节验证并显式执行。
- 0.1 发布验收覆盖 Windows/Linux 的常规 Unicode 路径；非 UTF-8、完整长路径/reparse tag、跨文件系统和高级物理空间语义后置。

版本验收：在中断扫描或目录遍历错误后，不会错误标记未覆盖路径为删除；外部删除只有 completed 覆盖扫描才能持久化为 deleted；`ls`、`du`、`largest PATH` 和 `dupes PATH` 能提供稳定清理指导；已缓存结果可在源目录离线后查询；所有命令保持只读。

## 版本 0.2：Python 索引与查询完善

- 优先强化 `dupes` 的理论可节省量排序/汇总、范围和阈值过滤、hardlink 去重、completeness 与稳定导出。
- 强化 `du` 的直接子项/深度展开，以及 `du` / `largest` 的 metric、top N、排序和阈值能力，服务大目录与大文件定位。
- 完善局部子树更新、当前状态校验和 stale hash 重算，并以最小的 `coverage`、`errors`、scan 摘要解释结果可信度。
- 配置化 include/exclude、大小与时间过滤。
- 按 [查询命令设计](query-cli-design.md) 逐步增加 GNU 风格常用参数；`stat`、`find`、`locate` 等通用查找便利功能后置。
- dirty state 与历史结果保留策略。
- 基准测试与故障注入测试。
- hash 并发、强制重算/只用缓存模式、独立运行审计、更多范围与阈值过滤，详见 [惰性 hash 后续设计](lazy-hash-future-design.md)。
- 补齐 Linux 非 UTF-8、Windows 长路径/reparse tag、跨 mount/volume 策略和 allocated size 平台校准。

## 版本 0.3：安全执行器设计与验证

- 定义逐字节复核、执行前路径/对象身份/metadata/hash 重验和同文件系统检查。
- 定义明确审批、幂等、跳过、重试、审计和崩溃恢复语义。
- 在实现任何 apply 前完成独立威胁模型、设计评审和故障注入验收。

## 阶段 4：Rust 扫描与 hash 后端

- 独立 `flatlas-core` 与 sidecar executable。
- 与 Python backend 对照同一测试语料和规范化 SQLite 结果。
- 加入有界并发、取消、进度统计和性能基准。

## 阶段 5：安全去重执行器

- plan 在 apply 前重新验证文件身份、metadata 与内容。
- 支持 dry-run、跳过、重试、审计和崩溃恢复。
- 首先实现 hardlink；reflink 与 delete 在独立能力探测、测试与策略明确后加入。

## 阶段 6：持续增量与体验

- 事件驱动 dirty queue 与定期 reconcile。
- 可选 TUI、shell completion、稳定 JSON 协议与发行打包。
