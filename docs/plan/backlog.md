# 当前阶段 backlog（0.2）

> 状态：计划。当前阶段的待办，各版本内按优先级从上到下排列（不编号，增删时不必重排）；完成后从本文删除条目，并把提案内容并入 `docs/design/`。版本级目标与 0.2.x 的划分见 [路线图](roadmap.md)，当前行为见 [design/](../design/architecture.md)。

## 当前阶段边界

- 0.2 保持只读：没有 plan apply，不删除、移动或创建 hardlink/reflink/symlink，也不持续监听文件系统。
- 所有条目必须保持 [架构不变量](../design/architecture.md#不可破坏的语义)；新 schema 只允许向前 migration 并保留既有数据。
- 0.1 的 JSON 形状、`paths`、`export`、`core.py` 已删除；机器输出以 [查询命令：机器输出](../design/queries.md#机器输出) 为准。
- 除非用户明确要求且设计已更新，不实现 TUI、Rust/PyO3、并行扫描或执行器。

## 0.2.0

Windows 原生与 WSL 发布验收已记入 [验证记录](../history/verification-log.md#020-发布验收2026-11-25)。

### 发布后采集机械硬盘基线

0.2.0 发布后，在具备 Windows 机械硬盘的环境检出 `v0.2.0` 采集基线，供 0.2.2 对比。优先观察重复刷新中的目录枚举和大子树查询首次运行时的索引页读取。源目录与 SQLite 索引放在同一块机械盘上，查询输出写到另一块盘。记录受测提交、盘型号及连接方式、文件系统、目录和条目数，以及完整命令、扫描范围、刷新 cutoff 与输入清单。

最小测试清单：

- **已有索引的刷新**。从相同的索引起点分别测试目录树无变化、少量目录有增删两种情形。用 `--refresh-before` 选中相同的旧目录范围，记录总耗时及 `readdir`、`observe`、`refresh_query`、`db_write` 的耗时和调用次数，并核对更新后的条目数与 coverage。
- **大子树查询**。运行 `find -prune`、`find --format jsonl` 和固定清单的 `du --files0-from -c --freed`。分别记录首次运行耗时及三次连续运行的中位数，核对输出行数和关键汇总。记录首次运行前的缓存准备方法；仅重启进程时仍称为首次运行。
- **新索引全量扫描**。从空索引扫描同一棵树，记录总耗时、条目数和完成后的索引体积。用 `scan-info ID --format json` 保存分阶段计时，重点看 `readdir`、`observe`、`db_write`、`commit` 和 `summaries`。刷新同样保存该输出。

保留 0.2.0 的原始索引和查询结果。0.2.2 使用独立索引，在同一块盘、同一棵树和相同输入下复测。若补测大批外部删除，在可丢弃副本上记录 `mark_missing` 和实际缺失规模，单列结果。基线与复测结果记入 [验证记录](../history/verification-log.md)。

## 0.2.1

- **只读 Python API（`flatlas.api`）**：一次打开索引、多次查询，默认以 SQLite 只读模式打开。首批覆盖 `find`、`du`、`largest`、`roots`、`scans`、`scan_info`，与 CLI 共用查询逻辑和机器记录约定。
  → [Python API 契约](proposals/automation.md#只读-python-api)

- **`scan --shallow --files0-from`**：一次 CLI 调用处理多个父目录，每个父目录独立记录 `scan_id` 和 `scope_id`。批次报告逐目录结果，只有 completed scope 才确认删除。
  → [批量浅扫描](proposals/automation.md#批量浅扫描)

- **扫描报告确认删除的条数**：`scan` 输出和 `scan.stats_json` 增加本次实际从 `present` 转为 `deleted` 的路径数。`entries_per_s` 说明按观测到的条目计算，便于核对重扫结果和写入成本。
  → [删除计数](proposals/automation.md#删除计数与-rm-帮助)

- **`rm` 帮助文字**：明确单个 PATH 也会立即移除索引中的对应子树，并提示先用 `du PATH` 查看规模。

- **schema v7 无迁移验收**：在 Windows 原生与 WSL 上，用 0.2.0 生成的 v7 索引验证只读打开、API 与 CLI 的记录一致性、批量浅扫的删除边界和历史扫描的读取。0.2.1 沿用 schema v7。
  → [发布验收](proposals/automation.md#schema-v7-无迁移验收)

## 0.2.2

- **按目录 mtime 的增量刷新**
  目标：目录未变化时跳过 readdir（浅扫描 `--shallow` 已实现，见 [扫描](../design/scanning.md#浅扫描--shallow)）。
  原因：Lustre 上 readdir 约 0.5–1 ms/目录，占全量扫描约三成；定期评估要反复刷新同一棵大树。
  → [proposals/incremental-refresh.md](proposals/incremental-refresh.md)

- **索引存储布局**
  目标：`path_display` 改为由 root 与 `path_key` 推出；需要 migration。
  原因：每行约 960 B、完整路径存三份；480 万条目的索引 4.6 GB，放在 Lustre 上冷读代价高。

- **机械硬盘验收**
  目标：按 [0.2.0 发布后采集的机械硬盘基线](#发布后采集机械硬盘基线) 重测，核对增量刷新减少的枚举次数、索引体积与查询耗时，并验证查询结果一致。
  原因：这两项降低的正是随机元数据读与索引页读，硬盘上收益应最明显，也最容易暴露访问模式问题。

## 0.2.x

- **清理 tombstone 与压缩索引**
  目标：只移除某个现存目录下 `state='deleted'` 的后代、保留 present 行（如 `rm --deleted PATH`），以及按需回收空间的 `VACUUM` 入口或说明。依赖 [待决策清单](decisions.md#索引与历史) 中 deleted 记录的保留策略。
  原因：现在的 `rm` 只能删整棵子树，要清 tombstone 得先拿到被删路径的清单；扫描确认的大批删除留下的 tombstone 会一直占索引，后续完整扫描的删除确认也要走过它们。删行后 SQLite 文件不缩小。

- **重复目录树候选**
  目标：只读索引，按目录内相对路径与 size 的集合找出结构相同或几乎相同的目录树，按组报告条目数与字节，作为 inode 清理的候选（不是删除授权）。需要独立设计：相似度定义、规模阈值、与文件级 `dupes` 的关系。
  原因：一次按 inode 的清理中，95% 的条目是同一份源码树的约两百份副本（构建快照与测试运行冻结的源码，每份 1.4 万–14 万个文件，目录名是 `src`、`source` 等通用词）；按名字的候选完全找不到，文件级 `dupes` 也不直接给出「这些整棵目录是同一份」。

- **lazy-hash 后续能力**：有界并发、`--cached` / `--rehash`、hash 运行审计、多 PATH 与 include/exclude。
  → [proposals/lazy-hash.md](proposals/lazy-hash.md)

- **query-cli GNU 兼容批次**：交互用 `ls` / `du` / `df` 参数，`coverage`、`errors` 诊断命令。机器输出部分已由 0.2.0 的约定取代。
  → [proposals/query-cli.md](proposals/query-cli.md)

- **跨平台与数据真实性**：Linux 非 UTF-8 路径、Windows 长路径与 reparse tag、跨 mount/volume 策略、allocated size 平台校准；hardlink、稀疏、压缩、reflink 与索引范围外 alias 的保守表达。
  → [路线图](roadmap.md)、[待决策清单](decisions.md)
