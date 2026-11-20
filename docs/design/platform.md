# 平台差异

> 状态：当前设计。描述各平台的 metadata 与占用空间采集差异及实测成本；随代码更新。跨平台边界总表见 [架构与边界](architecture.md#跨平台边界)。

## Windows：占用空间

Windows 占用空间复用 `path.allocated_size` 和目录汇总缓存，无需 schema migration。

### 采集与显示

Windows 扫描和 `ls` 的现场普通文件 metadata 查询通过标准库 `ctypes` 调用只读 Win32 API，不读取文件内容，也不修改文件：

- 普通、非压缩、非稀疏文件：空文件为 0；非空文件以仅 metadata 权限打开，使用 `FSCTL_GET_RETRIEVAL_POINTERS` 从 VCN 0 查询外部 extent。`ERROR_HANDLE_EOF` 表示没有外部数据簇，NTFS MFT 驻留文件因此记为 0，不按长度阈值猜测驻留状态。存在 extent（成功或 `ERROR_MORE_DATA`）时，`GetVolumePathNameW` 定位文件所在卷，`GetDiskFreeSpaceW` 获取每簇扇区数与每扇区字节数，按簇大小向上取整 logical size。不能假定所有卷都使用 4096 字节簇，也不能仅按盘符推断卷挂载点。
- 压缩或稀疏文件：`GetCompressedFileSizeW` 返回占用字节数，合并高低 DWORD 为 64 位整数，不再按 logical size 取整。低 DWORD 为 `0xffffffff` 时，只有 last error 非零才判定失败；调用前清除 last error。
- symlink、junction、其他 reparse point 和特殊文件不查询占用、不跟随目标，大小保持未知。WOF 或云占位等 reparse 文件也在此边界内。
- 非 Windows 平台的普通文件继续使用 `st_blocks * 512`，无 block 信息时保持未知。

这个口径对应 Explorer 属性中的基本“占用空间”估算，而不是文件的 logical size。例如普通文件长 5000 字节、卷簇大小 4096 字节时，占用列为 8192 字节。

目录占用继续聚合当前 `present` 普通文件的索引值，不加入目录对象或文件系统 metadata 开销。MFT 驻留数据为 0 表示没有为该数据另外分配外部簇，不表示 MFT 本身不占空间。hardlink 按路径累计，不能当作唯一物理占用或真实可回收空间。alternate data streams、共享存储及不同版本 Explorer 的细节未作完整校准，不能承诺与属性窗口逐字节一致。过滤、权限错误、跳过 reparse point、扫描时间和范围也可能造成差异。

extent 查询以 `FILE_FLAG_OPEN_REPARSE_POINT` 打开文件，再通过句柄属性拒绝已替换成目录、reparse、压缩或稀疏文件的条目，始终关闭句柄。不读取卷原始数据，也不解析 MFT。系统不支持该查询（例如部分 SMB 场景）时保留未知并记录 partial，不能猜测为 0 或悄悄回退成一簇。

API 查询失败时，普通文件仍保存已取得的 logical size、身份和时间，allocated size 写入 `NULL`；扫描按现有 `stat` metadata 错误记录审计并终结为 `partial`，该目录不推进成功枚举时间，整个 scope 不确认任何旧路径删除。任何文件占用未知时，祖先 allocated 汇总也是未知。`ls` 的现场查询失败则显示未知占用，不修改数据库。

表格在至少一行占用已知时显示 `ALLOC(B)`，未知行显示 `-`；全部未知时隐藏此列。`-h` 显示缩放后的 `ALLOC`；JSON/CSV 的 `allocated_size` 继续使用原始字节值或未知值。

### 更新已有索引

已有 Windows 索引中的 `NULL` 不在启动或 migration 时访问源文件自动回填。执行普通重扫：

```powershell
flatlas scan "C:\Users\example\Downloads"
flatlas ls "C:\Users\example\Downloads" -h
flatlas du "C:\Users\example\Downloads" -h
```

不要使用可能跳过已有新鲜目录的 `--refresh-before` 来做首次占用回填。`ls` 中直接普通文件使用现场 metadata；目录占用和 `du` 使用扫描后的缓存，因此需要重扫对应子树。

### 验证范围

`tests/test_allocation.py` 覆盖簇取整、空文件、非空驻留文件为 0、同样小但有 extent 的文件仍按簇计数、超过 4 GiB 的数值、压缩/稀疏分支、64 位 DWORD 合并与错误哨兵、实际卷路径、API 失败、句柄释放、打开后的类型替换、未知值向祖先传播、局部扫描不产生假删除、成功重扫恢复，以及特殊条目不查询占用。

Windows 集成测试使用临时普通文件、非空 MFT 驻留文件、原生 `FSCTL_SET_SPARSE` 和 `FSCTL_SET_COMPRESSION` 语料，核验扫描缓存及 `ls` / `du` 输出，确保 0 在多层目录聚合和 JSON 中仍为 0。文件系统没有将小文件驻留、不支持相应控制或没有 symlink 权限时，专项测试明确跳过。Linux CI 应运行平台无关的 API 模拟、POSIX block、错误注入和目录汇总测试，原生 Windows 用例在 Linux 跳过；Windows 本地通过不代表 Linux 验收完成。

参考：[Explorer 的计算说明](https://devblogs.microsoft.com/oldnewthing/20160427-00/?p=93365)、[FSCTL_GET_RETRIEVAL_POINTERS](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_get_retrieval_pointers)、[GetCompressedFileSizeW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getcompressedfilesizew)、[GetVolumePathNameW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getvolumepathnamew)、[GetDiskFreeSpaceW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getdiskfreespacew)。

## Linux / Lustre

非 Windows 平台的普通文件占用空间为 `st_blocks * 512`，无 block 信息时保持未知。扫描对每个条目只调用一次 `lstat`（见 [扫描与目录汇总](scanning.md#元数据成本)）；flatlas 当前使用普通 `lstat`，lazy 元数据仍是提案，见 [lazy 元数据](../plan/proposals/lazy-metadata.md)。

### Lustre 元数据实测（2026-11-19）

环境：一套生产 Lustre 文件系统，客户端 `lfs` 2.14.0_ddn259。以下为每条目的平均成本：

| 方式 | 每条目耗时 |
|---|---|
| 冷 `lstat` | 11–12 ms |
| 默认 `statx` | 约 30 ms |
| C `find -printf %s` | 11.8 ms |
| 16 线程 `lstat` | 约 2.6 ms（仅约 4 倍加速） |
| `lfs find --lazy` | 0.29 ms |
| `statx(AT_SYMLINK_NOFOLLOW \| AT_STATX_DONT_SYNC)` | 0.20 ms |

- 对同一条目重复 stat 会命中客户端缓存，因此系统调用次数不是成本所在；成本来自首次 stat 对 OST 的 size glimpse。
- 在 489 个静止文件上，`AT_STATX_DONT_SYNC` 得到的 size、ino、dev、mtime_ns 与 blocks 与 `lstat` 完全一致。
- 一次 1277 个条目的 flatlas 扫描耗时 16.2 s，其中 15.8 s 花在 stat 上。
- `lfs find --printf` 不输出 inode 与 device，无法直接作为 flatlas 的 metadata 来源。
