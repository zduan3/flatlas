# 当前阶段 backlog（0.2）

> 状态：计划。当前阶段按优先级排列的待办；完成后从本文删除条目，并把提案内容并入 `docs/design/`。版本级目标与 0.2.x 的划分见 [路线图](roadmap.md)，当前行为见 [design/](../design/architecture.md)。

## 当前阶段边界

- 0.2 保持只读：没有 plan apply，不删除、移动或创建 hardlink/reflink/symlink，也不持续监听文件系统。
- 所有条目必须保持 [架构不变量](../design/architecture.md#不可破坏的语义)；新 schema 只允许向前 migration 并保留既有数据。
- 0.1 的 JSON 形状与 `core.py` 不保留；机器输出以 [proposals/automation.md](proposals/automation.md) 为准。
- 除非用户明确要求且设计已更新，不实现 TUI、Rust/PyO3、并行扫描或执行器。

## 0.2.0

1. **机器输出约定**
   目标：返回多行的命令支持 `jsonl`/`csv`，统一条目记录字段（绝对 `path`、`kind`、`status`、大小、`files`/`entries`、时间、`complete_through_ns`），机器格式无隐式上限，统一退出码。
   原因：同名字段在各命令含义不同、默认上限静默截断、整数组 JSON，脚本必须固定工作目录并缓冲全部结果。
   → [proposals/automation.md](proposals/automation.md#020新约定)

2. **`find` 输出条目记录与新测试**
   目标：`find --format jsonl|csv|table`；`-size`、`-entries`、`-mtime`、`-status`。
   原因：概览与候选统计目前要多条命令、多次重开索引；一条 `find` 即可给出每个目录的子树字节与条目数。
   → [proposals/automation.md](proposals/automation.md#find-作为自动化入口)

3. **删除 0.1 残留**
   目标：删除 `paths`、`export` 命令组与 `core.py`；测试直接引用模块。
   → [proposals/automation.md](proposals/automation.md#删除的-01-残留)

4. **索引位置与配置隔离**
   目标：`FLATLAS_DB`；`FLATLAS_NO_CONFIG` / `--no-config`。
   原因：agent 沙箱内 `~/.config` 只读；用户默认参数会改变脚本中 scan 的行为。
   → [proposals/automation.md](proposals/automation.md#索引位置与配置隔离)

5. **README 自动化配方与验收**
   目标：根 README「脚本与自动化」一节；按提案验收，用新接口重新生成按目录的汇总表。

## 0.2.1

6. **只读 Python API（`flatlas.api`）**，CLI 改为其薄层。
7. **`scan --shallow --files0-from`**：一次扫描覆盖多个父目录，以重新观察确认删除。
   → [proposals/automation.md](proposals/automation.md#021后续)

## 0.2.2

8. **按目录 mtime 的增量刷新**
   目标：目录未变化时跳过 readdir（浅扫描 `--shallow` 已实现，见 [扫描](../design/scanning.md#浅扫描--shallow)）。
   原因：Lustre 上 readdir 约 0.5–1 ms/目录，占全量扫描约三成；定期评估要反复刷新同一棵大树。
   → [proposals/incremental-refresh.md](proposals/incremental-refresh.md)

9. **索引存储布局**
   目标：`path_display` 改为由 root 与 `path_key` 推出；需要 migration。
   原因：每行约 960 B、完整路径存三份；480 万条目的索引 4.6 GB，放在 Lustre 上冷读代价高。

## 0.2.x

10. **lazy-hash 后续能力**：有界并发、`--cached` / `--rehash`、hash 运行审计、多 PATH 与 include/exclude。
    → [proposals/lazy-hash.md](proposals/lazy-hash.md)

11. **query-cli GNU 兼容批次**：交互用 `ls` / `du` / `df` 参数，`coverage`、`errors` 诊断命令。机器输出部分已由 0.2.0 的约定取代。
    → [proposals/query-cli.md](proposals/query-cli.md)

12. **跨平台与数据真实性**：Linux 非 UTF-8 路径、Windows 长路径与 reparse tag、跨 mount/volume 策略、allocated size 平台校准；hardlink、稀疏、压缩、reflink 与索引范围外 alias 的保守表达。
    → [路线图](roadmap.md)、[待决策清单](decisions.md)
