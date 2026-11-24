# 查询命令

> 状态：当前设计。描述 `ls`、`du`、`largest`、`roots`、`df`、`paths`、`rm`、`scans` / `scan-info`、`export`、`plan` 的现有行为；随代码更新。`dupes` 见 [惰性 Hash 与重复候选](hashing.md)，`scan` 见 [扫描与目录汇总](scanning.md)，`config` 见 [命令默认参数配置](config.md)。

## 通用约定

- 除 `ls` 的一层实时枚举和 `df` 的实时容量查询外，查询只读取 SQLite 索引，不隐式遍历或更新文件系统；源目录离线时仍可使用。`dupes` 是唯一会读取文件内容的查询，且不改变 scan coverage。
- 当前状态查询只使用 `state='present'` 的路径；deleted tombstone 不参与 `paths`、`du`、`largest` 或 `dupes`。
- 未知值在表格中显示 `-`，在 JSON/CSV 中为 `null` / 空值，不能伪装为 0。
- 用户可预期错误（未初始化、路径未索引、namespace 未登记、参数非法等）输出 `error: ...` 并返回退出码 2，不输出 traceback。
- JSON/CSV 字段稳定、可解析；二进制 digest 以 hex 输出；`--output PATH` 写入文件（自动创建父目录）。
- `--db PATH` 只是全局索引位置的显式覆盖。
- 按路径定位条目（`du`、`find`、`ls` 等的 PATH，`scans PATH`、`rm PATH`）时，由已登记 root 的路径算出 `(root_id, path_key)` 走唯一索引；`path_display` 没有索引，只在这样查不到时（如非 UTF-8 名字）回退按显示路径全表查找。
- `flatlas --version` 输出正在运行的包版本；可访问源码仓库时附加 Git 提交、提交日期和 `dirty` 状态，否则回退为纯包版本。该命令无需初始化或打开索引数据库。

### 大小显示

`ls -h`、`du -h` 和 `largest -h`（长选项 `--human-readable`）按 GNU coreutils 的 `-h` 规则显示表格大小：以 1024 为底、单位 `K` 至 `E`、一律向上取整（大小不会被低估），小于 10 时保留一位小数（如 `1.0K`、`1.5M`、`12G`）；小于 1024 的值显示字节整数，未知值显示 `-`。`roots -h` 与 `df -h` 使用同一规则。大小表头省略 `(B)`；文件数、状态不缩放，JSON/CSV 仍输出原始字节整数。

表格按 Unicode 显示宽度对齐。`ALLOC(B)` 列在至少一行占用已知时显示，未知行显示 `-`；全部未知时隐藏此列。

## 职责划分

```text
ls       一层结构 + live/index 状态 + 已知空间提示
du       显式 scope 的递归索引汇总
largest  scope 内的最大单文件
dupes    scope 内的重复内容候选
```

- `ls PATH` 回答“PATH 当前有哪些直接子项，它们是什么，索引对它们了解多少”。它实时枚举一层，将文件自身大小与目录的已知递归聚合大小放在同一视图中，方便选择下一步分析目标。
- `du PATH ...` 回答“每个显式目标及其后代的索引聚合是多少”。它不枚举现场直接子项，默认每个参数一行，源目录离线时仍可查询。
- `largest PATH` 回答“PATH 子树中最大的单个当前普通文件是什么”，避免让 `ls` 或 `du` 承担全树文件排序。

## `ls [PATH]`

PATH 默认为当前目录，必须位于已登记的 namespace。命令实时枚举 PATH 的直接条目（文件、目录和特殊条目，包含名称以 `.` 开头的真实条目，不合成 `.` 与 `..`），对每个条目做一次 `lstat`，并与索引中该目录的 present 直接子项和目录汇总缓存合并。结果按名称排序，最后一列只显示条目名称。实时枚举或条目检查发生权限、I/O 等错误时命令以退出码 2 失败，不通过缺行暗示目录不存在。

表格列为 `T`、`S`、`LOGICAL(B)`、`N`、可用时的 `ALLOC(B)`、`SCANNED-THROUGH` 和 `NAME`：

- `T`：条目类型首字母（`d` 目录、`f` 普通文件、`s` symlink、`r` reparse point、`o` 其他）。
- `LOGICAL(B)`：普通文件为实时 stat 得到的自身 logical size；目录为索引中以它为根的普通文件递归 logical size（来自目录汇总缓存）；特殊条目为未知。flatlas 有意不显示 GNU `ls -l` 中目录对象自身的 `st_size`（常见为 4 KiB），因为它不代表内容总量。
- `N`：目录为递归普通文件数；普通文件和特殊条目在表格中显示 `-`（JSON/CSV 中普通文件的 `files` 为 1，特殊条目为 `null`）。
- `ALLOC(B)`：普通文件为实时采集的占用空间，目录为缓存中的递归占用汇总。
- `SCANNED-THROUGH`：目录子树 freshness 下界 `subtree_complete_through_ns`，含义与时间格式见 [扫描与目录汇总](scanning.md#目录汇总缓存)；非目录为 `-`。

`S` 状态（JSON/CSV `status` 字段对应 `scanned` / `unscanned` / `incomplete` / `missing`）：

- `ok`：最近覆盖该条目的 scope 为 completed，或未完成扫描已完整覆盖它，且实时条目与当前索引均为 present、类型一致。
- `new`：实时存在，但没有当前有效的扫描覆盖（未索引、类型变化或没有覆盖它的 scope）。
- `part`：最近未完成 scan 已观察该条目但尚未完成相应目录 coverage；未被本次访问的条目回退到此前最近 coverage 状态。
- `gone`：索引中 present 的直接条目在本次完整实时枚举中未出现。它只描述本次视图，不更新 `path.state`；表格继续展示索引中最后已知的文件大小或目录聚合，但不得计为新的现场观测。

目录状态只表示目录自身的完整枚举情况，普通文件和其他直接条目以父目录是否完整枚举为 coverage 依据；外层 cancelled scope 不会把全部直接条目统一显示成 `part`。`ls` 的状态列是快速告警灯，不是递归 coverage 诊断界面；详细判定见 [扫描与目录汇总](scanning.md#查询如何使用缓存)。

### `ls` 与删除状态

`ls` 只提供实时存在性证据，不负责持久化删除：

1. 用户在 flatlas 之外删除文件或目录后，索引暂时仍可保持 `present`。
2. `ls` 完整枚举父目录时将缺失的直接文件或目录显示为 `gone`，但不写入 `path.state`。
3. 要确认删除，必须扫描一个仍然存在且覆盖被删除路径的父目录；不能扫描已经不存在的目标本身。
4. 只有该 scan scope 完整完成，缺失路径及其已索引后代才标记为 `deleted`；partial、failed、cancelled 或枚举错误均不确认删除。
5. `deleted` 行保留在 SQLite 作为当前状态 tombstone，被当前状态查询排除；同一路径再次出现并被扫描时恢复为 `present`，metadata basis 不匹配时旧 hash 标记为 stale。

## `du [PATH ...]`

无 PATH 时汇总当前目录 `.`；每个参数输出一行递归汇总并保持参数顺序，因此也能处理 shell 或启动器提前展开后的通配结果。与 GNU `du` 一致，PATH 的上级目录会被解析，最后一级 symlink 默认不跟随，汇总的是 symlink 条目本身；PATH 以 `/` 结尾或使用 `-D`/`-H`（`--dereference-args`）时才跟随到目标，例如 `du ~/data/` 或 `du -D ~/data`。`largest`、`dupes` 的 PATH 仍完整解析。某个 PATH 出错（未索引、不在已登记 namespace 内）时，错误写到 stderr，其余 PATH 照常输出，命令最后以退出码 1 结束。`du` 不实时枚举目标：

- 已索引目录或 namespace root：读取目录汇总缓存中的 `subtree_files`、`subtree_logical_size` 与 `subtree_allocated_size`。
- 已索引普通文件：`N=1`，大小为索引中的文件大小。
- 已索引的特殊条目（FIFO、socket、设备文件、未跟随的 symlink 等）：文件数与大小均为未知（表格 `-`，JSON/CSV `null`），与 `ls` 一致。扫描不记录它们自身的大小，不能显示为 0。
- 已登记 namespace 内实际存在但尚未索引的目录：显示 `new`，文件数和大小为未知值；多路径查询仍保留其他已索引路径的汇总。其他未索引路径报错。

表格列为 `S`、`SIZE(B)`（logical bytes）、`N`（普通文件数）、`E`（条目数）、可用时的 `ALLOC(B)` 与 `PATH`（相对当前目录）。`E` 统计全部类型的条目（含目录、symlink、特殊条目）并包含目录自身，与 GNU `du --inodes` 一致，用于定位 inode 配额压力；文件与特殊条目为 1，未索引目录未知。`SIZE`、`N`、`ALLOC` 按路径计数：同一 inode 的每条 hardlink 路径都计一次（相当于 GNU `du -l`），因为它们直接读取目录汇总缓存。

`--freed` 增加两列，需要递归遍历 scope（约 60 万条目时约 10 s）：

- `UNIQ(B)`：scope 内每个 `(device, inode)` 只计一次，相当于 GNU `du` 默认口径。
- `FREED(B)`：只计索引中链接数（`nlink`）全部落在 scope 内的 inode，即删除整个 scope 后实际释放的空间；有链接在 scope 外（含索引范围外）的 inode 不计入。`nlink` 未记录的文件不计入，数量见 `unknown_link_files`。
- 两列有 `ALLOC` 时按 allocated size，否则按 logical size；JSON/CSV 同时给出 `unique_logical_size`、`unique_allocated_size`、`freed_logical_size`、`freed_allocated_size` 和 `freed_files`（释放的 inode 数）。
- 依据是最近一次扫描记录的 `nlink`；扫描之后在 scope 外新增或删除的链接不会反映出来。

深度展开与排序（都只读目录汇总缓存，不递归遍历文件）：

- `-d N`/`--max-depth N`：每个 PATH 之后按路径顺序（父目录在前）列出 N 层以内的已索引子目录；JSON/CSV 的 `depth` 为相对 PATH 的层数，PATH 自身为 0。与 GNU `du` 的后序输出不同。
- `--sort size|alloc|files|entries|freed`：按 logical size、allocated size、文件数、条目数或 `FREED`（需 `--freed`）降序排列全部行，未知值排在最后。
- `-n N`/`--limit N`：排序后保留前 N 行，例如 `du -d 3 --sort entries -n 20 ~/data` 列出条目最多的 20 个目录。

`--files0-from FILE` 从 FILE（`-` 为 stdin）读取以 NUL 分隔的 PATH（末尾 NUL 可省略），与 GNU `du` 相同：不能再同时给 PATH 参数；空文件名按 `FILE:序号` 报错、从 stdin 读时名为 `-` 的项报错，均跳过该项、其余照常输出，最后退出码 1。用于汇总 `find ... -print0` 给出的大量路径，例如 `flatlas find projects -type d -name build -prune -print0 | flatlas du --files0-from=- -c --freed`；`-c` 与 `--freed` 的并集合计因此不会被 xargs 分批拆开。

`-c`/`--total` 在最后加一行 `total`：`SIZE`、`N`、`E`、`ALLOC` 是各 PATH 行（不含 `-d` 展开的子目录）按路径的和（任一行未知则未知，参数重叠时会重复计数）；与 `--freed` 同用时，`UNIQ`、`FREED` 按全部参数的并集计算，重叠部分只计一次，回答“这些路径一起删能腾出多少”。

状态列与 `ls` 一样使用 `ok` / `new` / `part`，JSON/CSV 的 `status` 字段对应 `scanned` / `unscanned` / `incomplete`；`du` 不提供 `ls` 现场枚举得出的 `gone`。最近覆盖目标的 scope 为 completed 时显示 `ok`；最近覆盖它的 scan 未完成时，目录及其全部当前子目录都已在该 scan 中完整枚举才显示 `ok`（`--refresh-before` 扫描中改用子树 freshness 与 cutoff 比较）。目录的 `part` 状态可以附带最后已知聚合，但该值可能不完整或过期。

## `find [PATH ...] [EXPRESSION]`

用 GNU `find` 表达式查询索引中 PATH 下的 present 条目，只读 SQLite，不访问文件系统；未知 expression 报错（退出码 2），不静默忽略。命令自身的选项（`--db`、`-H`）必须写在 PATH 之前，第一个以 `-` 开头或为 `!`、`(`、`)` 的参数开始表达式。

- 起点：省略时为 `.`；按 GNU 规则打印为给定形式加相对分量（`.` → `./a`，`a/` → `a/b`）。最后一级 symlink 默认作为条目本身处理，`-H` 或末尾 `/` 时跟随，规则与 `du` 相同。某个起点出错时写 stderr，其余照常输出，最后退出码 1。
- 顺序：父目录在前的路径顺序（同一目录内按原始名字节排序），不是 `readdir` 顺序。
- 测试：`-name`/`-iname`（basename）、`-path`/`-ipath`（别名 `-wholename`/`-iwholename`，匹配打印出的路径，`*` 也匹配 `/`）、`-type f|d|l|p|s|c|b`（可用逗号列出多个），以及 `-true`、`-false`。模式为 shell 通配（`fnmatch`），不对开头的 `.` 特殊处理。
- 动作：`-print`、`-print0`、`-prune`（匹配的目录不再展开）；没有 `-print`/`-print0` 时与 GNU 一样对整个表达式隐式 `-print`。不提供 `-delete`、`-exec` 等修改或执行动作。
- 运算符：`( )`、`!`/`-not`、`-a`/`-and`（可省略）、`-o`/`-or`，优先级同 GNU；全局选项 `-maxdepth N`、`-mindepth N`。
- 结果是索引状态：未扫描或不完整的区域不会出现，扫描后的增删要重新扫描才反映。
- 实现：`path_key` 由 NUL 结尾的分量拼成，起点下的子树正好是唯一索引 `(root_id, path_key)` 上的一段连续区间，且区间顺序就是上面的输出顺序。因此按区间顺序读取，不用递归 CTE，也不排序；遇到 `-prune` 命中的目录、到达 `-maxdepth` 的目录或已删除的目录时，从该子树的上界（把末尾 NUL 换成 `0x01`）重新开始区间查询，整棵子树一行都不读。打印路径对相对 key 只做一次解码。2026-11 在 380 万条目、4.6 GB 的索引上，`-type d -name build -prune` 从 41.5 s 降到 19.9 s（本地盘，CPU 为主）。

典型用法是找出最外层的可重建目录再汇总：`flatlas find projects -type d \( -name build -o -name __pycache__ \) -prune -print0 | flatlas du --files0-from=- -c --freed`。

## `largest [PATH]`

PATH 默认为当前目录，必须是当前已索引的文件或目录 scope。只列 scope 内 present 普通文件，按 logical size 降序、路径升序稳定排序；默认返回 10 项，`-n` 是 `--limit` 的短别名。路径相对于 PATH 显示。表格只显示 `SIZE(B)`（使用 `-h` 时为 `SIZE`）和 `PATH`，不显示 allocated 列；JSON/CSV 含 `root_id`、`logical_size`、`allocated_size` 与 `path`。

## `roots`

只读 SQLite，不访问文件系统，显示每个 registered namespace 在索引中记录的内容：

```text
ID  TYPE    METADATA  FILES  LOGICAL(B)  SCAN  STATUS     SCANNED       ROOT
 1  lustre  lazy      19118   534929936     1  completed  Nov 19 22:47  /lustre/fs1
```

- `TYPE`：登记时记录的 `filesystem_type`，未知为 `-`。
- `METADATA`：扫描该 root 时采用的元数据方式，`lazy`（Lustre，见 [扫描与目录汇总](scanning.md#lustre-上的-lazy-元数据)）或 `exact`。由记录的类型决定，不检测当前挂载。
- `FILES`、`LOGICAL(B)`：namespace root 目录汇总中的已索引普通文件数与 logical size；尚未扫描时为 `-`。`-h` 缩放大小列，规则同 `ls` / `du`。
- `SCAN`、`STATUS`、`SCANNED`：该 root 最近一次 scan 的 ID、状态与结束时间（未结束时为开始时间），时间格式同 `ls` 的 `SCANNED-THROUGH`。
- JSON/CSV 输出 `id`、`root_path_display`、`platform`、`namespace_kind`、`filesystem_type`、`metadata`、`enabled`、`created_at_ns`、`updated_at_ns`、`files`、`logical_size`、`allocated_size`、`last_scan_id`、`last_scan_status`、`last_scan_started_at_ns`、`last_scan_finished_at_ns`；未知为 `null`。

## `df [FILE ...]`

实时查询容量，行为贴近 GNU `df`：

```text
Filesystem               Type    Size  Used  Avail  Use%  Mounted on
mgs@tcp:/fs1             lustre  325T  284T    42T   88%  /lustre/fs1
```

- 给出 FILE 时，对每个 FILE 本身调用 statfs 并显示其所在挂载点，不打开索引，FILE 也不必已登记。与 GNU 一致，查询的是 FILE 而非挂载点：带目录配额的文件系统（如 Lustre project quota）会返回该目录适用的配额，因此 `df ~/data` 可能显示项目配额而不是整个文件系统。
- 无 FILE 时列出全部 registered namespace（对 root 路径查询，即整个文件系统）。**这与 GNU 不同**：GNU 无参数时列出系统挂载表中的全部文件系统。
- `Filesystem` 为挂载表中的来源（设备或远端路径），取不到时为挂载点；`-T` 增加 `Type` 列（挂载表中的实时类型）。
- 默认列为 `1K-blocks`、`Used`、`Available`，按 1024 bytes 向上取整；`-h` 改为 `Size`、`Used`、`Avail`，规则见上文大小显示。
- `Used` 为 total − free，`Available` 为非特权用户可用空间；`Use%` = ⌈used ÷ (used + available)⌉，与 GNU 相同，因此 `Used + Available` 可以小于总量。
- 容量不可访问时显示 `-`，JSON/CSV 为 `null` 且 `status=unavailable`；可访问时 `status` 为 `ok`，root 被禁用时为 `disabled`。JSON/CSV 字段为 `filesystem`、`type`、`blocks_1k`、`used_1k`、`available_1k`、`use_percent`、`mounted_on`、`status`。
- 容量不缓存进 SQLite，`df` 不承诺离线可用。filesystem used 与 `du` 的 indexed allocated 汇总不是同一指标，两者差值不能解释为“未索引文件”或“可回收空间”。

## `paths [PATH]`

列出当前 present 路径（PATH 给出时限定为该已索引 scope 及其后代），按显示路径排序，默认最多 500 行（`--limit`）。输出 `root_id`、`path_display`、`entry_kind`、`logical_size`、`allocated_size`、`mtime_ns`，格式为 JSON（默认）或 CSV。

## `rm PATH`

只从 SQLite 递归移除该路径及其已索引后代，等价于对索引执行 `rm -r PATH`；它不访问、不验证也不改动实际文件。PATH 按词法转为绝对路径（不解析 symlink、不检查文件系统），必须与某个索引路径完全一致，否则报错。

- 路径行被物理删除，其 `file_hash` 与 `directory_summary` 随外键级联删除；这与扫描写入的 `deleted` tombstone 不同。
- 若子树被 dry-run plan 引用，整个 plan 一并移除，避免保留失效计划。
- 删除后刷新仍存在的父目录与祖先汇总。
- 输出 JSON：`path`、`paths_removed`、`plans_removed`。

## `scans [PATH]` 与 `scan-info ID`

两个命令只读取索引中的扫描审计记录，不访问文件内容。

- `scans [PATH]` 每个 scope 一行，按 scan ID 降序，默认最多 50 行（`--limit`），格式为表格（默认）、JSON 或 CSV。表格列为 `ID`、`STATUS`、`STARTED`、`FINISHED`（本地时区 ISO 8601）、`FILES`、`DIRS`、`ERRORS`、`SCOPE`；scope 路径不在索引中时显示 `key:<hex>`。JSON/CSV 另含 `kind`、`duration_ns`、`refresh_before_ns`、`scanner_version`、`note`、scope 状态与计数等字段。
- PATH 过滤同时包含覆盖该路径的祖先 scope 和位于该路径下的子树 scope；PATH 可以尚未索引，但必须位于已登记的 namespace。
- `scan-info ID` 以 JSON 展示单次 scan 的字段、分阶段计时 `stats`（见 [分阶段计时](scanning.md#分阶段计时)）、全部 scope（含状态、时间、计数）以及 `scan_error` 明细（路径、operation、错误码与消息）。

## `export dupes`

`export dupes [PATH] --output FILE [--format json|csv]` 与 `dupes` 使用同一实现和筛选参数，默认 JSON，写入文件后在 stdout 打印输出路径；语义见 [惰性 Hash 与重复候选](hashing.md#输出)。

## `plan create` / `plan show`

- `plan create --root PATH`：PATH 所属 namespace 必须已登记。命令使用该 root 内当前已缓存的 `full_ready` 重复组（不读取文件、不计算新的 hash，需要先运行 `dupes`），为每组按路径字典序选第一条路径为 canonical，其余每条路径生成一个 `hardlink` 操作（状态 `planned`；已是 canonical 的 hardlink alias 的路径跳过），记录预期 size、digest、算法和前置条件；plan 状态为 `dry_run`，创建即不可变（`immutable_at_ns`），`source_scan_id` 为该 root 最近一次 scan。stdout 只打印 plan ID。
- `plan show`：无 ID 时列出 plan 摘要（root、状态、时间、操作数和按预期 size 求和的理论节省量）；给出 ID 时列出该 plan 的操作明细。格式为 JSON（默认）或 CSV。

plan 是候选建议快照，不构成删除或替换授权；flatlas 没有 `apply`，不执行任何 operation。

相关提案：[查询命令 GNU 兼容规划](../plan/proposals/query-cli.md)。
