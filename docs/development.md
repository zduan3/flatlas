# 开发环境与依赖策略

> 状态：开发记录。说明本地环境、依赖与检查命令。

## 当前环境

- Python：固定 Python 3.12，由 `uv` 依据 `.python-version` 创建项目 `.venv`。
- 依赖：`pyproject.toml` 是唯一声明来源，`uv.lock` 是可复现锁文件。
- 当前运行时依赖：`blake3`（quick/full hash）、`platformdirs`（用户级配置位置）、`typer`（CLI）。
- 当前开发依赖：`pytest`、`ruff`、`pyright`。

使用：

```powershell
uv sync
uv run pytest -q
uv run ruff check .
uv run pyright
```

使用 `uv add <package>` 或 `uv add --dev <package>` 变更依赖；不要向项目 `.venv` 使用裸 `pip install`，也不要让 Pixi 与 uv 同时管理该项目依赖。Pixi Global 可以继续作为日常 Python，但本项目不依赖全局环境的偶然状态。

## 后续 Rust 配置

仅在进入 Rust 阶段时安装：

1. Visual Studio Build Tools 的 C++ 桌面开发组件及 Windows SDK。
2. `rustup`，选择 `x86_64-pc-windows-msvc` stable toolchain。
3. `rustfmt` 与 `clippy`。

```powershell
rustup default stable-x86_64-pc-windows-msvc
rustup component add rustfmt clippy
```

届时在项目根目录加入 `rust-toolchain.toml` 与 `native/` Cargo workspace。`rustup` 管 toolchain，Cargo 管 Rust crates，uv 仍只管 Python。若采用 PyO3，`maturin` 用于本地开发和构建；若采用 Rust sidecar，则不需要 maturin。
