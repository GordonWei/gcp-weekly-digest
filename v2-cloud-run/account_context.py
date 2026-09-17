"""
帳號用量建議區段 —— 週報的另一半。

週報的前半告訴你 GCP 這週出了什麼；這是後半：你自己的專案裡，有什麼值得處理。
release notes 回答不了後半，因為它不知道你手上有什麼——這就是這個模組存在的理由，
它不是「把週報 prompt 寫得更漂亮一點」。

只有在 FEATURE_ACCOUNT_ADVICE 打開時才會跑，因為它需要 Recommender 的讀取權限。
那是部署預設值，不是說它比較不重要。

──────────────────────────────────────────────────────────────
與 AWS 版（aws-weekly-digest/account_context.py）刻意不同的地方
──────────────────────────────────────────────────────────────

AWS 那邊我得自己寫 `_FLAGGED_PATTERNS`：從 Cost Explorer 的 usage type 名稱裡用字串
比對挑出 `PublicIPv4:IdleAddress`、`NatGateway-Hours` 這類「名字本身就是結論」的項目。
會那樣做是因為 AWS 沒有免費的等價品（Trusted Advisor 的成本檢查要 Business support）。

GCP 有 Recommender API，**Google 自己就把偵測做好了**，而且比我那五條字串比對完整得多
（閒置 VM／閒置磁碟／閒置 IP／機型 rightsizing／CUD／閒置專案）。把 AWS 的做法照搬過來，
等於自己重造一個比 Google 差的版本。

所以原則沒變、實作換了：**偵測交給確定性的東西，解釋才交給模型。**
在 AWS 上「確定性的東西」是我的字串比對；在 GCP 上是 Google 的 API。
模型在兩邊做的事情完全一樣——排優先順序、講清楚為什麼、以及誠實說出金額很小的時候。
"""

import os

# ⚠️ 需要新增相依套件 google-cloud-recommender（見 requirements.txt）
from google.cloud import recommender_v1

# 成本／閒置資源相關的 recommender。ID 取自官方清單：
# https://docs.cloud.google.com/recommender/docs/recommenders
_RECOMMENDERS = (
    ('google.compute.address.IdleResourceRecommender',      '閒置的靜態 IP 位址'),
    ('google.compute.disk.IdleResourceRecommender',         '閒置的永久磁碟'),
    ('google.compute.instance.IdleResourceRecommender',     '閒置的 VM'),
    ('google.compute.instance.MachineTypeRecommender',      'VM 機型過大（rightsizing）'),
    ('google.compute.commitment.UsageCommitmentRecommender', '可用承諾使用折扣（CUD）省錢'),
    ('google.resourcemanager.projectUtilization.Recommender', '整個專案幾乎沒在用'),
)

# location 萬用字元 "-"：**實測過了，不能用**（2026-09-01）。
# API 直接回 `400 InvalidArgument: Invalid location: -`，是明確的拒絕而不是權限問題，
# 所以原本「先試萬用字元、失敗退回逐一列舉」的雙路徑已經拿掉，只留逐一列舉。
#
# ⚠️ 第一次驗證時它回的是 403，看起來像「萬用字元不可用」其實是 quota project 沒設 +
# API 沒啟用。兩種失敗長得很像，如果那時就下結論會得到對的答案配錯的理由。

# 要查的 location。多數 recommender 的 location 是 zone 或 region，把 100 多個 zone
# 全掃一遍會讓呼叫次數爆炸，所以這裡只列這個專案實際有資源的，並開環境變數可調。
#
# ⚠️ **這份清單漏一個 location，就等於安靜地漏掉那裡的所有建議。** 2026-09-01 實測時
# 原本的預設值就漏了 `us-east1-c`——而那是這個專案唯一一台 VM 跟唯一一顆
# 磁碟所在的地方，等於當時整份清單掃不到任何 compute 資源。加資源到新區域時要回來補這裡。
_FALLBACK_LOCATIONS = [
    s.strip() for s in os.environ.get(
        'ADVICE_LOCATIONS',
        'global,asia-east1,asia-east1-a,asia-east1-b,asia-east1-c,'
        'us-central1,us-central1-a,us-east1,us-east1-c',
    ).split(',') if s.strip()
]

# 一個 recommender 最多取幾筆，避免單一類別灌爆 prompt。
MAX_PER_RECOMMENDER = int(os.environ.get('ADVICE_MAX_PER_RECOMMENDER', '10'))

# 整份清單的字元上限。超過就截斷，並且**在 prompt 裡明講截了幾筆**——
# 安靜截短的清單讀起來跟完整的一模一樣，這是 AWS 版學到的教訓。
MAX_LISTING_CHARS = int(os.environ.get('ADVICE_MAX_LISTING_CHARS', '12000'))


def _money_to_float(money):
    """google.type.Money → float。省錢在 API 裡是負數，這裡保持原樣不取絕對值。"""
    if not money:
        return 0.0
    return float(getattr(money, 'units', 0) or 0) + float(getattr(money, 'nanos', 0) or 0) / 1e9


def _fetch_one(client, project_id, recommender_id, location):
    parent = (f'projects/{project_id}/locations/{location}'
              f'/recommenders/{recommender_id}')
    return list(client.list_recommendations(parent=parent))


def fetch_recommendations(project_id):
    """回傳 [(recommender 中文說明, recommendation)]，只取還沒被處理掉的。

    會拋例外；由 build_advice_section 決定怎麼處理。

    🔴 這裡的 except 分兩類，不可以再合併回一個 bare except（2026-09-01 修）：

    「某個 recommender 在某個 location 不存在」是正常的（zone 級的 recommender 拿 region
    去問就會這樣），跳過即可。但「沒權限」「API 沒啟用」不是正常的——那種錯誤如果也一起
    跳過，整批會回傳空 list，而上層看到空 list 會印出「目前沒有任何待處理建議」。
    **權限不足和真的沒建議，輸出會長得一模一樣。**

    這不是假設性的擔憂：2026-09-01 第一次實測時就是這樣，quota project 沒設 + API 沒啟用
    讓 42 個 (recommender × location) 組合全部 403，腳本卻報告「抓取成功 ✅ 共 0 筆」。
    """
    from google.api_core import exceptions as gexc

    # 這幾類代表「這個組合問不到東西」，屬預期內，跳過。
    _SKIPPABLE = (gexc.InvalidArgument, gexc.NotFound)
    # 這幾類代表「環境沒設好」，必須讓它炸出來、不能靜靜當成沒建議。
    _FATAL = (gexc.PermissionDenied, gexc.Unauthenticated, gexc.ResourceExhausted)

    client = recommender_v1.RecommenderClient()
    found  = []
    skipped = 0

    for recommender_id, label in _RECOMMENDERS:
        recs = []
        for location in _FALLBACK_LOCATIONS:
            try:
                recs.extend(_fetch_one(client, project_id, recommender_id, location))
            except _FATAL:
                raise
            except _SKIPPABLE:
                skipped += 1
            except Exception as e:                              # noqa: BLE001
                # 沒歸類到的錯誤：不吞掉，也不中斷整批，但要在 log 裡看得見。
                print(f'[advice] ⚠️ {recommender_id} @ {location}: '
                      f'{type(e).__name__}: {e}')

        for r in recs:
            state = getattr(getattr(r, 'state_info', None), 'state', None)
            # 只要還沒被接受／關閉的建議；已處理過的沒必要每週再唸一次。
            if state is not None and state != recommender_v1.RecommendationStateInfo.State.ACTIVE:
                continue
            found.append((label, r))

    total = len(_RECOMMENDERS) * len(_FALLBACK_LOCATIONS)
    print(f'[advice] 掃過 {len(_RECOMMENDERS)} 個 recommender × '
          f'{len(_FALLBACK_LOCATIONS)} 個 location，'
          f'{skipped} 個組合不適用，取得 {len(found)} 筆建議')

    # 最後一道守衛：如果**每一個**組合都被跳過，那不是「沒有建議」，是「一次都沒真的查到」
    # ——多半代表 _FALLBACK_LOCATIONS 整份寫錯了。這種情況下回傳空 list 會讓上層印出
    # 「目前沒有任何待處理建議」，跟前面那個 bare except 的老問題是同一個形狀。
    if total and skipped == total:
        raise RuntimeError(
            f'{total} 個 (recommender × location) 組合全部不適用，'
            f'沒有任何一次呼叫成功——請檢查 ADVICE_LOCATIONS：{_FALLBACK_LOCATIONS}')

    return found


def format_recommendations(items, lang):
    """把建議render成 prompt 用的清單，回傳 (text, omitted_count)。"""
    zh = lang != 'en'
    by_label = {}
    for label, r in items:
        by_label.setdefault(label, []).append(r)

    lines, used, omitted = [], 0, 0
    for label, recs in by_label.items():
        recs = sorted(recs, key=lambda r: _money_to_float(
            getattr(getattr(getattr(r, 'primary_impact', None), 'cost_projection', None), 'cost', None)))
        head = f'### {label}（{len(recs)} 筆）' if zh else f'### {label} ({len(recs)})'
        block = [head]
        for r in recs[:MAX_PER_RECOMMENDER]:
            cost = _money_to_float(
                getattr(getattr(getattr(r, 'primary_impact', None), 'cost_projection', None), 'cost', None))
            # 負數代表省錢；轉成正的「每月可省」比較好讀。
            saving = f'{-cost:.2f}' if cost < 0 else f'{cost:.2f}'
            money  = (f'（每月約 {saving} {getattr(getattr(getattr(r, "primary_impact", None), "cost_projection", None), "cost", None).currency_code if cost else ""}）'
                      if cost else '')
            desc = (getattr(r, 'description', '') or '').strip()
            prio = getattr(r, 'priority', '')
            sub  = getattr(r, 'recommender_subtype', '')
            block.append(f'- {desc} {money}'.rstrip())
            if sub or prio:
                block.append(f'    subtype={sub} priority={prio}')
        if len(recs) > MAX_PER_RECOMMENDER:
            more = len(recs) - MAX_PER_RECOMMENDER
            block.append(f'    （另有 {more} 筆同類未列出）' if zh else f'    ({more} more not listed)')
            omitted += more

        rendered = '\n'.join(block)
        if used + len(rendered) > MAX_LISTING_CHARS and lines:
            omitted += sum(len(v) for v in by_label.values()) - used
            break
        lines.append(rendered)
        used += len(rendered) + 1

    return '\n\n'.join(lines), omitted


def _prompt_zh_tw(listing, project_id, omitted):
    trunc = (f'\n⚠️ 清單超出長度上限，有 {omitted} 筆未列出——**不要對未列出的項目提出建議**。\n'
             if omitted else '')
    return f"""你是一位資深 GCP 解決方案架構師，正在替一位技術主管檢視他自己的專案（`{project_id}`）。

## Google Cloud Recommender 目前提出的建議

{listing}
{trunc}
### 怎麼讀這份資料

- 這些**不是我推測的，是 Google Cloud Recommender API 直接產生的**，每一筆都對應到實際存在的資源。
- 金額是 Recommender 自己估的每月影響。**金額小不代表不值得處理**——閒置資源的問題常常
  不在當下的錢，而在它會一直存在下去，以及它代表某個東西被建立後就沒人管了。
- 你看得到「有哪些建議」，但**看不到這些資源實際上在做什麼**，不要假裝知道。

---

請把這些整理成給他看的一段。要求：

- **挑 3-5 條最值得動手的，依投入產出比排序**，不要每一筆都列一遍。
- 每條格式：

**[類別] 一句話結論**
- **Recommender 說了什麼**：[引用具體建議與金額]
- **建議動作**：[具體到可以直接執行的程度]
- **預期效果**：[省錢／降風險／省維運時間，擇一講清楚]

- **相關的建議要合併講**。例如同一台 VM 同時出現在「閒置 VM」和「機型過大」，那是一件事不是兩件。
- 如果金額全部都很小，**就誠實說金額很小**，不要硬把它講成省錢機會。
  值得講的是「這些東西為什麼會留在這裡」。
- 如果清單是空的或都不重要，就直說本週沒有值得處理的項目，不要硬湊。
- 直接輸出內容，不要前言、不要自我介紹、不要結尾客套。
- 使用繁體中文，標題不要使用 emoji。
"""


def _prompt_en(listing, project_id, omitted):
    trunc = (f'\n⚠️ The list exceeded the length budget; {omitted} items are omitted — '
             f'**do not give advice about the omitted ones**.\n' if omitted else '')
    return f"""You are a senior GCP solutions architect reviewing an engineering
leader's own project (`{project_id}`).

## What Google Cloud Recommender currently suggests

{listing}
{trunc}
### How to read this

- These are **not inferences — they come straight from the Google Cloud
  Recommender API**, and each maps to a resource that really exists.
- Amounts are Recommender's own monthly estimates. **A small amount does not
  mean it is not worth doing** — the problem with an idle resource is usually
  not today's cost but that it will stay there, and that it marks something
  created and then forgotten.
- You can see what is recommended. You cannot see what these resources are
  actually for, so do not pretend to.

---

Turn this into one section for them. Rules:

- **Pick the 3-5 highest-leverage items, ordered by return on effort.** Do not
  list everything.
- Format each as:

**[Category] one-line conclusion**
- **What Recommender says**: [quote the specific recommendation and amount]
- **Suggested action**: [concrete enough to act on]
- **Expected result**: [cost, risk, or maintenance time — pick one, be clear]

- **Merge related recommendations.** One VM appearing under both "idle VM" and
  "oversized machine type" is one problem, not two.
- If every amount is small, **say so plainly** rather than dressing it up as a
  savings opportunity. What is worth discussing is why these things are still here.
- If the list is empty or nothing matters, say there is nothing worth acting on
  this week. Do not pad.
- Output the section directly: no preamble, no sign-off.
"""


_PROMPTS = {'zh-TW': _prompt_zh_tw, 'en': _prompt_en}


def build_advice_section(lang, project_id, invoke_llm):
    """回傳 (markdown 區段, warning)。不會拋例外。

    Recommender 掛掉不該害你收不到週報——週報是產品，這一段是加值。但這個 repo 的姊妹
    專案已經吃過一次虧（except 是為了擋錯而寫，結果吞掉 parser error，連續幾週寄出一個
    永遠空白的區段），所以失敗會以 warning 回傳給呼叫端印出來，不在這裡安靜吸收掉。
    """
    builder = _PROMPTS.get(lang) or _PROMPTS['zh-TW']

    if not project_id:
        return '', 'account advice skipped: 沒有 GCP_PROJECT_ID'

    try:
        items = fetch_recommendations(project_id)
    except Exception as e:                                      # noqa: BLE001
        return '', f'account advice skipped: {type(e).__name__}: {e}'

    if not items:
        # 這是好消息，不是失敗；但還是要說出來，否則「沒有區段」跟「這週沒東西」
        # 在信裡看起來一模一樣。
        heading = '## 你的專案：優化與改善建議' if lang != 'en' else '## Your project: what to improve'
        body    = ('Google Cloud Recommender 目前對這個專案沒有任何待處理建議。'
                   if lang != 'en' else
                   'Google Cloud Recommender currently has no open recommendations for this project.')
        return f'\n\n---\n\n{heading}\n\n{body}\n', ''

    listing, omitted = format_recommendations(items, lang)
    heading = '## 你的專案：優化與改善建議' if lang != 'en' else '## Your project: what to improve'
    body    = invoke_llm(builder(listing, project_id, omitted))
    return f'\n\n---\n\n{heading}\n\n{body}\n', ''
