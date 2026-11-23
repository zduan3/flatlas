# File Atlas (`flatlas`)

跨 Windows/Linux 的持久化、只读文件系统索引工具，面向不跨文件系统的普通用户目录和归档目录，用于分析空间占用、定位最大文件和发现重复内容候选。扫描结果保存在 SQLite 中，可离线查询和导出，不会修改用户文件。当前开发阶段是 0.2；已交付的 MVP（0.1）以 git tag `v0.1.0` 冻结。本文只描述当前已实现的行为。

本文是面向日常使用的说明；当前设计、开发计划与历史记录请从 [开发文档导航](docs/README.md) 进入，当前阶段的待办见 [backlog](docs/plan/backlog.md)。

## 安装

项目使用 Python 3.12 与 uv：

```powershell
uv sync
uv run flatlas --help
```

安装为用户级命令行工具：

```powershell
uv tool install --editable .
flatlas --help
```

发布到包索引后，用户可使用 `uv tool install flatlas` 安装。

## 首次使用

`init` 仅登记当前路径所属的文件系统 namespace，不会遍历或扫描文件；Linux 上同时记录挂载点的文件系统类型，Lustre 上的扫描据此使用 lazy 元数据（大幅减少对存储服务器的请求，正在写入的文件大小可能滞后）。配置在用户级目录，Linux 通常为 `~/.config/flatlas`，Windows 通常为 `%LOCALAPPDATA%/flatlas`；索引数据库独立于项目目录。

```powershell
flatlas init D:\Archive
flatlas scan D:\Archive
flatlas scan D:\Archive\2026\08
flatlas rm D:\Archive\2026\08
flatlas dupes D:\Archive
flatlas du D:\Archive\2026
flatlas ls D:\Archive\2026
flatlas largest D:\Archive\2026 -n 50
flatlas export dupes --format csv --output duplicates.csv
flatlas plan create --root D:\Archive
```

`--db PATH` 仅用于测试、迁移、脚本或故障排查等需要覆盖全局索引位置的场景。未初始化时，命令只输出可读的 `error: ...` 信息并返回退出码 2。

## 当前 CLI

```text
flatlas --version
flatlas init [PATH]
flatlas config set scan.refresh-before VALUE
flatlas config get scan.refresh-before
flatlas config unset scan.refresh-before
flatlas config list [-a|--all]
flatlas roots [-h]
flatlas df [-h] [-T] [FILE ...]
flatlas scan PATH [--refresh-before VALUE | --full | --resume | --shallow]
flatlas scan --resume-from ID
flatlas scans [PATH] [--limit N] [--format table|json|csv]
flatlas scan-info ID
flatlas paths [PATH] [--limit N] [--format json|csv]
flatlas rm PATH
flatlas du [PATH ...] [-D] [-d N] [--sort KEY] [-n N] [--freed] [-c] [--format table|json|csv]
flatlas ls [PATH] [--format table|json|csv]
flatlas largest [PATH] [-n N|--limit N] [--format table|json|csv]
flatlas dupes [PATH] [--absolute] [--min-size B] [--max-size B] [--min-count N] [--min-savings B] [-n N] [--format table|json|csv]
flatlas export dupes [PATH] [--absolute] [dupes 筛选参数] --format json|csv --output PATH
flatlas plan create --root PATH
flatlas plan show [PLAN_ID]
```

在源码仓库或 editable 安装中，`flatlas --version` 同时显示当前 Git 提交、提交日期以及工作区是否为 `dirty`；无法访问 Git 元数据的安装包回退为只显示包版本。

## 命令默认参数

索引默认放在用户数据目录（Linux 通常为 `~/.local/share/flatlas/index.sqlite`），可以用 `index.dir` 改到别处，例如需要让 agent 沙箱可写的目录：

```bash
flatlas config set index.dir /path/to/flatlas      # 索引为 /path/to/flatlas/index.sqlite
```

修改只影响配置，不移动文件；已有索引时按提示把 `index.sqlite`（及 `-wal`/`-shm`）移到新目录，或重新 `init`。`--db` 仍可单次覆盖。

用 `config` 将默认参数保存到用户级 `config.toml`，当前支持 `scan.refresh-before`：

```powershell
flatlas config set scan.refresh-before 7d
flatlas config get scan.refresh-before
flatlas config list
flatlas config list -a
flatlas scan D:\Archive
flatlas scan D:\Archive --refresh-before 12h
flatlas scan D:\Archive --full
flatlas config unset scan.refresh-before
```

上述配置让普通 `scan` 只刷新从未完整扫描或最近完整扫描早于七天前的目录；相对时长在每次扫描启动时重新计算为固定 cutoff。也可保存 ISO 8601 时间，例如 `2026-01-01T00:00:00+08:00`。显式 `--refresh-before` 优先于配置，`--full` 临时执行完整扫描；移除配置后恢复默认完整扫描。`--resume` / `--resume-from` 继承历史扫描的固定 cutoff。`--full`、`--refresh-before`、`--resume`、`--resume-from` 互斥。

## 中断扫描

扫描过程中按 Ctrl-C 会停止遍历并保存已观测的数据，scan 记为 `cancelled`，不确认任何删除；保存阶段再按一次则跳过收尾，只保留已提交的 checkpoint。在 Linux 等 POSIX 系统上，`SIGTERM`（如批处理调度器终止作业）和 `SIGHUP`（如终端关闭或远程会话断开）按同样方式处理；在 `nohup` 下被忽略的 `SIGHUP` 仍保持忽略。之后可用 `flatlas scan PATH --resume` 或 `flatlas scan --resume-from ID` 继续，用 `flatlas scans` / `flatlas scan-info ID` 查看历史记录。扫描每 5 秒提交一次 checkpoint；进程被强制杀死（如 `SIGKILL`）时保留已提交的部分，`--resume` 判定原进程已退出后接管该扫描并从 checkpoint 继续。行为细节见 [扫描与目录汇总](docs/design/scanning.md#中断与续扫)。

`config` 可以在 `init` 前使用；读取或移除未设置的参数时无输出，读取返回退出码 0。配置命令不打开索引数据库，修改配置时保留已有数据库路径、其他字段和注释。配置默认值同样适用于显式指定 `--db` 的扫描。

`config list` 按键名排序，以 `key = value` 列出已设置的命令默认参数；`config list -a`（`--all`）列出所有支持的参数，未设置的值显示为 `<unset>`。没有已设置的参数时，普通 `list` 无输出并返回退出码 0。

## 只读访问

索引所在目录不可写时（例如在 agent 沙箱中查看他人或沙箱外维护的索引），`roots`、`du`、`ls`、`largest`、`scans` 等查询命令自动以只读方式打开，结果与正常打开相同；`scan`、`dupes` 等需要写入的命令报错退出。只读打开不会升级 schema，版本落后时先在可写处运行一次任意命令。

## 当前安全边界与能力

- 已验收的测试基线是一个不跨文件系统、使用当前平台常规 Unicode 文件名的普通目录；Linux 非 UTF-8 文件名可以扫描（显示为 `\xNN` 转义），但对它们的 hash 与按路径查询、完整长路径/reparse tag、复杂 mount 和物理共享空间语义尚未支持。
- 完整扫描与指定子树更新；仅在 scope 完整覆盖后将缺失路径标记为 `deleted`。
- size → quick BLAKE3 → full BLAKE3 的重复候选检测；默认不跟随 symlink 或 Windows reparse point。
- 支持路径、目录大小、最大文件、重复候选查询、JSON/CSV 导出和 dry-run plan。
- `roots` 只读索引，显示各 registered namespace 的文件系统类型、元数据方式（Lustre 为 lazy）、已索引文件数与大小和最近一次扫描。
- `df [FILE ...]` 贴近 GNU `df` 实时显示容量：给出 FILE 时对 FILE 本身查询（Lustre project quota 下即显示项目配额），无参数时列出 registered namespace；支持 `-h`、`-T`，不可访问的容量显示为未知。
- `du` 默认输出带表头且按列对齐的汇总，包含逻辑字节数、文件数、可用时的实际分配字节数和相对路径；紧凑表头使用 `SIZE(B)`、`N` 和 `ALLOC(B)`。
- `du` 可同时接受多个文件或目录路径，因此也能处理 shell 或启动器提前展开后的通配结果；某个路径出错时其余照常输出，退出码为 1。与 GNU `du` 一样默认不跟随参数中的 symlink，用 `PATH/` 或 `-D` 跟随。`--freed` 增加按 inode 去重的 `UNIQ` 和“删除后实际腾出”的 `FREED` 两列，`-c` 增加合计行（`--freed` 时按参数并集计算）；`E` 列是含目录自身的条目数（inode），`-d N` 展开子目录，`--sort` 与 `-n` 取 top-N，见 [查询命令](docs/design/queries.md#du-path-)。
- `ls -h [PATH]` 和 `du -h [PATH ...]`（长选项 `--human-readable`）按 GNU `-h` 规则显示表格大小（1024 为底、向上取整、小于 10 时保留一位小数，如 `1.0K`）；小于 1024 的值仍显示字节整数，未知值显示 `-`。大小表头省略 `(B)`，文件数不缩放，JSON/CSV 仍输出原始字节整数。
- `ls [PATH]` 实时枚举目标目录的一层文件、目录和特殊条目，并合并持久化 scan coverage。表格使用 `LOGICAL(B)`：文件显示自身 logical size 和 `N=1`，目录显示已知递归索引 logical size 和文件数；特殊条目的统计为未知；`SCANNED-THROUGH` 显示已索引目录子树的扫描新鲜度下界，未知显示 `-`；`ok`、`new`、`part`、`gone` 表示统计的覆盖和现场状态。
- `largest [PATH]` 只查询指定的已索引文件或目录 scope，PATH 默认为当前目录；按 logical size 降序、路径升序稳定排序，路径默认相对于 PATH。默认返回 10 项；-n 是 --limit 的短别名。
- 不删除、移动、hardlink、reflink、symlink 或持续监听文件系统。
- rm PATH 只从 SQLite 递归移除该路径及其已索引后代，等价于对索引执行 rm -r PATH；它不访问、不验证也不改动实际文件。若子树被 dry-run plan 引用，该计划会一并移除，避免保留失效计划。

## `ls` 与 `du`

- `ls PATH` 用于浏览 PATH 的一层 live 条目、索引 coverage 和已知空间提示。它有意不显示 GNU `ls -l` 中常见但不代表内容总量的目录对象 4 KiB 大小。
- `du PATH ...` 用于查询每个显式文件或目录 scope 的递归索引汇总；默认每个参数一行，即使源目录暂时离线仍可使用。
- `largest PATH` 用于排序 PATH 子树中的最大单个普通文件。

目录的 `part` / `gone` 状态可以附带最后已知聚合，但该值可能不完整或过期；未知必须显示为 `-` / `null`，不能伪装为 0。各查询命令的详细行为见 [查询命令](docs/design/queries.md)。

`SCANNED-THROUGH` 是当前已知目录子树中各目录最近成功完整枚举时间的最小值，并非不可变快照时间。表格按本地时区采用 GNU `ls -l` 风格：近六个月显示月、日和时分，更早或未来的时间显示月、日和年份。JSON/CSV 使用原始 UTC Unix 纳秒字段 `subtree_complete_through_ns`，文件和特殊条目为 `null` / 空值。

## 外部删除与索引

flatlas 不删除用户文件。文件或目录在外部被删除后：

```text
ls 父目录              → 可以显示 gone，但不修改数据库
scan 仍存在的父目录    → completed 后才将缺失路径标记 deleted
```

已经不存在的目标不能直接扫描，必须扫描覆盖它的现存父目录。partial、failed、cancelled 或枚举错误不会确认删除。deleted 记录保留在 SQLite，但不参与 `paths`、`du`、`largest` 或 `dupes` 的当前状态结果；同一路径重新出现并被扫描时恢复为 present，旧 hash 在 metadata basis 不匹配时变为 stale。

## 实际清理

`dupes` 与 `export dupes` 支持相同的筛选参数，大小与节省量使用字节整数：

```powershell
flatlas dupes D:\Archive --min-size 1048576 --max-size 1073741824 --min-count 3 --min-savings 2097152 -n 20
flatlas export dupes D:\Archive --min-size 1048576 --limit 20 --format json --output duplicates.json
```

- `--min-size` / `--max-size`：文件 logical size 的闭区间，默认不额外限制大小；在同大小候选选择和 hash 前生效，区间外文件不读取内容，也不显示已有缓存结果。0 B 和已知占用为 0 的文件仍排除。
- `--min-count`：完整内容重复组至少包含的副本数，默认 2；同一 inode 的多个 hardlink 算一份，只由 alias 组成的组不显示。组内有 inode 还链接到 PATH 之外时标出 `has links outside this group`，见 [惰性 Hash 与重复候选](docs/design/hashing.md#重复组)。
- `--min-savings`：每组最小 theoretical logical savings，包含边界，默认 0；按 `logical_size × (inodes - 1)` 计算，不是实际物理可回收空间。
- `--limit` / `-n`：筛选并排序后最多显示的组数，默认不限；按理论节省量降序、文件大小降序、算法与 digest 升序稳定排序，不截断组内成员。

组成员数、节省量与组数限制在 full hash 分组后应用，不减少候选 hash 读取。表格汇总仅统计筛选后显示的组；JSON/CSV 仍使用现有字段，`hash` 统计描述大小筛选后的全部候选，不因组数限制截断。`hash.complete` 仅描述该候选范围的 hash 处理状态，不保证 scan coverage 完整。过滤不修改路径状态或 scan coverage。空结果保持原有输出约定：JSON 的 `groups` 为 `[]`；CSV 零组时为空文件，没有独立摘要。

`flatlas dupes` 输出的是基于完整 BLAKE3 的重复内容候选和理论 logical savings，不是删除授权。需要实际去重时，应选择值得处理的较小目录，再让 jdupes 重新扫描、独立验证并由用户显式执行：

```powershell
flatlas dupes D:\Archive\Photos\2025
jdupes -r D:\Archive\Photos\2025
```

不要把 `flatlas export dupes` 的结果直接管道到删除命令。dry-run plan 也只是候选建议快照；flatlas 没有 `apply`。

## 开发文档

当前设计、开发计划、历史记录和调研资料均在[开发文档导航](docs/README.md)中分类；源码开发的环境准备和检查命令见[开发环境与依赖策略](docs/development.md)。
