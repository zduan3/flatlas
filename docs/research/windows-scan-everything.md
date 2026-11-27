# Windows 扫描调研：Everything 与 flatlas

调研与实测日期为 2026-11-27。对比依据是 Everything 的公开说明与 flatlas 的 Windows 实测，计时均来自 flatlas。

## Everything 的索引方式

- 本地 NTFS 首次建库读取主文件表（MFT），批量取得卷上的文件与目录元数据，建立名称和路径索引。名称和路径始终索引，大小、时间等属性可纳入索引。搜索查询已建立的索引。[Everything 开发者说明](https://www.voidtools.com/forum/viewtopic.php?t=12371) · [索引选项](https://www.voidtools.com/en-us/support/everything/indexes/)
- 建库后使用 Windows 维护的 USN Journal 更新索引。日志会截断，Journal ID 也可能改变。增量读取要检查游标是否还在有效范围内，断档时需要重建基线。[Everything 索引说明](https://www.voidtools.com/en-us/support/everything/indexes/) · [Microsoft USN Journal 数据结构](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ns-winioctl-usn_journal_data_v2)
- Folder Indexing 按路径枚举目录，可用于网络共享和以普通用户身份访问的目录。原生 NTFS 索引的卷访问可由 Everything Service 或提权进程承担。[Folder Indexing](https://www.voidtools.com/en-us/support/everything/folder_indexing/) · [Everything Service](https://www.voidtools.com/support/everything/everything_service/)

flatlas 用 SQLite 保存子树扫描覆盖、错误、文件身份、占用空间和惰性 hash，按显式 scope 更新。两者都复用持久索引。Everything 的卷级建库省去逐目录走访，flatlas 的 Windows 扫描还需要采集占用空间，并依据完成的 scope 确认缺失路径。[flatlas 架构](../design/architecture.md) · [Windows 占用空间设计](../design/platform.md#windows占用空间)

## flatlas 在 Windows 上的实测

`%USERPROFILE%` 的 scan 30 是一次混合重扫，共观测 658,893 个普通文件，其中 157,400 个首次入库。总耗时 851.772 秒，聚合的 `observe` 耗时 726.153 秒，占 85.3%。随后在 `scoop/apps` 对照重扫中拆分计时，首次定位到占用空间查询占 99.747 秒，`lstat` 占 6.184 秒。进一步拆分后，定位卷与查询簇大小占 `allocation` 的 95.0%，成为缓存的目标。

以下对照均在同一目录及相同起始索引的副本上完整重扫。对照前后逐路径占用值与目录汇总一致。

| 目录 | 文件数 | 目录数 | 总耗时，关闭 → 启用缓存 | 簇大小阶段，关闭 → 启用缓存 | 目录占用汇总 |
|---|---:|---:|---:|---:|---:|
| `%USERPROFILE%/scoop/apps` | 35,803 | 2,147 | 107.593 → 23.177 s | 91.562 → 7.504 s | 4,512,096,256 B |
| Rust 标准库 HTML 文档的 `core` 目录 | 37,626 | 376 | 146.703 → 13.791 s | 130.354 → 0.728 s | 269,471,744 B |

用已有 scan 30 覆盖记录的副本按原截止时间续扫 `%USERPROFILE%`，重新观测 612 个文件和 2,636 个目录，耗时 325.797 秒。其中 `refresh_query` 占 321.896 秒，`lstat` 和 `allocation` 分别占 0.737 和 0.448 秒。SQL 查询计划显示，列出已索引子目录时按卷的 `root_id` 读取路径索引，再过滤父目录。单独查询 `%USERPROFILE%` 的 45 个直接子目录时，原计划约需 0.56 秒，指定已有父目录索引后约需 0.3 毫秒，结果一致。续扫的覆盖工作量与 scan 30 有明显差异。

续扫查询改用已有的 `idx_path_root_parent_live` 后，以原索引的新副本重复上述续扫。文件数、目录数和查询次数一致。总耗时降到 3.335 秒，3,239 次 `refresh_query` 共耗时 0.053 秒，目录汇总耗时 1.915 秒。

续扫改用局部汇总后，在原索引的新副本上再次以同一截止时间复测。现场比上次多观测一个文件和一个目录，共观测 613 个文件和 2,637 个目录。总耗时 1.407 秒，最终汇总耗时 0.026 秒，609 个目录的汇总更新时间发生变化。在该副本的同一索引状态上再执行整树重算，全部 103,123 条汇总除 `updated_at_ns` 外逐字段一致。

scan 30 和上述 `%USERPROFILE%` 续扫均为 `partial`，复现两处访问拒绝和四处 Office `Wef` 长路径错误，确认删除数均为零。四条路径长 270–286 字符，单独复测时，普通路径的 `lstat` 均报 `WinError 3`，使用 `\\?\` 前缀后均成功。

## 供后续评估的 Windows 接口

- `GetFileInformationByHandleEx(FileIdExtdDirectoryInfo)` 可按目录批量取得名称、File ID、逻辑大小、分配大小、时间和 reparse tag。[Microsoft 目录元数据结构](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_extd_dir_info)
- `FSCTL_ENUM_USN_DATA` 是用于枚举本地 NTFS 卷文件记录的公开接口。Everything 的开发者公开说明初次建库使用 MFT，具体调用接口未在该说明中给出。[Microsoft 卷枚举接口](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_enum_usn_data) · [Everything 开发者说明](https://www.voidtools.com/forum/viewtopic.php?t=12371)
- `FSCTL_READ_USN_JOURNAL` 可从指定游标读取变化记录。改名和移动涉及新旧父目录，hardlink 变化可能影响多个路径。[Microsoft USN 记录](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fscc/d2a2b53e-bf78-4ef3-90c7-21b918fab304) · [Microsoft 日志读取参数](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-read_usn_journal_data_v0)

后续方向见[阶段路线图](../plan/roadmap.md#阶段-4rust-扫描与-hash-后端)。
