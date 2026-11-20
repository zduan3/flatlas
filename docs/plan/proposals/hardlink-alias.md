# dupes 识别 hardlink alias

> 状态：提案，未实现。当前 dupes 按路径计数、不合并 hardlink alias，见 [惰性 Hash 与重复候选](../../design/hashing.md#重复组)。

## 背景

当前重复组按路径计数，同一 `(device, inode)` 的每条路径都会被分别读取、分别计入 `count` 与 `theoretical_savings = size × (count - 1)`。用户用外部工具（例如 `fclones link`）把重复文件改为 hardlink 之后，`dupes` 仍报告同一组并给出完整节省量，同时再次读取每个 alias，既夸大可回收空间又浪费 I/O。

## 方案

- **hash 读取去重**：在同一次 `dupes` 中按 `(device, inode)` 只读取一次内容，把结果写入该 inode 的全部 alias 路径；每条路径仍各自校验 metadata basis。
- **组计数**：组内同时报告路径数与不同 inode 数；节省量改为 `size × (distinct inodes − 1)`。
- **已链接组**：所有路径都指向同一 inode 的组从重复结果中排除，或作为“已链接”单独报告（待定）。
- **索引范围外 alias**：`nlink` 大于范围内 alias 数时，物理节省量未知，必须保守显示（例如标记为不确定，而不是按范围内计数推算）。

## 语义与安全

- `(device, inode)` 不是跨历史永久 ID；只能在同一次查询中、依据仍与当前 metadata 一致时合并，不能跨历史复用。
- 路径冗余、logical 理论节省量、allocated size 与真实物理可回收空间分别报告，不相加或互换。
- JSON/CSV 新增或改变字段（如 `distinct_inodes`、节省量口径）属于机器输出格式变更，必须版本化并在 README 与回归测试中说明。
- 不改变 scan coverage、路径状态或 dry-run plan 的只读性质；plan 不应对同一 inode 的 alias 生成 hardlink 操作。

## 测试

- 同一 inode 的多条路径只读取一次内容，hash 结果对每条路径一致。
- 全部为同一 inode 的组被排除或标为已链接；混合组的节省量按不同 inode 计算。
- `nlink` 大于范围内 alias 数时，节省量标记为不确定。
- inode 复用、basis 变化时不错误合并。

## 待定

- 已链接组排除还是单独报告；表格与 JSON 的呈现方式。
- 与 `du` 的 hardlink 计数（`--count-links`，见 [查询命令 GNU 兼容规划](query-cli.md)）是否共用实现。
