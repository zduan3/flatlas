# 当前 MVP（0.1）实现状态

> 状态：冻结实现记录。描述已完成的 MVP（0.1）实现与验证范围。

更新时间：2026-09-11。

本文描述已完成的 Python MVP（版本号 0.1）只读基线；本文中的“MVP”和“0.1”是同义词。0.1 文档不再承载新功能，后续工作写入 `docs/0.2/`。本文不替代 [架构与边界](architecture.md)、[数据库结构](database-schema.md) 和 [功能范围与验收基线](acceptance.md)。MVP 面向不跨文件系统、使用当前平台常规 Unicode 文件名的普通用户目录和归档目录，提供空间清理指导但不执行清理。

## 已实现

### 安装、配置与入口

- `pyproject.toml` 定义 `flatlas = flatlas.cli:main` 命令行入口，可通过 `uv run flatlas ...` 开发运行，或用 `uv tool install --editable .` 安装为用户级工具。
- 通过 `platformdirs` 保存用户级配置和索引位置：Linux 通常使用 `~/.config/flatlas` 与 `~/.local/share/flatlas`，Windows 通常使用 `%LOCALAPPDATA%/flatlas`。
- `flatlas init [PATH]` 会创建全局配置和 SQLite 数据库，并仅登记 PATH 所在的文件系统 namespace；它不会扫描目录或创建 path 记录。
- `--db PATH` 可覆盖全局数据库位置，适用于测试、脚本和故障排查。
- 未初始化或 namespace 未登记等领域错误仅输出 `error: ...` 并以退出码 2 结束，不输出 Python traceback。

### 数据库与扫描

- 首次打开数据库时创建 schema migration、root、scan、scan_scope、path、scan_error、file_hash、plan 和 plan_operation 表及必要索引。
- SQLite 连接启用 foreign keys、WAL 和 busy timeout。
- `flatlas scan PATH` 支持完整 namespace 扫描或任意已登记 namespace 内的 subtree 扫描。交互式终端会在 stderr 原地显示当前阶段、已观察到的普通文件数、目录数和 logical bytes；最终 JSON 在 stdout 中包含 `logical_bytes_seen`。非交互式输出不写动态进度行。
- 每次扫描记录 scan、scope、路径 metadata 与错误；只有 scope 成功完成，才将该 scope 内未再次发现的旧 path 标记为 `deleted`。
- 目录枚举和 stat 失败会令 scope 变为 `partial` 或 `failed`，不会据此推断旧路径被删除。scan 不读取普通文件内容。
- 用户在外部删除文件或目录后，必须扫描一个仍存在且覆盖该路径的父目录；已经不存在的目标不能直接作为 scan PATH。completed scope 会将缺失目录及其已索引后代标记 deleted，partial/failed/cancelled 不会确认删除。
- deleted path 不物理移除，当前查询通过 `state='present'` 排除它；同一路径重新出现时 upsert 恢复 present、清除删除字段，并在 metadata basis 不匹配时将旧 hash 标记 stale。
- 默认不递归进入 symlink；Windows reparse point（包括 junction）记录为 `reparse`，不递归进入。
- Windows 的无符号文件身份值在写入 SQLite 前会映射到其有符号 64 位表示，保证同次及后续比较稳定。
- Windows 的 Python `st_ctime` 不是可靠的 POSIX change time，因此不作为 hash basis；可用的 birth time 仍独立保存和校验。

- flatlas rm PATH 是显式索引维护：递归物理删除 SQLite 中 PATH 的 path/hash 记录，不检查或修改实际文件。引用已移除路径的 dry-run plan 会整体删除，避免破坏其不可变快照。

### Hash、查询与候选计划

- `scan` 只采集路径与 metadata。`dupes PATH` 才在 PATH 子树内批量选择大于 0 B 的同大小候选，按需执行 size → quick BLAKE3 → full BLAKE3；0 B 文件和目录外文件不会因为本次查询而被读取或显示。
- 不超过 128 KiB 的候选在 quick 阶段读取全文并直接成为 `full_ready`；更大的文件 quick hash 读取首尾各 64 KiB，仅 quick 相同的组再读取全文。相同 full digest、算法和大小的当前文件构成重复组。
- 已有 quick/full hash 只有在文件身份、size、mtime、change/birth time 和算法参数仍与索引一致时才复用。每次内容读取使用打开后的前后 `fstat` 验证依据；变化记为 stale，I/O 错误记为 failed，均不会进入重复组。
- 惰性 hash 分批提交，重复执行 `dupes` 可复用已完成结果；交互式 stderr 显示候选、quick/full 文件数和字节进度。表格和 JSON 会明确报告 changed/error 导致的不完整结果，JSON 的稳定顶层结构为 `{"hash": ..., "groups": [...]}`。
- 可用命令：`roots`（别名 `df`）、`paths [PATH]`、`du [PATH ...]`、`ls [PATH]`、`largest [PATH]`、`dupes [PATH]`，其中路径参数均为可选索引范围。`roots` / `df` 默认以 GNU `df` 风格表格显示 registered namespace 的实时容量，容量不可访问时显示未知。`du` 默认以空格对齐的紧凑汇总表输出每个目标的逻辑字节数、文件数、可用时的 allocated 字节数和相对路径，并接受多个文件或目录路径以兼容调用层已经展开的通配结果。`largest` 只在指定的已索引文件或目录子树内查询，PATH 默认为当前目录，默认返回 10 项，--limit 与 -n 等价；默认使用与 `du` 相近的紧凑表格，路径相对于 PATH，JSON/CSV 使用相同的相对路径字段。`dupes` 只在指定的已索引目录子树内构成重复组，PATH 默认为当前目录，并按理论节省 logical size 降序显示汇总和缩进路径；路径默认相对于 PATH，`--absolute` 改为绝对路径，JSON/CSV 与 `export dupes` 使用相同显示规则。
- 当前 `ls` 实时枚举直接文件、目录和特殊条目，并合并持久化 coverage：`ok` 为完整扫描、`new` 为未扫描、`part` 为最近覆盖未完成、`gone` 为索引曾观察到但实时枚举已不存在；它不会据此写入 deleted 状态，枚举失败时整体报错。表头使用 `LOGICAL(B)`：文件显示自身 logical size 和 `N=1`，目录显示递归 indexed logical size，特殊条目为未知；part/gone 在有历史值时保留值和状态。
- 查询命令的后续默认行为调整、GNU 工具兼容边界、产品优先级和推荐参数规划见 [0.2 查询命令规划](../0.2/query-cli.md)；规划内容不应被误写为当前已实现能力。
- 查询可用 `--format json|csv` 与 `--output PATH` 导出；`flatlas export dupes` 提供快捷导出。
- `flatlas plan create --root PATH` 生成不可变 `dry_run` hardlink plan。canonical 以字典序最小路径确定；operation 带 size、full digest 与“不执行”的前置条件。MVP（0.1）没有 `apply` 命令。
- `dupes` 和 dry-run plan 都是清理候选与理论 logical savings 报告，不是安全删除或替换授权。实际去重应由 jdupes 对选定目录重新扫描、逐字节验证并由用户显式执行；flatlas 导出结果不应直接接入删除脚本。

## 当前代码布局

```text
src/flatlas/
  cli.py       # Typer 命令与用户错误边界
  config.py    # 用户级配置、数据库定位
  core.py      # SQLite、扫描、hash、查询、导出、dry-run plan
  errors.py    # 用户可见领域异常
  __main__.py  # python -m flatlas
```

`core.py` 目前集中实现 MVP（0.1）以降低初期复杂度。后续功能增长时，可按 architecture.md 的边界拆分为 `db/`、`scanning/`、`hashing/`、`queries/` 和 `planning/`，但不得改变现有数据库语义。

## 已验证的行为

`tests/test_mvp.py` 覆盖：

- init 仅登记 namespace，未创建 scan/path。
- 完整扫描发现同内容文件，并生成不可变 dry-run plan。
- 局部扫描可与已有索引文件形成重复组；成功重扫局部 scope 后，已删除文件被标记 deleted，scope 外文件仍为 present。
- `largest [PATH]` 只返回指定文件或目录 scope 内的当前普通文件，PATH 默认为 cwd，limit、相对路径和稳定大小排序已有回归测试。
- scan 不产生 hash；`dupes` 只读取查询范围的同大小候选，小文件只读一次，大文件 quick 不匹配时不做 full read，后续查询复用缓存。

当前检查命令：

```powershell
uv run ruff check .
uv run pytest -q
uv run pyright
```

## 完成说明

0.1 功能阶段已经完成。本版本明确不包含 `du --depth`、Linux 非 UTF-8、Windows 完整长路径/reparse tag、allocated size 平台校准、复杂过滤、历史 snapshot、监听、并发/Rust 后端或任何修改文件系统的执行器；这些能力按优先级在 0.2 及后续版本规划。扫描中断的增量 checkpoint 与部分结果持久化见 [0.2 扫描中断规划](../0.2/scan-interruption.md)；惰性 hash 的并发、审计、强制重算、过滤和物理节省量见 [0.2 惰性 Hash 规划](../0.2/lazy-hash.md)。
