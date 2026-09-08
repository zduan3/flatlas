# 同类工具与 MVP（0.1）功能对照

> 状态：调研记录。用于说明产品边界与外部工具对照。

本文记录 File Atlas（`flatlas`）与已调研的同类工具的功能边界。当前 MVP 的版本号是 0.1，本文中的“MVP”和“0.1”是同义词。它是产品范围记录，不要求复刻查重工具的命令行参数或交互；基础查询命令另以 GNU 工具作为用户心智模型，详见 [查询命令设计](query-cli-design.md)。

调研时间：2026-08-21。

## 对照对象

- [Duplicate Files Search & Link (Duplicate Searcher)](https://malich.ru/duplicate_searcher)：Windows 桌面工具；提供按内容找重复文件、识别已有 NTFS hardlink/symlink、删除/移动，以及以 hardlink 或 symlink 替换重复文件的能力。
- [jdupes](https://manpages.debian.org/unstable/jdupes/jdupes.1.en.html)：Linux/Unix 常用的重复文件命令行工具；提供精确内容匹配、递归扫描、JSON/汇总输出、筛选，以及删除、hardlink、symlink 和（受文件系统支持时）Btrfs 块级 dedupe 等动作。

二者都主要以一次命令的实时遍历为中心；`flatlas` 的定位不同：将扫描状态、路径、对象观测和哈希持久化到 SQLite，使后续局部更新、离线查询和审计成为一等能力。

从使用场景看，`flatlas` 以寻找大文件、大目录和重复内容候选来提供清理指导。它不优先复刻 ncdu 的交互式空间浏览，而是补充 ncdu 类空间分析流程中需要另行完成的重复候选识别和局部子树更新。通用文件名查找只是索引的附加价值，不属于 0.1 主目标。

## 能力矩阵

| 能力 | Duplicate Searcher | jdupes | flatlas 可实现的目标能力 | flatlas 0.1 |
|---|---|---|---|---|
| 以内容而非文件名识别重复文件 | 有 | 有 | 有 | 有：size → quick hash → BLAKE3 full hash |
| 递归目录扫描 | 有 | 有 | 有 | 有：完整扫描 |
| 新增目录后仅更新该子树 | 未作为核心能力说明 | 以本次命令遍历为中心，未作为核心增量索引能力说明 | 有 | 有：局部子树更新 |
| 离线浏览、查询既有扫描结果 | 未作为核心能力说明 | 未作为核心能力说明 | 有 | 有：SQLite 当前状态索引 |
| 扫描中断/遍历错误时保守处理旧记录 | 未比较 | 有遍历健壮性措施 | 有 | 有：未覆盖路径不得标记为删除 |
| 按重复组、路径、大小查询 | 有可视化/筛选 | 有分组、size、summary | 有 | 有：`dupes`、路径、`du`、`largest` |
| JSON / CSV 导出 | 未比较 | JSON | 有 | 有：JSON、CSV |
| dry-run 的不可变操作计划 | 未作为核心能力说明 | 可先打印结果再行动 | 有 | 有：plan 数据模型与导出 |
| 识别已存在的 hardlink/symlink | 有 | 有（hardlink 默认按安全策略排除） | 有 | 数据模型预留；查询输出尚未承诺 |
| 文件名、大小、时间、隐藏文件等过滤 | 有丰富筛选 | 有大小、隐藏、排除等筛选 | 有 | 非 0.1；后续配置化 include/exclude 与大小/时间过滤 |
| owner/group/mode、ACL、xattr 等安全差异处理 | 未比较 | 可按 owner/group/mode 排除匹配 | 有 | 非 0.1；未来执行器必须定义并核验 |
| 跟随 symlink、循环防护和跨文件系统策略 | 有链接可视化 | 有 symlink / 单文件系统选项 | 有 | 0.1 默认不跟随链接，验收 scope 不跨文件系统；高级策略后置 |
| 删除或移动重复文件 | 有 | 有删除 | 有 | 否：0.1 只读；未来独立设计和验收 |
| 以 hardlink 替换重复文件 | 有 | 有 | 有 | 否：0.1 只读；未来执行器优先评估 hardlink |
| 以 symlink 替换重复文件 | 有 | 有 | 技术可行 | 当前路线图未承诺 |
| reflink / Btrfs 块级 dedupe | 未比较 | 有（文件系统能力相关） | 有 | 否：须单独做能力探测、测试与策略 |
| 高并发、性能优先扫描 | 多驱动器/SSD 并行 | 性能优先 | 有 | 否：后续 Rust 扫描与 hash 后端 |
| 图形化/TUI 可视化 | 有 GUI | CLI 输出 | 可选 TUI | 否：阶段 5 |

## MVP（0.1）的明确交付边界

0.1 可用于下面的清理指导场景：首次扫描一个不跨文件系统的普通目录；每次新增子目录后只扫描这个子树；再从 SQLite 查询空间占用、最大文件和新旧文件之间的重复候选，并导出 JSON/CSV 或可选 dry-run plan。源目录之后不可访问时，已索引结果仍可查询。

0.1 不修改文件系统：不删除、不移动、不创建 hardlink/reflink/symlink，也不持续监听目录。full BLAKE3 相同表示重复内容候选和理论 logical savings，不是安全删除授权。它不替代 jdupes 或 Duplicate Searcher 的执行器；用户需要实际去重时，应使用 jdupes 对选定目录重新扫描、逐字节验证并显式执行，不能将 flatlas 导出结果直接接入删除命令。

## 执行器阶段必须补齐的安全规则

`jdupes` 默认会在哈希筛选后进行逐字节全文件验证，并将跳过验证的模式视为有数据丢失风险。0.1 不实现 apply；未来若在 `flatlas` 中实现 hardlink 执行器，流程应至少要求：

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

- 0.1：持久化索引、完整/局部扫描、空间查询、分级 hash、稳定导出和只读清理指导。
- 0.2：完善目录深度、筛选、重复节省量、hardlink 识别、coverage/completeness 和平台细节。
- 0.3：先完成逐字节复核、执行前重验、审批、审计和恢复设计，不提供 apply。
- 后续阶段：按独立安全验收引入 hardlink 执行器；reflink 与 delete 分别做能力探测和策略；再考虑 Rust、持续增量与 TUI。

## 资料

- Duplicate Searcher: <https://malich.ru/duplicate_searcher>
- jdupes manual: <https://manpages.debian.org/unstable/jdupes/jdupes.1.en.html>
- 本项目架构与边界：[architecture.md](architecture.md)
- 本项目路线图：[roadmap.md](roadmap.md)
