# 0.2 惰性 Hash 规划

> 状态：0.2 规划。不纳入已冻结的 MVP（0.1）。

## 状态与当前基线

本文记录 0.2 的惰性 Hash 规划。0.1 当前基线是：`scan` 只采集 metadata；`dupes PATH` 只为 PATH 子树内的同大小普通文件计算 hash，使用 size → quick → full 流水线、前后 `fstat` 校验和保守缓存复用。已完成的 hash 分批提交，因此中断 `dupes` 不会丢失此前已提交的缓存；这不等同于完整的 hash 运行审计或可恢复任务。0.1 的结果只用于清理指导，实际去重由 jdupes 重新扫描和验证。

## 后续能力

### 调度与性能

- 加入有界并发，分别限制打开文件数、读取任务数和 SQLite 写入批次。
- 根据旋转磁盘、SSD、网络盘等存储特征选择并发度；默认值必须保守，避免随机读取拖慢整机。
- 在真实的大文件数、同尺寸高碰撞和大文件语料上建立吞吐、打开次数、读取字节数和峰值内存基准。
- 支持后台 hash 与取消恢复，但后台结果仍必须满足相同的 metadata basis 校验。

### 用户控制与可观测性

- `--cached`：只查询当前有效的 full hash，不打开文件；输出必须说明尚未 hash 的候选，因此不能把空结果解释为“没有重复”。
- `--rehash`：忽略有效缓存并重新读取，作为校验或算法迁移工具；不得隐式成为默认行为。
- 为 hash 运行新增独立的 run/error 审计模型，记录 scope、算法版本、候选数、实际读取字节、cache hit、changed、failed、cancelled 和时间。它不应复用 `scan_scope`，因为 hash 失败不影响目录 coverage。
- 稳定机器输出继续区分完整结果与部分结果；CSV 在零重复组时也要有独立摘要表达方式，可考虑 summary sidecar 或新的流式记录类型。

### 查询范围与过滤

- 最小/最大文件大小、最小组文件数、最小理论节省量和明确的 include/exclude 过滤。
- 多个 PATH 的 union 与交叉目录比较；必须定义路径显示基准、namespace 边界和候选组语义。
- 算法升级和 quick 采样策略版本化；采样大小或布局变化必须使旧 quick cache 不可复用。

### 空间真实性与安全验证

- 识别同一文件对象的 hardlink alias，分别报告路径冗余、logical 理论节省量、allocated size 与可物理回收空间，不能把它们相加或互换。
- 对稀疏、压缩、reflink/shared extent 和索引范围外 alias 明确标记物理节省量未知或估算。
- `full hash` 适合持久缓存、跨多文件分组和后续增量查询；逐字节比较无需依赖碰撞假设，但不能作为可复用的全局分组键且会重复读取。未来任何修改文件系统的 apply 都必须在执行前重新验证身份与 metadata，并可在安全模式中逐字节比较；不能只凭历史 full hash 修改文件。

## Schema 与迁移原则

若新增 hash run、error、算法策略或统计字段，应提高 migration 版本并执行只向前迁移。迁移必须保留现有 `path`、`file_hash`、scan coverage 与 plan 数据；不得通过删表、重建数据库或清空缓存实现升级。

## 建议测试

- HDD/SSD 友好的有界并发与取消，不泄漏文件句柄，不阻塞 SQLite reader。
- 中断或崩溃后已提交 hash 可复用，未完成文件不会被误标为 `full_ready`。
- `--cached`、`--rehash` 和算法/采样版本变化具有可预测的 cache hit/miss。
- 多 PATH、过滤和 hardlink 语料不会越过查询范围，也不会夸大物理节省量。
- apply 前文件被替换、改写或换 inode 时，重验可靠拒绝操作。
