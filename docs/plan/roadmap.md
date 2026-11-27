# 阶段路线图

> 状态：计划。版本级目标的唯一副本；当前开发阶段为 0.2。当前行为见 [design/](../design/architecture.md)，当前阶段待办见 [backlog](backlog.md)。

## 产品优先级

File Atlas 首先服务于寻找大文件、大目录和重复内容，以减少存储占用。它不以复刻 ncdu 的交互式空间浏览为首要目标，而是用持久化索引补充重复文件识别、离线查询和局部子树更新。名称/路径查找属于附加能力，不能优先于 `du`、`largest`、`dupes`、局部扫描正确性和结果可信度。

## MVP（版本 0.1）：只读清理指导基线（已完成）

本路线图中的“MVP”和“0.1”指同一发布范围，不是两个阶段；后续版本从 0.2 继续演进。

- 面向不跨文件系统的普通用户目录或归档目录，提供全量 metadata 扫描与局部子树更新。
- SQLite/文件系统查询：`roots` / `df`、`paths`、`ls`、`du`、带 PATH scope 的 `largest` 和基础 `dupes`。
- `ls` 完成一层直接文件/目录/特殊条目视图：文件显示自身 logical size，目录显示递归 indexed logical size，`part` / `gone` 保留状态和可用的最后已知聚合；`du` 保持显式 scope 的递归汇总职责。
- metadata-only scan；`dupes` 在查询范围内执行 size → quick hash → BLAKE3 full hash 惰性流水线并复用缓存。
- JSON/CSV 导出；可保留仅供审查的 dry-run plan，但没有 apply。
- 输出用于定位空间大户和重复内容候选，不构成删除或替换授权。
- 实际去重由用户使用 jdupes 对选定目录重新扫描、逐字节验证并显式执行。
- 0.1 发布验收覆盖 Windows/Linux 的常规 Unicode 路径；非 UTF-8、完整长路径/reparse tag、跨文件系统和高级物理空间语义后置。

0.1 已冻结：代码基线为 git tag `v0.1.0`，功能范围、验收基线与实现概览分别见 [0.1 功能范围与验收基线](../history/0.1-acceptance.md) 和 [0.1 实现状态](../history/0.1-implementation.md)。

## 版本 0.2：Python 索引与查询完善（当前开发阶段）

在 0.1 的只读基线上完善 Python 索引、查询体验、结果可信度与惰性 hash，仍不修改用户文件系统。阶段目标：

- 大规模与网络文件系统上的扫描性能和可恢复性（lazy 元数据、短事务 checkpoint）。
- `dupes`、`du`、`largest` 的空间回收主线：hardlink 识别、深度展开、top N、metric、排序与阈值、completeness 与稳定导出。
- 局部子树更新、当前状态校验、stale hash 重算、dirty state 与历史结果保留策略，并以最小的 `coverage`、`errors`、scan 摘要解释结果可信度。
- 配置化 include/exclude、大小与时间过滤；hash 并发、强制重算/只用缓存模式与 [`dupes` 本次运行诊断](../design/hashing.md#输出)。
- 基准测试与故障注入测试。
- 补齐 Linux 非 UTF-8、Windows 长路径的现场查询及 hash 支持、reparse tag、跨 mount/volume 策略和 allocated size 平台校准。

0.2 以「自动化存储评估」为主线分批发布。场景来自 Lustre 上百万级条目的存储盘点与清理：后台扫描百万级条目、脚本生成按目录的字节与条目数概览和可重建候选、删除后同步索引并对比前后数值。0.1 的 JSON 形状与 `core.py` 不再保留，不考虑向后兼容。

| 版本 | 目标 | 内容 |
|---|---|---|
| 0.2.0（已发布，`v0.2.0`） | 脚本可以直接依赖的查询接口 | lazy 元数据、checkpoint 续扫、机器输出约定、`find --format`、`du --files0-from`、批量 `rm`、`FLATLAS_DB` / `--no-config` 与双平台验收。见 [发布记录](../history/0.2.0-release.md) |
| 0.2.1（当前） | 清理闭环、脚本复用与降低重复评估成本 | 已落地：[扫描确认删除计数](../design/scanning.md#分阶段计时)、[完整扫描并清理删除记录](../design/scanning.md#完整扫描并清理删除记录)、`rm` 帮助澄清、[按目录清理 tombstone](../design/queries.md#rm---deleted-path) 与 [只读 Python API](../design/queries.md#只读-python-api)、[索引布局与路径外键索引](../design/schema.md#migration-历史)、[按目录变化刷新](../design/scanning.md#按目录变化刷新--refresh-changed)。待推进（见 [backlog](backlog.md#021)）：机械硬盘对比 `v0.2.0` 基线及可选批量浅扫描 |
| 0.2.x | 按需推进的其余 0.2 能力 | 重复目录树候选、lazy-hash 后续能力（[proposals/lazy-hash.md](proposals/lazy-hash.md)）、交互用 GNU 参数（[proposals/query-cli.md](proposals/query-cli.md)）、跨平台与数据真实性 |

按优先级排列的具体条目见 [backlog](backlog.md)。

## 版本 0.3：安全执行器设计与验证

- 定义逐字节复核、执行前路径/对象身份/metadata/hash 重验和同文件系统检查。
- 定义明确审批、幂等、跳过、重试、审计和崩溃恢复语义。
- 在实现任何 apply 前完成独立威胁模型、设计评审和故障注入验收。

## 阶段 4：Rust 扫描与 hash 后端

性能或安全验证要求出现后，再引入独立 Rust core：

- Rust：批量扫描、FD 级 metadata/hash、并行任务、Linux 安全替换执行器。
- Python：CLI、配置、SQLite migration、查询、过滤、plan 与报告。
- 初期优先 Rust sidecar（SQLite、JSON Lines、exit code）；稳定后才评估 PyO3。
- Linux 专属能力如 `openat2`、`statx`、`inotify` 和 reflink 必须做 feature 分层；Windows 开发环境只能覆盖跨平台索引逻辑，不能替代 Linux 集成测试。
- 独立 `flatlas-core` 与 sidecar executable。
- 与 Python backend 对照同一测试语料和规范化 SQLite 结果。
- 加入有界并发、取消、进度统计和性能基准。

### Windows 扫描的远期评估

- 先评估 `FileIdExtdDirectoryInfo` 批量读取目录元数据。用现有扫描器逐路径对照 File ID、hardlink、reparse 以及驻留、稀疏、压缩文件的占用值。接口或字段不可用时，保留逐路径采集。
- 有整卷建库需求时，再评估本地 NTFS 的 `FSCTL_ENUM_USN_DATA`。验证卷权限、多个 hardlink 路径的枚举完整性、路径重建、reparse 边界和占用字段补采。普通目录与网络共享继续走目录枚举。接口与实测背景见 [Windows 扫描调研](../research/windows-scan-everything.md)。

## 阶段 5：安全去重执行器

- plan 在 apply 前重新验证文件身份、metadata 与内容。
- 支持 dry-run、跳过、重试、审计和崩溃恢复。
- 首先实现 hardlink；reflink 与 delete 在独立能力探测、测试与策略明确后加入。
- 执行流程与默认排除策略的最低要求见 [同类工具对照：执行器阶段必须补齐的安全规则](../research/tool-comparison.md#执行器阶段必须补齐的安全规则)。
- plan 的 `applied` / `partially_applied` / `failed` 状态与新的 `operation_attempt` 审计表（记录执行前复核、临时重命名、错误与恢复）在此阶段引入；安全模式将逐字节比较成功记为 `full_verification='byte_compare'`。

## 阶段 6：持续增量与体验

- 事件驱动 dirty queue 与定期 reconcile。
- Windows 先评估在显式刷新时读取已有的 USN Journal，按卷保存 Journal ID 和游标，定位需要重扫的文件与目录。改名覆盖新旧父目录，hardlink 变化覆盖受影响路径。日志断档或卷身份变化时回退常规扫描，删除仍由 `completed` scope 确认。见 [Windows 扫描调研](../research/windows-scan-everything.md#供后续评估的-windows-接口)。
- 可选 TUI、shell completion、稳定 JSON 协议与发行打包。

## 按需求启动的扫描优化

- 有从 WSL 扫描 Windows 挂载盘的实际需求时，再实现目录句柄复用。用同一目录 fd 枚举和读取子项属性，逐路径对照元数据与占用空间，并验证符号链接、错误处理和缺失路径确认。见 [WSL 9P 追踪记录](../research/windows-scan-everything.md#wsl-跨挂载扫描的-9p-追踪)。
