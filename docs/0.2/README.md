# 0.2 开发阶段总览

> 状态：当前开发阶段。本文汇总计划范围；除明确标记为已实现的内容外，不代表当前 CLI 已支持。

File Atlas 当前开发版本为 0.2.0。0.1 是已完成并冻结的只读 MVP 基线；0.2 继续保持不修改用户文件系统的边界，重点完善 Python 索引、查询体验、结果可信度与惰性 hash。0.2 不提供 plan apply，也不删除、移动或替换用户文件。

当前已实现的 0.2 阶段元数据能力：`flatlas --version` 输出正在运行的包版本；可访问源码仓库时附加 Git 提交、提交日期和 dirty 状态，否则回退为纯包版本。该命令无需初始化或打开索引数据库。

命令默认参数已支持用户级持久化：`flatlas config set/get/unset scan.refresh-before` 管理 `config.toml` 中 `[scan]` 表的 `refresh-before` 字符串。`config list` 列出已设置的命令默认参数，`config list -a` 列出所有支持的参数并将未设置值显示为 `<unset>`。显式 CLI 参数优先，`scan --full` 临时完整扫描，续扫继承历史 cutoff。配置可以先于 `init` 创建，修改时保留数据库路径、其他字段与注释，并使用同目录临时文件原子替换。行为与验收见 [命令默认参数配置](configuration.md)。

目录汇总缓存基础设施也已实现：migration 2 为每个已索引目录保存 direct/subtree 文件数、logical/allocated 汇总、目录自身最近完整枚举时间和子树 freshness 下界；扫描结束时批量、自底向上刷新 scope 与祖先，`ls` / `du` 读取缓存。migration 3 记录 `--refresh-before` 的固定时间点，使扫描可以只完整枚举从未成功扫描或成功时间更早的目录；跳过的新鲜子树不参与删除判定。`flatlas scan PATH --resume` 查找相同 scope 最近一次未完成扫描，`flatlas scan --resume-from ID` 从指定未完成 scan 读取唯一 scope；两者均继承固定 cutoff，以新的独立 scan 继续刷新，不保存 attempt 关系。已有数据库升级时 CLI 在 stderr 提示 migration 并显示目录回填进度。遍历阶段第一次中断会保存已观测数据与 cancelled 状态，收尾阶段再次中断才回滚；持久化工作队列和崩溃后续扫仍属于后续工作，详见 [目录汇总缓存与扫描时间](directory-summary.md)。

当前 Python core 已按职责拆分：`db.py` 管理 schema 与连接，`paths.py` 管理 namespace 和路径身份，`scanning.py` 负责 metadata 扫描，`hashing.py` 负责惰性 hash，`queries.py` 提供只读查询，`planning.py` 保存 dry-run plan，`output.py` 负责稳定导出。`core.py` 仅保留兼容性 re-export 与既有故障注入入口；该拆分不改变 0.1 数据库或 CLI 语义。

`du` 无路径参数时汇总当前目录 `.`；已登记但尚未扫描的目录显示 `new`，文件数和大小为未知值，多路径查询仍保留其他已索引路径的汇总。表格状态列与 `ls` 一样使用 `S` 和 `ok` / `new` / `part`；JSON/CSV 的 `status` 字段使用对应的 `scanned` / `unscanned` / `incomplete`。`du` 不实时枚举目标，不提供 `ls` 现场枚举得出的 `gone` 状态。

扫描审计可以通过 `flatlas scans [PATH]` 查询历史摘要，并用 `flatlas scan-info ID` 查看单次 scan 的 scope、cutoff、计数与错误明细。两个命令只读取索引；PATH 过滤同时包含覆盖该路径的祖先 scope 和位于该路径下的子树 scope。

## 计划能力摘要

`ls`、`du` 与 `largest` 已支持 `-h` / `--human-readable`：仅缩放表格中的大小（`ls` / `du` 为 logical/allocated，`largest` 仅显示 logical 与路径），以 1024 进制使用 `K`、`M`、`G`、`T`，保留至多一位小数；小于 1024 的值显示字节整数，超过 T 的值继续以 T 表示。大小表头省略 `(B)`，未知值仍为 `-`，文件数、状态及 JSON/CSV 原始字节值不变。

### 空间与重复候选查询

- 为 `du` 增加直接子项和深度展开，并完善 logical/allocated metric、top N、排序、阈值、总计与多路径行为。
- 为 `largest` 增加 metric、排序、阈值和结果数量控制。
- `dupes` 已有理论节省量排序与汇总；第 1 批大小、组成员数、理论节省量阈值和组数限制已实现，`export dupes` 共用参数。大小在 hash 前筛选，组级条件在 full hash 分组后应用，详见 [惰性 Hash 规划](lazy-hash.md)。多 PATH、include/exclude、hardlink alias 与 coverage completeness 仍待实现。
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

目录缓存、扫描完成时间及其与后续增量扫描的边界见 [目录汇总缓存与扫描时间](directory-summary.md)。

### 惰性 hash、性能与可观测性

- 加入保守的有界并发，分别约束文件打开、读取任务和 SQLite 写入批次，并针对 HDD、SSD 与网络盘校准默认值。
- 增加 `--cached` 与 `--rehash`，支持后台 hash、取消恢复、算法与 quick 采样策略版本化。
- 建立独立 hash run/error 审计，记录 scope、算法版本、候选数、读取字节、cache hit、changed、failed、cancelled 和耗时。
- 保持 metadata basis 的保守校验；身份、size、mtime 或可靠 change/birth time 不匹配时使缓存 stale 并重新计算。
- 建立大文件数、同尺寸高碰撞和大文件语料的基准测试与故障注入测试。

详细设计见 [惰性 Hash 规划](lazy-hash.md)。

### 跨平台与数据真实性

- Windows 普通文件的占用空间已采集：通过外部 extent 查询识别 MFT 驻留数据并计为 0，其他普通文件按所在卷簇大小取整，压缩/稀疏文件读取系统占用值；失败保留未知并令扫描 partial，不确认删除。已有索引需普通重扫回填，目录继续读取缓存。口径、限制与验证见 [Windows 占用空间](windows-allocation.md)。
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
