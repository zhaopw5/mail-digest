# 📧 邮件智能摘要 Agent（暂定名）

> 每天自动读取腾讯企业邮箱，把「正文 + 链接里的内容 + 附件里的内容」聚合起来，按个人偏好过滤、判断重要度、生成摘要，推送给用户。
> 一句话定位：**邮件驱动的个人信息聚合器**——正文只是入口，真正的内容在链接里、在附件里。

---

## 1. 项目背景

用户每天收到大量邮件但懒得读，又怕错过重要的。现成的"AI 邮箱"（网易邮箱大师 AI、Gmail/Outlook 内置摘要等）只能总结**邮件正文**，而用户实际需要的内容恰恰在正文之外：

- **场景一**：NASA ADS 文献订阅推送——邮件里只有标题和链接，摘要页的内容在网页上，邮箱内置 AI 读不到；
- **场景二**：学院发的基金申请 / 项目申请通知——正文只说"这是什么领域的项目"，实际内容（申报书、指南、通知全文）全在**附件**（含压缩包）里，邮箱内置 AI 读不到附件。

所以这个项目的核心价值 = **内容富化（链接抓取 + 附件解析）+ AI 摘要**，而不是简单的正文摘要。

## 2. 核心场景（需求定义）

### 场景一：NASA ADS 文献推送

- **现状**：每 1~几天收到一封 ADS 订阅推送，邮件列出若干条文献，每条只有标题 + 链接（形如 `https://ui.adsabs.harvard.edu/abs/<bibcode>/abstract`），点链接才能看到摘要。
- **目标**：agent 自动提取 bibcode → 调 NASA ADS 官方 API 拿到结构化摘要 → 按用户研究方向打分 → 进每日汇总。
- **期望产出**：每天/每周一封文献简报，包含标题、摘要、引用数、相关度，直接可读，不用再点链接。

### 场景二：学院基金 / 项目申请邮件

- **现状**：邮件正文只有领域简介，详细内容在附件里，附件可能是 PDF / Word / 压缩包（压缩包里可能还有压缩包）。
- **目标**：agent 自动下载附件 → 解压（递归）→ 解析各类文档 → 提取关键信息（申报条件、截止日期、资助额度、材料清单等）→ 进每日汇总。
- **期望产出**：每天一页"项目申报机会清单"，标明截止日期和核心要求，不漏项。

## 3. 为什么自建而不是用现成产品

- **海外工具**（Superhuman、Shortwave、SaneBox、Mailbutler 等）：基本只支持 Gmail / Outlook 生态，不支持腾讯企业邮箱。
- **国内厂商**（网易邮箱大师 AI、WPS 365、Coremail）：AI 是"邮箱内置功能"，只能在邮箱界面里用，且只读正文，覆盖不了上面两个场景。
- **"腾讯企业邮箱 + 自动聚合正文/链接/附件 + 每日推送"** 目前没有现成产品。
- **定位**：自用 / 学习项目，暂不商业化（商业化赛道拥挤且获客难，但自用价值成立）。

## 4. 总体架构

```
┌────────────┐   ┌──────────────────────────────┐   ┌──────────────┐   ┌────────────┐
│  IMAP 拉信  │ → │       内容富化层（核心差异化）      │ → │ LLM 过滤+摘要  │ → │  每日推送    │
│ 腾讯企业邮箱  │   │  正文提取 / 链接抓取 / 附件解析    │   │ 重要度判定     │   │ 邮件/飞书等   │
└────────────┘   └──────────────────────────────┘   └──────────────┘   └────────────┘
```

关键点：**内容富化层是本项目的差异化核心**，LLM 摘要只是最后一公里。

## 5. 技术方案细节

### 5.1 数据接入：腾讯企业邮箱

- **重要事实**：腾讯企业邮箱官方 OpenAPI 只有管理类接口（部门、成员、邮件组、标签、SSO、日志等），**不能读邮件内容**。读邮件必须走标准邮件协议。
- **IMAP 收信**：服务器 `imap.exmail.qq.com`，端口 `993`（SSL），用户名 = 完整邮箱地址。
- **SMTP 发信**（如需 agent 代发）：`smtp.exmail.qq.com`，端口 `465`（SSL）。
- **登录方式**：**16 位授权码**，不是邮箱密码。获取路径：邮箱设置 → 客户端设置 → 【获取授权密码】。授权码可随时重置作废。
- 参考：[什么是授权码（官方帮助）](https://exmail.qq.com/qy_mng_logic/help/authcodeSetting)
- **注意**：如果公司用企业微信，邮箱通常已与企业微信打通，可作为备选集成入口。

### 5.2 内容富化层

#### ADS 摘要抓取（场景一）

- 从邮件正文 / HTML 中提取 bibcode（19 位，正则形如 `\d{4}[A-Za-z&.+]{5}\.[A-Za-z0-9]{4}[A-Za-z]`，示例 `2024ApJ...963..100A`）。
- 调 NASA ADS 官方 API（比爬网页干净、无反爬问题）：
  - `GET https://api.adsabs.harvard.edu/v1/search/query?q=bibcode:<bibcode>&fl=title,abstract,author,bibcode,citation_count,doi`
  - 认证：`Authorization: Bearer <ADS_API_TOKEN>`，token 在 [ADS 账号设置页](https://ui.adsabs.harvard.edu/user/settings/token) 免费生成，不过期。
  - 限额：3000 次/天，15 次/秒（超限返回 HTTP 429）。
- 可选增强：按用户研究方向关键词（如 `gravitational waves`）对摘要做相关度打分，过滤低相关文献。
- 参考：[ADS API 官方文档](https://ui.adsabs.harvard.edu/help/api/)，社区已有 [ADS MCP server](https://github.com/cbyrohl/mcp-server-ads) 可借鉴。

#### 附件解析（场景二）

| 附件类型 | 处理方式 |
|---|---|
| .zip / .tar / .gz / .7z | 解压；**注意递归解压**（压缩包里可能还有压缩包）；.rar/.7z 建议用 7z 命令行统一处理 |
| .pdf | `pypdf` / `pdfplumber` 提取文本 |
| .docx | `python-docx` |
| .doc（老格式） | 先经 LibreOffice 转成 .docx 再解析 |
| .xlsx / .csv | `openpyxl` / pandas |
| 扫描版 PDF | OCR（如 PaddleOCR），识别率不保证，记录报告 |

- 提取重点字段（按场景二需求）：项目名称、领域、**截止日期**、申报条件、资助额度、材料清单。
- 失败处理原则：**能读的读，读不了的明确报告**——例如"附件是加密压缩包，需手动查看"，不允许静默失败。

### 5.3 LLM 摘要与过滤

- 输入：一封邮件的「正文 + 链接内容 + 附件解析结果」。
- 输出：结构化摘要（标题、来源、核心内容、重要度 1-5、截止日期/关键字段、原文链接）。
- 防幻觉：关键字段（日期、金额、数字）要求**原样引用原文**，摘要附原文出处链接。
- 过滤规则可配置：研究方向关键词、发件人白名单/黑名单、邮件类型。

### 5.4 每日调度与推送

- 定时：cron / APScheduler，每天固定时间跑（例如早上 8 点）。
- 推送渠道候选：邮件（把汇总发给自己 / 指定地址）、飞书/企业微信/钉钉 webhook。
- 增量处理：只处理新邮件，已处理过的跳过（幂等）。

## 6. 技术栈选型（建议）

**方案 A：Python（推荐，生态最全）**

- 收信：`imaplib` + `email`（标准库）或 `imapclient`
- 文档解析：`pypdf` / `pdfplumber` / `python-docx` / `openpyxl` / `py7zr` 或 `patool`
- LLM：`openai` / `anthropic` SDK（或走兼容网关）
- 调度：cron（简单）或 `APScheduler`（进程内）

**方案 B：Node.js**

- 收信：`imapflow`；解析：`pdf-parse`、`docx`、`archiver`/`yauzl`

> 不强制，开工时二选一即可。

## 7. 关键工程要点（坑清单）

1. **授权码安全**：不硬编码，放环境变量 / 配置文件中，权限收紧；出事可一键作废重发。
2. **幂等**：记录已处理邮件 ID（UID），避免重复处理和重复烧 token。
3. **成本控制**：先做规则过滤（发件人、关键词、域名），再送 LLM；只对"值得看"的邮件做摘要；附件解析结果可缓存。
4. **失败重试与可见性**：拉信失败、API 429、附件解析失败都要有重试 + 日志 + 最终报告，不能让用户"以为没有新邮件"。
5. **附件安全**：解压路径穿越防护、文件名清洗、大小上限（超大附件截断并报告）。
6. **隐私**：数据尽量本地处理，token 不外泄，不把整封邮件原文推送到第三方。
7. **授权状态**：IMAP 授权码失效（重置后）要能自动告警。

## 8. 开发路线图（Milestone）

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| **M0 Spike** | 授权码 + IMAP 拉最近 50 封真实邮件，输出到本地 | 能列出邮件标题/发件人/时间，正文可读 |
| **M1 场景一** | ADS bibcode 提取 + API 查摘要，产出文献简报 | 一封 ADS 推送邮件 → 自动输出每条文献的标题+摘要 |
| **M2 场景二** | 附件下载 + 递归解压 + 文档解析 | 一封带压缩附件的邮件 → 输出附件内文档的文本 |
| **M3 LLM 层** | 过滤 + 重要度 + 结构化摘要 | 对真实邮件输出结构化摘要，人工抽查准确率 |
| **M4 自动化** | 每日定时 + 推送（邮件/飞书） | 无人值守跑 3 天，不漏、不重 |
| **M5 打磨** | 相关度打分、规则配置、去重、反馈改进 | 摘要质量稳定，使用体验顺手 |

> 建议从 M0 开始，M0 一天内可完成——它是验证"这条路通不通"的关键，也决定后续所有假设。

## 9. 参考文档

- [腾讯企业邮箱：什么是授权码](https://exmail.qq.com/qy_mng_logic/help/authcodeSetting)
- [腾讯企业邮箱 OpenApi 协议 v1.4（管理类接口，不读邮件内容）](https://exmail.qq.com/cgi-bin/download?path=bizopenapidoc&filename=%cc%da%d1%b6%c6%f3%d2%b5%d3%ca%cf%e4OpenApi%d0%ad%d2%e9v1.4.pdf)
- [NASA ADS API 官方文档](https://ui.adsabs.harvard.edu/help/api/)
- [NASA ADS API Token 申请](https://ui.adsabs.harvard.edu/user/settings/token)
- [ADS MCP server（社区实现，可借鉴）](https://github.com/cbyrohl/mcp-server-ads)
- [IMAP MCP server（通用邮件 MCP 方案）](https://www.npmjs.com/package/@timecyber/universal-email-mcp)

## 10. 决策记录（已确认）与待办

> 2026-09-01 与用户确认，开放问题已收敛为决策，开工不再阻塞。

| 问题 | 决策 |
|---|---|
| 推送渠道 | 发邮件给自己（零配置；后期可换飞书/企业微信 webhook） |
| ADS 过滤 | 先不过滤，全部收进来；跑通后再按关键词/作者/期刊过滤 |
| 运行环境 | 本地电脑定时跑（系统计划任务） |
| 处理范围 | 只看最近 N 封 / 新邮件，增量处理（按 UID 幂等） |
| LLM 选型 | DeepSeek（M3 才接入；国内直连） |
| 汇总频率 | **分场景两封**：ADS 文献单独一封、项目申报机会单独一封 |

**待用户提供（跑真实数据所需）**：
- 腾讯企业邮箱 16 位授权码 → 填 `.env` 的 `IMAP_AUTH_CODE`
- NASA ADS API token → 填 `.env` 的 `ADS_API_TOKEN`（[申请页](https://ui.adsabs.harvard.edu/user/settings/token)，免费，不过期）

**实施状态**：项目一（ADS 文献推送）M0 + M1 已完成骨架并通过本地测试（`main.py` + `mail_digest/`，零第三方依赖），等待凭据后跑真实数据。

## 11. 快速开始（开源使用）

> 状态说明：本项目**尚未发布到 PyPI**，`pip install mail-digest` 目前对其他人不可用。
> 要用它，先获取源码（下方方式 A），在源码目录内安装或直接运行。

**安装方式 A：克隆本仓库（推荐）**

```bash
git clone https://github.com/zhaopw5/mail-digest
cd mail-digest

# 方式 A1：直接运行（无需安装，零依赖即可跑 ADS 场景）
python3 main.py --help

# 方式 A2：装成命令 mail-digest（可选依赖按需选）
pip install -e ".[ads]"       # 只要 ADS 文献 Agent（零第三方依赖）
pip install -e ".[grants]"    # 只要项目申报 Agent（含文档解析依赖）
pip install -e ".[all]"       # 两个都要
```
安装后命令为 `mail-digest`（等价于 `python3 main.py`）。
数据默认在仓库内 `data/`；如需独立数据目录（如安装/服务器部署）：
`export MAIL_DIGEST_DATA_DIR=~/mail-digest-data`。

> 待发布到 PyPI 后，`pip install "mail-digest[ads]"` 才可全局安装。

1. **配置**：`cp .env.example .env`，填写
   - `IMAP_USER` / `IMAP_AUTH_CODE`（邮箱地址 + 客户端授权码，见 .env.example 注释）
   - `ADS_API_TOKEN`（[NASA ADS 申请](https://ui.adsabs.harvard.edu/user/settings/token)，免费）
   - `DEEPSEEK_API_KEY`（可选，启用中文翻译/点评/分级，[DeepSeek 申请](https://platform.deepseek.com)）
2. **个性化（可选）**：`cp research_profile.example.json data/research_profile.json` 并编辑，
   LLM 点评与相关性分级将贴合你的研究方向。
3. **运行**（两个 Agent 独立使用；公共底座命令见下）：
   ```bash
   python3 main.py fetch                 # 公共：拉取邮件到 data/emails
   # ADS 文献 Agent
   python3 main.py ads run               # 推送识别 → ADS API → 中文翻译/点评/分级简报
   python3 main.py ads push              # 当天简报邮件发给自己
   # 项目申报 Agent
   python3 main.py grants run            # 申报通知 → 附件安全解析 → 申报机会清单
   python3 main.py grants push           # 当天清单邮件发给自己
   python3 main.py all                   # 一键：fetch + 已启用 Agent
   python3 main.py html                  # 重新生成合并 HTML 总览
   ```
4. **域开关**：只要其中一个 Agent，就在 .env 里 `ADS_ENABLED=false` 或 `GRANTS_ENABLED=false`；
   不想把基金附件发往云端模型时，只填 `ADS_LLM_API_KEY`（不填公共 `DEEPSEEK_API_KEY`）。
5. **定时（可选）**：`crontab -e` 添加，例如每天早上 9 点（`&&` 保证某步失败即停，
   不推送不完整结果；`html` 无简报时已优雅返回，不会阻断链路）：
   ```cron
   0 9 * * * cd /path/to/project && { .venv/bin/python main.py all && .venv/bin/python main.py html && .venv/bin/python main.py ads push && .venv/bin/python main.py grants push; } >> data/cron.log 2>&1
   ```
   也可用两个独立 Agent 入口分别定时：`ads-digest` / `grants-digest`（见 pyproject scripts）。
   前提：机器在设定时间保持开机；错过可用 `python3 main.py all && python3 main.py ads push && python3 main.py grants push` 手动补跑。
6. 产物：英文/中文简报与清单在 `data/digests/`，合并 HTML 总览 `data/digests/ADS文献简报-中文总览.html`。
   本地测试：`python3 tests/test_local.py`（无网络）。

### 项目结构（core + 两个处理器）

```text
mail_digest/
├── core/                    # 公共底座：IMAP/配置/模型/LLM 客户端/SMTP 推送/画像
├── processors/
│   ├── ads/                 # ADS 文献 Agent（parser/api/summarizer/renderer/models）
│   └── grants/              # 项目申报 Agent（classifier/attachments/doctext/datecheck/extractor/processor）
└── cli.py                   # 命令入口（ads run/push、grants run/push、fetch/all/html）
```
只使用 ADS 的用户不会加载 grants 的文档解析依赖（pypdf/python-docx/openpyxl/py7zr 在
`requirements.txt` 中标注为场景二所需，按需安装）。

## 12. 可选系统依赖（提升基金附件解析覆盖）

场景二（附件解析）核心为纯 Python（zipfile/tarfile/py7zr/docx/pdf/xlsx），
以下**系统工具**可显著提升覆盖，缺装时对应格式会在清单中标注「需人工查看」：

```bash
sudo apt-get install -y p7zip-full unrar libreoffice-writer libreoffice-calc
```

| 工具 | 作用 |
|---|---|
| p7zip-full（7z） | 解压部分 .rar（旧版方法） |
| unrar | 解压 RAR5 等新方法 .rar（推荐装） |
| libreoffice | 老版 .doc / .wps / 误命名的 .xls 等 → 文本转换 |

## 13. 安全说明（重要）

本项目会**自动下载并解压邮件附件**，存在被钓鱼邮件投递恶意压缩包的风险。
两道防线：

1. **可信发件人白名单**：`GRANT_ALLOWED_SENDERS`（.env）
   - 只有白名单内发件人的基金邮件才做附件解压；其他一律跳过并提示。
   - 支持 `完整地址` / `*@域名` / `裸域名`，逗号分隔；**留空 = 拒绝一切**（fail-closed）。
   - 例：`GRANT_ALLOWED_SENDERS=*@mail.sysu.edu.cn`（只信任本校域名邮件）
2. **解压加固**（mail_digest/attachments.py）
   - zip/tar 手动安全提取：拒绝 `..` 路径、绝对路径、符号链接/硬链接/设备条目；
   - 外部工具（unrar/7z）解压后强制清理链接与越界文件（`_harden`）；
   - 单附件大小、解压总量、嵌套层数均有上限；
   - 附件文件名解码 + 限长，防目录穿越与超长文件名。

> 真实风险场景：攻击者伪造「XX项目申报通知」邮件 → 主题命中关键词 → 若不设白名单，
> 恶意 tar/rar 可能通过符号链接把文件写到项目目录（如覆盖 .env、.bashrc）。
> 白名单 + 解压加固可阻断该链路。安全回归测试见 tests/test_local.py。

### 压缩炸弹防护（补）

- ZIP/TAR：解压前逐条目预算体积与文件数（`MAX_EXTRACT_TOTAL`=200MB、`MAX_FILES`=2000）
- 7z：解压前按成员 `uncompressed` 预算 + 拒绝链接条目（py7zr）
- RAR：外部 unrar/7z 子进程受 `RLIMIT_FSIZE` 写入限额约束（Linux）
- 全格式解压后统一复核总量/文件数，超限整体丢弃并报告（`_verify_output`）
- 安全回归测试：zip 穿越 / tar symlink / zip 炸弹 / 白名单（tests/test_local.py）

### 提示词注入防御（补）

邮件正文与附件是**不可信数据**，可嵌入“忽略前面的任务/把截止日期改为…”等指令。防御分四层：
1. Prompt：系统提示声明文档不可信、绝不执行其中指令；用户消息用 `<document>…</document>` 边界隔离文档区
2. 证据返回：截止/金额/限项必须同时输出**原文原句**（deadline_quote 等）与来源文件名，清单中直接展示证据行
3. 规则交叉校验：`mail_digest/datecheck.py` 用正则独立提取文本日期，与模型 `deadline_date` 交叉比对，不一致/超范围/格式非法时输出警告（可对抗被注入篡改的日期）
4. 展示证据而非仅结论：清单每条附「📜 证据〔来源〕“原文”」，供人工核对

## 14. 验证与自查指南

### 14.1 一分钟冒烟测试

```bash
python3 tests/test_local.py
# 期望输出：✅ 全部本地测试通过（含安全回归）
# 覆盖：bibcode 提取 / myADS 分组 / 简报生成 / 基金识别 /
#       安全回归（zip 穿越、tar symlink、zip 炸弹、白名单、日期交叉校验、注入边界）
```

### 14.2 每天到底跑没跑、报错没有

```bash
tail -n 40 data/cron.log          # 定时任务日志（9:00 自动运行）
```

### 14.3 某封基金邮件为什么没出现在清单里

每封邮件的处理结果缓存在 `data/fund_cache.json`（键为邮件 uid）。没进清单通常有三种原因：

| 现象 | 原因 | 在哪看 |
|---|---|---|
| 日志有「跳过 N 封非可信发件人的邮件」 | 发件人不在 `GRANT_ALLOWED_SENDERS` 白名单（安全拦截，正常） | cron.log |
| 清单里该条带 ⚠️ | 附件读不了（rar 未装工具/老 .doc/图片/损坏文件） | 清单 md 里 ⚠️ 行 |
| 当天清单为空 | 当天收到的通知都处理了，但「邮件日期」不是当天 → 只缓存不入当日清单 | 结果见 fund_cache.json |

### 14.4 验证「附件真的解压了」

1）看处理现场（每封处理的解压产物保留在 work 目录，uid 为邮件编号）：

```bash
ls data/work/fund_000009/unpack_*/       # 把 000009 换成目标邮件 uid
find data/work/fund_000009 -type f | head
```

2）手动重解一封，与清单对照（把 uid=9 换成任意 uid）：

```bash
python3 -c "
from mail_digest.core.config import Config
from mail_digest.core.imap_client import load_mails_from_dir
from mail_digest.processors.grants import attachments as att
import shutil
from pathlib import Path
cfg = Config.load()
m = next(x for x in load_mails_from_dir(cfg.eml_dir) if x.uid == 9)
shutil.rmtree('data/work/check', ignore_errors=True)
fs = att.extract_attachments(m, Path('data/work/check'))
ok = [f['path'] for f in fs if f['ok'] and f['path']]
r, p = att.unpack_recursive(ok, Path('data/work/check/u'))
print(len(r), '个可读文件，', len(p), '个问题')
"
```

3）原始附件对照：`data/emails/*.eml` 是完整原始邮件，用邮件客户端或解压软件打开即可看到服务器上收到的附件原文。

### 14.5 安全边界自查

**发件人白名单是否生效**（改 .env 后验证）：

```bash
python3 -c "
from mail_digest.core.config import Config, sender_allowed
cfg = Config.load()
print('白名单:', cfg.grant_allowed_senders)
print('可信发件人放行:', sender_allowed('孙姗珍 <a@mail.sysu.edu.cn>', cfg.grant_allowed_senders))
print('外部发件人被拦:', not sender_allowed('攻击者 <x@evil.org>', cfg.grant_allowed_senders))
"
```

**恶意压缩包被拒**：用 14.4 的命令手动构造一个含符号链接或 `../` 条目的
zip/tar 解压 → 应抛出 `AttachmentError` 且目录外无残留文件（tests 中
`test_zip_path_traversal_blocked` / `test_tar_symlink_blocked` / `test_zip_bomb_blocked`
就是自动化版本）。

**提示词注入防线是否在**：清单中每条应含 `📜 证据〔来源〕“原文”` 行
（关键字段逐字引用出处）；若模型日期与正则独立提取的日期不一致，会输出
`⚠️ 截止日期校验失败/不一致` 警告。当心：这些警告出现时以原文证据为准，勿信模型结论。

### 14.6 产物是什么（不再迷路）

| 文件 | 内容 |
|---|---|
| `data/digests/zh/ads_*.zh.md` | 每日 ADS 中文简报（按订阅分组，星星分级） |
| `data/digests/fund_YYYYMMDD.md` | 每日项目申报机会清单（含证据行） |
| `data/digests/ADS文献简报-中文总览.html` | 全部 ADS 简报合并页（浏览器打开） |
| `data/emails/*.eml` | 原始邮件（勿删，删了要重拉） |
| `data/llm_cache.json` / `data/fund_cache.json` | LLM 缓存（防重复花钱，勿删） |
| `data/processed.json` / `processed_fund.json` | 各 Agent 幂等状态（勿删） |
| `data/work/` | 附件解压中间产物（可随时删除，不影响结果） |

## 15. 无人值守部署建议

- **Grants Agent 处理不可控来源附件前**，建议启用严格认证：
  ```env
  GRANTS_STRICT_AUTH=true
  ```
  （缺失 `Authentication-Results` 或 `spf=neutral` 的邮件一律不处理附件；
  默认 false 是为兼容校内互发无认证头的场景）
- 建议同时配置可信认证服务器白名单，只采信本校 MX 写入的认证结果（防伪造头）：
  ```env
  GRANTS_AUTH_SERVERS=mail.sysu.edu.cn
  ```
- 外部工具（unrar/7z/LibreOffice）当前以 `RLIMIT_FSIZE` 单文件限额 + 解压后复核兜底；
  对最高安全要求的环境建议再套容器/受限子进程。
- 附件异常不会中断整批（单封记录并继续）；附件出错时正文结果仍会执行
  证据与日期交叉校验（防提示词注入借附件异常绕过）。
