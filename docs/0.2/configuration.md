# 命令默认参数配置

> 状态：0.2 已实现。配置使用既有 platformdirs 用户级目录中的 `config.toml`。

## CLI 与文件格式

```text
flatlas config set scan.refresh-before 7d
flatlas config get scan.refresh-before
flatlas config unset scan.refresh-before
flatlas config list
flatlas config list -a
```

当前只接受 `scan.refresh-before`，未知键或非法值以 `FlatlasError` 交由 CLI 输出 `error: ...` 并返回退出码 2。`get` 输出已保存的字符串；未设置时不输出并返回 0。`unset` 可重复执行。所有配置命令均无需初始化或打开 SQLite。

`list` 按键名排序，以 `key = value` 列出已设置的受支持命令默认参数；空列表无输出并返回 0。`list -a`（`--all`）同时显示尚未设置的受支持参数，其值为 `<unset>`。配置文件中的 `database`、`format_version` 与未知字段保持原样；列表范围是配置命令支持的默认参数。

```toml
format_version = 1
database = "/home/user/.local/share/flatlas/index.sqlite"

[scan]
refresh-before = "7d"
```

键名按命令与长选项组织，为后续支持其他默认参数保留扩展方式。值支持既有 `scan --refresh-before` 的正整数固定时长与 ISO 8601 日期时间语法。设置时验证格式、未来时间与 SQLite signed-64 纳秒范围；文件手工编辑后的配置在使用时验证。相对时长保存原始字符串，在每次普通 scan 启动时解析为固定 cutoff，写入既有 `scan.refresh_before_ns` 字段。

## 优先级和扫描语义

1. 显式 `--refresh-before VALUE` 使用指定值。
2. `--full` 使用完整扫描；`--resume` / `--resume-from` 使用历史记录的 scope 与 cutoff。
3. 普通 scan 使用保存的 `scan.refresh-before`。
4. 未设置时使用既有完整扫描行为。

四个显式选项互斥；配置默认值不参与互斥判断。`--db` 只覆盖数据库位置，普通扫描仍使用用户级默认参数。显式选项可覆盖非法的刷新默认值；配置文件本身必须能正常解析，才能定位全局数据库。

配置只决定调用现有扫描器时的 cutoff。跳过的新鲜子树不参与当前 scan 的删除判定，只有 completed scope 的已枚举范围才能确认缺失；partial、failed、cancelled 仍不确认删除。无需 schema migration。

## 配置读写和验收

配置允许先于 `init` 创建，此时只有默认参数与格式版本；`init` 后补数据库路径并保留默认参数。已有配置中的数据库路径、未知字段和注释由 TOMLKit 保留。写入先生成同目录临时文件再原子替换，替换失败保留原文件并报告领域错误；读取不创建任何目录或文件。

`tests/test_config.py` 覆盖命令往返、初始化前配置、未知键、非法类型与值、写入失败保留原文件、显式数据库覆盖、CLI 参数优先、完整扫描覆盖、相对时长每次重新解析、续扫 cutoff 继承，以及配置刷新跳过新鲜目录时不产生假删除。配置与依赖改动按开发约定执行全量测试；相同语料在 Windows 和 Linux 原生文件系统验证配置 I/O。
