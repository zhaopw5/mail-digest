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
- `received_at` 来自 IMAP `INTERNALDATE`（服务器收件时间），是推送窗口判定的唯一依据。
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
    "last_fetch_counts": {"initial": 180, "incremental": 0, "gap_retry": 0,
                          "failed": 0, "skipped": 0, "identity_repair": 0}
  }
}
```

- 拉取只请求 `UID last_uid+1:*`；`gaps` 是拉取失败、需要持续重试的 UID——
  **游标不会越过它们**（即使某个缺口 UID 小于 `last_uid`）。
- `uncovered_below` 非空表示首次接管尚未覆盖到更早的邮件（单轮上限
  `MAIL_DIGEST_FETCH_INITIAL_MAX`），后续 fetch 会分批补拉直到为 null。
- `last_fetch_at` 是"截至该时刻已检查过邮箱"的证据。截止点之后没有更新过它时，
  状态邮件**不会**声称"今日无新推送"。

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

`status` 语义（键是完整 `source_id`）：

| 值 | 含义 | 是否进入正式推送 |
|---|---|---|
| `ready` | 元数据齐全（配置了 LLM 时中文也齐全） | ✅ 是 |
| `empty` | 该邮件确实没有可提取文献（非失败） | ❌ 否（无需推送） |
| `retryable_error` | ADS API / LLM / 解析失败，或只完成一部分 | ❌ 否，且**下次 `ads run` 自动重试** |

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
- 状态文件损坏、或 schema 版本不认识时，正式推送**直接中止且不发信**。
- 覆盖式初始化（`ads state-init --force`）会先写时间戳备份 `ads_state.json.bak.<ts>`。

## 6. `data/processed.json` — 兼容字段（不再是判定依据）

新版本写入完整 `source_id` 列表，仅供查看与旧数据迁移；ADS 的"是否处理过"
一律以 `ads_manifest.json` 为准。旧版本内容（裸 UID 数组）会在首次运行时
一次性迁移到 manifest，且只迁移"能证明简报确实存在"的条目。

## 7. 简报产物

- 英文：`data/digests/ads_{YYYYMMDD|nodate}_{uid:06d}.md`
- 中文：`data/digests/zh/ads_{YYYYMMDD|nodate}_{uid:06d}.zh.md`
- 基金清单：`data/digests/fund_{YYYYMMDD}.md`

文件名里的日期只用于展示与排序，**不决定推送资格**：候选来自 manifest 的
`source_id` 记录，因此 `nodate` 简报同样会被推送。

## 8. 并发与崩溃

- `data/.ads_official.lock`：正式推送的互斥锁（`O_CREAT|O_EXCL`，含 pid，
  超过 30 分钟的陈旧锁会被接管）。
- SMTP 与本地文件不共享事务：若 SMTP 已接受但进程在写状态前崩溃，同一批简报
  可能被重发一次。要完全避免需要外部事务存储，当前实现选择"宁可重发，不可漏发"。
