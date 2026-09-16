# 0.2 开发阶段总览

> 状态：当前开发阶段。本文汇总计划范围；除明确标记为已实现的内容外，不代表当前 CLI 已支持。

File Atlas 当前开发版本为 0.2.0。0.1 是已完成并冻结的只读 MVP 基线；0.2 继续保持不修改用户文件系统的边界，重点完善 Python 索引、查询体验、结果可信度与惰性 hash。0.2 不提供 plan apply，也不删除、移动或替换用户文件。

## 计划能力摘要

### 空间与重复候选查询

- 为 `du` 增加直接子项和深度展开，并完善 logical/allocated metric、top N、排序、阈值、总计与多路径行为。
- 为 `largest` 增加 metric、排序、阈值和结果数量控制。
- 为 `dupes` 增加理论节省量排序与汇总、大小/组数/节省量阈值、多 PATH 范围、include/exclude 过滤、hardlink alias 识别和 completeness。
- 明确 logical size、allocated size、路径冗余、理论节省量与实际物理可回收空间的区别；未知值不得伪装为 0。

### GNU 风格查询体验

- 逐步为 `ls`、`du`、`roots` / `df` 增加常用 GNU 风格参数，同时保持 registered namespace、离线索引和 coverage 语义。
- 稳定表格、JSON 与 CSV 输出；显示缩放不得改变机器输出中的原始整数和字段语义。
- 优先规划 `coverage`、`errors`、`scans` / `scan-info`，用于解释扫描范围、错误和结果完整性。
- 后续按优先级评估只读的 `hash`、`verify`、`tree`、`stat`、`find`、`locate`、`readlink` 和 `findmnt`；名称查找不优先于空间回收主线。

详细参数和实现顺序见 [查询命令规划](query-cli.md)。

### 扫描、coverage 与中断恢复

- 完善局部子树更新、当前状态校验、dirty state 和历史结果保留策略。
- 通过最小 `coverage`、`errors` 与 scan 摘要说明 completed、partial、failed、cancelled 和 unscanned 状态。
- 扫描使用短事务 checkpoint，持久化已保存的 path、metadata、文件数、目录数和 logical bytes。
- 用户中断时将 scan/scope 持久化为 `cancelled` 并保留已 checkpoint 结果；只有 completed scope 才能在最终原子事务中确认缺失路径为 `deleted`。
- 为崩溃后遗留的 running scan 设计安全恢复或租约机制，不得盲目改写可能仍活跃的任务。

详细设计见 [扫描中断规划](scan-interruption.md)。

### 惰性 hash、性能与可观测性

- 加入保守的有界并发，分别约束文件打开、读取任务和 SQLite 写入批次，并针对 HDD、SSD 与网络盘校准默认值。
- 增加 `--cached` 与 `--rehash`，支持后台 hash、取消恢复、算法与 quick 采样策略版本化。
- 建立独立 hash run/error 审计，记录 scope、算法版本、候选数、读取字节、cache hit、changed、failed、cancelled 和耗时。
- 保持 metadata basis 的保守校验；身份、size、mtime 或可靠 change/birth time 不匹配时使缓存 stale 并重新计算。
- 建立大文件数、同尺寸高碰撞和大文件语料的基准测试与故障注入测试。

详细设计见 [惰性 Hash 规划](lazy-hash.md)。

### 跨平台与数据真实性

- 补齐 Linux 非 UTF-8 路径、Windows 长路径与 reparse tag、跨 mount/volume 策略和 allocated size 平台校准。
- hardlink、稀疏文件、压缩文件、reflink/shared extent 与索引范围外 alias 必须保守表达，不能夸大物理节省量。
- 新 schema 只允许向前 migration，必须保留现有 root、path、scan、coverage、error、hash 与 plan 数据。

## 不变边界

- `root` 仍是内部 scan namespace；`init` 只登记，不扫描。
- partial、failed、cancelled、过滤、权限错误、I/O 错误或 hash 错误均不得被解释为路径已删除。
- duplicate group 仍只使用 `present`、普通文件且 full hash 有效的记录。
- 0.2 仍为只读阶段：没有 apply，不删除、移动或创建 hardlink/reflink/symlink。
- 任何未来修改文件系统的执行器都必须另行完成逐字节复核、执行前重验、审批、审计和恢复设计。

版本级优先级见 [阶段路线图](../roadmap.md)，已交付行为仍以 [0.1 实现状态](../0.1/implementation.md) 为准。
