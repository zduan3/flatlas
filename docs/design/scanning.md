# 扫描与目录汇总

> 状态：当前设计。描述 metadata 扫描、目录汇总缓存与 freshness、`--refresh-before`、中断与续扫，以及删除确认规则；随代码更新。表结构与 migration 见 [数据库结构](schema.md)。

## 扫描流程

`flatlas scan PATH` 在 PATH 所属的已登记 namespace 内扫描一个目录 scope：PATH 即 namespace 根时 `scan.kind='full'`，否则为 `subtree`。已经不存在的目标不能直接扫描，必须扫描覆盖它的现存父目录。扫描只采集 metadata，不读取普通文件内容，也不创建新的 `file_hash`；已有 hash 的依据随 metadata 变化时标为 `stale`。

一次扫描先提交 running 的 `scan` 与 `scan_scope` 及 scope 的祖先路径，再递归枚举目录并 upsert 每个观测到的条目，遍历中按时间间隔提交 checkpoint（见「中断与续扫」），最后在一个事务中写入 scope/scan 状态、确认删除（仅 completed）并批量刷新目录汇总。scope 祖先无法 stat 时 scan 记为 `failed`；任一枚举或 stat 错误写入 `scan_error` 并使 scope 成为 `partial`；用户中断成为 `cancelled`；否则为 `completed`。

默认不跟随 symlink、junction 或其他 Windows reparse point；symlink 只读取并保存目标（readlink 失败时目标为空）。普通文件同时采集 allocated size，采集失败按 `stat` 错误记录并令 scope partial，见 [平台差异](platform.md)。

### 元数据成本

- 每个条目只做一次 `lstat`，类型由 `st_mode`（`S_ISLNK` / `S_ISDIR` / `S_ISREG`）与 Windows reparse 属性判定，不再额外调用 `islink` / `is_dir` / `is_file`。`lstat` 不跟随最后一级链接，因此分类与此前的探测结果一致。
- 扫描器把已知的父目录行 id 直接传给子条目的 upsert，不再逐条按 `path_key` 查找父目录；非目录条目不执行无效的 `directory_summary` 删除，非文件条目不执行无效的 `file_hash` 失效更新。
- 子条目的 `path_key` 由父目录的 key 加上 `fsencode(name)` 与 NUL 直接拼出（按时间刷新时子目录 key 取自索引），不再对每个条目调用 `path_key()` 重新 `relpath` 并逐分量编码；后者在深路径上每条约 80 µs。非 TTY 时 CLI 不传进度回调（此前每条仍调用一次，480 万条目约 88 s）。热缓存下 3.5 万条目子树的扫描从 7.5 s 降到 5.4 s。
- SQLite 页缓存上限为 256 MiB（按需分配），避免大索引在逐条查找时反复读取 B-tree 页。
- 按父目录查子项（递归遍历子树、目录汇总刷新、删除确认）走 `idx_path_parent`（migration 5）。此前这些查询只能用以 `root_id` 开头的索引，SQLite 每次都为全表临时建自动索引；在 122 万行的索引上，TensorRT-LLM（5.6k 目录）重扫的汇总刷新从 8.1 s 降到 0.7 s。
- 递归 CTE 一律写成 `FROM <cte> x CROSS JOIN path p ON ...`，外层也从 CTE 按主键回查 `path`。索引里没有 `sqlite_stat1`，计划只取决于 SQL 与索引：带 `p.root_id=?` 条件时 SQLite 会把 `path` 放在外层、按 `root_id` 扫整个 root，递归每取一行就扫一遍，浅扫描确认 41.5 万条删除因此跑不完；改后 3.7 s。`tests/test_query_plans.py` 对全部递归查询检查计划。
- 完整扫描的删除确认按 `path_key` 区间更新：子条目 key 是父 key 加一个分量，scope 的后代正好是唯一索引上的一段区间，比沿 `parent_path_id` 的递归 CTE 快约一倍（380 万行 6.6 s → 3.5 s）。scope 行由本次扫描首次记录时（`first_seen_scan_id` 为本次）不可能有已索引的后代，整段跳过；首次全量扫描 480 万条目时这一步原本要 94 s。
- 删除确认的成本与被改写的行数成正比：每条缺失路径都要更新行本身和含 `state` 的两个索引。2026-11 在 Lustre 上（索引 4.8 GB，也放在 Lustre）重扫一棵 391 万行、其中 368 万条已在外部删除的子树，总计 688 s，`mark_missing` 占 523 s，`readdir` + `observe` 只有 92 s。tombstone 不会自动清除，以后对该子树的完整扫描仍要在区间里走过这些行；已知删除清单时用 `rm --files0-from` 同步，已有 tombstone 时可用 [`rm --deleted PATH`](queries.md#rm---deleted-path) 清理。

### 分阶段计时

每次扫描把各阶段耗时写入 `scan.stats_json`（migration 5），`scan` 输出与 `scan-info ID` 的 `stats` 字段展示：

- `wall_s`、`entries_per_s`：本次扫描的总耗时与条目速率。速率的分子为本次观测的文件数加目录数，总耗时包含删除确认。
- `paths_deleted`：最终事务提交的本次缺失路径数，包含缺失目录的已索引后代，每条路径只计一次。普通扫描将这些行标记为 `deleted`，`--purge-deleted` 则直接移除这些行。该字段也显示在 `scan` 的顶层输出中；确认零条时为 `0`，旧扫描缺少该字段时为未知。
- `paths_purged`：`--purge-deleted` 实际从索引移除的路径数，包括本次缺失路径和旧 tombstone。该字段也显示在该模式的 `scan` 顶层输出中，无移除时为 `0`。
- `phases`：`readdir`（枚举目录）、`db_write`（path upsert）、`refresh_query`（`--refresh-before`/续扫的逐目录查询）、`progress`（进度回调）、`checkpoint`（批次的目录汇总与计数更新）、`commit`（checkpoint 提交）、`mark_missing` 或 `purge_missing`（按模式确认删除）、`summaries`，各含秒数与调用次数；`unaccounted_s` 为未归入任何阶段的时间。阶段在单个操作外计时，递归不会重复计入。
- 元数据阶段在 Windows 分为 `lstat`（遍历时的逐条目元数据读取）和 `allocation`（普通文件的占用空间查询）。类型判定和构造观测记录等开销计入 `unaccounted_s`。其他平台及此前的 Windows 扫描使用 `observe`，记录遍历时每个条目的完整观测耗时。
- `phases.allocation.phases` 细分普通非空、非压缩、非稀疏文件的占用空间查询：`extents` 包含打开文件句柄、校验属性、查询 extent 和关闭句柄；有外部 extent 时才调用 `cluster_size`，其中包含查缓存以及必要时定位卷和查询簇大小。调用次数包含缓存命中，耗时已计入 `allocation` 总计。压缩或稀疏文件的独立查询计入 `allocation` 总计。
- `exact_stat_fallbacks`、`exact_stat_fallback_s`：lazy root 上因 `blocks×512 < size` 回退到精确 `lstat` 的次数与耗时（包含在 `observe` 内）。
- `checkpoints`：本次提交的 checkpoint 数（含收尾前的最后一个）。
- 进入收尾、初始化失败或收尾回滚时写入统计。partial、failed、cancelled 的删除计数为 `0`。被杀后接管的扫描只记录删除计数 `0`；尚未收尾的扫描可能没有统计，migration 5 之前的扫描为 `null`。

### Lustre 上的 lazy 元数据

`init` 登记 root 时从 `/proc/self/mounts` 读取挂载点的文件系统类型，写入 `root.filesystem_type`；对已有 root 再次执行 `init` 会刷新该值，检测不到时不覆盖已有记录。扫描读取所属 root 的类型：

- `filesystem_type = 'lustre'` 时，把这一次 `lstat` 换成 `statx(AT_SYMLINK_NOFOLLOW | AT_STATX_DONT_SYNC)`（`flatlas.lazystat`），读取 MDT 上已有的 size 与时间，不向 OST 同步文件大小；该 scan 的 `scanner_version` 带 `+lazy-statx` 后缀。`ls` 的现场条目检查同样使用 lazy 方式。
- 普通文件的 `blocks × 512` 小于 size 时，改用精确 `lstat`。没有 size-on-MDT 记录（`lfs getsom` 返回 ENODATA）的文件，lazy 结果是客户端 close 时报给 MDT 的值：可能是占位值（blocks = 0、size 错误），也可能 size 正确而 blocks 只计到 close 时已落盘的部分（批量写入后立即 close，如 `git clone`、LFS checkout）。真正的稀疏文件同样走精确 `lstat`，只多花时间。
- `statx` 不可用（非 Linux、glibc < 2.28 或 `ENOSYS`）时回退到 `lstat`。
- 正在写入的文件，其 lazy size 与 mtime 可能滞后；同一机制下，已静止文件的 lazy mtime/ctime 也可能比 OST 上的值早约 1 s，且无法从 lazy 结果本身识别。这只影响索引中的估计值：hash 在打开文件后以 `fstat` 重验 basis，lazy root 上身份与 size 一致、仅时间不同的文件以 `fstat` 的精确时间写回 path 后继续（见 [惰性 Hash](hashing.md#metadata-basis-校验与缓存复用)），其余不一致的文件成为 stale，不会进入重复组。目录的 mtime 只在 MDT 上维护，不受影响。coverage、错误处理与删除确认规则不受影响。

每个条目首次 stat 的延迟是网络文件系统上扫描的主要成本，实测见 [平台差异](platform.md#linux--lustre)。`tests/test_scan_costs.py` 覆盖只调用一次 `lstat`、父目录关联正确，以及类型变化时仍会清除汇总并让 hash 失效。

## 目录汇总缓存

`directory_summary` 为每个当前已索引的 namespace root 或普通目录保存派生汇总，使 `ls` 和 `du` 不必在每次查询时递归聚合整棵 `path` 子树。缓存仍然只描述 SQLite 当前状态，不进行实时文件系统遍历，也不改变 coverage 与删除判定语义。

缓存保存：

- 直接普通文件的数量、logical size 和可用时的 allocated size。
- 整棵已索引子树的普通文件数量、logical size 和可用时的 allocated size。
- `self_completed_at_ns`：最近一次成功枚举并 stat 该目录全部直接条目的时间。
- `subtree_complete_through_ns`：该目录自身与所有当前 present 子目录的完成时间下界。
- `last_completed_scan_id` 与 `updated_at_ns`，分别用于关联扫描审计和区分 coverage freshness 与缓存计算时间。

`subtree_complete_through_ns` 只有在目录自身以及每个 present 子目录都至少成功完整枚举过一次时才非空，其值为各目录最近成功时间的最小值。它表示“当前已知子树的每个目录都至少在该时间之后成功观测过”，不表示该时刻的不可变快照。

`ls` 对已索引的目录条目以 `SCANNED-THROUGH` 列展示这个时间：本地时区、GNU `ls -l` 风格，近六个月且早于当前时间显示月、日和时分，其他时间显示月、日和年份。机器输出的 `complete_through_ns` 保留原始 UTC Unix 纳秒整数；无成功时间的目录显示未知，非目录条目在表格中显示 `-`。`part` / `gone` 目录可显示历史时间，但其状态仍提示最近覆盖不完整或现场缺失。

普通文件系统扫描无法提供原子快照：不同目录按遍历顺序在不同时间枚举，文件也可能在扫描期间新增、删除、改名或改变大小。因此即使 scan 为 completed，目录汇总仍是不同观测时间组成的当前索引估计值。`self_completed_at_ns`、`subtree_complete_through_ns` 和 `updated_at_ns` 分别描述成功观测时间、子树 freshness 下界和缓存计算时间，均不得表述为快照时间。

失败 attempt 在 freshness 计算中视为没有成功更新：它不推进也不清除 `self_completed_at_ns`，但仍完整保留在 `scan`、`scan_scope` 和 `scan_error` 中。查询可信度时可以据此区分“长期未扫描”和“最近尝试但读取失败”；不能因为失败不参与 freshness 就删除或隐藏审计记录。

目录大小与 freshness 是两个独立维度。partial scan 可以把已观测的新 metadata 写入缓存，同时保留因 coverage 不完整而不能确认删除的旧路径，因此汇总可能混合新旧观测；非空时间不能被解释为精确性证明。

### 查询如何使用缓存

`ls` 只读取指定目录的直接条目和缓存，不递归核验索引中的子目录。目录状态只表示目录自身的完整枚举情况，不保证全部后代都完整：未完成的普通扫描中，目录自身的 `last_completed_scan_id` 等于本次 scan ID 时显示 `ok`，自身未完成且本次已观察到时显示 `part`；未访问的旧分支仍按之前的覆盖记录判断。目录大小、文件数与 `SCANNED-THROUGH` 继续读取已有子树缓存，普通文件使用父目录的完成 scan ID 判断状态。`du` 保留目录及其当前子目录的 `last_completed_scan_id` 核验。两次扫描可能落在相同的文件系统时钟刻度，不能仅用成功时间与本次开始时间比较。显式 `--refresh-before` 扫描中，`ls` 使用缓存的目录自身完成时间和固定 cutoff 判断状态，`du` 使用子树 freshness，两者均无需在此分支递归查询。

### 更新规则

扫描以 completed、partial 或 cancelled 结束时，在同一扫描事务内自底向上刷新请求 scope 及其所有祖先：

1. 完整枚举且所有直接条目 stat 成功的目录推进自身完成时间。
2. 枚举或 stat 失败的目录保留上一次成功时间；如果从未成功过则继续为 `NULL`。失败不推进 freshness。
3. summary 只聚合仍为 `present` 的普通文件和普通子目录；不跟随 symlink 或 reparse point。
4. 任一纳入汇总的文件缺少 allocated size，或任一子目录的 allocated 汇总未知，父目录的 allocated 汇总为 `NULL`。
5. completed scope 仍先按既有规则确认缺失路径，再计算 summary；partial 或 cancelled scope 不确认任何旧路径删除，但会缓存已写入索引的已知统计。
6. 显式 `flatlas rm PATH` 删除索引子树后，刷新其仍存在的父目录与祖先汇总。
7. 目录从 deleted 重现、由其他类型变为目录或可靠对象身份变化时，旧成功时间失效；在新目录对象完成枚举前保持 `NULL`。

缓存刷新采用批量、自底向上的实现：先批量读取目标目录、直接文件统计和直接子目录缓存，在内存中按路径深度从叶子计算到祖先，最后使用 `executemany` 批量 UPSERT。SQLite 参数按最多 800 个目录分批，避免逐目录执行多条 SELECT，也避免超过变量数量限制。局部扫描只处理请求 scope 的 present 目录及其祖先；migration 2 回填按 root 处理全部当前目录（见 [Migration 历史](schema.md#migration-历史)）。

## 按时间刷新：`--refresh-before`

`flatlas scan PATH --refresh-before VALUE` 使用固定 cutoff 在递归过程中按目录选择工作，不在启动时加载完整目录树：`self_completed_at_ns` 为空或早于 cutoff 的目录需要完整枚举；已知子树的 `subtree_complete_through_ns` 不早于 cutoff 时可以整体剪枝；目录自身足够新但子树较旧时，从索引读取直接子目录并继续向下检查。索引中的旧子目录已经不存在或已不再是目录时，扫描回退为完整枚举其父目录，只有父目录枚举成功后才确认删除。

VALUE 支持带时区或使用本地时区的 ISO 8601 时间，以及 `30s`、`15m`、`12h`、`7d`、`2w` 形式的固定时长；未来时间被拒绝。相对时长在命令开始时解析成固定时间点并写入 `scan.refresh_before_ns`，不随扫描耗时变化。默认值可由 `scan.refresh-before` 配置提供，`--full` 临时完整扫描，见 [命令默认参数配置](config.md)。

刷新扫描只为实际完整枚举的目录推进 `self_completed_at_ns`。当整次 scope 没有错误并成为 completed 时，每个完整枚举目录只确认其直接缺失条目及这些条目的已索引后代；主动跳过的新鲜目录不参与当前 scan 的删除判定，也不视为已被当前 scan 观察。partial、failed 或 cancelled 刷新仍不确认任何删除。

## 按目录变化刷新：`--refresh-changed`

`flatlas scan PATH --refresh-changed` 检查目录本身的身份及 mtime、可用的 change time。与最近一次成功重列目录时的基线一致时，跳过该目录的 `readdir` 并继续检查已索引的普通子目录；发现子目录缺失或类型变化时重新列父目录。缺少可信基线的目录会重新枚举。此模式覆盖命令配置中的默认 cutoff。续扫继承变化检测模式，重新检查目录并复用已提交的基线。

成功的扫描为重新枚举且检查完直接条目的目录保存基线。跳过枚举的目录保留既有 freshness，其直接文件也保留上次观测的 metadata。只有 completed scope 中重新枚举成功的目录才确认缺失的直接条目及其后代。旧库升级后的基线从第一次成功重列开始建立。

`scan`、`scans` 和 `scan-info` 的机器输出用 `refresh_changed` 标记此模式。完成时的 `stats.directories_skipped` 统计跳过枚举的目录数。此模式可遗漏文件原地改写（包括大小变化），周期性的 `scan PATH --full` 用于重新采集所有文件 metadata。

## 中断与续扫

### Checkpoint

扫描创建 running 的 `scan`/`scan_scope` 并补齐 scope 祖先后立即提交，遍历中至少每 `CHECKPOINT_INTERVAL_S`（5 s）提交一次 checkpoint，在单个短事务内写入：

- 自上次 checkpoint 以来 upsert 的 path、metadata 与 `scan_error`；
- `scan.files_seen`、`dirs_seen`、`logical_bytes_seen`（migration 7）、`scan_scope.paths_seen` 与心跳 `scan.heartbeat_at_ns`；
- 这一批中遍历结束的目录的汇总，完整枚举的目录同时推进 `self_completed_at_ns`/`last_completed_scan_id`。遍历是后序的，目录结束时它访问过的子目录都已结束，汇总可以从子目录已提交的汇总自底向上算出；仍在遍历中的祖先保留旧汇总，直到它们结束或最终刷新。

删除确认只在最终事务中进行，与 completed 状态和整个 scope 的汇总刷新一起提交，因此任何中断、崩溃或被杀都只留下 checkpoint，不会删除任何旧路径。其他连接可以在扫描期间读到已提交的 checkpoint：已结束的目录按当前 scan 显示为已完成，未结束的部分按之前的覆盖判断（running 的处理与 cancelled 相同）。checkpoint 期间扫描进程持有写锁，其它写入者只能在两次 checkpoint 之间的空隙获得锁。

提交开销（2026-11 在 Lustre 上实测，63.8 万条目子树）：每次提交约 0.35 s，按 5 s 间隔约占扫描 7%。其中约 70% 是提交后顺带的 WAL checkpoint——每批写入 21–37 MiB WAL，远超默认 `wal_autocheckpoint` 1000 页，几乎每次提交都触发；WAL fsync 只占约 0.04 s。扫描期间关闭自动 checkpoint、结束时统一做一次（约 1 s）可把提交降到约 0.11 s（`synchronous=NORMAL` 再降到约 0.07 s），代价是扫描期间 WAL 涨到约 1 GB，NORMAL 还会在掉电时丢最近批次。当前未采用，保持 SQLite 默认；需要压低开销时优先调 `wal_autocheckpoint`，不改 `synchronous`。

不另外持久化工作队列：目录完成时间就是队列。续扫用原 cutoff 做 `--refresh-before`，已在 checkpoint 中完成的子树按 `subtree_complete_through_ns` 整体剪枝，只重新枚举崩溃点到 scope 路径上未完成的目录。

### 用户取消与信号

- 遍历阶段第一次 `KeyboardInterrupt` 停止继续枚举，先把已观测数据作为最后一个 checkpoint 提交，再在独立事务中批量刷新汇总，将 scan/scope 记为 `cancelled`，绝不运行缺失删除。
- 收尾阶段（含上述最后一个 checkpoint 和 completed 扫描的收尾）再次中断，回滚未提交的部分，用一条短事务把 scan/scope 记为 `cancelled`，计数取最后一个已提交 checkpoint 的值。
- 遍历中出现意外异常时同样回滚未提交批次，并把 scan/scope 记为 `failed` 后重新抛出。

CLI 在 POSIX 上把 `SIGTERM`（批处理调度器终止作业前发送）和 `SIGHUP`（终端关闭或远程会话断开）转为同一 `KeyboardInterrupt` 路径，因此它们与 Ctrl-C 一样保存已观测数据；父进程已忽略的信号（如 `nohup` 下的 `SIGHUP`）保持忽略。同一信号路由也作用于 `dupes`：hash 阶段中断时提交已完成的 hash 批次，见 [惰性 Hash 与重复候选](hashing.md)。

### 崩溃与 owner

`SIGKILL`、断电或进程崩溃会留下 status 仍为 `running` 的 scan 及其已提交的 checkpoint。scan 记录 owner（`scan.owner_json`：主机名、PID 命名空间、PID）与心跳，用于判断 owner 是否已死：

- 心跳超过 30 分钟（`ABANDONED_AFTER_NS`）视为已死；
- 否则只在 owner 与当前进程同一主机、同一 PID 命名空间且平台提供命名空间（Linux `/proc/self/ns/pid`）时探测 PID，进程不存在即已死；容器、agent 沙箱等不同 PID 命名空间互相看不到进程，不能探测，其他主机与不支持的平台同样只按心跳判断。

`--resume` / `--resume-from` 遇到 running 的 scan 时，owner 已死则把它记为 `cancelled`（`finished_at_ns` 取最后心跳，`note` 记录原 owner 与接管者）后照常续扫；owner 可能仍活着则报错并显示 owner 与最后 checkpoint 的时间。仍在运行的扫描在每次 checkpoint 与最终提交时都要求自己的 scan 仍为 running；若已被其它进程终结，回滚未提交部分并以 `ScanTakenOverError` 停止，不覆盖对方写入的状态。

### 续扫

`flatlas scan PATH --resume` 只匹配 root 与规范化 scope 完全相同的最近 scan。最近状态必须是 `cancelled`、`partial` 或 `failed`（或按上节接管的 running）；`completed` 不可恢复。恢复调用创建新的独立 scan，不保存 `resumed_from_scan_id`：若上次 scan 已有 `refresh_before_ns` 则原值继承，否则使用上次 scan 的 `started_at_ns`。因此多次中断始终使用最初的固定 cutoff，目录仍记录各自实际完成时间，失败目录的成功时间不推进并会在下次恢复时重试。

`flatlas scan --resume-from ID` 使用相同规则显式选择历史 scan，并从其唯一 `scan_scope` 读取 `scan-info ID` 所展示的 PATH；ID 必须存在、状态未完成且只有一个 scope。显式选择允许在存在更新的扫描记录时仍从指定的旧 cutoff 刷新，但不会修改或关联原 scan。`--full`、`--refresh-before`、`--resume`、`--resume-from` 互斥，使用 `--resume-from` 时不得再传 PATH。

## 浅扫描：`--shallow`

`flatlas scan PATH --shallow` 只重新枚举 PATH 的直接条目：对每个条目 stat 并 upsert，不进入子目录。用于外部删除了若干条目后确认这一层的删除，例如删掉某目录下几个顶层子目录后，不必全量重扫整棵树。

- 枚举完整时，确认 PATH 的直接缺失条目及其已索引后代为 deleted（与 `--refresh-before` 对单个完整目录的规则相同）；有错误时不确认。
- 扫描记录 `scan.max_depth = 0`（migration 6），`scans` 表格在 scope 后标 `(shallow)`。
- 覆盖语义：浅扫描 scope 只算观测了 PATH 的直接非目录条目。PATH 自身和子目录的状态描述整棵子树，仍取自之前覆盖它们的完整扫描；PATH 的目录汇总按新的直接条目重算，子目录汇总不变。
- 浅扫描新发现的子目录没有被枚举过，状态为 `new`（判据：目录的 `first_seen_scan_id` 晚于覆盖它的最近一次扫描），之后对它做普通扫描才变为 `ok`。
- 与 `--full`、`--refresh-before`、`--resume`、`--resume-from` 互斥，也不使用 `scan.refresh-before` 默认值；`--resume` 跳过浅扫描记录，`--resume-from` 拒绝浅扫描 ID。

## 删除确认规则

- 只有 completed `scan_scope` 才能确认缺失路径并标记为 `deleted` 或直接移除索引行；partial、failed、cancelled、枚举错误、stat 错误或 allocated size 采集失败均不确认删除。
- 普通扫描 completed 时，scope 后代中 `last_seen_scan_id` 不是本次 scan 的 present 路径标记为 deleted；`--refresh-before` 扫描只对完整枚举的目录、`--shallow` 只对 PATH 确认其直接缺失条目及其已索引后代。
- deleted 行保留在 SQLite，不参与 `find`、`du`、`largest` 或 `dupes` 的当前状态结果；同一路径重新出现并被扫描时恢复为 present，旧 hash 在 metadata basis 不匹配时变为 stale。
- `ls` 现场发现缺失只显示 `gone`，不写数据库。

### 完整扫描并清理删除记录

`flatlas scan PATH --purge-deleted`（`-p`）完整遍历 PATH，覆盖配置里的 `scan.refresh-before`，与 `--refresh-before`、`--shallow`、`--resume` 和 `--resume-from` 互斥。中断后可重新运行该选项完成一次完整扫描。

checkpoint 照常提交观测。scope 完整完成后，在最终事务按 `path_key` 区间直接移除本次缺失的路径及历史 tombstone。关联的 hash、缓存汇总及引用被移除路径的 dry-run plan 随之清理。重新观测到的路径、仍有效的 hash、其余 plan 和扫描审计保留。目录汇总与状态、计数在同一事务提交。

开始时发现同一 namespace 的重叠扫描仍在运行会报错；判定为已遗弃的重叠扫描先按 owner 规则记为 `cancelled`，再启动本次扫描。如果新重叠扫描在遍历期间启动，收尾仍刷新已提交观测对应的目录与祖先汇总，将本次记为 `failed` 并保留原行。
