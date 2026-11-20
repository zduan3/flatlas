# 待决策清单

> 状态：计划。列出尚未定案的产品与技术选择；在相应设计完成前，不应提前编码假设。当前开发阶段是 0.2；“MVP”专指已完成并冻结的 0.1 基线。

## 索引与历史

- 0.1 只维护当前状态、扫描 coverage/error 审计与 plan/operation 记录；不保存可查询的 immutable scan snapshot。是否在后续版本引入 snapshot，取决于保留策略与数据库增长评估。当前结构见 [数据库结构](../design/schema.md)。deleted path 的保留时长（当前只能用 `rm` 手动物理移除）同样待定。
- object observation 如何区分 inode 复用？哪些 `stat` 字段在各平台可用？0.1 不将 `(device, inode)` 当作跨历史永久 ID；复用 hash 时同时核验身份、size、mtime 与全部可用且可靠的 change/birth 时间。
- `config_json` / `policy_json` 的正式 schema 与版本化规则是什么？

## 扫描与过滤

- 0.1 的 scan coverage 最小粒度是请求扫描的 namespace root 或子树（`scan_scope`）；只有该 scope 完整遍历后，才可标记其中未再次观测到的路径为删除。
- include/exclude 规则采用哪种语法，如何在非 UTF-8 Linux 路径下定义语义？
- quick hash 当前采样首尾各 64 KiB、不超过 128 KiB 的文件直接全文读取（见 [惰性 Hash 与重复候选](../design/hashing.md)）；是否按存储类型调整采样大小，以及 hash I/O 调度策略如何定义？
- Windows Volume GUID / reparse tag 和 Linux mount/bind mount 的适配器如何实现？字段和空值语义已由 schema 确定。
- symlink、mount point、bind mount 与硬链接组采用何种扫描策略？
- lazy 元数据的启用方式：显式 `scan --lazy` 加配置 `scan.lazy`，还是在文件系统为 Lustre（`statfs` magic `0x0BD00BD0`）时自动启用？lazy 模式记录为 `scanner_version` 后缀还是新列？见 [lazy 元数据](proposals/lazy-metadata.md)。

## 去重语义

- canonical 选择如何处理保留区、替换区与 metadata 差异？
- safe/fast 模式分别要求何种执行前复核？
- hardlink 前需要比较哪些 metadata（ACL、xattr、capabilities、SELinux label 等）？
- 理论节省量在已有 hardlink、索引外 alias、reflink 与稀疏文件下如何标注不确定性？
- `allocated_size` 的跨文件系统可比性如何表达？报告时必须与 logical size 分开。
- 平台最低可靠 hash basis 在执行器设计前必须进一步明确。

## 跨平台与发布

- Python 0.1 的 Windows、macOS、Linux 支持边界是什么？0.1 发布验收限定常规 Unicode 路径和不跨文件系统的普通目录，完整平台角落语义后置。
- Linux-only 的安全执行器如何与跨平台只读索引 API 分层？
- Rust 首版使用 sidecar 还是直接 PyO3？
- 数据库格式何时开始提供兼容性承诺与 migration policy？
