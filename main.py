#!/usr/bin/env python3
"""Mail Digest 入口（薄壳）→ 逻辑在 mail_digest/cli.py。

两个独立 Agent + 公共底座：
  python main.py ads run | ads push
  python main.py grants run | grants push
  python main.py fetch | html | all
"""
from mail_digest.cli import main

if __name__ == "__main__":
    main()
