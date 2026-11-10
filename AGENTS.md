# AGENTS.md

## 项目定位

File Atlas (flatlas) 是跨平台、持久化且只读的文件系统索引工具。SQLite 保存当前路径状态、扫描 coverage、错误、hash 与 dry-run plan；它不是每次命令都重新遍历目录的即时查重工具。

当前开发阶段是纯 Python 3.12 的 0.2；MVP（0.1）已完成并冻结，0.2 在其只读基线上完善索引、查询、扫描中断恢复与惰性 hash。除非用户明确要求并且相关设计已更新，不实现删除、移动、hardlink/reflink/symlink 替换、监听、TUI、Rust/PyO3 或 plan apply 等后续规划内容。

开始功能修改前，先阅读与任务相关的 `docs/0.1/architecture.md`、`docs/0.1/database-schema.md`、`docs/0.1/acceptance.md` 和 `docs/0.1/implementation.md`，再阅读 `docs/0.2/README.md` 及其链接的专项规划。0.1 文档是不可破坏的冻结基线；0.2 文档承载当前阶段的新设计与验收标准。

## 绝不可破坏的语义

- `root` 是内部的文件系统 scan namespace，不是工作目录或一次 scan 的目标目录。`flatlas init [PATH]` 只登记 namespace，不扫描文件。
- 用户默认使用 platformdirs 的全局配置与索引；`--db` 只能作为显式覆盖，不能变为日常必填参数。
- 只在 `scan_scope.status='completed'` 时，才可以将该 scope 中未在本次 scan 观察到的旧 path 标记为 `deleted`。
- 遍历权限错误、I/O 错误、hash 错误、过滤、取消或中断均不得被解释为“路径已删除”。相应 scope 必须是 partial、failed 或 cancelled。
- 默认不跟随 symlink、junction 或其他 Windows reparse point。特殊文件不计算内容 hash。
- duplicate group 只能使用仍为 `present`、普通文件且 `file_hash.state='full_ready'` 的相同 size、algorithm 与 full digest。
- hash 复用必须保守：身份、size、mtime 及可用 change/birth time 任意不匹配或不可靠时，标记 stale 并重算。
- logical size、allocated size、理论节省量和真实物理可回收空间不可混为同一指标。
- dry-run plan 可创建、查询、导出，但 MVP 不执行 operation，也不修改用户文件。

## 数据库与迁移

- 保持 `PRAGMA foreign_keys = ON`、WAL 与 busy timeout。
- 不修改已发布 migration 的语义，也不通过删表、重建或清空数据库实现 schema 升级。新增 schema 变更时提高 migration 版本，并编写向前迁移和保留既有数据测试。
- path 的唯一键是 `(root_id, path_key)`；路径保存为 BLOB 的设计不可被仅 UTF-8 TEXT 的方案替换。显示路径只用于输出，不用于路径身份比较。
- 不要将 `(device, inode)` 当作跨历史永久 ID。Windows 无符号对象身份写入 SQLite 时必须保持稳定的 signed-64 映射。

## 代码与 CLI 约定

- 当前 MVP 实现在 `src/flatlas/core.py`；新增功能可以在职责清晰时拆分模块，但应保留公开 CLI 行为和数据库语义。
- 用户可预期错误抛出 `FlatlasError`，由 `flatlas.cli.main()` 统一渲染为 `error: ...` 并返回退出码 2；不得给普通输入错误输出 traceback。
- 新 CLI 命令默认只读；若未来增加风险操作，必须先有独立设计、明确 approval/dry-run、执行前重验与审计。
- JSON/CSV 输出要稳定、可解析；二进制 digest 输出为 hex，不能依赖 Python `bytes` 的 repr。
- 任何 Windows 路径链接在回复中使用正斜杠。

## 依赖、测试与交付

- 使用 uv：通过 `uv add` / `uv add --dev` 更新依赖，维护 `pyproject.toml` 与 `uv.lock` 一致；不要对项目 `.venv` 使用裸 `pip install`。
- 每次影响扫描、删除判定、hash 或 schema 的改动，都要添加/更新回归测试。优先覆盖局部 scan 与既有索引交互、错误注入和不产生假删除。
- 按改动范围选择测试，不再要求每次小改动都执行全量 pytest。范围表见 `docs/development.md` 的“按改动选择测试”；开发迭代可先运行单个 node 或测试文件，交付前运行受影响的全部 marker 范围。跨边界改动用 `or` 合并范围，不能只运行新增测试。
- Python 代码或测试改动交付前运行静态检查、类型检查和所选回归范围，例如 hash/dupes 改动：

  ```powershell
  uv run ruff check .
  uv run pytest -q -m "hash or safety"
  uv run pyright
  git diff --check
  ```

- 纯文档改动仅运行 `git diff --check`。新增测试必须具有相应功能 marker（可继承文件级 `pytestmark`），跨职责的回归可标多个；保持 marker 注册与严格检查，不根据函数名猜测测试归属。
- 以下情况运行 `uv run pytest -q` 全量：schema/migration、路径身份与 namespace、全局配置/数据库定位、公共 core facade 或跨模块重构、依赖/构建、pytest 配置/共享 fixture/测试分组调整、发布/里程碑验收，以及无法可靠确定影响范围时。
- 部分测试范围通过后，若没有新改动、失败或未解决疑点，不再追加全量或重复已通过检查。平台相关及跨平台 I/O 改动在 Windows/WSL 上运行相同受影响范围；普通平台无关改动无需每次两端全量。交付说明实际运行的范围、平台和跳过原因，不将局部通过描述为全量验收。
- 若平台特性无法在当前 Windows 环境验证，应说明限制并增加可在 Linux/Windows CI 上运行的专项测试计划；不要将 Windows 测试视为 Linux 安全行为的替代。

## 提交信息规范

使用 Conventional Commits 格式：

```text
<type>(<scope>): <简短祈使句摘要>

[可选正文：说明动机、关键行为和限制]

[可选页脚：BREAKING CHANGE、Refs 等]
```

- `type` 只能使用：`feat`（新用户能力）、`fix`（缺陷修复）、`docs`（仅文档）、`test`（仅测试）、`refactor`（不改变外部行为的重构）、`build`（依赖/构建）、`chore`（维护）。
- `scope` 使用受影响边界的小写名称，例如 `cli`、`config`、`db`、`scan`、`hash`、`query`、`plan`、`docs`；与 `type` 重复、跨多个边界或涉及整库时（例如重构）可省略。
- 摘要不超过 72 个字符，使用祈使句，说明结果而非实现步骤；可使用中文摘要，type 与 scope 保持英文。
- 一次提交只包含一个可独立回滚的意图。功能实现、无关格式化和后续重构应拆分；实现所必需的测试、文档和锁文件可与功能同一提交。
- 影响 schema 时，正文必须说明 migration 版本、向前迁移和数据保留策略；影响扫描 coverage、hash 或计划语义时，正文必须说明安全不变量及对应测试。
- 不在提交信息中声称跨平台验收已完成，除非相应 Windows/Linux 测试语料确已运行并记录结果。
