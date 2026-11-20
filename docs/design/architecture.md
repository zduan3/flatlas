# 架构与边界

> 状态：当前设计。描述 flatlas 现有的产品边界、分层、核心模型与不可破坏的语义；随代码更新。未来工作见 [plan/](../plan/roadmap.md)。

## 产品定位

File Atlas 是持久化的文件系统目录数据库，不是每次查询都重新遍历目录的查重工具。SQLite 保存扫描得到的目录、路径、文件对象、哈希、扫描状态和 dry-run plan；索引可在源目录不可访问时用于浏览、查询和导出。

它面向普通用户目录和归档目录，用于定位大文件、大目录和重复内容候选，为清理提供指导。输出不构成删除授权：flatlas 不删除、移动、hardlink、reflink、symlink 或持续监听文件系统，也没有 plan apply。实际去重由用户使用 jdupes 对选定目录重新扫描、逐字节验证并显式执行。

## 分层流水线

重复检测、生成候选报告和修改文件系统严格分层；flatlas 当前只实现前三层，止于报告与 dry-run plan：

```text
metadata 扫描/更新索引 → 查询范围内惰性 hash 与重复候选 → 生成报告或 dry-run plan
```

- 扫描只采集 metadata，不读取普通文件内容，见 [扫描与目录汇总](scanning.md)。
- `dupes` 在指定范围内以 size → quick BLAKE3 → full BLAKE3 惰性分级检测重复内容并复用可靠缓存，见 [惰性 Hash 与重复候选](hashing.md)。
- 空间、路径、审计查询与导出只读取索引（`ls` 另做一层实时枚举），见 [查询命令](queries.md)。
- `rm PATH` 是显式索引维护操作：仅从 SQLite 递归删除已索引路径及其 hash，不读取、验证或修改实际文件系统；这不构成文件删除能力。

## 代码组织

Python 3.12 实现按职责拆分：

| 模块 | 职责 |
|---|---|
| `db.py` | SQLite 连接、schema 与 migration，见 [数据库结构](schema.md) |
| `paths.py` | namespace 发现与登记、路径身份与 BLOB 编码、索引子树移除 |
| `scanning.py`、`summaries.py`、`refresh.py` | metadata 扫描、目录汇总缓存、`--refresh-before` 解析 |
| `allocation.py` | 平台占用空间采集，见 [平台差异](platform.md) |
| `hashing.py`、`duplicate_filters.py` | 惰性 hash 与 dupes 筛选 |
| `queries.py` | 只读查询 |
| `planning.py` | dry-run plan |
| `output.py` | JSON/CSV 导出 |
| `config.py` | 用户级配置与数据库定位，见 [命令默认参数配置](config.md) |
| `cli.py`、`errors.py`、`version.py` | Typer CLI、领域错误渲染、版本展示 |

`core.py` 仅保留兼容性 re-export 与既有故障注入入口。

## 核心模型

- `root`：内部的文件系统 scan namespace（Windows 为盘符/卷，POSIX 为路径所在的最近挂载点），不是工作目录或一次 scan 的目标目录。`flatlas init [PATH]` 只登记 namespace，不扫描文件。
- `path`：目录项、相对路径与最近观测状态。
- `object observation`：某一次观测到的底层文件对象。不能把 `(device, inode)` 视为跨历史的永久 ID，因为 inode 可复用。
- `scan` / `scan_scope`：完整或局部扫描及覆盖/错误状态。
- `hash`：quick/full hash、计算依据的 metadata 与有效性。
- `plan` / `plan_operation`：不可变的去重候选建议快照，只创建和查询，不执行。

用户默认使用 platformdirs 的全局配置与索引；`--db` 只是显式覆盖，不是日常必填参数。持久化层以 BLOB 保存路径身份，见 [数据库结构](schema.md)。

## 跨平台边界

Windows 与 Linux 提供相同的常规只读能力：完整/局部扫描、SQLite 离线查询、空间分析、分级 hash、导出和 dry-run plan。已验收范围限定为当前平台可正常表示的常规 Unicode 路径和不跨文件系统的目录 scope；Linux 非 UTF-8 round-trip、Windows Volume GUID、完整长路径/reparse tag 和复杂 mount/bind mount 策略尚未实现。平台差异不得改变“未完整覆盖即不判定删除”的一致性语义。

| 方面 | Windows | Linux |
|---|---|---|
| 扫描命名空间 | 每个盘符作为 scan namespace，以路径字节标识；未采集 Volume GUID | 路径所在的最近挂载点作为 scan namespace；扫描不检查子目录是否跨 mount |
| 路径 | 验收常规 Unicode；完整长路径和 `\\?\\` 前缀未适配 | 验收常规 Unicode；非 UTF-8 原始字节 round-trip 未验收 |
| 特殊目录项 | 默认不跟随 symlink、junction 和其他 reparse point | 默认不跟随 symlink；FIFO、socket、device 等记录为 `other` 且不 hash |
| metadata | owner/group/mode、POSIX change time 可为空；逻辑大小可靠，allocated size 见 [平台差异](platform.md) | `st_dev`/`st_ino`、mode、uid/gid 与 POSIX change time 可用；allocated size 由 `st_blocks * 512` 得出 |
| 增量来源 | 定期扫描指定子树，不使用 USN Journal | 定期扫描指定子树，不使用 inotify |

hash 复用在两个平台均采取保守规则：仅在当前文件身份、size、mtime 和全部可用且可靠的变更时间字段都匹配时复用；身份变化、字段不匹配或依据不可靠时重新 hash。

## 不可破坏的语义

以下不变量约束所有实现与评审；与 [AGENTS.md](../../AGENTS.md) 的“绝不可破坏的语义”一致。

**扫描与删除判定**

- 只在 `scan_scope.status='completed'` 时，才可以将该 scope 中未在本次 scan 观察到的旧 path 标记为 `deleted`。
- 遍历权限错误、I/O 错误、hash 错误、过滤排除、取消或中断均不得被解释为“路径已删除”；相应 scope 必须是 partial、failed 或 cancelled。
- `inotify` 或其他平台事件只能提示 dirty path，不能作为事实来源。
- 用户在外部删除条目后，`ls` 只能显示 `gone`；只有覆盖该路径的 completed scan scope 才能持久化 `deleted`。deleted 行作为 tombstone 保留并被当前状态查询排除；路径重现后恢复为 `present`，不可靠的旧 hash basis 必须 stale。
- 默认不跟随 symlink、junction 或其他 Windows reparse point。特殊文件不计算内容 hash。

**hash 与重复组**

- duplicate group 只能使用仍为 `present`、普通文件且 `file_hash.state='full_ready'` 的相同 size、algorithm 与 full digest。
- hash 复用必须保守：身份、size、mtime 及可用 change/birth time 任意不匹配或不可靠时，标记 stale 并重算。
- full BLAKE3 相同只作为重复内容候选；theoretical savings 只用于清理优先级参考。文档不得建议将候选导出直接接入删除命令。

**空间指标**

- `logical size`、按 inode 的 allocated size、索引范围内理论节省量与真实物理可回收空间必须分别报告，不可混为同一指标。
- 已有 hardlink、索引范围外的 alias、稀疏文件、压缩与 reflink sharing 会使真实物理节省量未知或只能估算。
- 未知值必须显示为 `-` / `null`，不能伪装为 0。

**只读边界**

- dry-run plan 可创建、查询、导出，但不执行 operation，也不修改用户文件。
- 新 CLI 命令默认只读；任何修改文件系统的执行器都必须另行完成逐字节复核、执行前重验、审批、审计和恢复设计。
- 新 schema 只允许向前 migration，必须保留现有 root、path、scan、coverage、error、hash 与 plan 数据。

## 查询的心智模型

`ls`、`du` 与 `df` 以 GNU Coreutils 为心智模型，但兼容不得牺牲持久化 coverage、离线查询、跨平台字段真实性或“只有 completed scope 才能持久化删除”的不变量：

- `ls` 是一层 live/index 合并视图：列出直接文件、目录和特殊条目；文件大小是自身 logical size，目录大小是已知递归索引聚合，而不是 GNU `ls -l` 的目录对象 `st_size`。`part` / `gone` 可以显示最后已知聚合，但必须保留不完整或现场缺失状态。
- `du` 是显式文件或目录 scope 的递归索引汇总，默认每个参数一行，并可在源目录离线时使用。`ls` 的空间列用于导航提示，`du` 是正式的 scope 汇总接口。
- `roots` 与别名 `df` 使用 GNU `df` 风格容量表，但只列 registered namespace。

各命令的具体行为见 [查询命令](queries.md)。

相关提案：[查询命令 GNU 兼容规划](../plan/proposals/query-cli.md)；版本级规划（含 Rust 边界）见 [路线图](../plan/roadmap.md)。
