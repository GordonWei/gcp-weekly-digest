"""
GCP Weekly Digest — Cloud Run Job (V2)

One email answers two questions: what happened in GCP this week, and what it
means for what I'm actually running.

The first half aggregates GCP Release Notes + Blog posts and hands them to
Vertex AI Gemini to write a categorized weekly digest. The second half reads
this project's own Cloud Recommender findings and appends "what's worth
acting on" (see account_context.py, gated by FEATURE_ACCOUNT_ADVICE). Release
notes alone can't cover that second half, because they have no idea what
you're actually running.

Mirrors the sister project AWS Weekly Digest
(github.com/GordonWei/aws-weekly-digest) — code structure, env var naming, and
output format are kept as symmetric as possible to make an "AWS vs GCP"
comparison piece easier to write later.

Key differences between V2 and V1 (Google Apps Script):
- Runtime: Apps Script -> Cloud Run Job (no longer tied to Google Workspace)
- AI SDK: Vertex AI REST + OAuth -> google-genai SDK + enterprise=True (direct
  IAM auth) (the generative_models module in google-cloud-aiplatform was
  deprecated on 2025-06-24 and will be removed on 2026-06-24; the official
  recommended path is now google-genai — verified against the pip package
  source that Client(enterprise=True, ...) is the current parameter and
  vertexai=True is a compatibility alias)
- Model: gemini-2.5-flash-lite (V1) -> gemini-3.1-flash-lite (2.5-flash-lite
  is being fully retired on 2026-10-16/20; staying on the V1 model would mean
  the weekly schedule doesn't survive two more months)
- Archive: Google Drive -> GCS
- Email: GmailApp (tied to a Google account) -> SendGrid (V2.0, 2026-08-27) ->
  Gmail API domain-wide delegation (V2.1, 2026-09-03, see README — the
  SendGrid account was permanently denied reactivation by the provider)
- Blog RSS source: the V1 feed https://cloud.google.com/blog/rss/ is dead (now
  returns HTML instead of XML, verified with curl); switched to the still-live
  https://cloudblog.withgoogle.com/rss/

V2.1 (2026-09-03): switched the email channel again, SendGrid -> Gmail API +
Service Account domain-wide delegation (impersonating your own Workspace/
Gmail account). Reason: the SendGrid account was permanently denied
reactivation after prolonged "deferred" status (see README), and Mailgun
requires a credit card on file which we'd rather avoid. If the recipient
domain's MX already points to Google, domain-wide delegation removes the need
to manage a third-party email service account. The EMAIL_PROVIDER env var
keeps the SendGrid path (code not removed); the default is now gmail.

**Keyless (added same day)**: the original design read a mounted SA JSON key
file, but deployment hit the org policy
`constraints/iam.disableServiceAccountKeyCreation` (`gcp-weekly-digest-mailer-sa`
can't create a key — this is a policy block, not an IAM permission gap).
Replaced with a keyless flow that never writes a key file to disk: the job's
runtime identity `gcp-weekly-digest-sa` (using ADC on Cloud Run) calls the IAM
Credentials API to signJwt as `gcp-weekly-digest-mailer-sa` (granted
`roles/iam.serviceAccountTokenCreator` on that SA resource, not at the project
level), producing a JWT with `sub=GMAIL_IMPERSONATE_USER`. That JWT is then
exchanged with the Google OAuth token endpoint for an access token
representing that user (RFC 7523 JWT-bearer flow) — that access token is what
actually carries the domain-wide delegation grant. No key file ever touches
disk, and no org policy exception is needed. See `_gmail_send()` and the
README for details.

V2.2: added `DIGEST_LANGUAGE` (`en` / `zh-TW`, default `en`) — following the
same approach as AWS Weekly Digest, one setting controls the digest body
(`_prompt_en`/`_prompt_zh_tw`), the account advice section
(`account_context.py` already supported both languages), and the email's
static strings (`_EMAIL_STRINGS`), so an English digest never ends up wrapped
in a Chinese-labelled email.
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

# ── Config (env-var driven, mirrors the AWS Lambda version's style) ──────
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

    # ── Email channel: gmail (V2.1 default, keyless domain-wide delegation) or sendgrid (kept, V2.0 legacy path) ──
    'EMAIL_PROVIDER':        os.environ.get('EMAIL_PROVIDER', 'gmail'),
    'GMAIL_MAILER_SA_EMAIL': os.environ.get(
        'GMAIL_MAILER_SA_EMAIL',
        f"gcp-weekly-digest-mailer-sa@{os.environ.get('GCP_PROJECT_ID', '')}.iam.gserviceaccount.com",
    ),
    'GMAIL_IMPERSONATE_USER': os.environ.get('GMAIL_IMPERSONATE_USER', ''),

    # ── Output channel toggles (false = reserved, code is already in place) ────────────
    'FEATURES': {
        'SEND_EMAIL':             os.environ.get('FEATURE_SEND_EMAIL',      'true') == 'true',
        'EMBED_CONTENT_IN_EMAIL': os.environ.get('FEATURE_EMBED_CONTENT',   'true') == 'true',
        'SAVE_TO_GCS':            os.environ.get('FEATURE_SAVE_TO_GCS',     'true') == 'true',
        'POST_TO_LINKEDIN':       os.environ.get('FEATURE_POST_TO_LINKEDIN', 'false') == 'true',
        'POST_TO_WEBHOOK':        os.environ.get('FEATURE_POST_TO_WEBHOOK',  'false') == 'true',
        # The digest's other half: looking back at what's actually worth acting on in
        # this project (see account_context.py). Off by default because it needs
        # Recommender read access, not because it matters less.
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
# Main entry point
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
                # Deliberately loud. Losing this section shouldn't take down the whole
                # digest, but it also shouldn't vanish silently — "section missing" and
                # "nothing to report this week" look identical in the email.
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
# Data fetch: GCP Release Notes (Atom Feed)
# ────────────────────────────────────────────────────────────
def fetch_gcp_release_notes():
    try:
        req = urllib.request.Request(RELEASE_NOTES_URL, headers={'User-Agent': 'GCP-Weekly-Digest/2.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8', errors='replace')

        # The Atom feed is well-formed XML with the namespace correctly declared on
        # the root element, so ElementTree's namespace-aware parsing works directly —
        # no need for regex-based namespace stripping (the AWS version's Blog RSS
        # once had incomplete manual stripping that made ElementTree throw an
        # unbound-prefix error and get silently swallowed; this sidesteps that class
        # of bug entirely).
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
# Data fetch: GCP Blog RSS (fails silently, skipped)
# ────────────────────────────────────────────────────────────
def fetch_gcp_blog_posts():
    try:
        req = urllib.request.Request(BLOG_RSS_URL, headers={'User-Agent': 'GCP-Weekly-Digest/2.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8', errors='replace')

        root    = ET.fromstring(raw)  # well-formed RSS 2.0, namespace correctly declared, no stripping needed
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
# Gemini invocation (google-genai SDK, Enterprise/Vertex mode, direct IAM auth)
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
    """Shared Gemini call path for both the digest body and the account advice section."""
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
# Output A: GCS archive
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
# Output B: Email (Gmail API domain-wide delegation, V2.1 default; SendGrid kept as the V2.0 legacy path)
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
    """Picks the send path based on EMAIL_PROVIDER; defaults to gmail (V2.1), keeps sendgrid (V2.0) for fallback/comparison."""
    if CONFIG['EMAIL_PROVIDER'] == 'sendgrid':
        _sendgrid_send(subject, html_body, text_fallback)
    else:
        _gmail_send(subject, html_body, text_fallback)


def _gmail_send(subject, html_body, text_fallback):
    """Gmail API + Service Account domain-wide delegation, keyless.

    Never writes an SA JSON key file to disk — the org policy
    `constraints/iam.disableServiceAccountKeyCreation` blocks creating SA keys in
    this project outright, so this uses short-lived token exchange instead, which
    has a smaller exposure surface than a standing key file.

    Flow (RFC 7523 JWT-bearer flow):
    1. The job's runtime identity (`gcp-weekly-digest-sa`, using ADC on Cloud Run)
       calls the IAM Credentials API to signJwt as `gcp-weekly-digest-mailer-sa` —
       this step requires the job SA to hold `roles/iam.serviceAccountTokenCreator`
       on the mailer SA (granted at the SA resource level, not project level). The
       resulting JWT carries `sub=GMAIL_IMPERSONATE_USER`; this `sub` claim is
       where domain-wide delegation actually takes effect.
    2. The signed JWT is exchanged with the Google OAuth token endpoint for an
       access token representing that Workspace user.
    3. That token is used to call the Gmail API `users.messages.send`.

    If the Workspace Admin Console hasn't added `GMAIL_MAILER_SA_EMAIL`'s numeric
    Client ID to the domain-wide delegation allowlist (Security -> API Controls ->
    Domain-wide Delegation, scope is `GMAIL_SCOPE` below), the step-2 token
    exchange returns `unauthorized_client` — that's not a code bug, it's just that
    manual step not done yet; the error message spells this out.
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

    # Step 1: the job's own runtime identity (ADC) gets a token that can call the IAM Credentials API
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

    # Step 2: exchange the signed JWT for an access token representing impersonate_user (where domain-wide delegation kicks in)
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

    # Step 3: assemble the message and send via the Gmail API
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
    """V2.0 legacy path, kept for fallback/comparison (EMAIL_PROVIDER=sendgrid)."""
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
# Output C: LinkedIn (reserved, FEATURE_POST_TO_LINKEDIN=false)
# To enable:
#   1. Create a LinkedIn Developer App and get an OAuth Access Token
#   2. Store in Secret Manager: linkedin-access-token / linkedin-person-urn
#   3. Add at deploy time: --set-secrets LINKEDIN_ACCESS_TOKEN=linkedin-access-token:latest,...
#   4. Set the env var FEATURE_POST_TO_LINKEDIN=true
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
# Output D: Webhook (reserved, FEATURE_POST_TO_WEBHOOK=false)
# To enable:
#   1. Create a webhook in n8n / Make and get the URL
#   2. Store in Secret Manager: webhook-url
#   3. Add at deploy time: --set-secrets WEBHOOK_URL=webhook-url:latest
#   4. Set the env var FEATURE_POST_TO_WEBHOOK=true
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
# Markdown -> HTML (for email, GCP blue theme)
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
# Markdown -> LinkedIn plain text
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
# Helper functions
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
        # Atom's updated/published fields are ISO 8601 (e.g. 2026-08-26T00:00:00-07:00)
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
