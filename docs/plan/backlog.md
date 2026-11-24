# 当前阶段 backlog（0.2）

> 状态：计划。当前阶段的待办，各版本内按优先级从上到下排列（不编号，增删时不必重排）；完成后从本文删除条目，并把提案内容并入 `docs/design/`。版本级目标与 0.2.x 的划分见 [路线图](roadmap.md)，当前行为见 [design/](../design/architecture.md)。

## 当前阶段边界

- 0.2 保持只读：没有 plan apply，不删除、移动或创建 hardlink/reflink/symlink，也不持续监听文件系统。
- 所有条目必须保持 [架构不变量](../design/architecture.md#不可破坏的语义)；新 schema 只允许向前 migration 并保留既有数据。
- 0.1 的 JSON 形状、`paths`、`export`、`core.py` 已删除；机器输出以 [查询命令：机器输出](../design/queries.md#机器输出) 为准。
- 除非用户明确要求且设计已更新，不实现 TUI、Rust/PyO3、并行扫描或执行器。

## 0.2.0

- **Windows 中途验证**
  目标：在 Windows 原生与 WSL 上各跑一次全量 pytest，修复问题并记入 [验证记录](../history/verification-log.md)。
  原因：自 `fix(du)` `12e3ce1` 之后的提交只在 Linux 验证过，其中涉及平台的有：查询输出直接写 `sys.stdout`（换行）、`realpath` 前缀记忆在 Windows 上回退 `Path.resolve()`、扫描由 `fsencode(name)` 拼接子条目 key、跨盘符的相对显示；`test_allocation.py` 中仅 Windows 运行的用例改为 `jsonl` 后未实际运行。在 `find --format` 之前清掉平台问题，之后出错容易定位。

- **索引位置与配置隔离**
  目标：`FLATLAS_DB`；`FLATLAS_NO_CONFIG` / `--no-config`。
  原因：agent 沙箱内 `~/.config` 只读；用户默认参数会改变脚本中 scan 的行为。
  → [proposals/automation.md](proposals/automation.md#索引位置与配置隔离)

- **README 自动化配方与 0.2.0 验收**
  目标：根 README「脚本与自动化」一节；按提案验收，用新接口重新生成按目录的汇总表。
  验收另含：Windows 原生与 WSL 全量 pytest，结果记入验证记录；机械硬盘性能基线——在 Windows 归档盘（索引放在同一块硬盘上）记录全量扫描、`--refresh-before` 刷新、大子树 `find -prune`、`du --files0-from -c --freed` 的耗时与 `scan-info` 分阶段计时。基线只记录、不设门槛：0.2 的性能改动只在 Lustre 与本地 SSD 上测过，按 `path_key` 顺序回表等访问模式在硬盘上的代价未知。

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

- **lazy-hash 后续能力**：有界并发、`--cached` / `--rehash`、hash 运行审计、多 PATH 与 include/exclude。
  → [proposals/lazy-hash.md](proposals/lazy-hash.md)

- **query-cli GNU 兼容批次**：交互用 `ls` / `du` / `df` 参数，`coverage`、`errors` 诊断命令。机器输出部分已由 0.2.0 的约定取代。
  → [proposals/query-cli.md](proposals/query-cli.md)

- **跨平台与数据真实性**：Linux 非 UTF-8 路径、Windows 长路径与 reparse tag、跨 mount/volume 策略、allocated size 平台校准；hardlink、稀疏、压缩、reflink 与索引范围外 alias 的保守表达。
  → [路线图](roadmap.md)、[待决策清单](decisions.md)
