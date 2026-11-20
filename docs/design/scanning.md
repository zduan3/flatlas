# 扫描与目录汇总

> 状态：当前设计。描述 metadata 扫描、目录汇总缓存与 freshness、`--refresh-before`、中断与续扫，以及删除确认规则；随代码更新。表结构与 migration 见 [数据库结构](schema.md)。

## 扫描流程

`flatlas scan PATH` 在 PATH 所属的已登记 namespace 内扫描一个目录 scope：PATH 即 namespace 根时 `scan.kind='full'`，否则为 `subtree`。已经不存在的目标不能直接扫描，必须扫描覆盖它的现存父目录。扫描只采集 metadata，不读取普通文件内容，也不创建新的 `file_hash`；已有 hash 的依据随 metadata 变化时标为 `stale`。

一次扫描在单个 SQLite 事务中完成：创建 running 的 `scan` 与 `scan_scope`，补齐 scope 的祖先路径，递归枚举目录并 upsert 每个观测到的条目，最后按结果写入 scope/scan 状态、确认删除（仅 completed）并批量刷新目录汇总。scope 祖先无法 stat 时 scan 记为 `failed`；任一枚举或 stat 错误写入 `scan_error` 并使 scope 成为 `partial`；用户中断成为 `cancelled`；否则为 `completed`。

默认不跟随 symlink、junction 或其他 Windows reparse point；symlink 只读取并保存目标（readlink 失败时目标为空）。普通文件同时采集 allocated size，采集失败按 `stat` 错误记录并令 scope partial，见 [平台差异](platform.md)。

### 元数据成本

- 每个条目只做一次 `lstat`，类型由 `st_mode`（`S_ISLNK` / `S_ISDIR` / `S_ISREG`）与 Windows reparse 属性判定，不再额外调用 `islink` / `is_dir` / `is_file`。`lstat` 不跟随最后一级链接，因此分类与此前的探测结果一致。
- 扫描器把已知的父目录行 id 直接传给子条目的 upsert，不再逐条按 `path_key` 查找父目录；非目录条目不执行无效的 `directory_summary` 删除，非文件条目不执行无效的 `file_hash` 失效更新。
- SQLite 页缓存上限为 256 MiB（按需分配），避免大索引在逐条查找时反复读取 B-tree 页。

在 Lustre 等网络文件系统上，每个条目首次 stat 的延迟是扫描的主要成本，实测见 [平台差异](platform.md#linux--lustre)。`tests/test_scan_costs.py` 覆盖只调用一次 `lstat`、父目录关联正确，以及类型变化时仍会清除汇总并让 hash 失效。

## 目录汇总缓存

`directory_summary` 为每个当前已索引的 namespace root 或普通目录保存派生汇总，使 `ls` 和 `du` 不必在每次查询时递归聚合整棵 `path` 子树。缓存仍然只描述 SQLite 当前状态，不进行实时文件系统遍历，也不改变 coverage 与删除判定语义。

缓存保存：

- 直接普通文件的数量、logical size 和可用时的 allocated size。
- 整棵已索引子树的普通文件数量、logical size 和可用时的 allocated size。
- `self_completed_at_ns`：最近一次成功枚举并 stat 该目录全部直接条目的时间。
- `subtree_complete_through_ns`：该目录自身与所有当前 present 子目录的完成时间下界。
- `last_completed_scan_id` 与 `updated_at_ns`，分别用于关联扫描审计和区分 coverage freshness 与缓存计算时间。

`subtree_complete_through_ns` 只有在目录自身以及每个 present 子目录都至少成功完整枚举过一次时才非空，其值为各目录最近成功时间的最小值。它表示“当前已知子树的每个目录都至少在该时间之后成功观测过”，不表示该时刻的不可变快照。

`ls` 对已索引的目录条目以 `SCANNED-THROUGH` 列展示这个时间：本地时区、GNU `ls -l` 风格，近六个月且早于当前时间显示月、日和时分，其他时间显示月、日和年份。JSON/CSV 保留原始 UTC Unix 纳秒整数；无成功时间的目录和非目录条目显示未知。`part` / `gone` 目录可显示历史时间，但其状态仍提示最近覆盖不完整或现场缺失。

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

## 中断与续扫

扫描使用单个事务，并实现两段式用户取消：

- 遍历阶段第一次 `KeyboardInterrupt` 停止继续枚举，转入批量汇总收尾，将 scan/scope 记为 `cancelled`，保留已观测 path、metadata 和缓存，同时绝不运行缺失删除。
- 收尾阶段再次中断则回滚本次扫描事务并放弃保存。
- 进程崩溃同样丢失整个未提交事务；这些情况下本次观察和 scan 记录均不存在，不能从该调用恢复。

CLI 在 POSIX 上把 `SIGTERM`（批处理调度器终止作业前发送）和 `SIGHUP`（终端关闭或远程会话断开）转为同一 `KeyboardInterrupt` 路径，因此它们与 Ctrl-C 一样保存已观测数据；父进程已忽略的信号（如 `nohup` 下的 `SIGHUP`）保持忽略。调度器在 `SIGTERM` 后的宽限期内若收尾未完成即 `SIGKILL`，本次扫描仍整体回滚。同一信号路由也作用于 `dupes`：hash 阶段中断时提交已完成的 hash 批次，见 [惰性 Hash 与重复候选](hashing.md)。

`flatlas scan PATH --resume` 只匹配 root 与规范化 scope 完全相同的最近 scan。最近状态必须是 `cancelled`、`partial` 或 `failed`；`completed` 不可恢复，`running` 不会被自动接管。恢复调用创建新的独立 scan，不保存 `resumed_from_scan_id`：若上次 scan 已有 `refresh_before_ns` 则原值继承，否则使用上次 scan 的 `started_at_ns`。因此多次用户中断始终使用最初的固定 cutoff，目录仍记录各自实际完成时间，失败目录的成功时间不推进并会在下次恢复时重试。

`flatlas scan --resume-from ID` 使用相同规则显式选择历史 scan，并从其唯一 `scan_scope` 读取 `scan-info ID` 所展示的 PATH；ID 必须存在、状态未完成且只有一个 scope。显式选择允许在存在更新的扫描记录时仍从指定的旧 cutoff 刷新，但不会修改或关联原 scan。`--full`、`--refresh-before`、`--resume`、`--resume-from` 互斥，使用 `--resume-from` 时不得再传 PATH。

当前恢复只适用于已经成功保存 `cancelled`/`partial`/`failed` 记录的调用，并且不持久化工作队列：续扫依据目录成功时间重新选择工作，不是崩溃后可恢复任务。

## 删除确认规则

- 只有 completed `scan_scope` 才能确认缺失路径为 `deleted`；partial、failed、cancelled、枚举错误、stat 错误或 allocated size 采集失败均不确认删除。
- 普通扫描 completed 时，scope 后代中 `last_seen_scan_id` 不是本次 scan 的 present 路径标记为 deleted；`--refresh-before` 扫描只对完整枚举的目录确认其直接缺失条目及其已索引后代。
- deleted 行保留在 SQLite，不参与 `paths`、`du`、`largest` 或 `dupes` 的当前状态结果；同一路径重新出现并被扫描时恢复为 present，旧 hash 在 metadata basis 不匹配时变为 stale。
- `ls` 现场发现缺失只显示 `gone`，不写数据库。

相关提案：[扫描短事务 checkpoint 与崩溃恢复](../plan/proposals/scan-checkpoint.md)、[lazy 元数据](../plan/proposals/lazy-metadata.md)、[浅扫描与增量刷新](../plan/proposals/incremental-refresh.md)。
