# 当前阶段 backlog（0.2）

> 状态：计划。当前阶段的待办，各版本内按优先级从上到下排列（不编号，增删时不必重排）；完成后从本文删除条目，并把提案内容并入 `docs/design/`。版本级目标与 0.2.x 的划分见 [路线图](roadmap.md)，当前行为见 [design/](../design/architecture.md)。

## 当前阶段边界

- 0.2 保持只读：没有 plan apply，不删除、移动或创建 hardlink/reflink/symlink，也不持续监听文件系统。
- 所有条目必须保持 [架构不变量](../design/architecture.md#不可破坏的语义)；新 schema 只允许向前 migration 并保留既有数据。
- 0.1 的 JSON 形状、`paths`、`export`、`core.py` 已删除；机器输出以 [查询命令：机器输出](../design/queries.md#机器输出) 为准。
- 除非用户明确要求且设计已更新，不实现 TUI、Rust/PyO3、并行扫描或执行器。

## 0.2.1

0.2.0 已于 `v0.2.0` 发布（验收见 [验证记录](../history/verification-log.md#020-发布验收2026-11-25)）。本批条目**实现明确、不改 schema（沿用 v7）**，适合在下次实际清理前直接落地：

- **扫描报告确认删除的条数**：`scan` 输出与 `scan.stats_json` 增加 `paths_deleted`，说明 `entries_per_s` 按观测条目计算。→ [proposals/automation.md](proposals/automation.md#删除计数与-rm-帮助)
- **`rm` 帮助文字**：明确单个 PATH 同样立即移除索引子树，并提示先用 `du PATH` 查看规模。
- **清理 tombstone 与压缩索引**：显式移除现存目录下 `state='deleted'` 的后代（如 `rm --deleted PATH`）、保留 present 行（不重算目录汇总），并补充按需 `VACUUM` 入口或说明。自动保留策略后置，见 [待决策清单](decisions.md#待实操回答的问题)。
- **只读 Python API（`flatlas.api`）**：`with open_index(db)` 复用只读连接执行 `find`/`du`/`largest`/`roots`/`scans`/`scan_info`，返回与 `jsonl` 一致的记录；也便于先用脚本原型验证后续分析需求。→ [proposals/automation.md](proposals/automation.md#只读-python-api)
- **`scan --shallow --files0-from`（按需可选）**：单次调用复用连接顺序浅扫多个父目录，每个父目录仍按独立 completed scope 确认删除；若日常清理以清单 + `rm --files0-from` 为主可后置。→ [proposals/automation.md](proposals/automation.md#批量浅扫描)

## 0.2.2

本批条目**方向明确，但涉及全仓改动、schema 迁移或跨文件系统语义折中**，待取得 `v0.2.0` 机械硬盘基线和刷新实测后再推进：

- **按目录 mtime 的增量刷新**：目录未变化时跳过 `readdir`（浅扫描 `--shallow` 已实现，见 [扫描](../design/scanning.md#浅扫描--shallow)）。→ [proposals/incremental-refresh.md](proposals/incremental-refresh.md)
- **索引存储布局**：`path_display` 改为由 root 与 `path_key` 推导，减少每行重复路径和冷读体积（影响全仓查询与排序，需 migration）。
- **机械硬盘基线与复测**：在具备 Windows 机械硬盘时，检出 `v0.2.0` 对一块真实目录树记录一次全量 `scan`、一次无变化 `--refresh-before`、一次大子树 `find --format jsonl` 的耗时、`scan-info` 分阶段计时与 `.db` 文件大小；0.2.2 在同一块盘与同一目录复跑对比，结果记入 [验证记录](../history/verification-log.md)。

## 0.2.x

本批条目**仍需实际清理经验指导设计**，先在真实任务中验证需求与边界再写实现；待观察的问题见 [待实操回答的问题](decisions.md#待实操回答的问题)：

- **重复目录树候选**：只读索引，按目录内相对路径与 size 找出结构相同或几乎相同的目录树副本，作为 inode 清理候选（不是删除授权）。建议先用 `flatlas.api` 在真实索引上跑脚本原型，再沉淀相似度与折叠规则。
- **lazy-hash 后续能力**：有界并发、`--cached` / `--rehash`、hash 运行审计、多 PATH 与 include/exclude。→ [proposals/lazy-hash.md](proposals/lazy-hash.md)
- **query-cli GNU 兼容批次**：交互用 `ls` / `du` / `df` 参数，`coverage`、`errors` 诊断命令；按实际缺口补齐，不照表全量实现。→ [proposals/query-cli.md](proposals/query-cli.md)
- **跨平台与数据真实性**：Linux 非 UTF-8 路径、Windows 长路径与 reparse tag、跨 mount/volume 策略、allocated size 平台校准及复杂物理空间语义的保守表达。→ [路线图](roadmap.md)、[待决策清单](decisions.md)
