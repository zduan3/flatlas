# 当前阶段 backlog（0.2）

> 状态：计划。当前阶段的待办，各版本内按优先级从上到下排列（不编号，增删时不必重排）；完成后从本文删除条目，并把提案内容并入 `docs/design/`。版本级目标与 0.2.x 的划分见 [路线图](roadmap.md)，当前行为见 [design/](../design/architecture.md)。

## 当前阶段边界

- 0.2 保持只读：没有 plan apply，不删除、移动或创建 hardlink/reflink/symlink，也不持续监听文件系统。
- 所有条目必须保持 [架构不变量](../design/architecture.md#不可破坏的语义)；新 schema 只允许向前 migration 并保留既有数据。
- 0.1 的 JSON 形状、`paths`、`export`、`core.py` 已删除；机器输出以 [查询命令：机器输出](../design/queries.md#机器输出) 为准。
- 除非用户明确要求且设计已更新，不实现 TUI、Rust/PyO3、并行扫描或执行器。

## 0.2.0

- **0.2.0 验收（Windows 与机械硬盘）**
  目标：Windows 原生与 WSL 各跑一次全量 pytest，修复问题并记入 [验证记录](../history/verification-log.md)；中途发现的 Windows 问题已由 `eb8395b`、`b20d227` 修复，其后新增的 `find --format`、元数据测试与 `FLATLAS_DB` / `--no-config`（含 `test_find_records.py`、`test_config.py` 新用例）尚未在 Windows 上运行。另记录机械硬盘性能基线：在 Windows 归档盘（索引放在同一块硬盘上）记录全量扫描、`--refresh-before` 刷新、大子树 `find -prune` 与 `find --format`、`du --files0-from -c --freed` 的耗时与 `scan-info` 分阶段计时。
  原因：0.2 的改动只在 Linux 上验证过，性能只在 Lustre 与本地 SSD 上测过；按 `path_key` 顺序回表等访问模式在硬盘上的代价未知。基线只记录、不设门槛，供 0.2.2 对比。

## 0.2.1

- **只读 Python API（`flatlas.api`）**，CLI 改为其薄层。
- **`scan --shallow --files0-from`**：一次扫描覆盖多个父目录，以重新观察确认删除。
  → [proposals/automation.md](proposals/automation.md#021后续)

## 0.2.2

- **按目录 mtime 的增量刷新**
  目标：目录未变化时跳过 readdir（浅扫描 `--shallow` 已实现，见 [扫描](../design/scanning.md#浅扫描--shallow)）。
  原因：Lustre 上 readdir 约 0.5–1 ms/目录，占全量扫描约三成；定期评估要反复刷新同一棵大树。
  → [proposals/incremental-refresh.md](proposals/incremental-refresh.md)

- **索引存储布局**
  目标：`path_display` 改为由 root 与 `path_key` 推出；需要 migration。
  原因：每行约 960 B、完整路径存三份；480 万条目的索引 4.6 GB，放在 Lustre 上冷读代价高。

- **机械硬盘验收**
  目标：在 0.2.0 基线的同一块硬盘、同一棵目录树上重测同一组操作，确认增量刷新与更小的索引在硬盘上确有收益、查询没有退化，结果记入验证记录。
  原因：这两项降低的正是随机元数据读与索引页读，硬盘上收益应最明显，也最容易暴露访问模式问题。

## 0.2.x

- **扫描报告确认删除的条数**
  目标：`scan` 输出与 `scan.stats_json` 增加本次标记为 deleted 的路径数；`entries_per_s` 注明只按观测到的条目计算，或另给不含 `mark_missing` 的速率。
  原因：清理后重扫时，这个数正是要核对的结果，现在只能对比前后两次 `du`；一次删除 368 万条目后的重扫只观测到 23 万条目，报告 336 条目/s，而时间主要花在没有计数的 `mark_missing` 上（[扫描](../design/scanning.md#元数据成本)）。

- **清理 tombstone 与压缩索引**
  目标：只移除某个现存目录下 `state='deleted'` 的后代、保留 present 行（如 `rm --deleted PATH`），以及按需回收空间的 `VACUUM` 入口或说明。依赖 [待决策清单](decisions.md#索引与历史) 中 deleted 记录的保留策略。
  原因：现在的 `rm` 只能删整棵子树，要清 tombstone 得先拿到被删路径的清单；扫描确认的大批删除留下的 tombstone 会一直占索引，后续完整扫描的删除确认也要走过它们。删行后 SQLite 文件不缩小。

- **`rm` 帮助文字**
  目标：说明单个 PATH 同样立即删除，并给出先用 `du PATH` 查看规模的提示。
  原因：帮助中「One PATH prints that path's counts」容易被读成只显示计数的预览。

- **重复目录树候选**
  目标：只读索引，按目录内相对路径与 size 的集合找出结构相同或几乎相同的目录树，按组报告条目数与字节，作为 inode 清理的候选（不是删除授权）。需要独立设计：相似度定义、规模阈值、与文件级 `dupes` 的关系。
  原因：一次按 inode 的清理中，95% 的条目是同一份源码树的约两百份副本（构建快照与测试运行冻结的源码，每份 1.4 万–14 万个文件，目录名是 `src`、`source` 等通用词）；按名字的候选完全找不到，文件级 `dupes` 也不直接给出「这些整棵目录是同一份」。

- **lazy-hash 后续能力**：有界并发、`--cached` / `--rehash`、hash 运行审计、多 PATH 与 include/exclude。
  → [proposals/lazy-hash.md](proposals/lazy-hash.md)

- **query-cli GNU 兼容批次**：交互用 `ls` / `du` / `df` 参数，`coverage`、`errors` 诊断命令。机器输出部分已由 0.2.0 的约定取代。
  → [proposals/query-cli.md](proposals/query-cli.md)

- **跨平台与数据真实性**：Linux 非 UTF-8 路径、Windows 长路径与 reparse tag、跨 mount/volume 策略、allocated size 平台校准；hardlink、稀疏、压缩、reflink 与索引范围外 alias 的保守表达。
  → [路线图](roadmap.md)、[待决策清单](decisions.md)
