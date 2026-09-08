# MVP（0.1）范围与验收标准

> 状态：开发契约。定义 MVP（0.1）范围、验收标准与发布门槛。

本文是 File Atlas MVP 的范围基线和完成验收标准。当前 MVP 的版本号是 0.1；本文中的“MVP”和“0.1”是同义词。MVP（0.1）定位为面向普通用户目录和归档目录的持久化、只读空间分析与重复文件候选发现工具：它提供清理指导，但不修改用户文件系统。需要实际去重时，用户应使用 jdupes 对选定目录重新扫描、独立验证并显式执行操作。

## 适用假设

- 用户查询和扫描一个不跨文件系统的普通目录 scope，例如用户目录、下载目录、照片目录或归档目录。
- `root` 仍是内部 filesystem scan namespace，用户目录只是该 namespace 内的 scope；`flatlas init [PATH]` 只登记 namespace，不扫描文件。
- MVP（0.1）处理当前平台可正常表示的常规文件名。Linux 非 UTF-8 路径 round-trip、Windows Volume GUID 和长路径/reparse tag 的完整适配不作为 MVP 发布门槛。
- 默认不跟随 symlink、junction 或其他 Windows reparse point；FIFO、socket、device 等特殊文件不计算内容 hash。
- logical size 是空间分析的基线指标；allocated size 仅在平台可靠提供时附加报告，未知必须显示为未知而不是 0。
- 不要求处理 ACL、xattr、SELinux、稀疏文件、压缩、reflink sharing 等高级物理空间语义。
- 文件系统可以在扫描或 hash 期间变化，但变化、读取失败或覆盖不完整的结果不得被误解释为路径已删除或可安全清理。

## MVP（0.1）功能范围

### 扫描命名空间与索引

- 管理多个内部 scan namespace；用户在 namespace 内选择普通目录作为扫描和查询 scope。
- SQLite schema 与 migration 保存当前路径状态、扫描、scope coverage、扫描错误、metadata、hash 和可选 dry-run plan。
- 保存当前状态与扫描审计，不保存可查询的完整 immutable scan snapshot。
- 支持完整 namespace 扫描和任意指定子树的局部更新。
- scan 只读取目录项和 metadata；内容 hash 由 `dupes` 在查询范围内按需计算。
- 只有 `scan_scope.status='completed'` 时，才将该 scope 内本次未观察到的旧路径标记为 `deleted`。
- 取消、权限错误、I/O 错误和扫描中改名不得造成假删除。MVP（0.1）不要求保存中断前的 checkpoint，也不以持久化 `cancelled` 终态作为发布门槛。
- 源目录之后不可访问时，已索引的当前状态仍可从 SQLite 查询。

### 空间占用查询

- `roots` / `df` 报告 registered namespace 的实时容量，容量不可访问时明确显示未知。
- `paths [PATH]` 浏览当前索引路径。
- `du [PATH ...]` 汇总指定文件或目录的 logical size、文件数和可用时的 allocated size。
- `ls [PATH]` 实时列出一层直接条目并合并持久化 coverage 状态：普通文件显示自身 logical size 和 `N=1`，目录显示已知的递归索引 logical size 和文件数，symlink/reparse/其他特殊条目不跟随且统计显示未知。
- `ls` 的目录大小有意不同于 GNU `ls -l` 的目录对象 `st_size`；表格使用 `LOGICAL(B)` 明确表示文件自身或目录树聚合。`part` / `gone` 可展示最后已知值，但状态必须保留；`new` 目录和任何未知统计显示 `-`，不能伪装成 0。
- `ls` 回答“一层结构、现场存在性和已知空间提示”；`du` 回答“每个显式 scope 的递归索引汇总”，并可在源目录离线时使用。
- `largest [PATH]` 查询指定文件或目录 scope 内最大的当前普通文件；PATH 默认为当前目录，默认返回 10 项；--limit 与 -n 等价，按 logical size 降序、路径升序稳定排序。
- MVP（0.1）接受当前 `du` 的单目标汇总能力；按直接子项或深度展开的 `du --depth` 属于后续增强，而不是 MVP 发布阻塞项。

### 外部删除与索引状态

- MVP（0.1）不删除文件或目录。用户在 flatlas 之外删除条目后，索引在确认扫描前可以继续保持 `present`。
- `ls` 可以把实时枚举未发现的直接文件或目录显示为 `gone`，但该观察不修改数据库。
- 要确认删除，必须重扫一个仍存在且覆盖被删除路径的父目录；已经不存在的目标不能作为 scan PATH。
- 只有覆盖该路径的 completed scope 才将缺失路径及其已索引后代标记为 `deleted`，并记录 `deleted_by_scope_id` 与 `deleted_at_ns`。
- partial、failed、cancelled、权限错误和 I/O 错误不得确认删除。
- deleted path 保留在 SQLite，不物理删除；当前状态查询排除它。相同路径重新出现并被扫描时恢复为 `present`，清除删除字段，并按 metadata basis 决定是否将旧 hash 标记为 stale。

- rm PATH 是独立的索引维护命令：不检查 PATH 是否仍存在于文件系统，直接从 SQLite 递归删除该路径与所有已索引后代（包括关联 hash）；实际文件完全不变。若 dry-run plan 引用了其中任一路径，必须删除整个失效 plan 及其 operations。

### 重复候选与导出

- `dupes [PATH]` 只在指定目录 scope 内，以 `size → quick BLAKE3 → BLAKE3 full hash` 分级发现重复内容候选。
- 0 B 文件不进入 hash 候选或重复组。
- hash 缓存仅在文件身份、size、mtime 和全部可用且可靠的 change/birth time 与索引依据一致时复用。
- 文件变化或读取失败时，对应文件不进入重复组，结果明确标记为 incomplete。
- 重复组报告的是基于完整 BLAKE3 的候选和 theoretical logical savings，不是经过逐字节复核的删除授权，也不代表真实物理可回收空间。
- table、JSON 和 CSV 输出必须稳定、可解析；digest 使用 hex，不能依赖 Python `bytes` repr。
- `export dupes` 用于人工审查或后续分析，不应被文档推荐为自动删除脚本的直接输入。

### dry-run plan

- 可以保留不可变 dry-run plan，将当前重复候选冻结为便于审查和导出的建议快照。
- plan 中的 canonical 只是确定性展示选择，suggested action 不表示已经满足安全执行条件。
- MVP（0.1）没有 `apply` 命令，plan 不替代 jdupes 的重新扫描、逐字节验证和用户批准。
- dry-run plan 属于辅助能力；空间查询、重复候选和只读保证才是 MVP 核心发布门槛。

## 与 jdupes 的职责边界

推荐工作流：

```text
flatlas scan PATH
flatlas du PATH
flatlas largest PATH --limit 50
flatlas dupes PATH

# 选择值得处理的较小目录后，由 jdupes 重新扫描和验证
jdupes -r PATH
```

- flatlas 负责长期索引、局部更新、离线空间查询、重复候选定位和清理优先级参考。
- jdupes 负责操作前重新读取文件、逐字节验证，以及用户显式选择的删除、hardlink 或其他动作。
- 不应将 flatlas 导出结果直接管道到删除命令，也不应仅依据 flatlas full hash 自动删除或替换文件。

## MVP（0.1）明确不包含

- 删除、移动或替换文件，以及任何 plan apply。
- 创建 hardlink、reflink 或 symlink。
- 逐字节重复内容复核和自动选择保留文件。
- 真实物理可回收空间计算和已有 hardlink 的完整物理空间分析。
- 跨文件系统 scope 的扫描策略。
- Linux 非 UTF-8 路径的发布保证，以及 Windows Volume GUID、完整长路径/reparse tag 适配。
- 完整历史 snapshot、复杂 include/exclude、高级筛选和 `du --depth`。
- USN Journal、inotify 或任何持续监听。
- TUI、图形界面、Rust/PyO3 和高并发后端。

## 验收矩阵

| 类别 | 测试场景 | 通过标准 |
|---|---|---|
| 数据库 | 新库初始化和重复打开 | migration 可重复执行；foreign keys、WAL、busy timeout 与必要索引生效；既有数据不丢失。 |
| init | 对普通目录执行 `init` | 只登记其 filesystem namespace，不创建 scan 或 path。 |
| 用户错误 | 未初始化、scope 不存在或未索引 | 输出 `error: ...`，退出码 2，不输出普通 Python traceback。 |
| 全量扫描 | 常规 Unicode 名称、普通文件、空目录和嵌套目录 | 路径、类型、logical size 和可用 metadata 与文件系统一致；scan 不读取普通文件内容。 |
| 局部更新 | 初次扫描后新增子目录，只扫描该子目录 | 新文件进入索引，可与旧索引文件形成重复候选；scope 外路径不被重新标记。 |
| 已确认文件删除 | 删除文件后重扫仍存在的父目录 | 只有覆盖该文件的 completed scope 将其标记为 `deleted`；记录确认 scope/time；scope 外路径不受影响。 |
| 已确认目录删除 | 删除含后代的目录后重扫仍存在的父目录 | 目录及其已索引后代都标记为 `deleted`；直接扫描已不存在目录得到普通用户错误。 |
| 无假删除 | 注入目录枚举错误、stat 错误或中断 | scope 未 completed 时不运行缺失标记；未覆盖旧路径仍为 `present`。 |
| 删除后恢复 | 已标记 deleted 的同一路径重新出现并重扫 | path 恢复为 `present`，删除字段清空；basis 不匹配的旧 hash 变为 stale。 |
| 扫描进度 | 扫描不同大小的固定语料 | 交互式 stderr 展示文件数、目录数和累计 logical bytes；最终 JSON 提供相同统计且 stdout 不被进度污染。 |
| `du` | 对固定文件和目录 scope 汇总 | logical size、文件数、可用 allocated size 和相对路径正确；多个 PATH 按输入顺序输出。 |
| `largest` | scope 内外均有不同大小文件 | 只返回 PATH scope 内当前普通文件；默认 PATH 为 cwd；limit 生效；按大小降序、路径升序稳定排序。 |
| `paths` | 全局和指定 scope 查询 | 只返回当前 `present` 路径，scope、limit 和排序稳定。 |
| `ls` 条目与状态 | 直接层包含文件、目录、symlink，以及已扫描、未扫描、partial 和现场移除条目 | 只列一层且覆盖各类型；正确显示 `ok`、`new`、`part`、`gone`；现场缺失不直接写入 `deleted`；枚举错误整体报错。 |
| `ls` 空间字段 | 已扫描/partial/gone 目录、已扫描/new 文件和特殊条目 | 文件显示自身 logical size、`N=1`；目录显示已知递归聚合；part/gone 保留状态；new 目录和特殊条目为未知；表头使用 `LOGICAL(B)`。 |
| `roots` / `df` | mock 容量成功和不可访问 | 两个命令一致；1K blocks、used、available、use% 正确；未知显示 `-`/`null` 而不是 0。 |
| 分级 hash | 相同内容、同大小不同内容、quick 不同和 quick 相同的大文件 | 只有 full hash 相同文件进入同一候选组；小文件只读一次；quick 不同的大文件不读取全文。 |
| 惰性范围 | 查询目录内外均有同大小文件 | 只打开查询 scope 内候选；scan 不产生 hash；重复查询复用有效缓存。 |
| Hash 一致性 | scan 后修改文件，并注入读取错误 | metadata 不一致或读取失败的文件不进入候选组，结果明确标为 incomplete。 |
| 空文件 | scope 内有多个 0 B 普通文件 | 不创建 hash，不显示为重复组。 |
| 重复项范围 | scope 内外均有相同内容文件 | `dupes PATH` 只用 PATH 子树内文件组成候选组；省略 PATH 时使用 cwd。 |
| 路径显示 | 从不同 cwd 查询同一 scope | 默认相对于 PATH 且不随调用 cwd 改变；`--absolute` 输出绝对路径。 |
| 离线查询 | 扫描并完成 hash 后使源目录不可访问 | 仍可查询此前索引的 `paths`、`du`、`largest` 和已缓存重复候选。 |
| JSON/CSV | 所有基础查询、空结果和 `--output` | JSON 可解析；CSV 为 UTF-8、列名与行结构稳定；digest 为 hex；数据写 stdout，进度和 warning 写 stderr。 |
| dry-run | 创建和展示 plan | 用户文件路径、内容和 metadata 完全不变；CLI 不提供 apply。 |
| 只读保证 | 对所有 MVP（0.1）命令比较测试目录前后快照 | 除显式 `--output` 和 flatlas 自身配置/SQLite 外，不创建、删除、改名或修改用户文件。 |

## 完成门槛

MVP（0.1）发布须同时满足：

1. Windows 和 Linux 至少各运行一套常规 Unicode 文件名语料；非 UTF-8 和高级平台细节可以记录为已知限制。
2. 任一遍历错误或中断都不会产生假删除。
3. `du`、带 scope 的 `largest` 和 `dupes` 能为一个普通目录提供稳定、可解释的空间清理指导。
4. 局部扫描新增目录后，可以从共同的查询 scope 中找出它与既有索引之间的重复候选。
5. JSON/CSV 可解析且字段稳定；incomplete、unknown 和 theoretical savings 的语义明确。
6. 所有 MVP（0.1）命令保持只读；文档不建议依据 flatlas 输出自动删除文件。
7. README 明确要求实际去重由 jdupes 重新扫描、验证并由用户显式执行。
8. `uv run ruff check .`、`uv run pytest -q`、`uv run pyright` 和 `git diff --check` 全部通过。

## 与路线图的关系

本文覆盖 [roadmap.md](roadmap.md) 的 MVP（0.1）只读基线。空间分析深度、筛选和物理空间解释属于后续索引增强；逐字节复核、安全重验、apply、hardlink/reflink/delete、审计和恢复必须在独立设计与验收完成后才能进入后续版本。
