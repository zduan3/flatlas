# 0.2 扫描中断规划

> 状态：0.2 规划。不纳入已冻结的 MVP（0.1）。

## 状态

本文记录 0.2 的扫描中断规划。0.1 只展示扫描进度并在 `KeyboardInterrupt` 时回滚整次扫描事务；当前 0.2 已实现单事务内的两段式用户取消，以及基于目录成功时间的显式 `--resume`，但尚未实现短事务 checkpoint、崩溃恢复或持久化工作队列。内容 hash 不属于 scan，另见 [0.2 惰性 Hash 规划](lazy-hash.md)。

目录汇总缓存与每目录完整枚举时间已经由 migration 2 落地。遍历阶段第一次中断会停止扫描并进入批量缓存收尾，成功后保存已观测数据并持久化 `cancelled`；收尾阶段再次中断则回滚本次扫描。它仍不构成 checkpoint 或可恢复工作队列。CLI 在 POSIX 上把 `SIGTERM`（批处理调度器终止作业前发送）和 `SIGHUP`（终端关闭或远程会话断开）转为同一 `KeyboardInterrupt` 路径，因此它们与 Ctrl-C 一样保存已观测数据；父进程已忽略的信号（如 `nohup` 下的 `SIGHUP`）保持忽略。调度器在 `SIGTERM` 后的宽限期内若收尾未完成即 `SIGKILL`，本次扫描仍整体回滚。相关字段和后续 freshness 选择边界见 [目录汇总缓存与扫描时间](../../design/scanning.md)。

`flatlas scan PATH --resume` 只匹配 root 与规范化 scope 完全相同的最近 scan。最近状态必须是 `cancelled`、`partial` 或 `failed`；`completed` 不可恢复，`running` 不会被自动接管。恢复调用创建新的独立 scan，不保存 `resumed_from_scan_id`：若上次 scan 已有 `refresh_before_ns` 则原值继承，否则使用上次 scan 的 `started_at_ns`。因此多次用户中断始终使用最初的固定 cutoff，目录仍记录各自实际完成时间，失败目录的成功时间不推进并会在下次恢复时重试。

`flatlas scan --resume-from ID` 使用相同规则显式选择历史 scan，并从其唯一 `scan_scope` 读取 `scan-info ID` 所展示的 PATH；ID 必须存在、状态未完成且只有一个 scope。显式选择允许在存在更新的扫描记录时仍从指定的旧 cutoff 刷新，但不会修改或关联原 scan。`--resume`、`--resume-from` 与 `--refresh-before` 互斥，使用 `--resume-from` 时不得再传 PATH。

当前恢复只适用于已经成功保存 `cancelled`/`partial`/`failed` 记录的调用。首次中断后的收尾阶段若再次中断、进程崩溃或异常导致单事务回滚，本次观察和 scan 记录均不存在，不能从该调用恢复；此限制仍需短事务 checkpoint 和持久化工作队列解决。

## 目标

- 中断后保留已经 checkpoint 的 path 和 metadata。
- 持久化 scan 的文件数、目录数和累计 logical bytes。
- 将 scan 与 scan_scope 明确终结为 `cancelled`。
- partial、failed 或 cancelled scope 绝不触发旧路径删除。
- 正常完成时，scope 的 completed 状态与 `_mark_missing()` 在最终事务中原子提交。

## 推荐实现

1. 新增向前 migration，为 `scan` 增加 `logical_bytes_seen INTEGER NOT NULL DEFAULT 0`，保留既有数据和已发布 migration 语义。
2. 创建 running scan 与 scan_scope 后立即提交；遍历阶段按路径数量或时间间隔执行短事务 checkpoint。
3. 每个 checkpoint 同时提交 path 写入、scan 计数与受影响的目录汇总，进度中的“已保存”统计只使用已提交计数。
4. 捕获遍历阶段第一次 `KeyboardInterrupt` 时停止枚举，批量刷新已观测数据的目录汇总并提交 cancelled 状态；收尾阶段再次中断时回滚未提交事务。短事务 checkpoint 落地后再将此流程改为回滚当前未完成批次并用独立事务终结。
5. 只有完整遍历和所需 hash 阶段成功后才在最终事务中设置 completed 并运行 `_mark_missing()`。
6. 进程崩溃可能遗留 running scan；在支持并发扫描前，应设计不会误伤其他活跃进程的恢复或租约机制，不能仅按状态盲目改写。

## 必要回归测试

- 中断后重新打开数据库，已 checkpoint 路径仍可查询。
- cancelled scan/scope 的计数、大小和结束时间正确。
- 第一次中断保留已观测 path 与目录汇总，收尾阶段第二次中断回滚本次扫描。
- 完整扫描后删除文件，再中断重扫，旧路径仍为 present。
- migration 升级保留 root、path、scan、error 和 hash 数据。
- 正常 completed scan 仍能准确标记已确认删除。
