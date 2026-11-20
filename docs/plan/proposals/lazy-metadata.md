# lazy 元数据（Linux statx `AT_STATX_DONT_SYNC`）

> 状态：提案，未实现。当前扫描对每个条目调用一次普通 `lstat`，见 [扫描与目录汇总](../../design/scanning.md#元数据成本)。

## 背景

在 Lustre 上，冷 `lstat` 的成本来自首次 stat 对 OST 的 size glimpse，约 11–12 ms/条目；多线程只能带来约 4 倍加速。`statx(AT_SYMLINK_NOFOLLOW | AT_STATX_DONT_SYNC)` 跳过该同步，约 0.20 ms/条目，且在静止文件上与 `lstat` 结果一致。实测数据见 [平台差异：Linux / Lustre](../../design/platform.md#linux--lustre)。

按实测估算，约 130 万 inode 的 Lustre 全量扫描用 `lstat` 需要 4.5–11 小时，lazy 模式约 5 分钟。

## 方案

- 在 Linux 上，把 `scanning._observation` 中唯一的 `lstat` 替换为通过 `ctypes` 调用的 `statx`：flags 为 `AT_SYMLINK_NOFOLLOW | AT_STATX_DONT_SYNC`，mask 为 `STATX_BASIC_STATS | STATX_BTIME`，并构造与 `os.stat_result` 字段兼容的对象（mode、ino、dev、nlink、uid、gid、size、blocks、mtime_ns、ctime_ns、可用时的 birthtime_ns），使类型判定、allocated size 与 hash basis 代码保持不变。
- `statx` 不可用（`ENOSYS`、旧内核或 libc 缺少符号）或非 Linux 平台时回退到 `lstat`。
- `ls` 的现场条目检查复用同一 `_observation`，随模式一致。

## 语义与安全

- **陈旧性**：lazy 模式下正在写入的文件的 size / mtime 可能滞后于 OST 上的真实值。扫描结果仍只是不同观测时间组成的当前索引估计值，与现有“非快照”语义一致。
- **hash 安全**：hash 已在打开文件后以 `fstat` 在读取前后重验 metadata basis；lazy 值与真实值不一致时文件变为 stale 而不会进入重复组，因此不会产生假重复组，只会令 dupes 结果标记为不完整。
- **coverage 与删除**：不变。lazy 只改变单个条目的 metadata 来源，不改变枚举、错误处理、scope 状态或删除确认规则。
- **可审计**：在 scan 行上记录本次是否使用 lazy 模式，例如 `scanner_version` 后缀，或通过新 migration 增加列（待定）。新列必须走向前 migration 并保留既有数据。

## 测试

- `statx` 结果与 `lstat` 在 mode、ino、dev、size、mtime_ns、ctime_ns、blocks 上逐字段对照（普通文件、目录、symlink、特殊文件）。
- 模拟 `ENOSYS` 与非 Linux 平台的回退路径。
- 构造 lazy 值过期的文件：dupes 不产生重复组，结果标记不完整，文件成为 stale。
- `test_scan_costs` 继续断言每个条目只有一次元数据请求。
- 在 Lustre 上记录扫描耗时，并与 `lstat` 基线对照。

## 待定

- 启用方式：显式 `scan --lazy` 加配置 `scan.lazy`，还是在文件系统为 Lustre（`statfs` magic `0x0BD00BD0`）时自动启用。见 [待决策清单](../decisions.md)。
- lazy 模式的记录方式：`scanner_version` 后缀还是新列。
