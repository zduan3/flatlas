# 同类工具与 MVP 功能对照

本文记录 File Atlas（`flatlas`）与已调研的同类工具的功能边界。它是产品范围记录，不要求复刻查重工具的命令行参数或交互；基础查询命令另以 GNU 工具作为用户心智模型，详见 [查询命令设计](query-cli-design.md)。

调研时间：2026-08-21。

## 对照对象

- [Duplicate Files Search & Link (Duplicate Searcher)](https://malich.ru/duplicate_searcher)：Windows 桌面工具；提供按内容找重复文件、识别已有 NTFS hardlink/symlink、删除/移动，以及以 hardlink 或 symlink 替换重复文件的能力。
- [jdupes](https://manpages.debian.org/unstable/jdupes/jdupes.1.en.html)：Linux/Unix 常用的重复文件命令行工具；提供精确内容匹配、递归扫描、JSON/汇总输出、筛选，以及删除、hardlink、symlink 和（受文件系统支持时）Btrfs 块级 dedupe 等动作。

二者都主要以一次命令的实时遍历为中心；`flatlas` 的定位不同：将扫描状态、路径、对象观测和哈希持久化到 SQLite，使后续局部更新、离线查询和审计成为一等能力。

从使用场景看，`flatlas` 以寻找大文件、大目录和重复内容来辅助释放空间。它不优先复刻 ncdu 的交互式空间浏览，而是补充 ncdu 类空间分析流程中需要另行完成的重复内容识别和局部子树更新。通用文件名查找只是索引的附加价值，不属于 MVP 主目标。

## 能力矩阵

| 能力 | Duplicate Searcher | jdupes | flatlas 可实现的目标能力 | flatlas MVP |
|---|---|---|---|---|
| 以内容而非文件名识别重复文件 | 有 | 有 | 有 | 有：size → quick hash → BLAKE3 full hash |
| 递归目录扫描 | 有 | 有 | 有 | 有：完整扫描 |
| 新增目录后仅更新该子树 | 未作为核心能力说明 | 以本次命令遍历为中心，未作为核心增量索引能力说明 | 有 | 有：局部子树更新 |
| 离线浏览、查询既有扫描结果 | 未作为核心能力说明 | 未作为核心能力说明 | 有 | 有：SQLite 当前状态索引 |
| 扫描中断/遍历错误时保守处理旧记录 | 未比较 | 有遍历健壮性措施 | 有 | 有：未覆盖路径不得标记为删除 |
| 按重复组、路径、大小查询 | 有可视化/筛选 | 有分组、size、summary | 有 | 有：`duplicates`、路径、`du`、`largest` |
| JSON / CSV 导出 | 未比较 | JSON | 有 | 有：JSON、CSV |
| dry-run 的不可变操作计划 | 未作为核心能力说明 | 可先打印结果再行动 | 有 | 有：plan 数据模型与导出 |
| 识别已存在的 hardlink/symlink | 有 | 有（hardlink 默认按安全策略排除） | 有 | 数据模型预留；查询输出尚未承诺 |
| 文件名、大小、时间、隐藏文件等过滤 | 有丰富筛选 | 有大小、隐藏、排除等筛选 | 有 | 非 MVP；阶段 2 配置化 include/exclude 与大小/时间过滤 |
| owner/group/mode、ACL、xattr 等安全差异处理 | 未比较 | 可按 owner/group/mode 排除匹配 | 有 | 非 MVP；hardlink 执行前必须定义并核验 |
| 跟随 symlink、循环防护和跨文件系统策略 | 有链接可视化 | 有 symlink / 单文件系统选项 | 有 | 非 MVP；须明确策略并测试 |
| 删除或移动重复文件 | 有 | 有删除 | 有 | 否：阶段 4 |
| 以 hardlink 替换重复文件 | 有 | 有 | 有 | 否：阶段 4，先实现 hardlink |
| 以 symlink 替换重复文件 | 有 | 有 | 技术可行 | 当前路线图未承诺 |
| reflink / Btrfs 块级 dedupe | 未比较 | 有（文件系统能力相关） | 有 | 否：须单独做能力探测、测试与策略 |
| 高并发、性能优先扫描 | 多驱动器/SSD 并行 | 性能优先 | 有 | 否：阶段 3 Rust 扫描与 hash 后端 |
| 图形化/TUI 可视化 | 有 GUI | CLI 输出 | 可选 TUI | 否：阶段 5 |

## MVP 的明确交付边界

MVP 可用于下面的增量归档场景：首次扫描一个根目录；每次新增子目录后只扫描这个子树；再从 SQLite 查询新旧文件之间的重复组并导出 JSON/CSV 或 dry-run plan。源目录之后不可访问时，已索引结果仍可查询。

MVP 不修改文件系统：不删除、不移动、不创建 hardlink/reflink/symlink，也不持续监听目录。它不以替代 jdupes 或 Duplicate Searcher 的执行器为目标，而是为后续安全执行器提供可信、可审计的索引输入。

## 执行器阶段必须补齐的安全规则

`jdupes` 默认会在哈希筛选后进行逐字节全文件验证，并将跳过验证的模式视为有数据丢失风险。为了在 `flatlas` 中安全地生成 hardlink，阶段 4 的 apply 流程应至少要求：

```text
size 相同
→ quick hash 相同
→ BLAKE3 full hash 相同
→ apply 前重新验证路径、对象身份及 metadata
→ 逐字节比较两端当前内容
→ 检查同一文件系统及权限/ACL/xattr/SELinux 等策略
→ 原子或可恢复地替换为 hardlink，并写入审计记录
```

硬链接后，任一路径发生原地写入都会修改同一底层内容。因此系统文件、安装器管理目录、可变应用数据和备份目录必须默认排除，除非用户显式制定并批准风险策略。

## 与现有路线图的对应

- 阶段 1：持久化索引、完整/局部扫描、基础查询、分级哈希、导出与 dry-run plan。
- 阶段 2：优先完善重复节省量、大文件/目录排序筛选、局部更新和 coverage/completeness；随后才是通用路径查找、历史策略与其他便利功能。
- 阶段 3：并发和性能导向的 Rust 扫描/hash 后端。
- 阶段 4：重新验证、hardlink、审计、崩溃恢复；reflink 与 delete 需独立能力探测和策略。
- 阶段 5：持续增量、TUI、稳定协议与发行体验。

## 资料

- Duplicate Searcher: <https://malich.ru/duplicate_searcher>
- jdupes manual: <https://manpages.debian.org/unstable/jdupes/jdupes.1.en.html>
- 本项目架构与边界：[architecture.md](architecture.md)
- 本项目路线图：[roadmap.md](roadmap.md)
