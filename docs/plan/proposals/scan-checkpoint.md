# 扫描短事务 checkpoint 与崩溃恢复

> 状态：提案，未实现。当前的单事务扫描、两段式取消与 `--resume` / `--resume-from` 见 [扫描与目录汇总](../../design/scanning.md#中断与续扫)。

## 背景

当前扫描在单个事务中完成：遍历阶段第一次中断可以保存已观测数据并记为 `cancelled`，但收尾阶段再次中断、进程崩溃或调度器在宽限期后 `SIGKILL` 都会回滚整个事务，本次观察和 scan 记录均不存在，无法续扫。续扫也不持久化工作队列，只能依据目录成功时间重新选择工作。内容 hash 不属于 scan，另见 [惰性 Hash 后续能力](lazy-hash.md)。

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
4. 遍历阶段 `KeyboardInterrupt` 的现有两段式处理保持语义不变；checkpoint 落地后将此流程改为回滚当前未完成批次并用独立事务终结。
5. 只有完整遍历和所需 hash 阶段成功后才在最终事务中设置 completed 并运行 `_mark_missing()`。
6. 持久化工作队列，使崩溃后的续扫按队列而不是仅根据时间戳猜测遍历位置继续。
7. 进程崩溃可能遗留 running scan；在支持并发扫描前，应设计不会误伤其他活跃进程的恢复或租约机制，不能仅按状态盲目改写。

## 测试

- 中断后重新打开数据库，已 checkpoint 路径仍可查询。
- cancelled scan/scope 的计数、大小和结束时间正确。
- 第一次中断保留已观测 path 与目录汇总，收尾阶段第二次中断回滚本次扫描。
- 完整扫描后删除文件，再中断重扫，旧路径仍为 present。
- migration 升级保留 root、path、scan、error 和 hash 数据。
- 正常 completed scan 仍能准确标记已确认删除。

## 待定

- checkpoint 的触发粒度（路径数量或时间间隔）与对扫描吞吐的影响。
- running scan 的租约/心跳机制与多进程并发扫描的关系。
