# 0.2 扫描中断规划

> 状态：0.2 规划。不纳入已冻结的 MVP（0.1）。

## 状态

本文记录 0.2 的扫描中断规划。0.1 只展示扫描进度；`KeyboardInterrupt` 仍会回滚整次扫描事务，不承诺保存中断前观察到的路径、metadata 或持久化 `cancelled` 终态。0.1 只要求中断不得造成假删除。内容 hash 不属于 scan，另见 [0.2 惰性 Hash 规划](lazy-hash.md)。

## 目标

- 中断后保留已经 checkpoint 的 path 和 metadata。
- 持久化 scan 的文件数、目录数和累计 logical bytes。
- 将 scan 与 scan_scope 明确终结为 `cancelled`。
- partial、failed 或 cancelled scope 绝不触发旧路径删除。
- 正常完成时，scope 的 completed 状态与 `_mark_missing()` 在最终事务中原子提交。

## 推荐实现

1. 新增向前 migration，为 `scan` 增加 `logical_bytes_seen INTEGER NOT NULL DEFAULT 0`，保留既有数据和已发布 migration 语义。
2. 创建 running scan 与 scan_scope 后立即提交；遍历阶段按路径数量或时间间隔执行短事务 checkpoint。
3. 每个 checkpoint 同时提交 path 写入与 scan 计数，进度中的“已保存”统计只使用已提交计数。
4. 捕获 `KeyboardInterrupt` 时回滚当前未完成批次，再用独立事务写入 cancelled 状态；CLI 输出最终已保存统计并返回退出码 130。
5. 只有完整遍历和所需 hash 阶段成功后才在最终事务中设置 completed 并运行 `_mark_missing()`。
6. 进程崩溃可能遗留 running scan；在支持并发扫描前，应设计不会误伤其他活跃进程的恢复或租约机制，不能仅按状态盲目改写。

## 必要回归测试

- 中断后重新打开数据库，已 checkpoint 路径仍可查询。
- cancelled scan/scope 的计数、大小和结束时间正确。
- 完整扫描后删除文件，再中断重扫，旧路径仍为 present。
- migration 升级保留 root、path、scan、error 和 hash 数据。
- 正常 completed scan 仍能准确标记已确认删除。
