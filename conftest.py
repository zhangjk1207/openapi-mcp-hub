"""pytest 根配置：把 src/ 注入 sys.path（无需安装即可跑测试）。"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# 测试进程不继承宿主机代理配置（respx mock / 本地 uvicorn 都不应走代理）
for _key in (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
):
    os.environ.pop(_key, None)
