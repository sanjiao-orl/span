# 寻章 Span — 源码开发协作说明（仅用于仓库开发）

> **边界说明**：本文件仅用于源码仓库开发协作，**构建发行包时自动排除**；面向终端使用者与研究工作区 AI 助手的手册位于 [packaging/AGENTS.md](packaging/AGENTS.md)（随交付包分发为 `AGENTS.md`，也可运行 `span help agent` 获取）。

- Python 包名：`chapspan`；CLI 入口：`span`（仅依赖 `PyYAML` 与 Python >= 3.10 标准库）。
- 本地开发、测试与打包流程见 [CONTRIBUTING.md](CONTRIBUTING.md)：
  - 运行全量测试：`PYTHONPATH=src python -B -m unittest discover -s tests`（Windows PowerShell 先设 `$env:PYTHONPATH = 'src'`）。
  - 文档卫生与包结构校验：`PYTHONPATH=src python -B -m unittest tests.test_docs_hygiene`。
  - 构建并校验跨平台发行包：`python -B scripts/build_release.py --no-build-isolation`。
- 架构与阅读边界约束：
  - 所有章节正文须经由带范围校验的服务读取，不引入破坏防剧透上限、一维物理坐标 `C0001..Cxxxx` 或快照身份的快捷读取路径。
  - [README.md](README.md)、[packaging/AGENTS.md](packaging/AGENTS.md) 与 [src/chapspan/help_topics.py](src/chapspan/help_topics.py) 均属随包发行文档，禁止引用仅限源码仓库的文件；修改 [packaging/AGENTS.md](packaging/AGENTS.md) 时须同步保持与 `HELP_TOPICS["agent"]` 逐字一致。
  - 新增或修改行为前需补充对应回归测试，提交前检查 `git diff --check`。
