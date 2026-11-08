# 后端测试目录

`backend/run_tests.py` 是这个目录的唯一入口（`unittest discover -s tests`）。不要直接
`python -m unittest discover`：它不读 `conftest.py`，缺了「忽略外部 `CP_DATA_DIR` +
会话临时目录整块回收」两道保护，历史上把线上库写坏过。入口与理由见 `run_tests.py` 顶部。

## 为什么文件平铺在这一层

- 命名：`test_<模块>.py` 写功能回归，`test_batchXX.py` 写按开发批次成组交付的契约。
- **刻意不按领域建子目录**：数十个模块用 `Path(__file__).resolve().parents[2]` 定位仓库根，
  再去读源码 / 配置 / 前端文件做源码级断言；移进子目录会让根路径指到 `backend/`。
  `run_tests.py` 与 CI 的超时、失败摘要步骤也都按单层 `tests/` 编写。真要分组，必须连同
  这些根路径、入口与 CI 一起改，并用全量测试验过。用这份说明代替目录树的"美观度"。
- 新增用例放本目录、文件名以 `test_` 开头即被发现；要读仓库文件照上面的 `parents[2]` 取根。

## 跑法

后端在 import 期依赖 `fcntl` / `termios`，Windows 导不进来，请在 Linux、WSL2 或部署机上跑：

```bash
cd backend && .venv/bin/python run_tests.py -v                       # 全套；make test 同义
cd backend && .venv/bin/python run_tests.py -v tests.test_repo_hygiene # 单模块
```

导入 `app.*` 之前必须先设好 `CP_DATA_DIR` / `CP_FILE_ROOTS` 默认值；细节见仓库根
`CONTRIBUTING.md` 的「跑测试要注意的三件事」。
