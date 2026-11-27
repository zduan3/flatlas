# 惰性 Hash 后续能力

> 状态：提案，未实现。当前的 hash 流水线、缓存校验、dupes 筛选和本次诊断见 [惰性 Hash 与重复候选](../../design/hashing.md)。

## 后续能力

### 调度与性能

- 评估 full hash 有界并发，以及 quick hash 在旋转磁盘、SSD、网络盘等存储介质上的调度。quick hash 已支持显式指定 worker 数，见 [当前流水线](../../design/hashing.md#流水线)。
- 在真实的大文件数、同尺寸高碰撞和大文件语料上建立吞吐、打开次数、读取字节数和峰值内存基准。
- 支持后台 hash 与取消恢复，但后台结果仍必须满足相同的 metadata basis 校验。

### 用户控制

- `--cached`：只查询当前有效的 full hash，不打开文件；输出必须说明尚未 hash 的候选，因此不能把空结果解释为“没有重复”。
- `--rehash`：忽略有效缓存并重新读取，作为校验或算法迁移工具；不得隐式成为默认行为。

### 查询范围与过滤

- include/exclude 过滤仍待设计。
- 多个 PATH 的 union 与交叉目录比较；必须定义路径显示基准、namespace 边界和候选组语义。
- 算法升级和 quick 采样策略版本化；采样大小或布局变化必须使旧 quick cache 不可复用。

### 空间真实性与安全验证

- 识别同一文件对象的 hardlink alias，分别报告路径冗余、logical 理论节省量、allocated size 与可物理回收空间，不能把它们相加或互换；alias 合并已实现，见 [重复组](../../design/hashing.md#重复组)。
- 对稀疏、压缩、reflink/shared extent 和索引范围外 alias 明确标记物理节省量未知或估算。
- `full hash` 适合持久缓存、跨多文件分组和后续增量查询；逐字节比较无需依赖碰撞假设，但不能作为可复用的全局分组键且会重复读取。未来任何修改文件系统的 apply 都必须在执行前重新验证身份与 metadata，并可在安全模式中逐字节比较；不能只凭历史 full hash 修改文件。

## Schema 与迁移原则

若日后出现按运行 ID 或时间回查的需求，再明确历史保留策略并评估独立的 run/error 表；届时以向前迁移保留现有 `path`、`file_hash`、scan coverage 与 plan 数据。

## 测试

- HDD/SSD 友好的有界并发与取消，不泄漏文件句柄，不阻塞 SQLite reader。
- 中断或崩溃后已提交 hash 可复用，未完成文件不会被误标为 `full_ready`。
- `--cached`、`--rehash` 和算法/采样版本变化具有可预测的 cache hit/miss。
- 多 PATH、过滤和 hardlink 语料不会越过查询范围，也不会夸大物理节省量。
- apply 前文件被替换、改写或换 inode 时，重验可靠拒绝操作。
