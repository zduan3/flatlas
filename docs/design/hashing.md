# 惰性 Hash 与重复候选

> 状态：当前设计。描述 `dupes` 的惰性 hash 流水线、缓存复用、筛选和分组语义；随代码更新。

## 流水线

`scan` 只采集 metadata；内容 hash 只在 `dupes PATH` 执行时、只为 PATH 子树内的候选文件计算。PATH 默认为当前目录，必须是已索引的目录或 namespace root。

1. **size**：单次范围查询选出 PATH 后代中仍为 `present`、普通文件、logical size 大于 0、未被大小筛选排除，且同大小文件数大于 1 的候选。0 B 文件不进入候选、不计算 hash、也不显示为重复组。
2. **quick BLAKE3**：读取文件头 64 KiB 与尾 64 KiB 计算 quick digest。不超过 128 KiB 的文件按前置 `fstat` 验证过的大小一次读取全文。末尾 `fstat` 发现依据变化时标为 `stale`，依据一致但短读时记为读取错误。成功时直接写为 `full_ready`（full digest 与 quick 相同）；已有的小文件 quick 缓存也直接提升为 `full_ready`，不重复读取。
3. **full BLAKE3**：只有 size 与 quick digest 都相同的组（至少两个成员）才以 1 MiB 块读取全文计算 full digest。

`dupes --quick-workers N` 最多同时执行 N 个 quick 文件读取任务，默认 `1` 走串行，N 必须为正整数。并发时同时预取最多 `2 × N` 个候选，主线程按原顺序收取结果、更新进度并写入 SQLite。硬链接别名在主线程处理，需要实际读取时也占用一个并发名额。full 阶段仍串行，quick 采样范围与缓存格式相同。

索引中 `allocated_size=0` 的普通文件（例如 Windows MFT 驻留数据）在同大小分组前排除：不读取内容、不计入 hash 候选，也不进入重复组，即使已有 `full_ready` 缓存。过滤后不足两个文件的大小组不计算 hash，重复组数量和 theoretical logical savings 只使用剩余成员。占用未知（`NULL`）仍参与候选，不能当作 0；该过滤不修改路径状态、scan coverage 或已有 hash，也不将 allocated size 当作真实可回收空间。已有索引以普通重扫更新占用值，无需 migration。

## metadata basis 校验与缓存复用

每个 `file_hash` 行记录计算依据：device、inode、size、mtime、change time 与 birth time（Windows 的 change time 为 `NULL`，不参与比较）。

- 打开文件后先 `fstat`：不是普通文件或与索引中的依据不一致时不读取内容；读取完成后再 `fstat` 一次，仍须与依据一致。例外：root 以 lazy 元数据扫描（Lustre）时，若 device、inode、size 一致而只有 mtime/change/birth time 不同，视为索引中的 lazy 时间滞后，以 `fstat` 的精确时间更新 path 与 hash basis 后继续读取（`tests/test_lazy_hash_basis.py`）；exact root 仍严格比较。其余任一不一致使该文件记为 `stale`（"metadata changed"），读取错误记为 `failed` 并保存错误信息。两者都令本次结果不完整，但不改变 scan coverage 或路径状态。
- quick/full 缓存只有在算法为 `blake3`、quick 采样大小等于当前 64 KiB，且依据与索引中当前 metadata 完全一致时才复用；否则重新读取。
- 扫描发现已用于 hash 的身份、size、mtime、change/birth time 任一变化时，把 `quick_ready` / `full_ready` 标为 `stale`，见 [扫描与目录汇总](scanning.md)。
- 写回 quick/full 摘要、小文件缓存提升或读取错误时，用带条件的 SQLite UPSERT 同时核对 path 状态、metadata basis 与最近扫描 ID。需要回填 lazy 时间时，先在同一写事务内按旧依据执行条件更新，再按回填后的依据写入摘要。索引已更新时，本次结果计为 changed 或保留已有的读取错误，旧任务的结果不会写回。被拒绝的摘要也不会供后续硬链接别名复用。
- `full_verification` 只写 `hash_only`；没有逐字节比较。

完成的 hash 每 100 次写入提交一次，结束时再提交。hash 阶段被中断（Ctrl-C，或 POSIX 上的 `SIGTERM` / `SIGHUP`）时先提交已完成的写入再退出，因此后续 `dupes` 可复用此前的结果；这不等同于 hash 运行审计或可恢复任务。
并发中断时取消尚未开始的读任务，等待运行中的文件操作结束并关闭句柄，保留主线程已写入的 hash。

交互式终端在 stderr 显示候选、quick、full 各阶段的文件数与字节数进度。

## 重复组

重复组由仍为 `present`、普通文件、`file_hash.state='full_ready'` 且 logical size、algorithm 与 full digest 都相同的记录组成，与 [架构不变量](architecture.md#不可破坏的语义) 一致。

- **hardlink alias 算一份**：`count` 是组内路径数，`inodes` 是组内不同 `(device, inode)` 的数量；同一 inode 的多条路径只算一份副本。只由同一 inode 的 alias 组成的组（`inodes < 2`）不是重复，不显示。`inode` 未记录的路径各自算一份。
- **theoretical savings** = `logical_size × (inodes - 1)`，是理论 logical savings，不是实际物理可回收空间。
- **`external_links`**：组内某个 inode 的 `nlink` 大于组内列出的 alias 数，即它还有链接在 PATH 之外或索引之外。此时把组内路径改链或删除不一定释放该 inode，实际回收量需要用 `du --freed` 或在更大的 PATH 上核对。
- `(device, inode)` 来自最近一次扫描，不是跨历史的永久身份；合并只在本次查询内进行。
- **候选与读取**：同大小候选要求至少 2 个不同 inode，只有 alias 的大小不读取内容。同一次 `dupes` 中，alias 的索引依据与缓存匹配时用 `lstat` 核对实际元数据。实际依据一致即可复用摘要，省去内容读取；否则重新校验。
- 组按理论节省量降序、文件大小降序、算法与 digest 升序稳定排序；组内路径按路径升序。路径默认相对于 PATH 显示，`--absolute` 显示绝对路径。

## 筛选参数

`dupes` 的筛选参数如下，大小与节省量均为字节整数：

| 参数 | 默认 | 语义与应用时机 |
|---|---|---|
| `--min-size` / `--max-size` | 0 / 不限 | 文件 logical size 闭区间；在同大小候选分组和 hash 前生效，也约束最终查询。区间外文件不读取，也不显示已有缓存结果 |
| `--min-count` | 2 | 组内不同 inode 数下限（含边界）；full hash 分组后应用 |
| `--min-savings` | 0 | 每组理论 logical savings 下限（含边界）；full hash 分组后应用 |
| `--limit` / `-n` | 不限 | 筛选和稳定排序后最多保留的组数，不截断组内成员 |

大小参数必须在 SQLite 非负 signed-64 范围内且 min 不大于 max；路径数至少 2、节省量非负、组数至少 1，非法输入以退出码 2 结束且不打开数据库。组成员数、节省量与组数限制在 full hash 分组后应用，不减少候选 hash 读取。筛选不删除 hash、不修改 path/coverage，也不改变 dry-run plan 的行为。

## 输出

- **计时**：表格显示 `Timing`，JSONL 首行和 `--report` 的 `type: hash` 记录包含同一份 `stats`。其中 `wall_s` 从打开索引前计到关闭索引后，`unaccounted_s` 为总耗时减去顶层阶段耗时。统计快照在格式化和写出结果前生成。
- **阶段**：`phases` 按耗时降序记录每个实际执行阶段的累计秒数 `s` 和调用次数 `calls`。阶段包括 `index_open` / `index_close`、`candidates`（候选查询和载入）、`cache_lookup`（检查 quick 哈希缓存）、`quick_hash` / `full_hash`（校验与计算）、`full_selection`（按 quick digest 选择 full 候选）、`writeback_lock`、`db_write`、`commit` 和 `result_groups`（结果查询、分组和排序）。
- **写回阶段**：`writeback_lock` 记录执行 `BEGIN IMMEDIATE` 的耗时，也包括硬链接复用前实际开启写事务的情况。`db_write` 记录条件检查与写入的合并耗时，包括被拒绝的尝试和所需的 lazy 时间回填。`commit` 单列事务提交，索引关闭计入 `index_close`。
- **哈希子阶段**：`quick_hash` 和 `full_hash` 可细分为 `open`（路径构造与打开文件）、`fstat`、`read`（含 quick 采样的 seek）、`blake3`（更新与取摘要）和 `alias_stat`（复用硬链接摘要前的元数据校验）。并发 quick 的父阶段记录主线程等待结果和串行处理别名的耗时，子阶段累计各文件任务的耗时，可能因重叠而超过父阶段。串行模式的子阶段耗时已计入父阶段。全缓存命中时会省略未执行的阶段，秒数保留三位小数。

- **表格**：首行汇总筛选后显示的组数、冗余副本数（`inodes - 1` 之和）与理论节省量，再逐组列出大小 × 路径数（与 inode 数不同时附 `(N inodes)`）、节省量、`has links outside this group`（`external_links`）和路径；末尾报告候选数、缓存命中和实际读取量，处理不完整时列出 changed 与 error 数及逐文件明细。
- **jsonl**：第一行是 `type: hash` 的摘要记录（`complete`、`candidate_files`、`candidate_bytes`、`content_files_read`、`changed_files`、`errors`、`cache_hits`、`content_bytes_read`），描述大小筛选后的全部候选，不因组数限制截断；其后每组一行 `type: group`：`algorithm`、hex `digest`、`logical_size`、`count`、`inodes`、`theoretical_savings`、`external_links` 与 `paths`（绝对路径列表）。最后为每项异常输出一条 `type: hash_error` 记录，含绝对 `path`、`stage`（`quick` / `full`）、`status`（`changed` / `failed`）、可空 `error_code` 和 `message`。零组时仍输出摘要和可用明细。
- **CSV**：每组一行，列同组记录，`paths` 为 JSON 列表；没有摘要行，零组时只有表头。用 `--report FILE` 另存诊断。
- 表格路径默认相对 PATH，`--absolute` 改为绝对路径；机器输出始终是绝对路径。
- hash 不完整时 stderr 输出 `warning: duplicate analysis incomplete: ...`。

`cache_hits` 统计本次开始时可复用 quick hash 的候选路径数，也包括有效的 full 缓存。一次运行内复用 hardlink alias 的 digest 时不增加命中计数。`content_files_read` 统计读到内容的候选路径数，`content_bytes_read` 统计交给 hash 的实际字节数，包括 quick 样本、full 读取和失败前已读的内容。

`--report FILE` 可与任意输出格式组合，以独立 JSONL 文件保存本次摘要和逐文件异常。SQLite 保留路径的 hash 缓存和最近错误；按次回查可通过保留命令报告实现。

`hash.complete` 仅描述该候选范围的 hash 处理状态，不保证 scan coverage 完整，也不表示筛选范围外没有重复。`--output FILE` 把同样的内容写入文件。

## 结果的使用边界

`dupes` 输出的是基于完整 BLAKE3 的重复内容候选和理论 logical savings，不是删除授权。需要实际去重时，应选择值得处理的较小目录，让 jdupes 重新扫描、独立验证并由用户显式执行；不要把 `dupes` 的导出结果直接管道到删除命令。

相关提案：[lazy-hash 后续能力](../plan/proposals/lazy-hash.md)。
