"""本地测试（无网络）：bibcode 校验/提取、邮件分类、简报生成。

运行：python3 tests/test_local.py
"""
from __future__ import annotations

import email
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mail_digest.ads import (
    extract_bibcodes,
    is_valid_bibcode,
    parse_myads_sections,
    subscription_label,
)
from mail_digest.classify import is_ads_email
from mail_digest.digest import build_ads_digest
from mail_digest.imap_client import _parse_message
from mail_digest.models import ADSArticle, Mail

SAMPLE = Path(__file__).parent / "sample_ads.eml"
EXPECTED = ["2024ApJ...963..100A", "2023MNRAS.520.1001A", "2021PhRvD.104h4042A"]


def load_sample() -> Mail:
    msg = email.message_from_bytes(SAMPLE.read_bytes())
    return _parse_message(uid=123, folder="INBOX", msg=msg)


def test_is_valid_bibcode() -> None:
    assert is_valid_bibcode("2024ApJ...963..100A")
    assert not is_valid_bibcode("2024ApJ...963")            # 太短
    assert not is_valid_bibcode("x024ApJ...963..100A")      # 前 4 位非数字
    assert not is_valid_bibcode("2024ApJ...963..100.")      # 末位非字母
    assert not is_valid_bibcode("2024ApJ...963..100A ")     # 长度 20


def test_is_ads_email() -> None:
    assert is_ads_email(load_sample())

    normal = Mail(
        uid=1, folder="INBOX", message_id="", subject="周会纪要",
        from_="admin@example.com", date=None,
        body_text="请查收会议纪要", body_html="", raw_path=Path(""),
    )
    assert not is_ads_email(normal)


def test_extract_bibcodes() -> None:
    mail = load_sample()
    assert extract_bibcodes(mail) == EXPECTED


def test_bare_bibcode_in_text() -> None:
    """正文里直接写裸 bibcode（无链接）也能提取。"""
    mail = Mail(
        uid=2, folder="INBOX", message_id="", subject="x", from_="y",
        date=None, body_text="推荐阅读 2024ApJ...963..100A 这篇论文",
        body_html="", raw_path=Path(""),
    )
    assert extract_bibcodes(mail) == ["2024ApJ...963..100A"]


def test_build_digest() -> None:
    mail = load_sample()
    articles = [
        ADSArticle(bibcode=bc, title=f"Title {i}", abstract="Abstract text.",
                   authors=["A. Author"], citation_count=3, source="api")
        for i, bc in enumerate(EXPECTED)
    ]
    # 新的分组签名：[(订阅名, [文献])]
    text = build_ads_digest(mail, [("grb_cosmicray", articles)])
    assert "ADS 文献简报" in text
    assert "grb_cosmicray" in text
    assert "伽马射线暴与宇宙线" in text
    assert "Title 0" in text
    assert "https://ui.adsabs.harvard.edu/abs/2024ApJ...963..100A/abstract" in text


def test_parse_myads_sections() -> None:
    body = (
        "myADS Personal Notification Service Results\n\n"
        "grb_cosmicray (https://ui.adsabs.harvard.edu:443/search?q=full%3A%22GRB%22)\n"
        '"GRB test," Author, A (2024ApJ...963..100A)\n'
        '"Another," Author, B (2023MNRAS.520.1001A)\n\n'
        "solaractivity_cosmicray (https://ui.adsabs.harvard.edu:443/search?q=solar)\n"
        '"Solar flare," Author, C (2021PhRvD.104h4042A)\n'
    )
    sections = parse_myads_sections(body)
    assert sections == [
        ("grb_cosmicray", ["2024ApJ...963..100A", "2023MNRAS.520.1001A"]),
        ("solaractivity_cosmicray", ["2021PhRvD.104h4042A"]),
    ]
    # 非 myADS 格式返回空
    assert parse_myads_sections("普通邮件正文 2024ApJ...963..100A") == []
    assert subscription_label("grb_cosmicray") == "伽马射线暴与宇宙线"
    assert subscription_label("unknown_sub") == "unknown_sub"


if __name__ == "__main__":
    test_is_valid_bibcode()
    test_is_ads_email()
    test_extract_bibcodes()
    test_bare_bibcode_in_text()
    test_build_digest()
    test_parse_myads_sections()
    print("✅ 全部本地测试通过")
