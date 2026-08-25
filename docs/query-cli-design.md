# `ls` / `du` / `df` 查询命令设计

## 状态与目标

本文定义 File Atlas 查询体验的后续兼容基线。当前实现仍以 [MVP 实现状态](mvp-implementation.md) 为准；本文中的“目标默认行为”和参数除明确标为已实现者外，均是设计规划，不代表当前 CLI 已支持。

兼容参照采用 GNU Coreutils 的 `ls` 与 `du`。Linux 上的 BusyBox、BSD 工具和发行版 alias 可能不同，因此这里追求的是一致的用户心智模型和常用参数语义，不承诺逐字符复刻 GNU 输出。

设计目标：

- `flatlas ls [PATH]` 默认接近用户主动执行 `ls -Al PATH`：列出一层直接条目、包含真实隐藏条目但不合成 `.` 与 `..`、采用长格式。
- `flatlas du [PATH ...]` 默认接近 `du -s PATH ...`：每个参数仅输出一条递归汇总。
- `flatlas roots` 与别名 `flatlas df` 默认接近 GNU `df` 的容量表，但范围只包含 registered namespace。
- 后续增加的短参数和长参数，在不破坏持久化索引、coverage 与跨平台语义时优先沿用 GNU 名称。
- flatlas 独有能力使用可读的长参数表达，不占用与 GNU 含义冲突的短参数。
- 表格服务交互使用；JSON/CSV 字段必须稳定、明确，不因终端缩写而改变语义。

## 为什么只能“类似”而不能完全兼容

GNU `ls` 和 `du` 主要读取当前文件系统；flatlas 同时使用实时目录枚举、持久化路径状态、scan coverage 和离线 metadata。由此产生不可消除的差异：

| 方面 | GNU Coreutils | flatlas 设计 |
|---|---|---|
| 数据来源 | 当前文件系统 | 实时枚举与 SQLite 索引的受控合并 |
| 离线查询 | 源路径不可访问时失败 | 索引查询仍可用；实时模式失败时不得伪造状态 |
| 删除判断 | 本次调用观察当前目录 | 只有 completed scan scope 才能持久化 `deleted`；`ls` 的实时 `gone` 不写数据库 |
| 目录大小 | `ls -l` 显示目录项自身大小，`du` 递归估算空间 | 为归档浏览优化，`ls` 可附带索引中的递归总量，但必须明确它不是目录项 inode 大小 |
| 空间指标 | `du` 默认以文件系统使用量和 block 单位输出 | 同时保存 logical 与可用时的 allocated size；任一未知不得伪装为 0 |
| hardlink | `du` 默认对同一 inode 只计一次，`-l` 才重复计数 | 只能基于当前可靠对象身份保守去重；身份不可靠时必须标注限制 |
| symlink / reparse | 由 `-H`、`-L`、`-P` 等控制 | MVP 默认不跟随 symlink 或 Windows reparse point；兼容参数须在跨平台策略完成后加入 |
| 排序与字符 | locale、终端与环境变量会影响输出 | 表格按 Unicode 显示宽度对齐；机器输出使用稳定字段和排序规则 |

`df` 还有一项产品边界差异：GNU `df` 无参数时列出当前系统挂载表中的文件系统；flatlas 的 `roots` / `df` 无参数时只列内部 registered namespace。两者不能静默混为一谈。

## `ls` 的目标行为

### 默认行为

目标默认命令：

```text
flatlas ls [PATH]
```

在交互体验上视为隐式选择 `-A -l`：

1. PATH 省略时使用当前目录。
2. 只列 PATH 的直接条目，不递归展开。
3. 文件与目录都列出，包含名称以 `.` 开头的真实条目。
4. 最后一列只显示条目名称，不附加 PATH 前缀。
5. 默认长表包含类型/coverage 状态、可用 metadata、索引统计和名称。
6. 目录的统计列表示索引中的递归总量；文件的统计列表示文件自身。
7. `new`、`part`、`gone` 的不完整或不可用统计默认显示 `-`，不能显示为精确的 0。

推荐的紧凑表格概念如下；实际列应根据跨平台可用性和终端宽度逐步落地：

```text
S     MODE        NLINK  OWNER  GROUP  SIZE      ALLOC     MTIME             NAME
ok    drwxr-xr-x      3  1000   1000   12.4 MiB  16.0 MiB  2026-08-25 10:00  archive
new   d---------      -  -      -      -         -         -                 incoming
part  drwx------      2  1000   1000   -         -         2026-08-25 09:30  private
gone  d---------      -  -      -      -         -         -                 removed
```

其中 `S` 是 flatlas 扩展：

- `ok`：最近覆盖该目录的 scope 为 completed，且实时条目与当前索引均为 present。
- `new`：实时存在，但没有当前有效的完整扫描覆盖。
- `part`：最近覆盖 scope 为 running、partial、failed 或 cancelled。
- `gone`：索引曾观察到该直接子目录，但本次完整实时枚举未观察到。它只描述本次视图，不更新 `path.state`。

实时枚举只要发生权限、I/O 或类型检查错误，命令就应非零退出，不得通过缺行暗示目录不存在。离线使用由显式的数据源参数处理。

### GNU 兼容参数规划

| 优先级 | 参数 | 规划语义 |
|---|---|---|
| P1 | `-a`, `--all` | 按 GNU 语义包含全部条目，并额外合成 `.` 与 `..`；与默认的隐式 `-A` 有意不同。 |
| P0 | `-l` | 显式请求长格式；目标默认已启用。与 `--format table` 配合，不改变 JSON/CSV schema。 |
| P0 | `-h`, `--human-readable` | 以 1024 进制缩写显示 size；只影响表格，不改变机器输出中的整数值。 |
| P0 | `-A`, `--almost-all` | 包含真实隐藏条目但不显示 `.` / `..`；目标默认已启用，保留该参数作为显式兼容写法。 |
| P1 | `-d`, `--directory` | 将参数目录当作一个条目，而不是列其内容。 |
| P1 | `-R`, `--recursive` | 递归列出子目录；必须携带每个目录的 coverage 状态且有深度/数量保护。 |
| P1 | `-S` | 按 size 降序排序；目录使用明确选择的递归统计指标。 |
| P1 | `-t` | 按 mtime 降序排序。 |
| P1 | `-r`, `--reverse` | 反转当前排序。 |
| P1 | `-1` | 每行一个名称；主要用于脚本和窄终端。 |
| P2 | `--sort=WORD` | 支持 `name`、`size`、`time`、`status`、`none`。 |
| P2 | `--time=WORD` | 在平台可用时选择 mtime、ctime/status 或 birth time；不可用字段显示 `-`。 |
| P2 | `-F`, `--classify` | 为目录、symlink 等追加类型指示符；不得把 reparse point 误标为普通目录。 |
| 暂缓 | `-H`, `-L` | 只有 symlink/reparse 的跨平台跟随策略、循环保护和 mount 边界完成设计后实现。 |

### 易用性扩展参数规划

这些是 flatlas 推荐能力，不强行映射为 GNU 短参数：

| 参数 | 推荐理由 |
|---|---|
| `--source=merge|live|index` | `merge` 为默认实时+索引状态视图；`live` 只看当前条目；`index` 支持离线浏览。 |
| `--status=STATUS,...` | 快速筛选 `ok`、`new`、`part`、`gone`，适合发现待扫描目录。 |
| `--dirs-only` / `--files-only` | 比 shell glob 更可靠，避免通配符在进入 flatlas 前被展开。 |
| `--totals=logical|allocated|both|none` | 明确目录递归统计列，避免把 logical、allocated 与真实可回收空间混淆。 |
| `--allow-partial-totals` | 默认隐藏不完整统计；显式启用后用 `~` 或独立 completeness 字段标注估计值。 |
| `--absolute` | 默认只显示名称；需要复制完整路径时显式请求。 |
| `--null` | 使用 NUL 分隔名称，安全处理换行等特殊字符；语义参照 Coreutils 通用做法。 |

## `du` 的目标行为

### 默认行为

目标默认命令：

```text
flatlas du [PATH ...]
```

默认行为等价于隐式 `--summarize`：

1. 没有 PATH 时汇总当前目录 `.`，不再默认跨全部 registered namespace。
2. 每个参数仅输出一行递归总量，保持参数顺序。
3. 多路径参数继续支持 shell 或启动器已经展开的结果。
4. 默认不输出每个后代；这与 `du -s` 一致。
5. 全局 namespace 汇总改由明确的 flatlas 参数 `--all-roots` 请求。
6. 表格默认同时明确 logical、allocated 和文件数；allocated 不完整时显示 `-`，不回退成 0。
7. JSON/CSV 使用完整字段名和原始整数，不受 `-h`、表头缩写或 block 显示影响。

推荐默认表格：

```text
USED(K)  APPARENT(B)      N  PATH
  16384      12345678  1024  archive
      -          4096     1  windows-or-unknown
```

- `USED(K)` 对应可可靠聚合的 allocated size，并以 1024-byte block 显示，接近 GNU `du` 默认心智模型。
- `APPARENT(B)` 对应 logical/apparent size，避免 Windows 或某些文件系统没有 allocated 数据时命令失去用途。
- 两列并存是 flatlas 的透明性扩展；不能宣称 USED 是真实物理可回收空间。

hardlink 的默认计数应尽量对齐 GNU `du`：同一可靠对象身份在同一次聚合中只计一次；`--count-links` 才按每条路径重复计数。由于 `(device, inode)` 不是跨历史永久 ID，只能在身份与观测依据仍可靠时去重，否则必须保守计数并在机器输出中暴露 completeness/limitations。

### GNU 兼容参数规划

| 优先级 | 参数 | 规划语义 |
|---|---|---|
| P0 | `-s`, `--summarize` | 每个参数只输出一个总量；目标默认已启用，保留参数作为显式兼容写法。 |
| P0 | `-h`, `--human-readable` | 1024 进制的人类可读单位。 |
| P0 | `--si` | 1000 进制单位。 |
| P0 | `-b`, `--bytes` | 等价于 apparent size 且 block size 为 1；表格可只显示 apparent 列。 |
| P0 | `--apparent-size` | 以 logical/apparent size 作为筛选、排序和主显示指标。 |
| P0 | `-B SIZE`, `--block-size=SIZE` | 控制表格的 used/apparent 显示缩放；机器输出仍保留字节整数。 |
| P0 | `-c`, `--total` | 在所有参数后增加 grand total，并明确 hardlink 去重范围。 |
| P1 | `-a`, `--all` | 输出目录以及普通文件，而非仅目录。 |
| P1 | `-d N`, `--max-depth=N` | 输出不超过指定深度的目录；`-d 0` 与 `-s` 等价。 |
| P1 | `-l`, `--count-links` | 每条 hardlink 路径均计数；默认尽量按可靠对象身份去重。 |
| P1 | `-S`, `--separate-dirs` | 目录总量不包含子目录。 |
| P1 | `-x`, `--one-file-system` | 限制在参数所在 namespace/filesystem；必须同时遵守扫描时的 mount coverage。 |
| P1 | `--inodes` | 将文件数/对象数作为主指标，服务“目录中文件过多”场景。 |
| P2 | `-t SIZE`, `--threshold=SIZE` | 按当前主指标过滤输出。 |
| P2 | `--time[=WORD]` | 输出目录树中最近的时间；需定义索引聚合与缺失字段语义。 |
| P2 | `--exclude=PATTERN`, `-X FILE` | 仅在 include/exclude 语法、非 UTF-8 路径和 coverage 影响完成设计后实现。 |
| 暂缓 | `-H`, `-L`, `-P` | 等 symlink/reparse 与跨文件系统边界设计完成后实现。 |

### 易用性扩展参数规划

| 参数 | 推荐理由 |
|---|---|
| `--all-roots` | 替代旧的“无参数即跨 namespace”默认行为，使全局汇总成为显式操作。 |
| `--metric=used|apparent|both|files` | 比组合多个传统参数更直接，也方便跨平台解释。 |
| `--coverage=complete|any` | 默认只把 complete coverage 当作精确结果；`any` 允许查看带 completeness 标记的部分统计。 |
| `--source=index|live` | `index` 为默认，保留离线优势；`live` 只作为显式校验/诊断，不隐式重扫。 |
| `--stale=error|mark|include` | 控制 stale 或 coverage 不完整数据；默认 `mark`，机器输出必须带状态。 |
| `--columns=LIST` | 为交互表格选择列，不改变 JSON/CSV schema。 |
| `--format=table|json|csv` | 保留现有稳定机器输出能力；未来可增加 `jsonl`，不复用 GNU 的 `--format` 含义。 |

## `roots` / `df` 的行为与规划

### 当前默认行为

`df` 是 `roots` 的等价别名，两者读取同一批 registered namespace，并以 GNU `df` 熟悉的列顺序输出：

```text
FILESYSTEM  1K-BLOCKS       USED  AVAILABLE  USE%  MOUNTED ON
C:\         997482492  391581348  605901144   40%  C:\
```

- `FILESYSTEM` 当前使用 registered namespace 路径；在可靠的 device/source 名称采集落地后可显示设备名。
- `1K-BLOCKS`、`USED`、`AVAILABLE` 来自本次实时文件系统容量查询，不来自路径索引，也不等同于 indexed logical/allocated 汇总。
- block 单位固定为 1024 bytes，非整数向上取整，接近 GNU 默认行为。
- `USE%` 由 used/total 向上取整并限制在 0–100%；不得用 indexed 文件大小推导。
- `MOUNTED ON` 是 namespace 挂载路径。
- 实时容量不可访问时，表格显示 `-`；JSON/CSV 使用 `null` 并提供 `status=unavailable`，不能显示 0。
- `--format json|csv` 保留既有 root 身份字段并追加完整容量字段；默认表格只展示 GNU 风格核心列。

与 GNU `df` 不同，无参数只列 registered namespace，不枚举全部系统 mount。这样 `roots` 与 `df` 始终等价，也让用户明确看到 flatlas 的扫描边界。未来系统全量 mount 视图必须通过显式参数请求。

### GNU 兼容参数规划

| 优先级 | 参数 | 规划语义 |
|---|---|---|
| P0 | `-h`, `--human-readable` | 以 1024 进制缩写显示容量。 |
| P0 | `-H`, `--si` | 以 1000 进制缩写显示容量。 |
| P0 | `-k` | 显式选择 1024-byte blocks；与当前默认幂等。 |
| P0 | `-B SIZE`, `--block-size=SIZE` | 控制表格容量缩放；机器输出继续保存 byte 或明确的原始整数。 |
| P0 | `-T`, `--print-type` | 显示 filesystem type；未知时为 `-`。 |
| P1 | `[FILE ...]` | 显示每个参数所在的 registered namespace，并按首次出现去重。未登记文件系统返回领域错误。 |
| P1 | `-i`, `--inodes` | 平台可可靠取得时显示 inode total/used/available；Windows 不伪造 inode 指标。 |
| P1 | `--output=FIELD_LIST` | 采用 GNU field 名称 `source`、`fstype`、`size`、`used`、`avail`、`pcent`、`target` 选择表格列。 |
| P1 | `--total` | 增加容量总计；必须避免重复 namespace，并说明跨设备相加只是展示总和。 |
| P2 | `-t TYPE`, `--type=TYPE` | 仅显示指定 filesystem type。 |
| P2 | `-x TYPE`, `--exclude-type=TYPE` | 排除指定 filesystem type。 |
| P2 | `-l`, `--local` | 只显示可确认是 local 的文件系统；未知类型不能默认当作 local。 |
| P2 | `-P`, `--portability` | 使用 POSIX header 与单行布局；只影响表格。 |
| 暂缓 | `--sync`, `--no-sync` | 不应在只读索引 CLI 中隐式触发全局 sync；只有独立风险/性能设计后评估。 |

### 易用性扩展参数规划

| 参数 | 推荐理由 |
|---|---|
| `--all-mounted` | 显式列出系统 mount，与默认 registered-only 视图区分。 |
| `--registered-only` | 显式表达默认范围，方便脚本自描述。 |
| `--status=ok|unavailable|disabled` | 筛选当前可访问性或配置状态。 |
| `--columns=LIST` | flatlas 风格的可读列选择别名；不能与 GNU `--output` 的字段语义冲突。 |
| `--format=table|json|csv` | 保持稳定机器输出；注意 GNU `df --output` 选择列，而 flatlas `--format` 选择编码。 |

### 数据真实性与测试边界

- 容量来自操作系统实时查询，因此 `df` 不承诺离线容量；SQLite 只保存 root 身份与配置，不缓存容量快照。
- `USED + AVAILABLE` 不一定等于 total；文件系统保留块、权限和平台 API 差异必须允许存在。
- `df` 的 filesystem used 与 `du` 的 indexed allocated 汇总不是同一指标，两者差值不能直接解释为“未索引文件”或“可回收空间”。
- 同一 namespace 的 `roots` 与 `df` 表格、JSON 和错误行为必须逐项一致。
- Windows/Linux 测试应 mock 固定容量验证单位和舍入，并各自增加真实文件系统 smoke test。

## 当前行为到目标行为的迁移

| 命令 | 当前实现 | 目标默认 | 兼容迁移 |
|---|---|---|---|
| `ls` 条目范围 | 仅实时直接子目录，并合并 coverage | 像 `ls -Al` 一样列出全部直接文件与目录、包含真实隐藏条目但不合成 `.` / `..` | 先扩展核心查询与测试，再增加 `--dirs-only` 保留当前视图 |
| `ls` 名称 | 只显示名称 | 保持只显示名称 | `--absolute` 显式请求完整路径 |
| `ls` 长格式 | 状态、递归大小、文件数 | 增加可用 mode/nlink/owner/group/mtime，保持状态与目录总量扩展 | 缺失 metadata 显示 `-`，不得伪造 Unix 权限 |
| `du` 无参数 | 聚合所有 registered namespace | 汇总当前目录 `.` | 旧行为迁移到 `--all-roots` |
| `du` 参数 | 每个参数一条汇总 | 保持，等价隐式 `-s` | 接受显式 `-s` |
| `du` size | logical bytes、文件数、可用 allocated bytes | used block、apparent bytes、文件数并列且语义清晰 | `-b`、`--apparent-size`、`-B`、`-h` 控制表格 |
| 机器输出 | JSON/CSV 完整字段 | 保持稳定字段；新增 completeness/status 字段 | schema 变更须测试列名、类型和顺序 |
| `roots` / `df` | `roots` 为 df 风格表格，`df` 是等价别名；只列 registered namespace | 保持默认范围，逐步增加 GNU 缩放、类型、字段选择和 FILE 参数 | 系统全部 mount 只能由 `--all-mounted` 显式请求 |

默认行为变更应在同一功能提交中更新 README、`--help`、JSON/CSV 回归测试和 Windows/Linux 专项测试。若后续已有正式发布版本，必须提供 release note；CLI 参数兼容与机器输出 schema 兼容分开管理。

## 实现顺序与验收

### 第一批：默认心智模型

- `ls` 默认列出文件和目录，包含隐藏条目，维持长表与 coverage 状态。
- `du` 无参数改为当前目录；新增 `--all-roots`。
- 接受 `ls -A -l` 与 `du -s` 作为显式但幂等的兼容写法；`ls -a` 另行增加 `.` 与 `..`。
- `roots` / `df` 增加 `-h`、`-k`、`-T`，并固定 registered-only 默认范围。
- 为 partial/stale/unknown allocated size 增加机器可读 completeness 字段。

### 第二批：最常用显示参数

- 两个命令实现 `-h`、`--si`、`--block-size` 的一致缩放层。
- `du` 实现 `-b`、`--apparent-size`、`-c`、`-d`。
- `ls` 实现 `-1`、`-S`、`-t`、`-r`、`--sort`。

### 第三批：过滤、对象与边界

- hardlink 默认去重与 `du -l`。
- `du -x` 与 mount/namespace coverage。
- `--exclude`、`-X` 与非 UTF-8 路径策略。
- symlink/reparse 参数只在循环保护和跨平台语义完成后加入。

每批至少覆盖：

- 无参数、单参数、多参数与当前目录行为。
- `roots` 与 `df` 等价、容量不可访问、block 舍入、use% 和 registered/all-mounted 范围。
- 空目录、隐藏条目、普通文件、目录、symlink/reparse 与特殊文件。
- completed、partial、failed、cancelled、未扫描、stale、实时缺失和离线源。
- logical 与 allocated 不同、allocated 未知、稀疏文件、hardlink 和跨 namespace。
- 表格对齐、Unicode 名称、JSON/CSV 可解析及字段稳定性。
- 查询不修改用户文件，也不绕过 completed scope 才能持久化删除的安全不变量。

## 参考资料

- [GNU `ls` invocation](https://www.gnu.org/software/coreutils/manual/html_node/ls-invocation.html)
- [GNU `ls`: which files are listed](https://www.gnu.org/software/coreutils/manual/html_node/Which-files-are-listed.html)
- [GNU `du` invocation](https://www.gnu.org/software/coreutils/manual/html_node/du-invocation.html)
- [GNU `df` invocation](https://www.gnu.org/software/coreutils/manual/html_node/df-invocation.html)
- [GNU Coreutils block size](https://www.gnu.org/software/coreutils/manual/html_node/Block-size.html)
