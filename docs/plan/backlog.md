# 当前阶段 backlog（0.2）

> 状态：计划。当前阶段的待办，各版本内按优先级从上到下排列（不编号，增删时不必重排）；完成后从本文删除条目，并把提案内容并入 `docs/design/`。版本级目标与 0.2.x 的划分见 [路线图](roadmap.md)，当前行为见 [design/](../design/architecture.md)。

## 当前阶段边界

- 0.2 保持只读：没有 plan apply，不删除、移动或创建 hardlink/reflink/symlink，也不持续监听文件系统。
- 所有条目必须保持 [架构不变量](../design/architecture.md#不可破坏的语义)；新 schema 只允许向前 migration 并保留既有数据。
- 0.1 的 JSON 形状、`paths`、`export`、`core.py` 已删除；机器输出以 [查询命令：机器输出](../design/queries.md#机器输出) 为准。
- 除非用户明确要求且设计已更新，不实现 TUI、Rust/PyO3、并行扫描或执行器。

## 0.2.1

机械硬盘基线与可选批量浅扫描见下列待办。未定案的后续 schema 需求见 [数据库修改计划](#数据库修改计划)。

- **机械硬盘基线与复测**：在具备 Windows 机械硬盘时，检出 `v0.2.0` 对一块真实目录树记录一次全量 `scan`、一次无变化 `--refresh-before`、一次大子树 `find --format jsonl` 的耗时、`scan-info` 分阶段计时与 `.db` 文件大小；0.2.1 完成后在同一块盘与同一目录复跑对比，结果记入 [验证记录](../history/verification-log.md)。
- **`scan --shallow --files0-from`（按需可选）**：一次调用复用连接，按 NUL 分隔的清单顺序浅扫多个父目录。每个目录各有独立的 scan 与 scope 并独立提交，只有 completed scope 才确认缺失的直接条目及其后代。若日常清理以清单 + `rm --files0-from` 为主可后置。

## 0.2.x

本批条目**仍需实际清理经验指导设计**，先在真实任务中验证需求与边界再写实现；待观察的问题见 [待实操回答的问题](decisions.md#待实操回答的问题)：

- **重复目录树候选**：只读索引，按目录内相对路径与 size 找出结构相同或几乎相同的目录树副本，作为 inode 清理候选（不是删除授权）。建议先用 `flatlas.api` 在真实索引上跑脚本原型，再沉淀相似度与折叠规则。
- **lazy-hash 后续能力**：评估 full hash 有界并发、`--cached` / `--rehash`、多 PATH 与 include/exclude。→ [proposals/lazy-hash.md](proposals/lazy-hash.md)
- **query-cli GNU 兼容批次**：交互用 `ls` / `du` / `df` 参数，`coverage`、`errors` 诊断命令；按实际缺口补齐，不照表全量实现。→ [proposals/query-cli.md](proposals/query-cli.md)
- **跨平台与数据真实性**：Linux 非 UTF-8 路径、Windows 长路径的现场查询及 hash 支持、reparse tag、跨 mount/volume 策略、allocated size 平台校准及复杂物理空间语义的保守表达。→ [路线图](roadmap.md)、[待决策清单](decisions.md)

## 数据库修改计划

以下为 [schema v8](../design/schema.md) 之后各项待办触发迁移的条件。

| 功能 | 结构影响与迁移安排 |
|---|---|
| [Hash 调度、缓存控制与版本化](proposals/lazy-hash.md) | 有界并发、复用已提交的缓存继续处理、`--cached` / `--rehash`、多 PATH 和查询过滤沿用现有结构。采样策略可通过 `quick_algorithm` 标识区分布局版本，并继续校验 `quick_sample_bytes`；若需要持久化可恢复任务队列或同一文件的多个算法结果，再设计后续迁移。 |
| [查询与覆盖诊断](proposals/query-cli.md) | GNU 参数、`coverage` 和 `errors` 的首期查询复用 `path`、`directory_summary`、`scan`、`scan_scope` 和 `scan_error`。若实测需要持久缓存 `du` 的对象去重聚合，再设计迁移，并验证重叠 scope 与跨目录 hardlink 的计数。 |
| [跨平台与数据真实性](decisions.md#扫描与过滤) | Linux 非 UTF-8 与 Windows 长路径先沿用 BLOB 路径模型；reparse tag、卷标识和单条路径的占用字节已有 `path.reparse_tag`、`root.namespace_id_raw`、`path.allocated_size`。若最终策略要求独立记录 mount/volume 别名、跨边界的覆盖关系、共享 extent 或占用估算来源，再评估后续迁移。卷标识参与 namespace 查找时需验证旧 root 与 coverage 的归属。 |
| 重复目录树候选 | 首版按相对路径与大小从现有索引计算。只有实测表明需要持久化目录树指纹时，才考虑新增派生缓存，并定义扫描、索引移除和算法版本变化时的失效规则。 |
| [过滤扫描、dirty 状态与历史对比](decisions.md#索引与历史) | 查询过滤、按 `deleted_at_ns` 实施 tombstone 保留策略及外部 JSONL 对比可沿用现有结构。若定案要求持久化可查询的过滤盲区或 dirty 路径、或在库内比较历史 scan，则评估新增覆盖明细、待刷新记录或独立 snapshot 表；历史记录从功能启用后开始积累。 |
| 机械硬盘基线与复测、可选批量浅扫描 | 基线和复测使用现有统计；批量浅扫描为每个目录复用已有的 `scan.max_depth` 与独立 `scan_scope`。两项均可沿用现有结构。 |

各次迁移沿用现有向前迁移机制，保留 root/path 身份及既有扫描、覆盖、错误、hash 和 plan 数据，并用旧库升级用例验证数据保留与失败回滚。0.2.x 的结构改动待功能定案后随对应版本安排。
