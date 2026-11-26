# SQLite 数据库结构

> 状态：当前设计。描述 schema v8（`db.SCHEMA_VERSION = 8`）的持久化模型、迁移与查询约束；随代码更新。

## 目标与范围

本结构持久化根目录、当前路径状态、扫描覆盖、文件 metadata、目录汇总、哈希和 dry-run plan，支持先全量索引、后续仅扫描新增或变化子树、再与历史已索引文件查找重复候选的流程。

基线决策是：**保存当前状态与扫描审计，不保存可查询的完整历史快照。** 每个路径保留最近一次确认状态；`scan`、`scan_scope` 和 `scan_error` 保留扫描是否完整及失败原因。未来如需历史快照，应添加独立 snapshot 表，而不是把当前状态表变成无界 observation 日志。

SQLite 连接必须启用：

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA cache_size = -262144;  -- 页缓存上限 256 MiB，按需分配
```

默认 2 MiB 页缓存在索引变大后会让逐条 `path_key` 查找反复读取 B-tree 页，在网络文件系统上每次未命中都是远程读取；缓存按需分配，小索引不承担该上限。

所有时间均为 UTC Unix 纳秒（`*_ns`，`INTEGER`）。文件路径以 root-relative 原始字节存为 `BLOB`；Windows 将原生 Unicode 以固定 UTF-8 编码存入 BLOB，Linux 直接存 `os.fsencode()` 的结果。显示字符串只用于 UI/导出，不参与唯一性或路径比较；SQLite TEXT 必须是合法 UTF-8，因此显示字符串、错误消息中不可解码的字节分别写作 `\xNN`、`\udcNN`（`paths.display_path` / `display_text`），否则一个这样的文件名会让扫描写入失败并以 `failed` 中止。Linux 的非 UTF-8 名称用原始字节匹配 PATH 参数，也用原始字节重新打开文件计算 hash。

## 路径编码与对象身份

`path_key` 使用路径段原始字节以 `0x00` 分隔并以 `0x00` 结尾的编码。例如 `a/b.txt` 为 `b"a\\x00b.txt\\x00"`，根目录为零长度 blob。POSIX 路径段不能含 NUL，故该编码无歧义；它也不依赖 UTF-8。

`device + inode`（以及平台适配器可提供时的 `object_id_raw`）只表示某次扫描中的文件对象观测，**不是跨历史永久对象 ID**。因 inode 或文件 ID 可以复用，复用 full hash 必须同时匹配文件身份、size、mtime 与全部可用的 change/birth 时间。Windows 无符号对象身份写入 SQLite 时映射到 signed-64 范围（大于 `2**63 - 1` 的值减去 `2**64`），保持稳定。

`root` 是内部的 **scan namespace**，不是用户查询时必须选择的业务 root。Windows 通常是一块卷的盘符/挂载点，Linux 通常是 `/` 或单独挂载点；全局查询可跨全部 `root` 聚合。该边界不可移除，因为扫描 coverage、挂载策略和任何 hardlink 的同文件系统限制都依赖它。当前实现中 Windows namespace 为路径的盘符根，POSIX 为路径所在的最近挂载点；`namespace_id_raw` 与 `root_path_raw` 相同。`filesystem_type` 在 POSIX 上登记时从 `/proc/self/mounts` 取该挂载点的类型，再次 `init` 时刷新，检测不到时保持原值；Windows 为空。扫描据此决定是否使用 lazy 元数据。

## 实体关系

```text
root 1 ── * scan 1 ── * scan_scope
  │                 └── * scan_error
  ├── * path ── 0..1 file_hash
  │          └── 0..1 directory_summary（目录与 root）
  └── * plan ── * plan_operation

path.parent_path_id ── path.id
```

- `root`：受管理的扫描根、显示信息与将来的配置。
- `scan`：一次完整或局部扫描的生命周期和摘要。
- `scan_scope`：本次扫描申请覆盖的一个子树；只有它完整完成，才允许将该子树内未再次看到的路径标为删除。
- `path`：每个 root-relative 目录项的**当前**状态，目录、常规文件、symlink 和其他类型都占一行。
- `file_hash`：常规文件路径的当前 quick/full hash 及其计算依据。
- `directory_summary`：每个当前 root 或普通目录的派生汇总与扫描 freshness，见 [扫描与目录汇总](scanning.md)。
- `plan` / `plan_operation`：不可变的 dry-run 候选建议快照；只创建和导出，不执行，也不构成删除或替换授权。

## DDL（schema v8）

以下为语义等价的可读版本；实际语句见 `db.py` 的 `DDL` 与 `DIRECTORY_SUMMARY_MIGRATION` 及后续 migration。

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
    note               TEXT,
    refresh_before_ns  INTEGER, -- migration 3；--refresh-before 的固定 cutoff，普通扫描为 NULL
    stats_json         TEXT,    -- migration 5；分阶段计时 JSON，见 scanning.md「分阶段计时」
    max_depth          INTEGER, -- migration 6；NULL 为不限深度，0 为 --shallow
    logical_bytes_seen INTEGER NOT NULL DEFAULT 0, -- migration 7；已提交的普通文件 logical bytes
    heartbeat_at_ns    INTEGER, -- migration 7；最后一次 checkpoint 时间
    refresh_changed    INTEGER NOT NULL DEFAULT 0 CHECK (refresh_changed IN (0, 1)), -- migration 8
    owner_json         TEXT     -- migration 7；运行进程 {host, pid_ns, pid}，用于判断 running scan 是否已被遗弃
);

CREATE TABLE path (
    id                    INTEGER PRIMARY KEY,
    root_id               INTEGER NOT NULL REFERENCES root(id),
    parent_path_id        INTEGER REFERENCES path(id),
    path_key              BLOB NOT NULL,
    name_raw              BLOB NOT NULL,
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

-- migration 2
CREATE TABLE directory_summary (
    path_id                      INTEGER PRIMARY KEY REFERENCES path(id) ON DELETE CASCADE,
    direct_files                 INTEGER NOT NULL DEFAULT 0 CHECK (direct_files >= 0),
    direct_logical_size          INTEGER NOT NULL DEFAULT 0 CHECK (direct_logical_size >= 0),
    direct_allocated_size        INTEGER CHECK (direct_allocated_size IS NULL OR direct_allocated_size >= 0),
    subtree_files                INTEGER NOT NULL DEFAULT 0 CHECK (subtree_files >= 0),
    subtree_logical_size         INTEGER NOT NULL DEFAULT 0 CHECK (subtree_logical_size >= 0),
    subtree_allocated_size       INTEGER CHECK (subtree_allocated_size IS NULL OR subtree_allocated_size >= 0),
    self_completed_at_ns         INTEGER,
    subtree_complete_through_ns  INTEGER,
    last_completed_scan_id       INTEGER REFERENCES scan(id),
    updated_at_ns                INTEGER NOT NULL,
    direct_entries               INTEGER NOT NULL DEFAULT 0 CHECK (direct_entries >= 0),  -- migration 4
    subtree_entries              INTEGER NOT NULL DEFAULT 0 CHECK (subtree_entries >= 0), -- migration 4
    enumerated_mtime_ns           INTEGER,         -- migration 8
    enumerated_change_time_ns     INTEGER          -- migration 8
);
```

`path.deleted_by_scope_id` 引用了定义在后面的 `scan_scope`。SQLite 允许外键目标表在 `CREATE TABLE` 时尚未创建；也可在 migration 中先创建 `scan_scope`，再创建 `path` 以方便阅读。

`namespace_id_raw` 在 Windows 可保存 Volume GUID 的标准化字节形式，在 Linux 可保存挂载实例/设备身份的适配器值；无法可靠取得时允许为 `NULL`，此时 `root_path_raw` 仍是 namespace 的稳定键。`object_id_raw` 同理：Windows 适配器可保存文件 ID，纯 Python 无法可靠提供时为 `NULL`，再使用 `device` / `inode` 与时间 metadata 进行保守判断。

`reparse_tag` 预留给 Windows 区分 junction、symlink 和其他 reparse point；当前扫描不写入该列，reparse point 只按文件属性记为 `reparse`。默认不跟随 `symlink` 或 `reparse`。POSIX FIFO、socket、device 等存为 `other`，不建立 `file_hash`。`scan_error.operation` 的 `readlink`、`hash_quick`、`hash_full` 为预留值：当前 readlink 失败只令 `symlink_target_raw` 为 `NULL`，hash 错误写入 `file_hash.error_message`。

应用的读写连接和只读连接都注册 `flatlas_display_path`。视图 `path_with_display` 用该函数从 root 原始路径、平台与 `path_key` 推导显示路径。路径 ID、唯一键和面向用户的路径排序保留原语义。读回文件系统时直接使用原始字节构造本机路径。

## 索引

```sql
CREATE INDEX idx_scan_root_started ON scan(root_id, started_at_ns DESC);
CREATE INDEX idx_scope_scan_status ON scan_scope(scan_id, status);
CREATE INDEX idx_path_root_parent_live ON path(root_id, parent_path_id, state);
CREATE INDEX idx_path_parent ON path(parent_path_id);  -- migration 5
CREATE INDEX idx_path_root_kind_size_live ON path(root_id, entry_kind, logical_size)
    WHERE state = 'present' AND entry_kind = 'file';
CREATE INDEX idx_hash_quick ON file_hash(quick_algorithm, quick_digest)
    WHERE state IN ('quick_ready', 'full_ready');
CREATE INDEX idx_hash_full ON file_hash(full_algorithm, full_digest)
    WHERE state = 'full_ready';
CREATE INDEX idx_plan_root_created ON plan(root_id, created_at_ns DESC);
CREATE INDEX idx_plan_operation_canonical_path ON plan_operation(canonical_path_id);     -- migration 8
CREATE INDEX idx_plan_operation_replacement_path ON plan_operation(replacement_path_id); -- migration 8
-- migration 2
CREATE INDEX idx_directory_summary_freshness
    ON directory_summary(self_completed_at_ns, subtree_complete_through_ns);
```

`path_key` 的唯一索引用于稳定地 upsert 路径；`parent_path_id` 使目录浏览和递归 CTE 能准确定位一个 scope 的后代，而无需依赖二进制前缀匹配。

## 扫描与删除语义

每个扫描按下列规则写入；遍历中的观测以短事务 checkpoint 提交，删除确认只在最终事务中进行（完整流程、刷新扫描、中断与崩溃恢复见 [扫描与目录汇总](scanning.md)）：

1. 创建 `scan(status='running')`，并为请求的 namespace root 或子树创建一个 `scan_scope(status='running')`；目标即 namespace 根时 `scan.kind='full'`，否则为 `subtree`。
2. 枚举到路径时，以 `(root_id, path_key)` upsert `path`，更新 metadata、`state='present'` 和 `last_seen_scan_id`。Windows 使用 UTF-8 编码的原生 Unicode path；Linux 保留原始字节。任何已用于 hash 的 identity、size、mtime、change/birth time 变化或不可可靠比较时，将对应 `file_hash.state` 标记为 `stale`。
3. scan 不读取普通文件内容，也不创建新的 `file_hash`。已有 hash 的依据随 metadata 变化时标为 `stale`。
4. 遍历或 stat 失败时插入 `scan_error`，并将 scope 标为 `partial` 或 `failed`。
5. **仅当某个 `scan_scope.status='completed'` 时**，在完整扫描的 `path_key` 区间或局部扫描实际覆盖的目录中确认缺失路径。普通扫描将其更新为 `deleted`，填充 `deleted_by_scope_id` 与 `deleted_at_ns`；[`--purge-deleted`](scanning.md#完整扫描并清理删除记录) 在完整扫描收尾直接移除缺失路径与历史 tombstone。
6. scope 为 `partial`、`failed` 或 `cancelled` 时保留旧路径及其删除状态。最后汇总 `scan.status`。

这满足关键安全要求：中断扫描、权限错误和扫描中改名均不得被解释为“文件已删除”。最终事务同时在 `scan.stats_json.paths_deleted` 记录本次确认删除的行数。`deleted` 行可通过 [`flatlas rm --deleted PATH`](queries.md#rm---deleted-path) 连同其 hash 显式移除，已知索引子树也可用 `rm PATH` 或 `rm --files0-from` 整体移除。

删除状态的用户可见流程如下：

```text
用户在 flatlas 之外删除文件或目录
→ SQLite 中暂时仍可为 present
→ ls 完整枚举父目录时可显示 gone，但不写数据库
→ 用户扫描仍存在且覆盖该路径的父目录
→ completed scope 将缺失路径及其已索引后代标记 deleted
```

已经不存在的路径不能直接作为 `scan PATH`，必须扫描其仍存在的父目录。`find`、`du`、`largest`、`dupes` 等当前状态查询只使用 `state='present'`；关联的 `file_hash` 可以保留，但 deleted path 不参与重复组。相同 `(root_id, path_key)` 后续重新出现时，upsert 将其恢复为 `present`、清除 `deleted_by_scope_id` / `deleted_at_ns`，并在身份、size、mtime 或可用 change/birth time 与旧 basis 不匹配时把 hash 标记为 stale。

`ls` 的 `gone` 与持久化 `deleted` 必须保持分离：`gone` 是一次成功实时枚举提供的现场缺失提示，可能附带最后已知文件大小或目录递归聚合；`deleted` 是 completed scan coverage 才能写入的当前状态 tombstone。

## Hash 与重复组查询

quick hash 只用于缩小候选集；重复组必须以相同 `logical_size`、`full_algorithm` 和 `full_digest` 查询，并限制路径和 hash 都仍为当前有效状态：

```sql
SELECT h.full_algorithm, h.full_digest, p.logical_size,
       group_concat(p.path_display, char(10)) AS paths
FROM file_hash AS h
JOIN path_with_display AS p ON p.id = h.path_id
WHERE h.state = 'full_ready'
  AND p.state = 'present'
  AND p.entry_kind = 'file'
GROUP BY h.full_algorithm, h.full_digest, p.logical_size
HAVING count(*) > 1;
```

`file_hash` 的写入时机、metadata basis 校验、批量提交与 dupes 筛选见 [惰性 Hash 与重复候选](hashing.md)。Windows 上 Python 的 `st_ctime` 不提供稳定的 POSIX change-time 语义，因此 `change_time_ns` 与 `basis_change_time_ns` 在 Windows 为 `NULL`、不参与 hash basis；`mtime_ns`、可用的 `birth_time_ns`、对象身份和 size 仍必须一致。Linux 上可靠的 `st_ctime_ns` 参与校验。

`full_verification` 当前只写入 `hash_only`，因此重复组只是清理候选；`byte_compare` 为预留值，当前没有逐字节比较。

## Plan 不可变性

当前只创建 `dry_run` plan，并在创建时同时设置 `created_at_ns` 与 `immutable_at_ns`。一旦设置 `immutable_at_ns`，应用层不得更新 `policy_json`、操作目标、预期 size 或 digest；变更必须创建新 plan，并将旧 plan 标为 `superseded`。plan 是候选建议快照，不表示其操作已安全验证。

`plan.status` 的 `draft`、`applied`、`partially_applied`、`failed`，`plan_operation.status` 的 `applied`、`failed`，以及 `action` 的 `reflink` / `delete` 均为预留值，当前代码不写入；flatlas 没有 apply。显式 `rm PATH` 若移除被 plan 引用的路径，会连同引用它的整个 plan 一起删除。

## Migration 历史

`schema_migration` 每行记录一个已提交的版本。已发布 migration 的语义不可修改；新增 schema 变更时提高版本号，只做向前迁移，并保留既有数据，不通过删表、重建或清空数据库实现升级。

| 版本 | 说明（`description`） | 内容 |
|---|---|---|
| 1 | `initial persistent index schema` | 初始 schema：root、scan、scan_scope、path、scan_error、file_hash、plan、plan_operation 与索引；设置 `application_id` |
| 2 | `cache directory usage and scan freshness` | 原地创建 `directory_summary` 与 freshness 索引，并为全部 root 回填当前目录汇总 |
| 3 | `record directory refresh cutoff` | 为 `scan` 增加可空 `refresh_before_ns`（列已存在时只写版本记录） |
| 4 | `count directory entries of every kind` | 为 `directory_summary` 增加 `direct_entries`、`subtree_entries`，并为全部 root 重算目录汇总 |
| 5 | `record per-phase scan timing and index path parents` | 为 `scan` 增加可空 `stats_json`，并建 `idx_path_parent`（均可重复执行）|
| 6 | `record scan depth limit` | 为 `scan` 增加可空 `max_depth`（`CHECK max_depth >= 0`）；既有扫描为 NULL |
| 7 | `checkpoint running scans with owner and heartbeat` | 为 `scan` 增加 `logical_bytes_seen`（默认 0）、可空 `heartbeat_at_ns` 与 `owner_json`；既有扫描保持原值 |
| 8 | `derive display paths, index plan references, and store directory refresh baselines` | 原地移除冗余的 `path.path_display`，新增派生视图与两个计划路径外键索引；记录刷新模式与可空的 `directory_summary.enumerated_mtime_ns`、`enumerated_change_time_ns` |

migration 2 回填既有索引时，历史完成时间只从覆盖相应目录的 completed `scan_scope` 保守恢复；partial、failed 或 cancelled scope 不用于推断完整时间。migration 不重建或清空现有 root、path、scan、error、hash 或 plan 数据。

migration 3 记录 `--refresh-before` 解析后的固定 UTC Unix 纳秒时间点。既有普通扫描保留为 `NULL`，迁移不修改 scan、scope、path、error、hash 或 plan 的既有数据。

migration 4 的条目数统计 present 的全部类型（普通文件、目录、symlink、特殊条目）：`direct_entries` 是直接子项数，`subtree_entries` 是全部后代加目录自身，与 GNU `du --inodes` 一致，用于把 inode 配额归因到目录。回填按现有 path 行重算各 root 的汇总，完成时间（`self_completed_at_ns`、`subtree_complete_through_ns`、`last_completed_scan_id`）保持原值。`directory_summary` 的建表语句已包含这两列，因此从版本 1 升级时 migration 2 直接建出含条目列的表，migration 4 只补版本记录与重算。约 13 万目录的索引在 Lustre 上回填约 3 分钟。

v8 在同一迁移事务内使用 `ALTER TABLE ... DROP COLUMN` 和新增 DDL，保留路径 ID 以及所有引用。既有目录基线为空，既有 scan 的 `refresh_changed` 为 0。移除列会改写 SQLite 的记录页，旧库升级时需预留 WAL 空间。

### 执行机制

连接先只读检查 `schema_migration` 是否包含当前所需的全部版本；满足要求时跳过全部 migration 写操作，包括 application ID 设置和 DDL。已有 WAL 数据库也不重复设置 journal mode，因此扫描持有写事务时，`ls` 等查询可以读取已提交的索引。只有缺少迁移版本时才以 `BEGIN IMMEDIATE` 获取写锁，并在锁内重新检查版本，避免并发打开重复升级。待执行的 schema、回填数据与版本记录在同一事务中提交，失败则一起回滚；DDL 逐条执行而不用 `executescript`，以免隐式提交释放写锁。两个扫描仍会竞争同一数据库的写锁。

调用者对索引没有写权限时（agent 沙箱、只读挂载、他人的索引），WAL 模式无法创建 `-shm` 或取得写锁，普通打开会失败。查询命令（`roots`、`df`、`scans`、`scan-info`、`du`、`find`、`ls`、`largest`、`plan show`）此时改用 `db.open_database_readonly`：先以 `mode=ro` 打开，若有写入者正在运行（`-wal` 存在）即读取其已提交的快照；没有 `-wal` 且无法创建时以 `immutable=1` 打开，此时假定没有并发写入者。只读连接不执行 migration，schema 版本落后时报告领域错误，提示在可写处运行一次任意命令。会写入的命令（`init`、`scan`、`rm`、`dupes`、`plan create`）不回退，直接报告无法以写方式打开。

CLI 打开已有数据库并检测到待执行 migration 时，先在 stderr 输出 migration 版本与说明。目录回填期间交互式终端原地显示“已完成目录数 / 总目录数”，非交互式调用按进度里程碑输出；提交 migration 版本记录后再显示完成提示。stdout 不混入 migration 信息，以保持 JSON/CSV 可解析。新建空数据库的 schema 初始化默认静默。migration 中断时不写完成版本，下次打开数据库可幂等重试。

回归测试覆盖扫描 A 时查询 B、当前 schema 的只读检查、待迁移时的写锁竞争、并发打开后的版本复查，以及迁移故障回滚和重试。这些测试可在 Windows/Linux CI 运行；Windows 本地结果不能替代 Linux 验证。

未决的 schema 设计点见 [待决策清单](../plan/decisions.md)。
