"""配置加载。

敏感信息（授权码、API token）一律从项目根目录的 .env 读取，
不要硬编码、不要提交到版本库（见 .gitignore）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def sender_allowed(from_header: str, allowed: str) -> bool:
    """判断邮件发件人是否在可信白名单内（用于决定是否处理其附件）。

    allowed: 逗号分隔；支持完整地址（a@b.cn）、域名通配（*@b.cn）、裸域名（b.cn）。
    空串 → False（fail-closed：宁可漏处理，也不给未知发件人的附件执行解压）。
    """
    if not allowed or not allowed.strip():
        return False
    import re
    m = re.search(r"<([^>]+)>", from_header or "")
    addr = (m.group(1) if m else from_header or "").strip().lower()
    if not addr:
        return False
    for item in allowed.split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item.startswith("*@"):
            if addr.endswith(item[1:]):            # *@domain
                return True
        elif "@" in item:
            if addr == item:                        # 完整地址
                return True
        else:
            if addr.endswith("@" + item):           # 裸域名
                return True
    return False


def _load_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析：每行 KEY=VALUE，支持 # 注释与引号包裹。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass
class Config:
    # ---- 腾讯企业邮箱（IMAP）----
    imap_host: str = "imap.exmail.qq.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_auth_code: str = ""          # 16 位授权码，不是登录密码

    # ---- NASA ADS API ----
    ads_api_token: str = ""
    ads_api_base: str = "https://api.adsabs.harvard.edu/v1/search/query"
    ads_request_interval: float = 0.1   # 两次请求最小间隔（秒），官方限 15 次/秒
    ads_fields: tuple[str, ...] = field(default=(
        "title", "abstract", "author", "bibcode",
        "citation_count", "doi", "pubdate",
    ))

    # ---- DeepSeek LLM（M3：中文翻译 + 一句话点评 + 分级）----
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_request_interval: float = 0.2   # 两次 LLM 请求最小间隔（秒）

    # ---- SMTP（M4：每日邮件推送）----
    smtp_host: str = "smtp.exmail.qq.com"
    smtp_port: int = 465

    # ---- 基金邮件可信发件人（安全）----
    # 逗号分隔：完整地址 / *@域名 / 裸域名。为空则拒绝处理任何基金邮件附件（fail-closed）
    grant_allowed_senders: str = ""

    # ---- 路径 ----
    data_dir: Path = PROJECT_ROOT / "data"
    eml_dir: Path = PROJECT_ROOT / "data" / "emails"
    digest_dir: Path = PROJECT_ROOT / "data" / "digests"
    zh_digest_dir: Path = PROJECT_ROOT / "data" / "digests" / "zh"
    processed_file: Path = PROJECT_ROOT / "data" / "processed.json"
    llm_cache_file: Path = PROJECT_ROOT / "data" / "llm_cache.json"

    # ---- 行为 ----
    default_recent: int = 50            # fetch 默认拉最近 N 封
    default_folder: str = "INBOX"
    default_ads_limit: int = 20         # ads 一次最多处理的邮件数

    @classmethod
    def load(cls, env_path: Path | None = None) -> "Config":
        env = _load_dotenv(env_path or PROJECT_ROOT / ".env")
        cfg = cls()
        cfg.imap_host = env.get("IMAP_HOST", cfg.imap_host)
        try:
            cfg.imap_port = int(env.get("IMAP_PORT", cfg.imap_port))
        except ValueError:
            pass
        cfg.imap_user = env.get("IMAP_USER", cfg.imap_user)
        cfg.imap_auth_code = env.get("IMAP_AUTH_CODE", cfg.imap_auth_code)
        cfg.ads_api_token = env.get("ADS_API_TOKEN", cfg.ads_api_token)
        cfg.deepseek_api_key = env.get("DEEPSEEK_API_KEY", cfg.deepseek_api_key)
        cfg.deepseek_model = env.get("DEEPSEEK_MODEL", cfg.deepseek_model)
        cfg.smtp_host = env.get("SMTP_HOST", cfg.smtp_host)
        try:
            cfg.smtp_port = int(env.get("SMTP_PORT", cfg.smtp_port))
        except ValueError:
            pass
        cfg.grant_allowed_senders = env.get("GRANT_ALLOWED_SENDERS", cfg.grant_allowed_senders)
        return cfg
