# 0.2 目录汇总缓存与扫描时间

> 状态：基础设施和按时间刷新已实现；中断续扫工作队列尚未实现。

## 目的

`directory_summary` 为每个当前已索引的 namespace root 或普通目录保存派生汇总，使 `ls` 和 `du` 不必在每次查询时递归聚合整棵 `path` 子树。缓存仍然只描述 SQLite 当前状态，不进行实时文件系统遍历，也不改变 coverage 与删除判定语义。

缓存保存：

- 直接普通文件的数量、logical size 和可用时的 allocated size。
- 整棵已索引子树的普通文件数量、logical size 和可用时的 allocated size。
- `self_completed_at_ns`：最近一次成功枚举并 stat 该目录全部直接条目的时间。
- `subtree_complete_through_ns`：该目录自身与所有当前 present 子目录的完成时间下界。
- `last_completed_scan_id` 与 `updated_at_ns`，分别用于关联扫描审计和区分 coverage freshness 与缓存计算时间。

`subtree_complete_through_ns` 只有在目录自身以及每个 present 子目录都至少成功完整枚举过一次时才非空，其值为各目录最近成功时间的最小值。它表示“当前已知子树的每个目录都至少在该时间之后成功观测过”，不表示该时刻的不可变快照。

普通文件系统扫描无法提供原子快照：不同目录按遍历顺序在不同时间枚举，文件也可能在扫描期间新增、删除、改名或改变大小。因此即使 scan 为 completed，目录汇总仍是不同观测时间组成的当前索引估计值。`self_completed_at_ns`、`subtree_complete_through_ns` 和 `updated_at_ns` 分别描述成功观测时间、子树 freshness 下界和缓存计算时间，均不得表述为快照时间。

失败 attempt 在 freshness 计算中视为没有成功更新：它不推进也不清除 `self_completed_at_ns`，但仍完整保留在 `scan`、`scan_scope` 和 `scan_error` 中。查询可信度时可以据此区分“长期未扫描”和“最近尝试但读取失败”；不能因为失败不参与 freshness 就删除或隐藏审计记录。

## 更新规则

扫描成功或以 partial 结束时，在同一扫描事务内自底向上刷新请求 scope 及其所有祖先：

1. 完整枚举且所有直接条目 stat 成功的目录推进自身完成时间。
2. 枚举或 stat 失败的目录保留上一次成功时间；如果从未成功过则继续为 `NULL`。失败不推进 freshness。
3. summary 只聚合仍为 `present` 的普通文件和普通子目录；不跟随 symlink 或 reparse point。
4. 任一纳入汇总的文件缺少 allocated size，或任一子目录的 allocated 汇总未知，父目录的 allocated 汇总为 `NULL`。
5. completed scope 仍先按既有规则确认缺失路径，再计算 summary；partial scope 不确认任何旧路径删除，但会缓存已写入索引的已知统计。
6. 显式 `flatlas rm PATH` 删除索引子树后，刷新其仍存在的父目录与祖先汇总。
7. 目录从 deleted 重现、由其他类型变为目录或可靠对象身份变化时，旧成功时间失效；在新目录对象完成枚举前保持 `NULL`。

缓存刷新采用批量、自底向上的实现：先批量读取目标目录、直接文件统计和直接子目录缓存，在内存中按路径深度从叶子计算到祖先，最后使用 `executemany` 批量 UPSERT。SQLite 参数按最多 800 个目录分批，避免逐目录执行多条 SELECT，也避免超过变量数量限制。局部扫描只处理请求 scope 的 present 目录及其祖先；migration 回填按 root 处理全部当前目录。

migration 2 原地创建该表并回填既有索引。历史完成时间只从覆盖相应目录的 completed `scan_scope` 保守恢复；partial、failed 或 cancelled scope 不用于推断完整时间。migration 不重建或清空现有 root、path、scan、error、hash 或 plan 数据。

migration 3 为 `scan` 增加可空的 `refresh_before_ns`，记录 `--refresh-before` 解析后的固定 UTC Unix 纳秒时间点。既有普通扫描保留为 `NULL`，迁移不修改 scan、scope、path、error、hash 或 plan 的既有数据。

CLI 打开已有数据库并检测到待执行 migration 时，必须先在 stderr 输出 migration 版本与说明。目录回填期间交互式终端原地显示“已完成目录数 / 总目录数”，非交互式调用按进度里程碑输出；提交 migration 版本记录后再显示完成提示。stdout 不得混入 migration 信息，以保持 JSON/CSV 可解析。新建空数据库的 schema 初始化默认静默。migration 中断时不写完成版本，下次打开数据库可幂等重试。

目录大小与 freshness 是两个独立维度。partial scan 可以把已观测的新 metadata 写入缓存，同时保留因 coverage 不完整而不能确认删除的旧路径，因此汇总可能混合新旧观测。机器输出后续应分别暴露数值、freshness 和 coverage/error 状态，而不能把非空时间解释为精确性证明。推荐的可信度概念为：

- `fresh`：所有目录都成功过，子树最旧成功时间在策略阈值内，且没有需要单独提示的近期错误。
- `stale`：所有目录都成功过，但 freshness 下界超过阈值。
- `unknown`：至少一个当前目录从未成功完整枚举。
- `warning`：存在历史成功数据，但之后的 attempt 出现 partial、failed 或 cancelled；已知汇总仍可显示，但需要错误提示。

## 当前限制与后续工作

当前扫描仍使用单个事务，但实现了两段式用户取消：遍历阶段第一次 `KeyboardInterrupt` 停止继续枚举，转入批量汇总收尾，将 scan/scope 记为 `cancelled`，保留已观测 path、metadata 和缓存，同时绝不运行缺失删除；收尾阶段再次中断才回滚本次扫描事务并放弃保存。进程崩溃仍会丢失整个未提交事务。后续短事务 checkpoint 必须在同一事务中提交 path 写入、扫描计数和受影响 summary，崩溃后通过持久化 work queue 恢复，不能仅根据时间戳猜测遍历位置。

`flatlas scan PATH --refresh-before VALUE` 使用固定 cutoff 在递归过程中按目录选择工作，不在启动时加载完整目录树：`self_completed_at_ns` 为空或早于 cutoff 的目录需要完整枚举；已知子树的 `subtree_complete_through_ns` 不早于 cutoff 时可以整体剪枝；目录自身足够新但子树较旧时，从索引读取直接子目录并继续向下检查。索引中的旧子目录已经不存在时，扫描回退为完整枚举其父目录，只有父目录枚举成功后才确认删除。VALUE 支持带时区或使用本地时区的 ISO 8601 时间，以及 `30s`、`15m`、`12h`、`7d`、`2w` 形式的固定时长。相对时长在命令开始时解析成固定时间点并写入 scan，不随扫描耗时变化。

刷新扫描只为实际完整枚举的目录推进 `self_completed_at_ns`。当整次 scope 没有错误并成为 completed 时，每个完整枚举目录只确认其直接缺失条目及这些条目的已索引后代；主动跳过的新鲜目录不参与当前 scan 的删除判定。partial、failed 或 cancelled 刷新仍不确认任何删除。

目录缓存不改变安全不变量：只有 completed `scan_scope` 才能确认缺失路径为 `deleted`，跳过的新鲜子树不视为已被当前 scan 观察。当前按时间刷新仍使用单事务和固定 cutoff，但不持久化工作队列，因此不是崩溃后可恢复任务。
