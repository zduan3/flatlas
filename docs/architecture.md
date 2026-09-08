# 架构与边界

> 状态：开发契约。定义产品边界与不可破坏的语义。

## 产品定位

File Atlas 是持久化的文件系统目录数据库，不是每次查询都重新遍历目录的查重工具。SQLite 保存扫描得到的目录、路径、文件对象、哈希、扫描状态和操作审计；索引可在源目录不可访问时用于浏览、查询和导出。

重复检测、生成候选报告和未来实际修改文件系统必须严格分层：

```text
metadata 扫描/更新索引 → 查询范围内惰性 hash 与重复候选 → 生成报告或 dry-run plan

未来版本：重新扫描与安全复核 → 明确审批 → apply → 审计
```

## Python MVP（0.1）范围

Python 3.12 MVP 的版本号是 0.1；本文中的“MVP”和“0.1”是同义词。MVP（0.1）是只读基线，面向不跨文件系统的普通用户目录和归档目录，只包含以下能力：

- SQLite schema、migration 与当前状态索引。
- 完整扫描与指定子树的局部更新。
- 扫描覆盖状态；只有确认子树完整遍历后才标记旧路径为删除。
- 目录大小、带 PATH scope 的最大文件、路径与基础重复候选查询。
- scan 只采集 metadata；`dupes` 在指定范围内以 size、quick hash、BLAKE3 full hash 惰性分级检测重复内容并复用可靠缓存。
- JSON/CSV 导出，以及可选 dry-run plan 的数据模型与查询。
- rm PATH 是显式索引维护操作：它仅从 SQLite 递归删除已索引路径及其 hash，不读取、验证或修改实际文件系统；这不构成文件删除能力。

MVP（0.1）的输出用于清理指导，不构成删除授权。实际去重由用户使用 jdupes 对选定目录重新扫描、逐字节验证并显式执行。MVP 明确不包含：文件删除或移动、plan apply、hardlink/reflink/symlink 替换、逐字节复核、inotify/持续监听、TUI、PyO3 和 Rust 实现。

## 核心模型

- `root`：被管理的扫描根目录及其配置。
- `path`：目录项、相对路径与最近观测状态。
- `object observation`：某一次观测到的底层文件对象。不能把 `(device, inode)` 视为跨历史的永久 ID，因为 inode 可复用。
- `scan`：完整或局部扫描及覆盖/错误状态。
- `hash`：quick/full hash、计算依据的 metadata 与有效性。
- `plan` / `operation`：未来用于去重计划和审计的不可变记录。

Linux 路径可能不是 UTF-8；持久化层应预留 BLOB 路径与显示编码策略。Windows 首版可以使用原生 Unicode 路径，但不得让此实现限制未来的字节路径支持。

## MVP（0.1）跨平台边界

0.1 在 Windows 与 Linux 上提供相同的常规只读能力：完整/局部扫描、SQLite 离线查询、空间分析、分级 hash、导出和可选 dry-run plan。发布验收限定为当前平台可正常表示的常规 Unicode 路径和不跨文件系统的目录 scope；Linux 非 UTF-8 round-trip、Windows Volume GUID、完整长路径/reparse tag 和复杂 mount/bind mount 策略留给后续版本。平台差异仍不得改变“未完整覆盖即不判定删除”的一致性语义。

| 方面 | Windows | Linux |
|---|---|---|
| 扫描命名空间 | 每个盘符或卷挂载点作为受管理的 scan namespace；后续可用 Volume GUID 稳定识别 | `/` 与独立挂载点作为 scan namespace；必须明确是否跨 mount/bind mount |
| 路径 | 0.1 验收常规 Unicode；完整长路径和 `\\?\\` 前缀适配后续补齐 | 0.1 验收常规 Unicode；非 UTF-8 原始字节 round-trip 后续补齐 |
| 特殊目录项 | 默认不跟随 symlink、junction 和其他 reparse point | 默认不跟随 symlink；FIFO、socket、device 等记录为 `other` 且不 hash |
| metadata | owner/group/mode、POSIX change time 可为空；逻辑大小可靠，allocated size 可能未知 | `st_dev`/`st_ino`、mode、uid/gid 与 POSIX change time 可用；allocated size 可由 block 信息估算 |
| 增量来源 | 0.1 定期扫描指定子树；USN Journal 留给后续 Windows 专用能力 | 0.1 定期扫描指定子树；inotify 只能作为后续 dirty 提示，不能作为事实来源 |

hash 复用在两个平台均采取保守规则：仅在当前文件身份、size、mtime 和全部可用且可靠的变更时间字段都匹配时复用；身份变化、字段不匹配或依据不可靠时重新 hash。0.1 不创建 hardlink，也不执行 plan；同卷/同文件系统、ACL/xattr、逐字节比较和执行前重验属于未来执行器的安全边界。

## 一致性原则

- `inotify` 或其他平台事件只能提示 dirty path，不能作为事实来源。
- 遍历权限错误、I/O 错误、过滤排除与扫描中断都不能被解释为“路径已删除”。
- 仅在 size、mtime、全部可用的 change/birth 时间与对象身份均未变化时复用 full hash；严格验证模式还需重新哈希。
- `logical size`、按 inode 的 allocated size、索引范围内理论节省量与真实物理可回收空间必须分别报告。
- 已有 hardlink、索引范围外的 alias、稀疏文件、压缩与 reflink sharing 会使真实物理节省量未知或只能估算。
- full BLAKE3 相同只作为 0.1 的重复内容候选；theoretical savings 只用于清理优先级参考。文档不得建议将候选导出直接接入删除命令。
- 用户需要实际去重时，应让 jdupes 重新扫描所选目录并使用其独立验证和显式操作流程。
- `ls` 是一层 live/index 合并视图：列出直接文件、目录和特殊条目；文件大小是自身 logical size，目录大小是已知递归索引聚合，而不是 GNU `ls -l` 的目录对象 `st_size`。`part` / `gone` 可以显示最后已知聚合，但必须保留不完整或现场缺失状态。
- `du` 是显式文件或目录 scope 的递归索引汇总，默认每个参数一行，并可在源目录离线时使用。`ls` 的空间列用于导航提示，`du` 是正式的 scope 汇总接口。
- 用户在外部删除条目后，`ls` 只能显示 `gone`；只有覆盖该路径的 completed scan scope 才能持久化 `deleted`。deleted 行作为 tombstone 保留并被当前状态查询排除；路径重现后恢复为 `present`，不可靠的旧 hash basis 必须 stale。

## 查询 CLI 兼容基线

`ls`、`du` 与 `df` 的后续交互设计以 GNU Coreutils 为心智模型：`flatlas ls` 的目标默认接近 `ls -Al` 的一层条目列表，但为清理指导将文件自身 logical size 与目录递归 indexed logical size 统一放在 `LOGICAL(B)` 列；`flatlas du` 默认接近 `du -s` 的显式 scope 汇总；`flatlas roots` 与别名 `df` 使用 GNU `df` 风格容量表，但默认只列 registered namespace。兼容不得牺牲持久化 coverage、离线查询、跨平台字段真实性或“只有 completed scope 才能持久化删除”的不变量。

具体默认行为、与 GNU 的差异、空间回收主线、参数优先级和迁移计划见 [`query-cli-design.md`](query-cli-design.md)。

## 后续 Rust 边界

性能或安全验证要求出现后，再引入独立 Rust core：

- Rust：批量扫描、FD 级 metadata/hash、并行任务、Linux 安全替换执行器。
- Python：CLI、配置、SQLite migration、查询、过滤、plan 与报告。
- 初期优先 Rust sidecar（SQLite、JSON Lines、exit code）；稳定后才评估 PyO3。

Linux 专属能力如 `openat2`、`statx`、`inotify` 和 reflink 必须做 feature 分层；Windows 开发环境只能覆盖跨平台索引逻辑，不能替代 Linux 集成测试。
