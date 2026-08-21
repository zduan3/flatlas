# 待决策清单

这些事项尚未决定；在相应阶段设计完成前，不应提前编码假设。

## 索引与历史

- MVP 只维护当前状态、扫描 coverage/error 审计与 plan/operation 记录；不保存可查询的 immutable scan snapshot。是否在阶段 2 引入 snapshot，取决于保留策略与数据库增长评估。详见 [数据库结构草案](database-schema.md)。
- object observation 如何区分 inode 复用？哪些 `stat` 字段在各平台可用？MVP 不将 `(device, inode)` 当作跨历史永久 ID；复用 hash 时同时核验身份、size、mtime 与全部可用的 change/birth 时间。
- MVP 的目录聚合统计动态计算；如基准测试证明需要，再在阶段 2 维护可失效的增量缓存。

## 扫描与过滤

- MVP 的 scan coverage 最小粒度是请求扫描的 root 或子树（`scan_scope`）；只有该 scope 完整遍历后，才可标记其中未再次观测到的路径为删除。
- include/exclude 规则采用哪种语法，如何在非 UTF-8 Linux 路径下定义语义？
- quick hash 的首尾采样大小、短文件策略和 I/O 调度策略是什么？

## 去重语义

- canonical 选择如何处理保留区、替换区与 metadata 差异？
- safe/fast 模式分别要求何种执行前复核？
- hardlink 前需要比较哪些 metadata（ACL、xattr、capabilities、SELinux label 等）？
- 理论节省量在已有 hardlink、索引外 alias、reflink 与稀疏文件下如何标注不确定性？

## 跨平台与发布

- Python MVP 的 Windows、macOS、Linux 支持边界是什么？
- Linux-only 的安全执行器如何与跨平台只读索引 API 分层？
- Rust 首版使用 sidecar 还是直接 PyO3？
- 数据库格式何时开始提供兼容性承诺与 migration policy？
