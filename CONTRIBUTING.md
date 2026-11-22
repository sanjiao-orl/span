# Contributing to Span

感谢你对寻章 Span（`chapspan`）的关注。

## 本地开发环境

需要 Python 3.10 及以上版本，运行时仅依赖 `PyYAML`：

```bash
git clone https://github.com/sanjiao-orl/span.git
cd span
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows:     .\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

不安装可编辑包直接从源码运行测试或 CLI 时，需将 `src` 加入 `PYTHONPATH`：

```bash
# macOS / Linux
PYTHONPATH=src python -B -m unittest discover -s tests

# Windows PowerShell
$env:PYTHONPATH = 'src'
python -B -m unittest discover -s tests
```

## 文档卫生与发行包校验

仓库对面向源码开发的文档与随发行包分发的用户文档保持严格隔离：

- [README.md](README.md)、[packaging/AGENTS.md](packaging/AGENTS.md) 与 [src/chapspan/help_topics.py](src/chapspan/help_topics.py) 会随 wheel / 发行 ZIP 分发，不得引用仅存在于源码开发环境的路径或文档；
- [packaging/AGENTS.md](packaging/AGENTS.md) 必须与 `HELP_TOPICS["agent"]` 保持逐字一致。

修改代码或文档后，请运行完整单测与打包校验：

```bash
PYTHONPATH=src python -B -m unittest tests.test_docs_hygiene
python -B scripts/build_release.py --no-build-isolation
```

（Windows PowerShell 中改用 `$env:PYTHONPATH = 'src'`。）
