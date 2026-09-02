"""用户研究画像（LLM 点评/相关度分级的个性化上下文）。

隐私设计：本项目开源，真实个人信息一律不放进代码。
个性化画像放在 data/research_profile.json —— 该文件已被 .gitignore 忽略，
不会随代码提交。仓库自带 data/research_profile.example.json 作为填写模板。

用法：
  1) cp data/research_profile.example.json data/research_profile.json
  2) 编辑 data/research_profile.json 填入自己的研究方向
  3) 不配置时使用内置的通用占位画像（不带任何个人信息），功能照常运行。
"""
from __future__ import annotations

import json
from pathlib import Path

_PROFILE_FILE = Path(__file__).resolve().parent.parent / "data" / "research_profile.json"

# 通用占位画像（不含任何真实个人信息；只用于演示与保证功能可用）
DEFAULT_PROFILE: dict = {
    "summary": (
        "示例画像：高能天体物理方向研究者。请把 data/research_profile.example.json "
        "复制为 data/research_profile.json 并填入自己的研究方向，"
        "LLM 的翻译点评与相关性分级将据此个性化。"
    ),
    "focus_topics": [
        "伽马射线暴", "宇宙线", "脉冲星", "中子星", "黑洞吸积",
    ],
    "method_topics": [
        "时间序列分析", "机器学习", "谱拟合",
    ],
    "secondary_topics": [
        "暗物质", "中微子",
    ],
    "known_literature": [
        "（示例）此处列出你已读并跟踪的关键文献，点评时可避免重复推荐",
    ],
}


def _load_profile() -> dict:
    try:
        if _PROFILE_FILE.exists():
            data = json.loads(_PROFILE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("summary"):
                return data
    except Exception:
        pass
    return DEFAULT_PROFILE


PROFILE = _load_profile()
PROFILE_SUMMARY: str = str(PROFILE.get("summary", ""))
FOCUS_TOPICS: list = list(PROFILE.get("focus_topics", []))
METHOD_TOPICS: list = list(PROFILE.get("method_topics", []))
SECONDARY_TOPICS: list = list(PROFILE.get("secondary_topics", []))
KNOWN_LITERATURE: list = list(PROFILE.get("known_literature", []))
