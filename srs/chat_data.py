"""聊天室持久化数据目录。"""

import os
from pathlib import Path


DATA_DIR = Path(os.getenv("CHAT_DATA_DIR") or Path(__file__).resolve().parent)
DATA_DIR.mkdir(parents=True, exist_ok=True)
