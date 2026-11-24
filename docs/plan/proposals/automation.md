# 自动化存储评估：机器输出约定与查询入口

> 状态：提案，未实现。0.2.0 的主体；版本划分见 [路线图](../roadmap.md)，当前行为见 [查询命令](../../design/queries.md)。不保留 0.1 的 JSON 形状与 `core.py`，不考虑向后兼容。

## 动机

0.1 的命令面向人工逐条查询：表格贴近 GNU，JSON 是表格的副产品。在 Lustre 上对百万级条目做存储评估与清理时，流程是脚本化的：

1. 扫描（后台、可续扫）；
2. 生成概览：每个顶层目录和项目的字节与条目数、按目录名初筛的可重建候选及 hardlink 去重后可腾出的空间；
3. 逐项目下钻，产出删除清单（引用核对在 flatlas 之外）；
4. 在文件系统上删除后同步索引，重新生成概览，记录前后数值；
5. 重复内容导出给 fclones 复核。

0.1 的接口在这些步骤上出现的问题：

- **同名字段含义不同**：`path` 在 `du` 中相对当前目录，在 `largest` 中相对 PATH，`ls` 只有 `name`，`paths` 叫 `path_display` 且目录大小为空；状态在 JSON 中是 `scanned`，在表格中是 `ok`。脚本必须固定工作目录才能解析结果。
- **静默截断**：`paths` 默认 500 行、`largest` 10 行、`scans` 50 行，机器输出不说明截断。
- **一项统计要多条命令**：找候选与算大小要 `find` 加 `du --files0-from` 两次读取；概览表对每个目录各起两个进程、每次重开 4.6 GB 索引（8.6 min）；另一种做法是直接 import 内部函数，而它们没有任何约定。
- **整数组 JSON**：百万行结果必须整体缓冲。
- **环境不可控**：索引位置只能写在 `~/.config`，agent 沙箱内只读；用户配置的 `scan.refresh-before` 会悄悄改变脚本中 scan 的行为。

## 0.2.0：新约定

### 输出格式

- 返回多行的命令（`du`、`ls`、`find`、`largest`、`dupes`、`scans`、`roots`、`df`、`plan show`）支持 `--format table|jsonl|csv`；返回单个对象的命令（`scan`、`scan-info`、`rm`、`init`）支持 `--format table|json`。删除数组形式的 `json`。
- 默认仍是 `table`，不按是否为终端自动切换；脚本显式写 `--format jsonl`。
- `jsonl` 逐行写出，不缓冲整个结果；`csv` 列与 `jsonl` 字段一一对应，未知值为空。

### 条目记录

`du`、`ls`、`find`、`largest` 输出同一种记录，字段固定、按需追加的字段只在给出对应选项时出现：

| 字段 | 含义 |
|---|---|
| `type` | `entry`；汇总行为 `total` |
| `path` | 绝对显示路径。表格中才按 GNU 规则显示为相对参数的形式 |
| `kind` | `file`、`dir`（含 namespace root）、`symlink`、`reparse`、`other` |
| `status` | `ok`、`new`、`part`、`gone`，表格与机器格式使用同一组词 |
| `logical_size`、`allocated_size` | 字节；目录为子树合计；未知为 `null` |
| `files`、`entries` | 子树普通文件数与条目数（含自身，同 `du --inodes`）；文件为 1、1 |
| `mtime_ns` | 条目自身的修改时间 |
| `complete_through_ns` | 子树最近一次完整覆盖的时间；未完整覆盖为 `null` |
| `depth` | 相对所在参数的深度 |
| `unique_*`、`freed_*` | 仅 `du --freed` |

`dupes` 输出组记录（`type: group`，含 digest、size、各路径的绝对 `path`）；`scans` 输出扫描记录；字段名与单位沿用上表规则（字节、`_ns` 整数、绝对路径）。

### 限制与退出码

- 机器格式没有隐式上限；`-n/--limit` 只在显式给出时生效。`largest` 的默认 10 行只作用于表格。
- 退出码：0 所有参数成功；1 部分参数失败（未索引、读不到），其余照常输出；2 用法或领域错误。错误一律写 stderr，以 `error: ` 开头并带上出错的参数。
- 结果是否完整看每行的 `status` 与 `complete_through_ns`，不改变退出码。

### `find` 作为自动化入口

- 加 `--format`：`table`/`jsonl`/`csv` 输出条目记录；无 `--format` 时保持 GNU 风格逐行路径与 `-print0`。
- 新增测试：`-size [+-]N[kMG]`（logical size；目录取子树合计）、`-entries [+-]N`（子树条目数，flatlas 独有）、`-mtime [+-]N`（天）、`-status WORD`。目录的子树值来自目录汇总缓存，不额外遍历。
- 效果：「每个子目录的字节与条目数」是一条 `find DIR -mindepth 1 -maxdepth 1 -type d --format jsonl`；「候选及其大小」是一条带 `-prune` 的 `find --format jsonl`；hardlink 去重后的并集仍用 `du --files0-from -c --freed`。

### 索引位置与配置隔离

- 环境变量 `FLATLAS_DB` 指定索引文件；优先级 `--db` > `FLATLAS_DB` > 配置。agent 沙箱与批处理脚本不必写 `~/.config`。
- `FLATLAS_NO_CONFIG=1`（或全局 `--no-config`）忽略用户配置的命令默认参数；索引位置仍可由 `--db` / `FLATLAS_DB` 给出。

### 验收

- 在一个数百万条目的真实索引上，用不超过三条 CLI 命令加本地分组重新生成按目录的字节、条目数与可重建候选汇总表，数值与旧接口得出的一致，结果与当前目录无关。
- 所有返回多行的命令在 `jsonl` 下逐行输出；字段与本提案的表一致，由测试按字段集校验。
- 根 README 增加「脚本与自动化」一节：概览、候选、删除后同步的三段配方。

## 0.2.1：后续

- **只读 Python API**（`flatlas.api`）：一次打开索引、多次查询，返回与 `jsonl` 相同字段的记录迭代器；CLI 改为它的薄层。用于需要多次组合查询的报告脚本，取代对内部模块的直接引用。
- **基于重新观察的删除后同步**：`scan --shallow --files0-from PARENTS` 在一次扫描中覆盖多个父目录，由文件系统观察确认删除并写入 tombstone，保留 `deleted_by_scope_id` 审计。`rm --files0-from` 保留，用于移除不再关心或登记错误的子树。

## 不在本提案内

- 交互参数（GNU `ls`/`du`/`df` 选项）仍按 [查询命令 GNU 兼容规划](query-cli.md)，排在本提案之后。
- 扫描前后两次结果的差异报告、`find -printf`、非 UTF-8 路径在机器输出中的无损表示：见 [待决策清单](../decisions.md)。
