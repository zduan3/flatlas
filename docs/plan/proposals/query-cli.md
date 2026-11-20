# 查询命令 GNU 兼容规划

> 状态：提案，未实现。当前 `ls`、`du`、`largest`、`roots`、`df`、`scans` 等行为见 [查询命令](../../design/queries.md)；本文只描述后续参数、子命令与默认行为调整。

## 目标

兼容参照优先采用 GNU Coreutils 与 GNU Findutils。Linux 上的 BusyBox、BSD 工具和发行版 alias 可能不同，因此这里追求的是一致的用户心智模型和常用参数语义，不承诺逐字符复刻 GNU 输出。

- `flatlas ls [PATH]` 保持接近 `ls -Al PATH` 的一层长格式列表。
- `flatlas du [PATH ...]` 保持接近 `du -s PATH ...`：每个参数仅输出一条递归汇总。
- `flatlas df` 贴近 GNU `df`；`flatlas roots` 只读索引记录，不承担容量视图。
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
| symlink / reparse | 由 `-H`、`-L`、`-P` 等控制 | 默认不跟随 symlink 或 Windows reparse point；兼容参数须在跨平台策略完成后加入 |
| 排序与字符 | locale、终端与环境变量会影响输出 | 表格按 Unicode 显示宽度对齐；机器输出使用稳定字段和排序规则 |

`df` 还有一项产品边界差异：GNU `df` 无参数时列出当前系统挂载表中的文件系统；flatlas 的 `df` 无参数时只列内部 registered namespace。两者不能静默混为一谈。

## `ls` 规划

### GNU 兼容参数规划

| 优先级 | 参数 | 规划语义 |
|---|---|---|
| P1 | `-a`, `--all` | 按 GNU 语义包含全部条目，并额外合成 `.` 与 `..`；与默认的隐式 `-A` 有意不同。 |
| P0 | `-l` | 显式请求长格式；目标默认已启用。与 `--format table` 配合，不改变 JSON/CSV schema。 |
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
| `--hide-uncertain-totals` | 默认用 `part` / `gone` 状态展示仍可用的最后已知统计；需要只看完整值时将这些统计隐藏为 `-`。机器输出始终保留独立 status。 |
| `--absolute` | 默认只显示名称；需要复制完整路径时显式请求。 |
| `--null` | 使用 NUL 分隔名称，安全处理换行等特殊字符；语义参照 Coreutils 通用做法。 |

### `coverage` 与 `ls` 的职责边界

`ls` 回答“这个目录当前有哪些直接子项”，`coverage` 回答“索引对这棵目录树了解得有多完整、哪些结论可信”。`ls` 的状态列是快速告警灯，不是完整的扫描诊断界面：它把实时存在性与索引覆盖度压缩成 `ok`、`new`、`part`、`gone`，其中 `gone` 只表示本次完整实时枚举没有看到旧索引条目，不等于数据库已经可靠确认 `deleted`。

仅给 `ls` 增加一列不能满足以下需求：

- 递归展示盲区位于哪一层，而不是只把直接子目录标成 `part`。
- 展开最近 scan、scope 边界、completed/partial/failed/cancelled 状态、上次完整扫描时间及错误原因。
- 汇总一棵树中完整、部分和未扫描的目录/文件数量，并说明大小统计为何不完整。
- 在 registered root 尚未扫描、根目录读取失败、源路径离线或没有可列实时条目时，仍审计已有 scan/scope/error 记录。
- 区分“实时存在性证据”和“扫描覆盖证据”，避免把枚举错误或局部 scan 范围外的缺失解释为删除。

因此 `ls` 应保留紧凑状态列，并允许输出指向进一步诊断所需的 path/status；独立的 `coverage [PATH]` 则提供递归汇总、scope 边界和错误明细。首期若只需较小实现，可以先增加 `ls --coverage` 摘要，但不能以此取代后续 coverage 审计视图。

### 可信度分级

目录大小与 freshness 是两个独立维度。机器输出后续应分别暴露数值、freshness 和 coverage/error 状态，而不能把非空时间解释为精确性证明。推荐的可信度概念为：

- `fresh`：所有目录都成功过，子树最旧成功时间在策略阈值内，且没有需要单独提示的近期错误。
- `stale`：所有目录都成功过，但 freshness 下界超过阈值。
- `unknown`：至少一个当前目录从未成功完整枚举。
- `warning`：存在历史成功数据，但之后的 attempt 出现 partial、failed 或 cancelled；已知汇总仍可显示，但需要错误提示。

## `du` 规划

### 深度展开、top-N 与条目数

这是当前阶段优先级最高的 `du` 能力（见 [backlog](../backlog.md)）：

- `-d N` / `--max-depth=N`：输出不超过指定深度的目录，`-d 0` 与 `-s` 等价；配合 top-N 与排序，定位大目录无需逐层执行 `ls`。
- top-N 与排序：按 logical、allocated 或文件数排序并截取前 N 项，使“文件过多的目录”与“占用最大的目录”都能直接列出。
- 目录条目数（inode 归因）：当前目录汇总只统计普通文件数；需要在 `directory_summary` 中增加包含目录、symlink 与特殊条目的条目总数，用于定位 inode 配额压力。这需要新的向前 migration 与回填。

### 目标默认表格

```text
USED(K)  APPARENT(B)      N  PATH
  16384      12345678  1024  archive
      -          4096     1  windows-or-unknown
```

- `USED(K)` 对应可可靠聚合的 allocated size，并以 1024-byte block 显示，接近 GNU `du` 默认心智模型。
- `APPARENT(B)` 对应 logical/apparent size，避免 Windows 或某些文件系统没有 allocated 数据时命令失去用途。
- 两列并存是 flatlas 的透明性扩展；不能宣称 USED 是真实物理可回收空间。
- 全局 namespace 汇总由明确的 flatlas 参数 `--all-roots` 请求。

hardlink 的默认计数应尽量对齐 GNU `du`：同一可靠对象身份在同一次聚合中只计一次；`--count-links` 才按每条路径重复计数。由于 `(device, inode)` 不是跨历史永久 ID，只能在身份与观测依据仍可靠时去重，否则必须保守计数并在机器输出中暴露 completeness/limitations。

### GNU 兼容参数规划

| 优先级 | 参数 | 规划语义 |
|---|---|---|
| P0 | `-s`, `--summarize` | 每个参数只输出一个总量；目标默认已启用，保留参数作为显式兼容写法。 |
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

## `df` 规划

### GNU 兼容参数规划

| 优先级 | 参数 | 规划语义 |
|---|---|---|
| P0 | `-H`, `--si` | 以 1000 进制缩写显示容量。 |
| P0 | `-k` | 显式选择 1024-byte blocks；与默认幂等。 |
| P0 | `-B SIZE`, `--block-size=SIZE` | 控制表格容量缩放；机器输出继续保存 byte 或明确的原始整数。 |
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
- Windows/Linux 测试应 mock 固定容量验证单位和舍入，并各自增加真实文件系统 smoke test。

## `largest` 规划

- 增加 metric（logical / allocated）、排序、阈值和结果数量控制。

## 后续查询子命令规划

后续子命令不应只是重新包装操作系统工具，而应利用持久化索引，把路径状态、scan coverage、错误、hash 状态和实时差异叠加到熟悉的查询模型上。普通索引查询不得隐式遍历或更新文件系统；内容分析命令必须像当前 `dupes` 一样明确说明会在查询范围内读取候选文件，且不得改变 scan coverage。

### 产品价值优先级

名称/路径查找只是持久化索引带来的附加能力，不优先于空间回收主线；产品优先级见 [路线图](../roadmap.md#产品优先级)，当前阶段排序见 [backlog](../backlog.md)。

### 候选命令与优先级

| 优先级 | 子命令 | 兼容参照 | flatlas 增强信息与边界 |
|---|---|---|---|
| P0 | `coverage [PATH]` | flatlas 原生命令 | 按目录展示 completed、partial、failed、cancelled 与 unscanned，定位索引盲区和不完整统计的来源。 |
| P0 | `errors [PATH]` | flatlas 原生命令 | 汇总遍历、权限、I/O 和 hash 错误，支持按 scan、目录、operation 与错误码筛选和聚合。 |
| P1 | `hash PATH...` | `b2sum` 等 checksum 工具 | 仅把 `full_ready` digest 当作可用内容 hash，同时展示 algorithm、state 与 basis；实时校验和是否回写索引必须由独立参数明确控制。 |
| P1 | `verify [PATH...]` | flatlas 原生命令 | 只读比较索引与当前文件系统，报告新增、metadata 改变、stale hash 和未确认缺失；读取失败或 coverage 不完整不得把路径判为 deleted。 |
| P2 | `tree [PATH]` | `tree` | 以索引生成目录层级，附带 logical/allocated 总量、文件数、coverage 和错误标记；与 `du --max-depth` 重叠，只有确有层级浏览需求时实现。 |
| P2 | `stat PATH...` | GNU `stat` | 展示索引 metadata、路径状态、最后观测 scan、coverage、hash 状态与 digest；可规划只读 `--live` 比较，但不得隐式更新索引。 |
| P3 | `find [PATH] [EXPR...]` | GNU `find` | 在 SQLite 中查询而非实时遍历；可考虑 `-name`、`-iname`、`-type`、`-size`、`-mtime`、`-uid`、`-gid`、`-links`，并增加索引状态条件。不提供 `-delete`、`-exec` 等动作。 |
| P3 | `locate PATTERN...` | GNU `locate` | 直接复用全局路径索引，不另设 `updatedb`；除匹配路径外显示 root、present/deleted 状态、最后扫描时间和 coverage。 |
| P3 | `readlink PATH...` | GNU `readlink` | 显示索引中的 symlink target、Windows reparse 类型和最后观测状态，不跟随目标。 |
| P3 | `findmnt` | `findmnt` | 展示 registered namespace、mount 与扫描边界；只有可靠的跨平台 mount/source/type metadata 落地后实现。 |

`scans` / `scan-info` 已提供最小扫描审计（见 [查询命令](../../design/queries.md#scans-path-与-scan-info-id)）。这里的 P0 是对空间回收主线的支撑优先级，不表示要先于 `dupes`、`du`、`largest` 和局部扫描本身。`coverage`、`errors` 应采用满足可信度诊断所需的最小设计，避免审计界面反过来延迟核心查询能力。

### 命令分组与默认数据源

- 熟悉的系统视图：`ls`、`du`、`df`、`stat`、`find`、`locate`。
- 索引可信度视图：`verify`、`coverage`、`errors`。
- 索引管理与审计：`scan`、`scans`、`scan-info`、`hash`、`dupes`、`plan`。

GNU `find` 默认遍历实时目录树，而 `flatlas find` 应默认查询 SQLite；未知或尚未支持的 expression 必须报错，不能静默忽略。若未来提供统一数据源参数，应采用显式的 `--source=index|live` 或各命令已定义的受控变体，并说明 live 查询是否只比较、是否计算 hash、是否更新数据库。

`locate` 与 flatlas 的持久化索引天然匹配：它不需要第二套文件名数据库，但必须显式显示 stale、deleted 或 coverage 不完整的结果，避免旧索引被误解为当前存在性证明。

### 暂不规划的命令

| 命令 | 原因 |
|---|---|
| `file` | 当前没有 MIME、内容特征或可执行格式索引，仅凭 `path.kind` 不足以兼容。 |
| `grep` | 当前不索引文件内容。 |
| scan 间 `diff` | 数据库保存当前路径状态而非不可变历史快照，无法可靠重建任意两次扫描。 |
| `getfacl`、`lsattr` | schema 尚未保存 ACL、xattr 或完整平台属性。 |
| 修改文件系统的 `mv`、`ln`、`touch`、`chmod`、`chown` | 超出只读边界；不得借查询子命令引入文件修改（现有 `flatlas rm` 只维护索引）。 |
| 精确兼容 checksum `--check` | 需先设计实时内容读取、digest 算法选择、stale 判断及是否回写索引。 |
| `ncdu` / TUI | 需要独立交互与性能设计，不作为查询 CLI 的近期兼容目标。 |

## 默认行为迁移

| 命令 | 当前实现 | 目标 | 兼容迁移 |
|---|---|---|---|
| `ls` 长格式 | `T`、`S`、`LOGICAL(B)`、`N`、可用 `ALLOC(B)`、`SCANNED-THROUGH`、`NAME` | 逐步增加 mode/nlink/owner/group/mtime | 未知显示 `-`，不得伪造 Unix 权限或目录对象 `st_size` |
| `ls` 名称 | 只显示名称 | 保持 | `--absolute` 显式请求完整路径 |
| `du` 全局汇总 | 无参数汇总当前目录，无跨 namespace 汇总 | 保持默认 | 增加 `--all-roots` |
| `du` size | logical bytes、文件数、可用 allocated bytes | used block、apparent bytes、文件数并列且语义清晰 | `-b`、`--apparent-size`、`-B` 控制表格 |
| 机器输出 | JSON/CSV 完整字段 | 保持稳定字段；新增 completeness/status 字段 | schema 变更须测试列名、类型和顺序 |
| `df` | 无参数只列 registered namespace（已支持 FILE、`-h`、`-T`） | 保持默认范围，逐步增加 GNU 缩放与字段选择 | 系统全部 mount 只能由 `--all-mounted` 显式请求 |

默认行为变更应在同一功能提交中更新 README、`--help`、JSON/CSV 回归测试和 Windows/Linux 专项测试。若后续已有正式发布版本，必须提供 release note；CLI 参数兼容与机器输出 schema 兼容分开管理。

## 实现顺序与验收

### 空间回收主线

1. `dupes` 在已有可节省量排序与范围/阈值筛选之上补齐 hardlink alias 与 completeness，使结果可以直接指导清理决策。
2. `du` / `largest` 补齐大目录与大文件的 top、depth、metric 和 human-readable 查询，形成非交互式空间定位闭环。
3. 验证局部 scan 与旧索引的重复组交互、stale hash 重算以及 partial/error/cancelled 不产生假删除。
4. 增加最小的 `coverage` / `errors` / scan 摘要，能够解释上述结果何时精确、何时不完整。
5. 完成稳定 JSON/CSV 导出和 dry-run plan 后，再投入通用文件查找与更完整的 GNU 参数兼容。

下面的批次只描述 `ls` / `du` / `df` 兼容参数内部的依赖顺序，不高于上述产品主线。

### 兼容第一批：默认心智模型

- `du` 新增 `--all-roots`，显式请求跨全部 registered namespace 的汇总。
- 接受 `ls -A -l` 与 `du -s` 作为显式但幂等的兼容写法；`ls -a` 另行增加 `.` 与 `..`。
- `df` 增加 `-k`、`-H`、`-B`，并固定 registered-only 默认范围（`-h`、`-T`、FILE 已实现）。
- 为 partial/stale/unknown allocated size 增加机器可读 completeness 字段。

### 兼容第二批：最常用显示参数

- 在已有 `-h` 之上实现 `--si`、`--block-size` 的一致缩放层。
- `du` 实现 `-b`、`--apparent-size`、`-c`、`-d`。
- `ls` 实现 `-1`、`-S`、`-t`、`-r`、`--sort`。

### 兼容第三批：过滤、对象与边界

- hardlink 默认去重与 `du -l`。
- `du -x` 与 mount/namespace coverage。
- `--exclude`、`-X` 与非 UTF-8 路径策略。
- symlink/reparse 参数只在循环保护和跨平台语义完成后加入。

每批至少覆盖：

- 无参数、单参数、多参数与当前目录行为。
- `df` 容量不可访问、block 舍入、use% 和 registered/all-mounted 范围。
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
- [GNU `stat` invocation](https://www.gnu.org/software/coreutils/manual/html_node/stat-invocation.html)
- [GNU Findutils manual](https://www.gnu.org/software/findutils/manual/html_mono/find.html)
- [GNU `b2sum` invocation](https://www.gnu.org/software/coreutils/manual/html_node/b2sum-invocation.html)
- [GNU Coreutils block size](https://www.gnu.org/software/coreutils/manual/html_node/Block-size.html)
