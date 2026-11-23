# 当前阶段 backlog（0.2）

> 状态：计划。当前阶段按优先级排列的待办；完成后从本文删除条目，并把提案内容并入 `docs/design/`。版本级目标见 [路线图](roadmap.md)，当前行为见 [design/](../design/architecture.md)。

## 当前阶段边界

- 0.2 保持只读：没有 plan apply，不删除、移动或创建 hardlink/reflink/symlink，也不持续监听文件系统。
- 所有条目必须保持 [架构不变量](../design/architecture.md#不可破坏的语义)；新 schema 只允许向前 migration 并保留既有数据。
- 除非用户明确要求且设计已更新，不实现 TUI、Rust/PyO3 或执行器。

## 待办（按优先级）

1. **扫描短事务 checkpoint 与崩溃恢复**
   目标：扫描按批次提交并持久化工作队列，崩溃或 `SIGKILL` 后可续扫。
   原因：当前单事务扫描在收尾阶段中断、崩溃或调度器宽限期后被杀时整体回滚，长扫描的全部观测丢失。
   → [proposals/scan-checkpoint.md](proposals/scan-checkpoint.md)

2. **按目录 mtime 的增量刷新**
   目标：目录未变化时跳过 readdir（浅扫描 `--shallow` 已实现，见 [扫描](../design/scanning.md#浅扫描--shallow)）。
   原因：大树上刷新仍需完整 readdir；分阶段计时显示 Lustre 上 readdir 约 1 ms/目录。
   → [proposals/incremental-refresh.md](proposals/incremental-refresh.md)

3. **lazy-hash 后续能力**
   目标：有界并发、`--cached` / `--rehash`、hash 运行审计、多 PATH 与 include/exclude。
   原因：大语料上 hash 串行读取慢，且 hash 结果缺少可审计的运行记录。
   → [proposals/lazy-hash.md](proposals/lazy-hash.md)

4. **query-cli GNU 兼容批次**
   目标：按批次增加 `ls` / `du` / `df` 常用 GNU 参数，以及 `coverage`、`errors` 等可信度诊断命令。
   原因：降低使用门槛，并解释结果何时完整、何时不完整。
   → [proposals/query-cli.md](proposals/query-cli.md)

5. **跨平台与数据真实性**
   目标：Linux 非 UTF-8 路径、Windows 长路径与 reparse tag、跨 mount/volume 策略、allocated size 平台校准；hardlink、稀疏、压缩、reflink 与索引范围外 alias 的保守表达。
   原因：这些情况目前不在验收范围内，可能导致路径无法表示或物理节省量被夸大。
   → [路线图](roadmap.md)、[待决策清单](decisions.md)
