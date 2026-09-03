"""
GCP Weekly Digest — Cloud Run Job (V2)

一封信回答兩個問題：GCP 這週出了什麼，以及這件事對我現在跑的東西代表什麼。

前半彙整 GCP Release Notes + Blog，交給 Vertex AI Gemini 寫成分類週報。
後半讀這個專案自己的 Cloud Recommender 建議，附上「有什麼值得處理」
（見 account_context.py，用 FEATURE_ACCOUNT_ADVICE 開啟）。
release notes 給不了後半，因為它不知道你手上有什麼。

對標姊妹專案 AWS Weekly Digest（github.com/GordonWei/aws-weekly-digest），
程式結構、環境變數風格、輸出格式盡量對稱，方便寫「AWS vs GCP」對照文章。

V2 相較 V1（Google Apps Script）的關鍵差異：
- 執行環境：Apps Script → Cloud Run Job（不綁 Google Workspace）
- AI SDK：Vertex AI REST + OAuth → google-genai SDK + enterprise=True（IAM 直接認證）
  （google-cloud-aiplatform 的 generative_models 模組已於 2025-06-24 棄用、
  2026-06-24 起下架，官方目前的建議路徑是 google-genai，經查證 pip 套件原始碼
  確認 Client(enterprise=True, ...) 為現行參數，vertexai=True 為相容別名）
- 模型：gemini-2.5-flash-lite（V1）→ gemini-3.1-flash-lite
  （2.5-flash-lite 將於 2026-10-16/20 全面下架，沿用 V1 模型會讓每週排程活不過兩個月）
- 存檔：Google Drive → GCS
- Email：GmailApp（綁 Google 帳號）→ SendGrid（V2.0，2026-08-27）→ Gmail API 網域範圍委派
  （V2.1，2026-09-03，見 README——SendGrid 帳號被寄信服務商永久拒絕重啟）
- Blog RSS 來源：V1 用的 https://cloud.google.com/blog/rss/ 已失效（改回傳 HTML，非 XML，
  實測 curl 驗證），改用目前仍有效的 https://cloudblog.withgoogle.com/rss/

V2.1（2026-09-03）：Email 管道再次更換，SendGrid → Gmail API + Service Account
網域範圍委派（impersonate 你自己的 Workspace/Gmail 帳號）。原因：SendGrid 帳號
因長期 deferred 被永久拒絕重啟（見 README），Mailgun 需要綁信用卡不想用；
若收件網域的 MX 本來就指向 Google，網域範圍委派可以免除管理第三方 Email 服務帳號。
EMAIL_PROVIDER 環境變數保留 SendGrid 路徑（程式碼未刪除），預設值改為 gmail。

**keyless（同日追加）**：原設計讀取掛載的 SA JSON 金鑰檔，部署時撞上組織政策
`constraints/iam.disableServiceAccountKeyCreation`（`gcp-weekly-digest-mailer-sa`
無法建立金鑰，非權限不足）。改為不落地任何金鑰檔的 keyless 流程：Job 執行身分
`gcp-weekly-digest-sa`（Cloud Run 上用 ADC）先用 IAM Credentials API 對
`gcp-weekly-digest-mailer-sa` 做 signJwt（已授 `roles/iam.serviceAccountTokenCreator`
在該 SA 資源上，非專案層級），簽出 `sub=GMAIL_IMPERSONATE_USER` 的 JWT，再拿它跟 Google OAuth
token endpoint 換一個代表該使用者的 access token（RFC 7523 JWT-bearer flow）——
這個 token 才是真正握有網域範圍委派授權的憑證。全程沒有金鑰檔案落地，也不需要
組織政策例外。詳見 `_gmail_send()` 與 README。

V2.2：加入 `DIGEST_LANGUAGE`（`en` / `zh-TW`，預設 `en`）——比照 AWS Weekly Digest
的做法，一個設定同時控制週報主體（`_prompt_en`/`_prompt_zh_tw`）、帳號建議區段
（`account_context.py` 本來就已支援兩種語言）、email 靜態文字（`_EMAIL_STRINGS`），
避免英文週報裝在中文標籤的信裡寄出。
"""

import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types
from google.cloud import storage

import account_context

# ── 設定（環境變數驅動，比照 AWS Lambda 版風格）──────────────────
CONFIG = {
    'GCP_PROJECT_ID':   os.environ.get('GCP_PROJECT_ID', ''),
    'VERTEX_LOCATION':  os.environ.get('VERTEX_LOCATION', 'global'),
    'GEMINI_MODEL':     os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite'),
    'RECIPIENT_EMAIL':  os.environ.get('RECIPIENT_EMAIL', ''),
    'SENDER_EMAIL':     os.environ.get('SENDER_EMAIL', ''),
    'GCS_BUCKET':       os.environ.get('GCS_BUCKET', ''),
    'DAYS_LOOKBACK':    int(os.environ.get('DAYS_LOOKBACK', '7')),
    'MAX_RELEASE_NOTES': int(os.environ.get('MAX_RELEASE_NOTES', '80')),
    'MAX_BLOG_POSTS':   int(os.environ.get('MAX_BLOG_POSTS', '15')),
    'SENDGRID_API_KEY': os.environ.get('SENDGRID_API_KEY', ''),

    # 'en' or 'zh-TW'. One setting covers digest body, advice section (see
    # account_context.py, which already supported both), and the static
    # strings in the email wrapper — mirrors AWS Weekly Digest's DIGEST_LANGUAGE.
    'DIGEST_LANGUAGE':  os.environ.get('DIGEST_LANGUAGE', 'en'),

    # ── Email 管道：gmail（V2.1 預設，keyless 網域範圍委派）或 sendgrid（保留，V2.0 舊路徑）──
    'EMAIL_PROVIDER':        os.environ.get('EMAIL_PROVIDER', 'gmail'),
    'GMAIL_MAILER_SA_EMAIL': os.environ.get(
        'GMAIL_MAILER_SA_EMAIL',
        f"gcp-weekly-digest-mailer-sa@{os.environ.get('GCP_PROJECT_ID', '')}.iam.gserviceaccount.com",
    ),
    'GMAIL_IMPERSONATE_USER': os.environ.get('GMAIL_IMPERSONATE_USER', ''),

    # ── 輸出管道開關（false = 預留，程式碼已就位）────────────
    'FEATURES': {
        'SEND_EMAIL':             os.environ.get('FEATURE_SEND_EMAIL',      'true') == 'true',
        'EMBED_CONTENT_IN_EMAIL': os.environ.get('FEATURE_EMBED_CONTENT',   'true') == 'true',
        'SAVE_TO_GCS':            os.environ.get('FEATURE_SAVE_TO_GCS',     'true') == 'true',
        'POST_TO_LINKEDIN':       os.environ.get('FEATURE_POST_TO_LINKEDIN', 'false') == 'true',
        'POST_TO_WEBHOOK':        os.environ.get('FEATURE_POST_TO_WEBHOOK',  'false') == 'true',
        # 週報的另一半：回頭看這個專案自己有什麼值得處理的（見 account_context.py）。
        # 預設關閉是因為它需要 Recommender 的讀取權限，不是因為它比較不重要。
        'ACCOUNT_ADVICE':         os.environ.get('FEATURE_ACCOUNT_ADVICE',   'false') == 'true',
    },
}

RELEASE_NOTES_URL = 'https://docs.cloud.google.com/feeds/gcp-release-notes.xml'
BLOG_RSS_URL       = 'https://cloudblog.withgoogle.com/rss/'

# Static strings in the email wrapper, keyed by DIGEST_LANGUAGE. The digest
# body itself comes from the Gemini prompt (_prompt_zh_tw / _prompt_en); this
# covers everything else a recipient sees, so an 'en' digest never ends up
# inside a Chinese-labelled email or vice versa.
_EMAIL_STRINGS = {
    'zh-TW': {
        'stats':        '{today}｜Release Notes {rn} 條，Blog {blog} 篇',
        'gcs_link':      'GCS 原始存檔',
        'footer':        'GCP Weekly Digest Bot｜自動產生，請審閱後再分享',
        'view_html':     '（請以 HTML 郵件查看）',
        'error_subject': '[GCP] Weekly Digest 產生失敗',
        'error_body':    '錯誤訊息：{msg}',
        'error_text':    '錯誤：{msg}',
    },
    'en': {
        'stats':        '{today} | {rn} Release Notes, {blog} Blog posts',
        'gcs_link':      'GCS archive',
        'footer':        'GCP Weekly Digest Bot | Auto-generated, review before sharing',
        'view_html':     '(View this email as HTML)',
        'error_subject': '[GCP] Weekly Digest generation failed',
        'error_body':    'Error: {msg}',
        'error_text':    'Error: {msg}',
    },
}


def _strings():
    return _EMAIL_STRINGS.get(CONFIG['DIGEST_LANGUAGE']) or _EMAIL_STRINGS['en']


# ────────────────────────────────────────────────────────────
# 主程式
# ────────────────────────────────────────────────────────────
def main():
    print('開始產生 GCP Weekly Digest...')
    try:
        release_notes = fetch_gcp_release_notes()
        blog_posts    = fetch_gcp_blog_posts()
        print(f'抓取完成：{len(release_notes)} 條 Release Notes，{len(blog_posts)} 篇 Blog')

        if not release_notes:
            raise ValueError('本週 Release Notes 為空，請確認 RSS Feed 是否正常。')

        digest_content = call_gemini(release_notes, blog_posts)
        print('Gemini 分析完成')

        if CONFIG['FEATURES']['ACCOUNT_ADVICE']:
            section, warn = account_context.build_advice_section(
                CONFIG['DIGEST_LANGUAGE'], CONFIG['GCP_PROJECT_ID'], _invoke_gemini)
            if warn:
                # 故意印得很大聲。這一段掉了不該連累週報，但也不能無聲無息地消失——
                # 「區段不見」跟「這週沒東西可講」在信裡看起來一模一樣。
                print(f'WARNING: {warn}')
            digest_content += section
            print('帳號建議已附加' if section else '帳號建議沒有產出內容')

        gcs_url = None
        if CONFIG['FEATURES']['SAVE_TO_GCS'] and CONFIG['GCS_BUCKET']:
            gcs_url = save_to_gcs(digest_content)
            print(f'GCS 存檔：{gcs_url}')

        if CONFIG['FEATURES']['SEND_EMAIL']:
            send_email(digest_content, gcs_url, len(release_notes), len(blog_posts))
            print('Email 已寄出')

        if CONFIG['FEATURES']['POST_TO_LINKEDIN']:
            post_to_linkedin(digest_content)

        if CONFIG['FEATURES']['POST_TO_WEBHOOK']:
            post_to_webhook(digest_content, gcs_url)

        print('完成！')

    except Exception as e:
        print(f'錯誤：{e}')
        _send_error_email(str(e))
        raise


# ────────────────────────────────────────────────────────────
# 資料抓取：GCP Release Notes（Atom Feed）
# ────────────────────────────────────────────────────────────
def fetch_gcp_release_notes():
    try:
        req = urllib.request.Request(RELEASE_NOTES_URL, headers={'User-Agent': 'GCP-Weekly-Digest/2.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8', errors='replace')

        # Atom feed 本身即為 well-formed XML，namespace 已在根節點正確宣告，
        # 直接用 ElementTree 的 namespace-aware 解析即可，不需要任何 regex 手動剝除
        # namespace（AWS 版 Blog RSS 曾因手動剝除不完整導致 ElementTree 報
        # unbound prefix 而被靜默吞掉，這裡從根本避開同類型問題）。
        ns   = {'atom': 'http://www.w3.org/2005/Atom'}
        root = ET.fromstring(raw)

        cutoff = datetime.now(timezone.utc) - timedelta(days=CONFIG['DAYS_LOOKBACK'])
        items  = []

        for entry in root.findall('atom:entry', ns):
            title = (entry.findtext('atom:title', default='', namespaces=ns) or '').strip()
            link_el = entry.find('atom:link', ns)
            link  = link_el.get('href', '') if link_el is not None else ''
            summary = _strip_html(
                entry.findtext('atom:content', default='', namespaces=ns)
                or entry.findtext('atom:summary', default='', namespaces=ns)
                or ''
            )
            pub_raw = entry.findtext('atom:updated', default='', namespaces=ns) \
                or entry.findtext('atom:published', default='', namespaces=ns)
            pub = _parse_iso_date(pub_raw)

            if title and pub and pub >= cutoff:
                items.append({'title': title, 'summary': summary[:400], 'link': link, 'published': pub})

        return items[:CONFIG['MAX_RELEASE_NOTES']]

    except Exception as e:
        print(f'[Release Notes] 抓取失敗：{e}')
        return []


# ────────────────────────────────────────────────────────────
# 資料抓取：GCP Blog RSS（失敗靜默跳過）
# ────────────────────────────────────────────────────────────
def fetch_gcp_blog_posts():
    try:
        req = urllib.request.Request(BLOG_RSS_URL, headers={'User-Agent': 'GCP-Weekly-Digest/2.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8', errors='replace')

        root    = ET.fromstring(raw)  # well-formed RSS 2.0，namespace 已正確宣告，不需剝除
        channel = root.find('channel')
        if channel is None:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=CONFIG['DAYS_LOOKBACK'])
        items  = []

        for item in channel.findall('item'):
            title = (item.findtext('title') or '').strip()
            link  = item.findtext('link') or ''
            desc  = _strip_html(item.findtext('description') or '')[:250]
            pub   = _parse_rss_date(item.findtext('pubDate') or '')

            if title and pub and pub >= cutoff:
                items.append({'title': title, 'description': desc, 'link': link})

        return items[:CONFIG['MAX_BLOG_POSTS']]

    except Exception as e:
        print(f'[Blog] 跳過：{e}')
        return []


# ────────────────────────────────────────────────────────────
# Gemini 呼叫（google-genai SDK，Enterprise/Vertex 模式，IAM 直接認證）
# ────────────────────────────────────────────────────────────
def call_gemini(release_notes, blog_posts):
    builder = _PROMPT_BUILDERS.get(CONFIG['DIGEST_LANGUAGE']) or _PROMPT_BUILDERS['en']
    return _invoke_gemini(builder(release_notes, blog_posts))


def _prompt_zh_tw(release_notes, blog_posts):
    today      = _fmt_date(datetime.now())
    week_range = _week_range()

    rn_text = '\n\n'.join(
        f"[RN{i+1}] {item['title']}\n摘要：{item['summary']}\n連結：{item['link']}"
        for i, item in enumerate(release_notes)
    )
    blog_text = '\n\n'.join(
        f"[B{i+1}] {item['title']}\n{item['description']}\n連結：{item['link']}"
        for i, item in enumerate(blog_posts)
    ) if blog_posts else '（本週無新 Blog）'

    return f"""你是一位精通 Google Cloud Platform 的首席雲端架構師，同時也是優秀的技術寫作者。
請根據以下本週（{week_range}）的 GCP Release Notes 和 Blog，整理一份供技術社群分享的週報。
請直接輸出週報內容，不要有任何前言、自我介紹或說明文字。

## GCP Release Notes（共 {len(release_notes)} 條）
{rn_text}

## GCP Blog Posts（共 {len(blog_posts)} 篇）
{blog_text}

---

## 輸出格式（嚴格遵守，使用繁體中文，標題請勿使用 emoji）

# GCP Weekly Digest｜{today}

> 本週總覽：[2-3 句：哪些領域有重大更新、本週整體方向感]

---

## 數字總覽
- AI & Machine Learning：N 條
- Compute & Container：N 條
- Data & Analytics：N 條
- Networking & Security：N 條
- Developer Tools：N 條

---

## AI & Machine Learning

### [功能名稱]
**一句話重點**：[說明這個更新做了什麼]
**商業/技術價值**：[對工程師或企業的實際影響，30 字以內]
**狀態**：[GA / Preview / Deprecated]
**官方連結**：[對應的連結]

[此分類下列出 3-6 條最重要的更新，過濾純 Bug Fix 和文件修正]

---

## Compute & Container
[同上格式，3-6 條]

---

## Data & Analytics
[同上格式，3-6 條]

---

## Networking & Security
[同上格式，3-5 條]

---

## Developer Tools & Other
[同上格式，3-5 條]

---

## 本週精選 Blog
[列出 2-3 篇最值得閱讀的 Blog，格式：**標題**：一句話說明為何值得看 → 連結]

---

## 本週架構師觀點
[選出本週最值得關注的 1 個更新，解釋它對企業雲架構師的深層意義，約 100 字，用第一人稱「我認為...」]

---
GCP Weekly Digest Bot｜{today}
"""


def _prompt_en(release_notes, blog_posts):
    today      = _fmt_date(datetime.now())
    week_range = _week_range()

    rn_text = '\n\n'.join(
        f"[RN{i+1}] {item['title']}\nSummary: {item['summary']}\nLink: {item['link']}"
        for i, item in enumerate(release_notes)
    )
    blog_text = '\n\n'.join(
        f"[B{i+1}] {item['title']}\n{item['description']}\nLink: {item['link']}"
        for i, item in enumerate(blog_posts)
    ) if blog_posts else '(No new blog posts this week)'

    return f"""You are a principal cloud architect who knows Google Cloud Platform inside out, and a skilled technical writer.
Based on this week's ({week_range}) GCP Release Notes and Blog posts below, write a digest for a technical community audience.
Output the digest content directly, with no preamble, self-introduction, or meta commentary.

## GCP Release Notes ({len(release_notes)} total)
{rn_text}

## GCP Blog Posts ({len(blog_posts)} total)
{blog_text}

---

## Output format (follow exactly, no emoji in headings)

# GCP Weekly Digest | {today}

> This week at a glance: [2-3 sentences: which areas saw major updates, overall direction this week]

---

## By the numbers
- AI & Machine Learning: N items
- Compute & Container: N items
- Data & Analytics: N items
- Networking & Security: N items
- Developer Tools: N items

---

## AI & Machine Learning

### [Feature name]
**One-line summary**: [What this update does]
**Business/technical value**: [Practical impact for engineers or businesses, under 30 words]
**Status**: [GA / Preview / Deprecated]
**Official link**: [Corresponding link]

[List the 3-6 most significant updates in this category, filter out pure bug fixes and doc corrections]

---

## Compute & Container
[Same format as above, 3-6 items]

---

## Data & Analytics
[Same format as above, 3-6 items]

---

## Networking & Security
[Same format as above, 3-5 items]

---

## Developer Tools & Other
[Same format as above, 3-5 items]

---

## This week's picks from the Blog
[List 2-3 blog posts most worth reading, format: **Title**: one sentence on why it's worth reading -> link]

---

## Architect's take of the week
[Pick the single most noteworthy update this week and explain what it means for enterprise cloud architects at a deeper level, about 100 words, first person "I think...")]

---
GCP Weekly Digest Bot | {today}
"""


_PROMPT_BUILDERS = {'zh-TW': _prompt_zh_tw, 'en': _prompt_en}


def _invoke_gemini(prompt):
    """週報本體與帳號建議共用同一條 Gemini 路徑。"""
    client = genai.Client(
        enterprise=True,
        project=CONFIG['GCP_PROJECT_ID'],
        location=CONFIG['VERTEX_LOCATION'],
    )

    response = client.models.generate_content(
        model=CONFIG['GEMINI_MODEL'],
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=8192),
    )

    return response.text


# ────────────────────────────────────────────────────────────
# 輸出 A：GCS 存檔
# ────────────────────────────────────────────────────────────
def save_to_gcs(content):
    today = _fmt_date(datetime.now(), '%Y-%m-%d')
    key   = f'digests/{today}/gcp-weekly-digest.md'

    client = storage.Client(project=CONFIG['GCP_PROJECT_ID'])
    bucket = client.bucket(CONFIG['GCS_BUCKET'])
    blob   = bucket.blob(key)
    blob.upload_from_string(content, content_type='text/markdown; charset=utf-8')

    return f'gs://{CONFIG["GCS_BUCKET"]}/{key}'


# ────────────────────────────────────────────────────────────
# 輸出 B：Email（Gmail API 網域範圍委派，V2.1 預設；SendGrid 為 V2.0 舊路徑保留）
# ────────────────────────────────────────────────────────────
def send_email(digest_content, gcs_url, rn_count, blog_count):
    s        = _strings()
    today    = _fmt_date(datetime.now())
    gcs_link = (f'<p style="margin:12px 0"><a href="https://console.cloud.google.com/storage/browser/{CONFIG["GCS_BUCKET"]}" '
                f'style="color:#4285f4;font-weight:600">{s["gcs_link"]}</a></p>') if gcs_url else ''
    body_html = markdown_to_html(digest_content) if CONFIG['FEATURES']['EMBED_CONTENT_IN_EMAIL'] else ''
    stats     = s['stats'].format(today=today, rn=rn_count, blog=blog_count)

    html_body = f"""
<div style="font-family:-apple-system,Arial,sans-serif;max-width:680px;margin:0 auto;color:#202124">
  <div style="background:#4285f4;padding:20px 32px;border-radius:8px 8px 0 0">
    <h1 style="margin:0;color:white;font-size:20px;font-weight:700">GCP Weekly Digest</h1>
    <p style="margin:6px 0 0;color:#c5d8ff;font-size:14px">{stats}</p>
  </div>
  <div style="background:#ffffff;padding:24px 32px;border:1px solid #e0e0e0;border-top:none;border-radius:0 0 8px 8px">
    {gcs_link}
    <div style="margin-top:16px">{body_html}</div>
    <p style="margin:24px 0 0;font-size:12px;color:#999;border-top:1px solid #f0f0f0;padding-top:12px">
      {s['footer']}
    </p>
  </div>
</div>"""

    _dispatch_send(
        subject=f'[GCP] Weekly Digest {today}',
        html_body=html_body,
        text_fallback=f'GCP Weekly Digest {today}\n{s["view_html"]}',
    )


def _send_error_email(error_msg):
    s = _strings()
    try:
        _dispatch_send(
            subject=s['error_subject'],
            html_body=f'<p>{html.escape(s["error_body"].format(msg=error_msg))}</p>',
            text_fallback=s['error_text'].format(msg=error_msg),
        )
    except Exception as e:
        print(f'Error email 也寄失敗：{e}')


def _dispatch_send(subject, html_body, text_fallback):
    """依 EMAIL_PROVIDER 選路徑，預設 gmail（V2.1），保留 sendgrid（V2.0）供切回/比較。"""
    if CONFIG['EMAIL_PROVIDER'] == 'sendgrid':
        _sendgrid_send(subject, html_body, text_fallback)
    else:
        _gmail_send(subject, html_body, text_fallback)


def _gmail_send(subject, html_body, text_fallback):
    """Gmail API + Service Account 網域範圍委派（domain-wide delegation），keyless。

    不落地任何 SA JSON 金鑰檔——組織政策 `constraints/iam.disableServiceAccountKeyCreation`
    直接擋掉這個專案底下建立 SA 金鑰，改走短期 token 換取的方式，暴露面比常駐金鑰檔更小。

    流程（RFC 7523 JWT-bearer flow）：
    1. Job 執行身分（`gcp-weekly-digest-sa`，Cloud Run 上用 ADC）呼叫 IAM Credentials
       API 對 `gcp-weekly-digest-mailer-sa` 做 signJwt——這一步需要 Job SA 對 mailer SA
       擁有 `roles/iam.serviceAccountTokenCreator`（已授在 SA 資源層級，非專案層級）。
       簽出的 JWT 帶 `sub=GMAIL_IMPERSONATE_USER`，這個 `sub` 欄位就是網域範圍委派
       真正生效的地方。
    2. 拿簽好的 JWT 向 Google OAuth token endpoint 換一個代表該 Workspace 使用者的
       access token。
    3. 用這個 token 呼叫 Gmail API `users.messages.send`。

    若 Workspace Admin Console 尚未把 `GMAIL_MAILER_SA_EMAIL` 的 numeric Client ID
    加入網域範圍委派白名單（安全性 → API 控制項 → 網域範圍委派，scope 見下方
    `GMAIL_SCOPE`），第 2 步的 token 交換會回 `unauthorized_client`——不是程式碼問題，
    是那個手動步驟還沒做，錯誤訊息裡有明確提示。
    """
    import base64
    import time
    import requests
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    import google.auth
    import google.auth.transport.requests as ga_requests

    GMAIL_SCOPE = 'https://www.googleapis.com/auth/gmail.send'
    mailer_sa = CONFIG['GMAIL_MAILER_SA_EMAIL']
    impersonate_user = CONFIG['GMAIL_IMPERSONATE_USER']

    # Step 1：Job 自己的執行身分（ADC）取得能呼叫 IAM Credentials API 的 token
    source_creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    source_creds.refresh(ga_requests.Request())

    now = int(time.time())
    jwt_payload = {
        'iss': mailer_sa,
        'sub': impersonate_user,
        'scope': GMAIL_SCOPE,
        'aud': 'https://oauth2.googleapis.com/token',
        'iat': now,
        'exp': now + 3600,
    }
    sign_resp = requests.post(
        f'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{mailer_sa}:signJwt',
        headers={'Authorization': f'Bearer {source_creds.token}', 'Content-Type': 'application/json'},
        json={'payload': json.dumps(jwt_payload)},
        timeout=15,
    )
    if sign_resp.status_code != 200:
        raise RuntimeError(f'[Gmail] signJwt 失敗（{sign_resp.status_code}）：{sign_resp.text}')
    signed_jwt = sign_resp.json()['signedJwt']

    # Step 2：拿簽好的 JWT 換代表 impersonate_user 的 access token（網域範圍委派生效點）
    token_resp = requests.post(
        'https://oauth2.googleapis.com/token',
        data={'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': signed_jwt},
        timeout=15,
    )
    if token_resp.status_code != 200:
        raise RuntimeError(
            f'[Gmail] 網域範圍委派 token 交換失敗（{token_resp.status_code}）：{token_resp.text}\n'
            f'若訊息含 unauthorized_client：Workspace Admin Console 尚未把 {mailer_sa} 的 '
            f'numeric Client ID 加入網域範圍委派白名單（scope: {GMAIL_SCOPE}）。'
        )
    access_token = token_resp.json()['access_token']

    # Step 3：組信、送 Gmail API
    message = MIMEMultipart('alternative')
    message['to'] = CONFIG['RECIPIENT_EMAIL']
    message['from'] = CONFIG['SENDER_EMAIL']
    message['subject'] = subject
    message.attach(MIMEText(text_fallback, 'plain'))
    message.attach(MIMEText(html_body, 'html'))
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

    send_resp = requests.post(
        'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
        headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'},
        json={'raw': raw},
        timeout=15,
    )
    if send_resp.status_code != 200:
        raise RuntimeError(f'[Gmail] 寄信失敗（{send_resp.status_code}）：{send_resp.text}')
    print(f'[Gmail] 已送出，message id：{send_resp.json().get("id")}')


def _sendgrid_send(subject, html_body, text_fallback):
    """V2.0 舊路徑，保留供切回/對照（EMAIL_PROVIDER=sendgrid）。"""
    if not CONFIG['SENDGRID_API_KEY']:
        print('[SendGrid] 缺少 SENDGRID_API_KEY，跳過寄信')
        return

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    message = Mail(
        from_email=CONFIG['SENDER_EMAIL'],
        to_emails=CONFIG['RECIPIENT_EMAIL'],
        subject=subject,
        html_content=html_body,
        plain_text_content=text_fallback,
    )
    sg = SendGridAPIClient(CONFIG['SENDGRID_API_KEY'])
    resp = sg.send(message)
    print(f'[SendGrid] 狀態碼：{resp.status_code}')


# ────────────────────────────────────────────────────────────
# 輸出 C：LinkedIn（預留，FEATURE_POST_TO_LINKEDIN=false）
# 啟用步驟：
#   1. 建立 LinkedIn Developer App，取得 OAuth Access Token
#   2. 存入 Secret Manager：linkedin-access-token / linkedin-person-urn
#   3. 部署時加 --set-secrets LINKEDIN_ACCESS_TOKEN=linkedin-access-token:latest,...
#   4. 設定環境變數 FEATURE_POST_TO_LINKEDIN=true
# ────────────────────────────────────────────────────────────
def post_to_linkedin(content):
    token = os.environ.get('LINKEDIN_ACCESS_TOKEN', '')
    urn   = os.environ.get('LINKEDIN_PERSON_URN', '')
    if not token or not urn:
        print('[LinkedIn] 缺少 LINKEDIN_ACCESS_TOKEN 或 LINKEDIN_PERSON_URN，跳過')
        return None

    post_text = markdown_to_linkedin(content)[:2950]
    payload = json.dumps({
        'author': f'urn:li:person:{urn}',
        'lifecycleState': 'PUBLISHED',
        'specificContent': {
            'com.linkedin.ugc.ShareContent': {
                'shareCommentary': {'text': post_text},
                'shareMediaCategory': 'NONE',
            },
        },
        'visibility': {'com.linkedin.ugc.MemberNetworkVisibility': 'PUBLIC'},
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://api.linkedin.com/v2/ugcPosts', data=payload,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
                 'X-Restli-Protocol-Version': '2.0.0'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            print(f'[LinkedIn] 發文成功：{result.get("id")}')
            return result.get('id')
    except Exception as e:
        print(f'[LinkedIn] 發文失敗：{e}')
        return None


# ────────────────────────────────────────────────────────────
# 輸出 D：Webhook（預留，FEATURE_POST_TO_WEBHOOK=false）
# 啟用步驟：
#   1. 在 n8n / Make 建立 Webhook，取得 URL
#   2. 存入 Secret Manager：webhook-url
#   3. 部署時加 --set-secrets WEBHOOK_URL=webhook-url:latest
#   4. 設定環境變數 FEATURE_POST_TO_WEBHOOK=true
# Payload: { title, content, linkedInText, gcsUrl, generatedAt }
# ────────────────────────────────────────────────────────────
def post_to_webhook(content, gcs_url):
    webhook_url = os.environ.get('WEBHOOK_URL', '')
    if not webhook_url:
        print('[Webhook] 缺少 WEBHOOK_URL，跳過')
        return

    payload = json.dumps({
        'title':        f'GCP Weekly Digest {_fmt_date(datetime.now())}',
        'content':      content,
        'linkedInText': markdown_to_linkedin(content)[:2950],
        'gcsUrl':       gcs_url or '',
        'generatedAt':  datetime.now(timezone.utc).isoformat(),
    }).encode('utf-8')

    req = urllib.request.Request(
        webhook_url, data=payload,
        headers={'Content-Type': 'application/json'}, method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            print('[Webhook] 已送出')
    except Exception as e:
        print(f'[Webhook] 失敗：{e}')


# ────────────────────────────────────────────────────────────
# Markdown → HTML（Email 用，GCP 藍色主題）
# ────────────────────────────────────────────────────────────
def markdown_to_html(markdown):
    def _unescape(s):
        return s.replace(r'\*', '*').replace(r'\_', '_').replace(r'\#', '#').replace(r'\[', '[').replace(r'\]', ']')
    def _bold(s):
        return re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', s)

    out = []
    for raw in markdown.split('\n'):
        line = _unescape(raw)
        if re.match(r'^\|[-| :]+\|$', line.strip()):
            continue
        if line.startswith('# '):
            out.append(f'<h1 style="color:#202124;font-size:20px;margin:20px 0 6px;font-weight:700">{_bold(line[2:])}</h1>')
        elif line.startswith('## '):
            out.append(f'<h2 style="color:#4285f4;font-size:15px;font-weight:700;margin:20px 0 4px;padding-bottom:4px;border-bottom:2px solid #e8f0fe">{_bold(line[3:])}</h2>')
        elif line.startswith('### '):
            out.append(f'<h3 style="color:#202124;font-size:14px;font-weight:600;margin:12px 0 2px">{_bold(line[4:])}</h3>')
        elif line.startswith('> '):
            out.append(f'<blockquote style="border-left:3px solid #4285f4;margin:8px 0;padding:8px 14px;background:#f8f9fa;color:#444;font-style:italic;border-radius:0 4px 4px 0">{_bold(line[2:])}</blockquote>')
        elif line.startswith('---'):
            out.append('<hr style="border:none;border-top:1px solid #e0e0e0;margin:14px 0">')
        elif line.startswith('- '):
            out.append(f'<li style="margin:2px 0;color:#333;font-size:14px;line-height:1.6">{_bold(line[2:])}</li>')
        elif line.startswith('|'):
            cells = [c.strip() for c in line.split('|') if c.strip()]
            if cells:
                out.append(f'<p style="margin:2px 0;font-size:13px;color:#555">{" &nbsp;|&nbsp; ".join(cells)}</p>')
        elif not line.strip():
            out.append('<div style="height:4px"></div>')
        else:
            out.append(f'<p style="margin:3px 0;color:#333;font-size:14px;line-height:1.6">{_bold(line)}</p>')

    return '\n'.join(out)


# ────────────────────────────────────────────────────────────
# Markdown → LinkedIn 純文字
# ────────────────────────────────────────────────────────────
def markdown_to_linkedin(markdown):
    def _unescape(s):
        return s.replace(r'\*', '*').replace(r'\_', '_').replace(r'\#', '#').replace(r'\[', '[').replace(r'\]', ']')
    def _strip(s):
        return re.sub(r'\*\*?([^*]+)\*\*?', r'\1', s)

    lines = []
    for raw in markdown.split('\n'):
        line = _unescape(raw)
        if re.match(r'^\|[-| :]+\|$', line.strip()):
            continue
        if line.startswith('# '):     lines.append('📋 ' + _strip(line[2:]).upper())
        elif line.startswith('## '):  lines.append('\n【' + _strip(line[3:]) + '】')
        elif line.startswith('### '): lines.append('◆ ' + _strip(line[4:]))
        elif line.startswith('> '):   lines.append(_strip(line[2:]))
        elif line.startswith('---'):  lines.append('─────────────────')
        elif line.startswith('- '):   lines.append('• ' + _strip(line[2:]))
        elif line.startswith('|'):
            cells = [c.strip() for c in line.split('|') if c.strip()]
            if cells: lines.append(' | '.join(cells))
        else: lines.append(_strip(line))

    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


# ────────────────────────────────────────────────────────────
# 工具函式
# ────────────────────────────────────────────────────────────
def _strip_html(text):
    if not text:
        return ''
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def _parse_rss_date(date_str):
    for fmt in ('%a, %d %b %Y %H:%M:%S %z', '%a, %d %b %Y %H:%M:%S GMT', '%Y-%m-%dT%H:%M:%S%z'):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_iso_date(date_str):
    if not date_str:
        return None
    try:
        # Atom 的 updated/published 為 ISO 8601（例：2026-08-26T00:00:00-07:00）
        return datetime.fromisoformat(date_str.strip())
    except ValueError:
        return None


def _fmt_date(dt, fmt='%Y/%m/%d'):
    return dt.strftime(fmt)


def _week_range():
    today = datetime.now()
    return f'{_fmt_date(today - timedelta(days=CONFIG["DAYS_LOOKBACK"]))} - {_fmt_date(today)}'


if __name__ == '__main__':
    main()
