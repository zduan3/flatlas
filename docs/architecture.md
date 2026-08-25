# 架构与边界

## 产品定位

File Atlas 是持久化的文件系统目录数据库，不是每次查询都重新遍历目录的查重工具。SQLite 保存扫描得到的目录、路径、文件对象、哈希、扫描状态和操作审计；索引可在源目录不可访问时用于浏览、查询和导出。

重复检测、生成操作计划和实际修改文件系统必须严格分层：

```text
扫描/更新索引 → 查询重复项 → 生成不可变 plan → 审批或 dry-run → apply → 审计
```

## Python MVP 范围

Python 3.12 MVP 只包含以下能力：

- SQLite schema、migration 与当前状态索引。
- 完整扫描与指定子树的局部更新。
- 扫描覆盖状态；只有确认子树完整遍历后才标记旧路径为删除。
- 目录大小、最大文件、路径与基础重复组查询。
- 以 size、quick hash、BLAKE3 full hash 分级检测重复内容。
- dry-run plan 的数据模型与导出。

MVP 明确不包含：文件删除、hardlink/reflink 替换、inotify/持续监听、TUI、PyO3 和 Rust 实现。

## 核心模型

- `root`：被管理的扫描根目录及其配置。
- `path`：目录项、相对路径与最近观测状态。
- `object observation`：某一次观测到的底层文件对象。不能把 `(device, inode)` 视为跨历史的永久 ID，因为 inode 可复用。
- `scan`：完整或局部扫描及覆盖/错误状态。
- `hash`：quick/full hash、计算依据的 metadata 与有效性。
- `plan` / `operation`：未来用于去重计划和审计的不可变记录。

Linux 路径可能不是 UTF-8；持久化层应预留 BLOB 路径与显示编码策略。Windows 首版可以使用原生 Unicode 路径，但不得让此实现限制未来的字节路径支持。

## MVP 跨平台边界

MVP 在 Windows 与 Linux 上提供相同的只读能力：完整/局部扫描、SQLite 离线查询、分级 hash、导出和 dry-run plan。平台差异必须封装在扫描适配层，不能改变“未完整覆盖即不判定删除”的一致性语义。

| 方面 | Windows | Linux |
|---|---|---|
| 扫描命名空间 | 每个盘符或卷挂载点作为受管理的 scan namespace；后续可用 Volume GUID 稳定识别 | `/` 与独立挂载点作为 scan namespace；必须明确是否跨 mount/bind mount |
| 路径 | 原生 Unicode，保留实际大小写；处理长路径和 `\\?\\` 前缀 | 使用 `os.fsencode()` 原始字节保存；显示层采用明确的错误替代策略 |
| 特殊目录项 | 默认不跟随 symlink、junction 和其他 reparse point | 默认不跟随 symlink；FIFO、socket、device 等记录为 `other` 且不 hash |
| metadata | owner/group/mode、POSIX change time 可为空；逻辑大小可靠，allocated size 可能未知 | `st_dev`/`st_ino`、mode、uid/gid 与 POSIX change time 可用；allocated size 可由 block 信息估算 |
| 增量来源 | MVP 定期扫描指定子树；USN Journal 留给后续 Windows 专用能力 | MVP 定期扫描指定子树；inotify 只能作为后续 dirty 提示，不能作为事实来源 |

hash 复用在两个平台均采取保守规则：仅在当前文件身份、size、mtime 和全部可用的变更时间字段都匹配时复用；任一字段缺失、身份变化或语义不可靠时重新 hash。MVP 不创建 hardlink，因此同卷/同文件系统限制只在 dry-run plan 的 precondition 中报告，不执行修改。

## 一致性原则

- `inotify` 或其他平台事件只能提示 dirty path，不能作为事实来源。
- 遍历权限错误、I/O 错误、过滤排除与扫描中断都不能被解释为“路径已删除”。
- 仅在 size、mtime、全部可用的 change/birth 时间与对象身份均未变化时复用 full hash；严格验证模式还需重新哈希。
- `logical size`、按 inode 的 allocated size、索引范围内理论节省量与真实物理可回收空间必须分别报告。
- 已有 hardlink、索引范围外的 alias、稀疏文件、压缩与 reflink sharing 会使真实物理节省量未知或只能估算。

## 查询 CLI 兼容基线

`ls` 与 `du` 的后续交互设计以 GNU Coreutils 为心智模型：`flatlas ls` 的目标默认接近 `ls -Al`，即包含真实隐藏条目但不合成 `.` 与 `..`；`flatlas du` 的目标默认接近 `du -s`。兼容不得牺牲持久化 coverage、离线查询、跨平台字段真实性或“只有 completed scope 才能持久化删除”的不变量。

具体默认行为、与 GNU 的差异、参数优先级和迁移计划见 [`query-cli-design.md`](query-cli-design.md)。

## 后续 Rust 边界

性能或安全验证要求出现后，再引入独立 Rust core：

- Rust：批量扫描、FD 级 metadata/hash、并行任务、Linux 安全替换执行器。
- Python：CLI、配置、SQLite migration、查询、过滤、plan 与报告。
- 初期优先 Rust sidecar（SQLite、JSON Lines、exit code）；稳定后才评估 PyO3。

Linux 专属能力如 `openat2`、`statx`、`inotify` 和 reflink 必须做 feature 分层；Windows 开发环境只能覆盖跨平台索引逻辑，不能替代 Linux 集成测试。
