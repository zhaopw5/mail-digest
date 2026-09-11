# 状态文件契约（v2，对应提交 becc67a 之后）

本文件是给**审查者与二次开发者**看的：程序在 `data/` 下写了哪些文件、字段含义是什么、
哪些不变量必须成立。改代码时请同步改这里，否则外部审计脚本会读错。

所有 JSON 都由 `mail_digest/core/state.py:write_json_atomic` 原子写入
（临时文件 + fsync + `os.replace`），因此不会出现被截断的半截文件。

## 1. `data/emails/index.json` — 缓存邮件身份索引

```json
{
  "schema_version": 2,
  "emails": {
    "20260910_000178_u1.eml": {
      "source_id": "INBOX:1:1784174618",
      "folder": "INBOX",
      "uid": 1784174618,
      "uidvalidity": 1,
      "received_at": "2026-09-10T19:41:23+08:00"
    }
  }
}
```

- **键是 `.eml` 文件名**，不是 UID。旧版本（schema 缺失）以裸 UID 为键、
  值里带 `eml` 字段，读取时会自动归一化；写入一律是新格式。
- `received_at` 来自 IMAP `INTERNALDATE`（服务器收件时间），是推送窗口判定的依据；
  极少数服务器不返回 `INTERNALDATE` 时回退为本地缓存文件的 mtime，此时该值只能算近似。
  邮件自带的 `Date:` 头只用于展示与文件命名，缺失也不影响推送资格。
- **不要用裸 UID 反查身份**：UIDVALIDITY 变化后 UID 会被复用，那正是"新邮件被旧简报
  冒名登记"的根因。

## 2. `data/emails/*.eml` — 原始邮件缓存

文件名 `{YYYYMMDD|nodate}_{uid:06d}_u{UIDVALIDITY}.eml`，尾部 `_u<N>` 是身份的一部分。
无此后缀的文件属于旧版本遗留，程序会把它们移入 `data/emails/_legacy_unidentified/`
（保留内容，不删除）——该目录里的文件不参与任何判定。

## 3. `data/imap_state.json` — 拉取游标

```json
{
  "INBOX": {
    "uidvalidity": 1,
    "last_uid": 1784174623,
    "uncovered_below": null,
    "gaps": [],
    "last_fetch_at": "2026-09-11T10:35:02+08:00",
    "last_fetch_ok": true,
    "last_fetch_counts": {"initial": 180, "incremental": 0, "gap_retry": 0,
                          "failed": 0, "skipped": 0, "identity_repair": 0}
  }
}
```

- 拉取只请求 `UID last_uid+1:*`；`gaps` 是拉取失败、需要持续重试的 UID——
  **游标不会越过它们**（即使某个缺口 UID 小于 `last_uid`）。
- `uncovered_below` 非空表示接管尚未覆盖到更早的邮件（`--recent N` 只接管一部分、
  或单轮上限 `MAIL_DIGEST_FETCH_INITIAL_MAX` 截断），后续 fetch 会自动分批补拉直到为 null。
- **IMAP `SEARCH` 返回非 OK 时 fetch 直接失败**：写入 `last_fetch_ok: false` 与
  `last_fetch_error`，但**不更新** `last_fetch_at`——"没检查成"绝不能被记成"检查过了"。
- 状态邮件只有在「`last_fetch_ok: true` + `last_fetch_at` 晚于本次截止点 + `gaps` 为空
  + `uncovered_below` 为空」四条同时成立时才允许说"今日无新推送"；否则改为
  「本次未检测到有效的邮件拉取记录」或「本次未能完整确认邮箱」。

## 4. `data/ads_manifest.json` — ADS 逐封处理状态

```json
{
  "schema_version": 1,
  "items": {
    "INBOX:1:1784174618": {
      "status": "ready",
      "received_at": "2026-09-10T19:41:23+08:00",
      "en_file": "ads_20260910_1784174618.md",
      "zh_file": "ads_20260910_1784174618.zh.md",
      "errors": [],
      "updated_at": "2026-09-11T10:30:00+08:00"
    }
  }
}
```

`status` 语义（键是完整 `source_id`）。**manifest 一旦存在，它就是唯一权威**：
候选只从 `ready` 项产生，`ready` 为空就返回空，不会退回"扫描磁盘上的简报文件"
（那种回退会把部分翻译的简报当成功发出去并登记 `official_sent`）。

| 值 | 含义 | 是否进入正式推送 |
|---|---|---|
| `ready` | 元数据齐全（配置了 LLM 时中文也齐全） | ✅ 是 |
| `empty` | 该邮件确实没有可提取文献（非失败） | ❌ 否（无需推送） |
| `retryable_error` | ADS API / LLM / 解析失败、返回内容不完整（如空对象、缺标题），或只完成一部分 | ❌ 否，且**下次 `ads run` 自动重试** |

只有 `ready` 会被视为"处理完毕"。失败邮件绝不会被记成已处理——这是
"下次运行会自动重试"这句话成立的唯一依据。

## 5. `data/ads_state.json` — 正式推送状态

```json
{
  "schema_version": 2,
  "last_official_cutoff": "2026-09-11T09:00:00+08:00",
  "last_official_sent_at": "2026-09-11T09:00:07+08:00",
  "items": {
    "INBOX:1:1784174618": {
      "received_at": "2026-09-10T19:41:23+08:00",
      "digest_file": "ads_20260910_1784174618.zh.md",
      "official_sent_at": "2026-09-11T09:00:07+08:00",
      "cutoff": "2026-09-11T09:00:00+08:00"
    }
  }
}
```

- `last_official_cutoff` 是**计划**截止点（`MAIL_DIGEST_PUSH_TIME`，默认 09:00），
  不是进程启动时间，也不是发送完成时间；后者记在 `last_official_sent_at`。
- 状态文件损坏、或 schema 版本不认识时，正式推送**直接中止且不发信**
  （`ads_manifest.json` 遇到不认识的 schema 同样中止，不猜、不当空状态）。
- 覆盖式初始化（`ads state-init --force`）会先写备份，文件名含微秒
  （`ads_state.json.bak.<ts><us>`，避免同秒重复初始化互相覆盖）。
  **备份写失败会中止覆盖并报错**，不会"备份失败还照旧覆盖"。
- `ads state-init --dry-run` 可先预览会标记/保留哪些简报，不写任何文件。

## 6. `data/processed.json` — 兼容字段（不再是判定依据）

新版本写入完整 `source_id` 列表，仅供查看与旧数据迁移；ADS 的"是否处理过"
一律以 `ads_manifest.json` 为准。旧版本内容（裸 UID 数组）会在首次运行时迁移到
manifest，**一律标为 `retryable_error` 而不是 `ready`**——旧版本失败时同样会写出英文
简报，所以"文件存在"证明不了当时处理完整。这些邮件会在下次 `ads run` 重新处理，
核对完整后才转为 `ready`；旧文件另存为 `processed.json.legacy.bak` 供人工比对。

## 7. 简报产物

- 英文：`data/digests/ads_{YYYYMMDD|nodate}_{uid:06d}_u{UIDVALIDITY}.md`
- 中文：`data/digests/zh/ads_{YYYYMMDD|nodate}_{uid:06d}_u{UIDVALIDITY}.zh.md`

尾部 `_u<N>` 是完整身份的一部分：缺少它时，同一天、同一 UID、不同 UIDVALIDITY 的
两封邮件会指向同一个物理文件，后处理的直接覆盖前者的内容。旧版本（无 `_u`）的简报
不再参与候选选择，可移到 `data/digests/_legacy_named/` 归档。
- 基金清单：`data/digests/fund_{YYYYMMDD}.md`

文件名里的日期只用于展示与排序，**不决定推送资格**：候选来自 manifest 的
`source_id` 记录，因此 `nodate` 简报同样会被推送。

## 8. 并发与崩溃

- `data/.ads_official.lock`：正式推送的互斥锁（`O_CREAT|O_EXCL`，含 pid，
  超过 30 分钟的陈旧锁会被接管）。
- SMTP 与本地文件不共享事务：若 SMTP 已接受但进程在写状态前崩溃，同一批简报
  可能被重发一次。要完全避免需要外部事务存储，当前实现选择"宁可重发，不可漏发"。
