"""DeepSeek LLM 客户端（OpenAI 兼容 Chat Completions，JSON 模式）。

用途：把文献英文信息翻译成中文 + 生成面向用户研究方向的一句话点评与分级。
零第三方依赖（urllib）。缓存由调用方负责（main.py 存 data/llm_cache.json）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

class LLMError(Exception):
    """LLM 调用失败（认证、限流退避后仍失败、网络错误、输出不可解析等）。"""


class DeepSeekClient:
    def __init__(self, api_key: str, model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com", interval: float = 0.2,
                 usage_log=None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.interval = interval
        self._last_call = 0.0
        self.usage_log = usage_log          # 可选：每次调用追加 usage 到该文件（JSON Lines）

    def complete_json(self, messages: list[dict], temperature: float = 0.2,
                      timeout: int = 120) -> dict:
        """调 chat/completions 并解析 JSON 内容；重试 3 次，失败抛 LLMError。"""
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        for attempt in range(3):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                self._log_usage(data)
                return json.loads(content)
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                if exc.code == 401:
                    raise LLMError(
                        "DeepSeek API key 无效（HTTP 401），请检查 .env 的 DEEPSEEK_API_KEY"
                    ) from exc
                raise LLMError(f"DeepSeek HTTP {exc.code}: {exc.reason}") from exc
            except urllib.error.URLError as exc:
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise LLMError(f"DeepSeek 网络错误: {exc.reason}") from exc
            except (KeyError, json.JSONDecodeError) as exc:
                if attempt < 2:
                    time.sleep(2)
                    continue
                raise LLMError(f"DeepSeek 返回无法解析: {exc}") from exc
        raise LLMError("DeepSeek 请求失败（重试后仍失败）")

    def _log_usage(self, data: dict) -> None:
        """记录本次调用的 token 用量（供成本审计；需 API 返回 usage）。"""
        if not self.usage_log:
            return
        try:
            u = data.get("usage") or {}
            d = u.get("prompt_tokens_details") or {}
            rec = {
                "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                "model": self.model,
                "prompt_tokens": u.get("prompt_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "cache_hit": u.get("prompt_cache_hit_tokens", d.get("cached_tokens", 0)),
                "cache_miss": u.get("prompt_cache_miss_tokens", 0),
            }
            with open(self.usage_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass                     # 用量记录失败不影响调用

    def _throttle(self) -> None:
        wait = self.interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()


