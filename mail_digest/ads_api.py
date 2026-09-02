"""NASA ADS API 客户端。

纯标准库 urllib 实现，零第三方依赖。
文档：https://ui.adsabs.harvard.edu/help/api/
限额：3000 次/天，15 次/秒（超限返回 HTTP 429）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import ADSArticle


class ADSAPIError(Exception):
    """ADS API 调用失败（认证、限流退避后仍失败、网络错误等）。"""


class ADSClient:
    def __init__(self, token: str, base_url: str, interval: float = 0.1):
        self.token = token
        self.base_url = base_url
        self.interval = interval          # 两次请求最小间隔（秒）
        self._last_call = 0.0

    def fetch_bibcode(self, bibcode: str, fields: list[str]) -> dict | None:
        """查询单个 bibcode 的结构化信息，返回第一条文档；未命中返回 None。

        429（限流）退避重试：1s → 2s → 4s，仍失败抛 ADSAPIError。
        """
        params = {
            "q": f"bibcode:{bibcode}",
            "fl": ",".join(fields),
            "rows": "1",
        }
        url = f"{self.base_url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token}"}
        )
        for attempt in range(4):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                docs = data.get("response", {}).get("docs", [])
                return docs[0] if docs else None
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(2 ** attempt)          # 1s, 2s, 4s
                    continue
                if exc.code == 401:
                    raise ADSAPIError(
                        "ADS API token 无效（HTTP 401），请检查 .env 中的 ADS_API_TOKEN"
                    ) from exc
                raise ADSAPIError(f"ADS API HTTP {exc.code}: {exc.reason}") from exc
            except urllib.error.URLError as exc:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise ADSAPIError(f"ADS API 网络错误: {exc.reason}") from exc
        return None

    def _throttle(self) -> None:
        """按最小间隔限流（默认 0.1s = 10 次/秒，低于官方 15 次/秒上限）。"""
        wait = self.interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()


def fill_from_doc(art: ADSArticle, doc: dict) -> None:
    """把 API 返回的文档字段填充进 ADSArticle（字段可能是列表，需归一）。"""
    title = doc.get("title", "")
    art.title = title[0] if isinstance(title, list) and title else str(title or "")
    art.abstract = str(doc.get("abstract", "") or "")
    authors = doc.get("author", [])
    art.authors = list(authors) if isinstance(authors, list) else []
    art.citation_count = doc.get("citation_count")
    doi = doc.get("doi", "")
    art.doi = doi[0] if isinstance(doi, list) and doi else str(doi or "")
    art.pubdate = str(doc.get("pubdate", "") or "")
    art.source = "api"
