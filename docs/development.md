# 开发环境与依赖策略

## 当前环境

- Python：固定 Python 3.12，由 `uv` 依据 `.python-version` 创建项目 `.venv`。
- 依赖：`pyproject.toml` 是唯一声明来源，`uv.lock` 是可复现锁文件。
- 当前没有运行时或开发依赖；功能设计落实时再添加。

建议的首批依赖：

```powershell
uv add blake3 typer
uv add --dev pytest hypothesis ruff pyright
uv add --optional ui rich
```

- `blake3`：quick hash 与 full hash。
- `typer`：CLI 子命令。
- `pytest`、`hypothesis`：测试与边界状态生成。
- `ruff`、`pyright`：质量检查。
- `rich`：仅在提供纯文本回退时作为可选 `ui` extra；否则应是普通运行时依赖。

使用：

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run pyright
uv sync --extra ui
```

不要向项目 `.venv` 使用裸 `pip install`，也不要让 Pixi 与 uv 同时管理该项目依赖。Pixi Global 可以继续作为日常 Python，但本项目不依赖全局环境的偶然状态。

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
