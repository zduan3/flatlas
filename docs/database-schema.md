# SQLite 数据库结构草案（MVP）

## 目标与范围

本结构服务于只读 Python MVP：持久化根目录、当前路径状态、扫描覆盖、文件 metadata、哈希和 dry-run plan。它支持先全量索引、后续仅扫描新增或变化子树、再与历史已索引文件查重的流程。

本草案的基线决策是：**保存当前状态与扫描审计，不保存可查询的完整历史快照。** 每个路径保留最近一次确认状态；`scan`、`scan_scope` 和 `scan_error` 保留扫描是否完整及失败原因。未来如需历史快照，应添加独立 snapshot 表，而不是把当前状态表变成无界 observation 日志。

SQLite 连接必须启用：

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
```

所有时间均为 UTC Unix 纳秒（`*_ns`，`INTEGER`）。文件路径以 root-relative 原始字节存为 `BLOB`；Windows MVP 将原生 Unicode 以固定 UTF-8 编码存入 BLOB，Linux 直接存 `os.fsencode()` 的结果。显示字符串只用于 UI/导出，不参与唯一性或路径比较。

## 路径编码与对象身份

`path_key` 使用路径段原始字节以 `0x00` 分隔并以 `0x00` 结尾的编码。例如 `a/b.txt` 为 `b"a\\x00b.txt\\x00"`，根目录为零长度 blob。POSIX 路径段不能含 NUL，故该编码无歧义；它也不依赖 UTF-8。

`device + inode`（以及平台适配器可提供时的 `object_id_raw`）只表示某次扫描中的文件对象观测，**不是跨历史永久对象 ID**。因 inode 或文件 ID 可以复用，复用 full hash 必须同时匹配文件身份、size、mtime 与全部可用的 change/birth 时间；严格模式还需重新读取内容。

`root` 是内部的 **scan namespace**，不是用户查询时必须选择的业务 root。Windows 通常是一块卷的盘符/挂载点，Linux 通常是 `/` 或单独挂载点；全局查询可跨全部 `root` 聚合。该边界仍不可移除，因为扫描 coverage、挂载策略和 future hardlink 的同文件系统限制都依赖它。

## 实体关系

```text
root 1 ── * scan 1 ── * scan_scope
  │                 └── * scan_error
  ├── * path ── 0..1 file_hash
  └── * plan ── * plan_operation

path.parent_path_id ── path.id
```

- `root`：受管理的扫描根、显示信息与将来的配置。
- `scan`：一次完整或局部扫描的生命周期和摘要。
- `scan_scope`：本次扫描申请覆盖的一个子树；只有它完整完成，才允许将该子树内未再次看到的路径标为删除。
- `path`：每个 root-relative 目录项的**当前**状态，目录、常规文件、symlink 和其他类型都占一行。
- `file_hash`：常规文件路径的当前 quick/full hash 及其计算依据。
- `plan` / `plan_operation`：不可变的 dry-run 去重方案；MVP 只创建和导出，不执行。

## MVP DDL

```sql
PRAGMA application_id = 1179402580; -- "FLAT" 的固定项目标识

CREATE TABLE schema_migration (
    version       INTEGER PRIMARY KEY,
    applied_at_ns INTEGER NOT NULL,
    description   TEXT NOT NULL
);

CREATE TABLE root (
    id                 INTEGER PRIMARY KEY,
    root_path_raw      BLOB NOT NULL UNIQUE,
    root_path_display  TEXT NOT NULL,
    platform           TEXT NOT NULL CHECK (platform IN ('windows', 'linux', 'macos')),
    namespace_kind     TEXT NOT NULL CHECK (namespace_kind IN ('windows_volume', 'posix_mount', 'path_anchor')),
    namespace_id_raw   BLOB,
    filesystem_type    TEXT,
    case_sensitive     INTEGER CHECK (case_sensitive IN (0, 1)),
    enabled            INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    config_json        TEXT NOT NULL DEFAULT '{}',
    created_at_ns      INTEGER NOT NULL,
    updated_at_ns      INTEGER NOT NULL
);

CREATE TABLE scan (
    id                 INTEGER PRIMARY KEY,
    root_id            INTEGER NOT NULL REFERENCES root(id),
    kind               TEXT NOT NULL CHECK (kind IN ('full', 'subtree')),
    status             TEXT NOT NULL CHECK (status IN ('running', 'completed', 'partial', 'failed', 'cancelled')),
    started_at_ns      INTEGER NOT NULL,
    finished_at_ns     INTEGER,
    scanner_version    TEXT NOT NULL,
    files_seen         INTEGER NOT NULL DEFAULT 0,
    dirs_seen          INTEGER NOT NULL DEFAULT 0,
    errors_seen        INTEGER NOT NULL DEFAULT 0,
    note               TEXT
);

CREATE TABLE path (
    id                    INTEGER PRIMARY KEY,
    root_id               INTEGER NOT NULL REFERENCES root(id),
    parent_path_id        INTEGER REFERENCES path(id),
    path_key              BLOB NOT NULL,
    name_raw              BLOB NOT NULL,
    path_display          TEXT NOT NULL,
    entry_kind            TEXT NOT NULL CHECK (entry_kind IN ('root', 'directory', 'file', 'symlink', 'reparse', 'other')),
    state                 TEXT NOT NULL CHECK (state IN ('present', 'deleted')),
    device                INTEGER,
    inode                 INTEGER,
    object_id_raw         BLOB,
    mode                  INTEGER,
    uid                   INTEGER,
    gid                   INTEGER,
    nlink                 INTEGER,
    logical_size          INTEGER,
    allocated_size        INTEGER,
    mtime_ns              INTEGER,
    change_time_ns        INTEGER,
    birth_time_ns         INTEGER,
    symlink_target_raw    BLOB,
    reparse_tag           INTEGER,
    first_seen_scan_id    INTEGER NOT NULL REFERENCES scan(id),
    last_seen_scan_id     INTEGER NOT NULL REFERENCES scan(id),
    deleted_by_scope_id   INTEGER REFERENCES scan_scope(id),
    deleted_at_ns         INTEGER,
    UNIQUE (root_id, path_key),
    CHECK (
        (entry_kind = 'file' AND logical_size IS NOT NULL)
        OR entry_kind <> 'file'
    )
);

CREATE TABLE scan_scope (
    id                    INTEGER PRIMARY KEY,
    scan_id               INTEGER NOT NULL REFERENCES scan(id) ON DELETE CASCADE,
    root_id               INTEGER NOT NULL REFERENCES root(id),
    scope_path_key        BLOB NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('running', 'completed', 'partial', 'failed', 'cancelled')),
    started_at_ns         INTEGER NOT NULL,
    finished_at_ns        INTEGER,
    paths_seen            INTEGER NOT NULL DEFAULT 0,
    error_count           INTEGER NOT NULL DEFAULT 0,
    UNIQUE (scan_id, scope_path_key)
);

CREATE TABLE scan_error (
    id                    INTEGER PRIMARY KEY,
    scan_scope_id         INTEGER NOT NULL REFERENCES scan_scope(id) ON DELETE CASCADE,
    path_key              BLOB NOT NULL,
    operation             TEXT NOT NULL CHECK (operation IN ('enumerate', 'stat', 'readlink', 'hash_quick', 'hash_full')),
    error_code            TEXT,
    message               TEXT NOT NULL,
    occurred_at_ns        INTEGER NOT NULL
);

CREATE TABLE file_hash (
    path_id               INTEGER PRIMARY KEY REFERENCES path(id) ON DELETE CASCADE,
    state                 TEXT NOT NULL CHECK (state IN ('missing', 'quick_ready', 'full_ready', 'failed', 'stale')),
    quick_algorithm       TEXT,
    quick_digest          BLOB,
    quick_sample_bytes    INTEGER,
    full_algorithm        TEXT,
    full_digest           BLOB,
    full_verification     TEXT CHECK (full_verification IN ('hash_only', 'byte_compare')),
    basis_device          INTEGER,
    basis_inode           INTEGER,
    basis_size            INTEGER,
    basis_mtime_ns        INTEGER,
    basis_change_time_ns  INTEGER,
    basis_birth_time_ns   INTEGER,
    last_hashed_scan_id   INTEGER REFERENCES scan(id),
    error_message         TEXT,
    CHECK (
        (state = 'missing' AND quick_digest IS NULL AND full_digest IS NULL)
        OR state <> 'missing'
    ),
    CHECK (full_digest IS NULL OR (full_algorithm IS NOT NULL AND basis_size IS NOT NULL))
);

CREATE TABLE plan (
    id                    TEXT PRIMARY KEY, -- UUID
    root_id               INTEGER NOT NULL REFERENCES root(id),
    source_scan_id        INTEGER REFERENCES scan(id),
    status                TEXT NOT NULL CHECK (status IN ('draft', 'dry_run', 'superseded', 'applied', 'partially_applied', 'failed')),
    policy_json           TEXT NOT NULL,
    created_at_ns         INTEGER NOT NULL,
    immutable_at_ns       INTEGER,
    exported_at_ns        INTEGER,
    note                  TEXT
);

CREATE TABLE plan_operation (
    id                    INTEGER PRIMARY KEY,
    plan_id               TEXT NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
    ordinal               INTEGER NOT NULL,
    action                TEXT NOT NULL CHECK (action IN ('hardlink', 'reflink', 'delete')),
    canonical_path_id     INTEGER NOT NULL REFERENCES path(id),
    replacement_path_id   INTEGER NOT NULL REFERENCES path(id),
    expected_size         INTEGER NOT NULL,
    expected_full_digest  BLOB NOT NULL,
    expected_algorithm    TEXT NOT NULL,
    preconditions_json    TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('planned', 'skipped', 'applied', 'failed')),
    result_json           TEXT,
    UNIQUE (plan_id, ordinal),
    UNIQUE (plan_id, replacement_path_id),
    CHECK (canonical_path_id <> replacement_path_id)
);
```

`path.deleted_by_scope_id` 引用了定义在后面的 `scan_scope`。SQLite 允许外键目标表在 `CREATE TABLE` 时尚未创建；也可在 migration 中先创建 `scan_scope`，再创建 `path` 以方便阅读。

`namespace_id_raw` 在 Windows 可保存 Volume GUID 的标准化字节形式，在 Linux 可保存挂载实例/设备身份的适配器值；无法可靠取得时允许为 `NULL`，此时 `root_path_raw` 仍是 namespace 的稳定键。`object_id_raw` 同理：Windows 适配器可保存文件 ID，纯 Python 无法可靠提供时为 `NULL`，再使用 `device` / `inode` 与时间 metadata 进行保守判断。

`reparse_tag` 仅 Windows 使用，用来区分 junction、symlink 和其他 reparse point；Linux 保持 `NULL`。MVP 默认不跟随 `symlink` 或 `reparse`。POSIX FIFO、socket、device 等存为 `other`，不建立 `file_hash`。

## 索引

```sql
CREATE INDEX idx_scan_root_started ON scan(root_id, started_at_ns DESC);
CREATE INDEX idx_scope_scan_status ON scan_scope(scan_id, status);
CREATE INDEX idx_path_root_parent_live ON path(root_id, parent_path_id, state);
CREATE INDEX idx_path_root_kind_size_live ON path(root_id, entry_kind, logical_size)
    WHERE state = 'present' AND entry_kind = 'file';
CREATE INDEX idx_path_last_seen ON path(root_id, last_seen_scan_id);
CREATE INDEX idx_hash_quick ON file_hash(quick_algorithm, quick_digest)
    WHERE state IN ('quick_ready', 'full_ready');
CREATE INDEX idx_hash_full ON file_hash(full_algorithm, full_digest)
    WHERE state = 'full_ready';
CREATE INDEX idx_plan_root_created ON plan(root_id, created_at_ns DESC);
CREATE INDEX idx_plan_operation_plan_status ON plan_operation(plan_id, status);
```

`path_key` 的唯一索引用于稳定地 upsert 路径；`parent_path_id` 使目录浏览和递归 CTE 能准确定位一个 scope 的后代，而无需依赖二进制前缀匹配。

## 扫描与删除语义

每个扫描在一个事务中按下列规则写入：

1. 创建 `scan(status='running')`，并为全根或请求的每个子树创建 `scan_scope(status='running')`。
2. 枚举到路径时，以 `(root_id, path_key)` upsert `path`，更新 metadata、`state='present'` 和 `last_seen_scan_id`。Windows 使用 UTF-8 编码的原生 Unicode path；Linux 保留原始字节。任何已用于 hash 的 identity、size、mtime、change/birth time 变化或不可可靠比较时，将对应 `file_hash.state` 标记为 `stale`。
3. 计算 hash 后更新 `file_hash`；只有 `file_hash.state='full_ready'` 的同一 size/full digest 才进入重复候选组。
4. 遍历、stat 或 hash 失败时插入 `scan_error`，并将 scope 标为 `partial` 或 `failed`。
5. **仅当某个 `scan_scope.status='completed'` 时**，利用 `parent_path_id` 的递归 CTE 找到该 scope 后代，将其中 `last_seen_scan_id <> 当前 scan` 的 `present` 路径更新为 `deleted`，再填充 `deleted_by_scope_id` 与 `deleted_at_ns`。
6. scope 只要是 `partial`、`failed` 或 `cancelled`，绝不依据本次扫描修改任何旧路径为 `deleted`。最后才汇总 `scan.status`。

这满足 MVP 的关键验收：中断扫描、权限错误、过滤排除和扫描中改名均不得被解释为“文件已删除”。`deleted` 行暂不物理清理，保留至手动维护任务或保留策略落地。

## Hash 与重复组查询

quick hash 只用于缩小候选集；重复组必须以相同 `logical_size`、`full_algorithm` 和 `full_digest` 查询，并限制路径和 hash 都仍为当前有效状态：

```sql
SELECT h.full_algorithm, h.full_digest, p.logical_size,
       group_concat(p.path_display, char(10)) AS paths
FROM file_hash AS h
JOIN path AS p ON p.id = h.path_id
WHERE h.state = 'full_ready'
  AND p.state = 'present'
  AND p.entry_kind = 'file'
GROUP BY h.full_algorithm, h.full_digest, p.logical_size
HAVING count(*) > 1;
```

MVP 的 `full_verification` 应先写入 `hash_only`。阶段 4 的 hardlink apply 必须重新验证 metadata 和内容；安全模式应将最终逐字节比较成功记为 `byte_compare`，不得仅凭哈希创建链接。Windows 上若没有可靠的 change-time 字段，hash 复用必须更保守：任一待比较 basis 缺失即重新 hash。

## Plan 不可变性与后续执行

MVP 只能创建 `draft` / `dry_run` plan。一旦设置 `immutable_at_ns`，应用层不得更新 `policy_json`、操作目标、预期 size 或 digest；变更必须创建新 plan，并将旧 plan 标为 `superseded`。

阶段 4 才允许写入 `applied`、`partially_applied` 或 `failed` 状态，并应新增 `operation_attempt` 审计表来记录执行前复核、临时重命名、错误与恢复。`action` 中预留 `reflink` / `delete` 不代表它们属于 MVP。

## 待确认的设计点

- Windows Volume GUID / reparse tag 和 Linux mount/bind mount 的适配器实现细节；本草案已确定字段和空值语义。
- `allocated_size` 的采集方式和跨文件系统可比性；报告时必须与 logical size 分开。
- quick hash 的首尾采样大小、短文件规则和 hash I/O 调度。
- `config_json` / `policy_json` 的正式 schema 与版本化规则。
- deleted path 的保留时长、历史 snapshot 是否在阶段 2 引入。
- symlink、mount point、bind mount 与硬链接组的扫描策略。
