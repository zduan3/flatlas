# 待决策清单

> 状态：计划。列出尚未定案的产品与技术选择；在相应设计完成前，不应提前编码假设。当前开发阶段是 0.2；“MVP”专指已完成并冻结的 0.1 基线。

## 待实操回答的问题

下列问题不适合仅凭纸面设计拍板，应在后续真实盘点与清理中带着观察并记录结论，再反哺 0.2.1 后续项与 0.2.x 设计（各阶段按成熟度的划分见 [backlog](backlog.md)）：

- **删除同步与 tombstone**
  - 实际清理后，走“清单 + `rm --files0-from`”与走“重扫父目录 / 浅扫确认删除”的使用频率分别是多少？`scan --shallow --files0-from` 是否真的高频需要？
  - 有了手动 `rm --deleted PATH` 后，是否仍需要自动保留时长策略或可查询的 scan snapshot 来做前后对比，还是保留清理前后的 `jsonl` 导出在外部 diff 就够用？
- **增量刷新与机械硬盘性能（决定 0.2.1 存储与刷新改造的收益与边界）**
  - 在 Windows 机械硬盘与 Lustre 上，重复刷新和首次大子树查询的真正瓶颈分别是 `readdir`、`stat` 还是 SQLite 随机页读？
  - 目标文件系统（NTFS、Lustre、本地 ext4）上目录 mtime 在条目增删改名和文件原地增长时的表现是否稳定？按目录 mtime 跳过 `readdir` 会漏掉原地增长文件，在定期盘点场景下能否接受，以及如何在 `status` / freshness 中诚实标注？
  - 去掉冗余 `path_display`（改由 `root` + `path_key` 推导）后，大索引体积与冷读耗时能省多少？把 SQL 的 `ORDER BY path_display` 改为按 `path_key` 或 Python 侧排序后，对查询耗时和输出顺序是否有负面影响？
- **重复目录树候选（指导 inode 清理能力）**
  - 先用 `flatlas.api` 在真实索引上写一次性脚本试跑：高 inode 占用的目录树副本（如构建快照、冻结源码树）以完全一致为主，还是以多出少量 `.log`/`.pyc`/时间戳文件的近似一致为主？
  - 实用的最小条目数/体积阈值怎么定？父目录和子目录同时命中重复时，按什么规则折叠才最方便人工核对？
- **重复文件 `dupes` 与交互查询（指导 0.2.x 按需投入）**
  - 在实际清理中运行 `dupes` 的频率与真实瓶颈是什么（小文件多、大文件多、还是同 size 碰撞多）？是否确有必要引入并发调度、`--cached` / `--rehash` 或独立的 hash 运行审计表？
  - 日常交互使用 `ls` / `du` / `find` / `df` 时，真正反复觉得缺失的 GNU 参数或诊断命令有哪些（按实际痛点补齐，不照 manpage 全量对齐）？

## 索引与历史

- 0.1 只维护当前状态、扫描 coverage/error 审计与 plan/operation 记录；不保存可查询的 immutable scan snapshot。是否在后续版本引入 snapshot，取决于保留策略与数据库增长评估。当前结构见 [数据库结构](../design/schema.md)。deleted path 的自动保留时长同样待定；一次扫描确认 368 万条删除后，这些 tombstone 占索引中该子树行数的 94%。
- object observation 如何区分 inode 复用？哪些 `stat` 字段在各平台可用？0.1 不将 `(device, inode)` 当作跨历史永久 ID；复用 hash 时同时核验身份、size、mtime 与全部可用且可靠的 change/birth 时间。
- `config_json` / `policy_json` 的正式 schema 与版本化规则是什么？

## 扫描与过滤

- 0.1 的 scan coverage 最小粒度是请求扫描的 namespace root 或子树（`scan_scope`）；只有该 scope 完整遍历后，才可标记其中未再次观测到的路径为删除。
- include/exclude 规则采用哪种语法，如何在非 UTF-8 Linux 路径下定义语义？
- quick hash 当前采样首尾各 64 KiB、不超过 128 KiB 的文件直接全文读取（见 [惰性 Hash 与重复候选](../design/hashing.md)）；是否按存储类型调整采样大小，以及 hash I/O 调度策略如何定义？
- Windows Volume GUID / reparse tag 和 Linux mount/bind mount 的适配器如何实现？字段和空值语义已由 schema 确定。
- symlink、mount point、bind mount 与硬链接组采用何种扫描策略？

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

## 自动化与机器输出

- 非 UTF-8 名字在机器输出中如何无损表示？`path` 是显示路径（不可解码字节写作 `\xNN`），不能回到文件系统或回传给 `--files0-from`。候选：额外的 `path_raw`（base64 或 hex），仅在显示路径不精确时出现。
- 扫描前后两次结果的差异报告（清理前后对比）由 flatlas 提供，还是由脚本对比两次 `jsonl`？前者需要可查询的 scan snapshot（见「索引与历史」）。
- 是否提供 `find -printf` 子集？`--format jsonl|csv` 已覆盖自动化需求，`-printf` 只服务于交互。
