"""
Account usage advice section — the digest's other half.

The first half of the digest tells you what happened in GCP this week; this
is the second half: what's worth acting on in your own project. Release
notes can't answer that, because they have no idea what you're actually
running — that's the whole reason this module exists, not just "making the
digest prompt a bit fancier."

Only runs when FEATURE_ACCOUNT_ADVICE is on, because it needs Recommender
read access. That's just the deployment default, not a statement that it
matters less.

──────────────────────────────────────────────────────────────
Deliberate differences from the AWS version (aws-weekly-digest/account_context.py)
──────────────────────────────────────────────────────────────

On the AWS side I had to write `_FLAGGED_PATTERNS` myself: string-matching
against Cost Explorer usage type names to pick out items like
`PublicIPv4:IdleAddress` and `NatGateway-Hours`, where the name itself is
basically the conclusion. That approach exists because AWS has no free
equivalent (Trusted Advisor's cost checks require Business support).

GCP has the Recommender API — **Google has already done the detection
work**, and it's far more thorough than my five string patterns (idle VMs,
idle disks, idle IPs, machine-type rightsizing, CUDs, idle projects).
Porting the AWS approach over here would just mean reinventing a worse
version of what Google already built.

So the principle stays the same, only the implementation changes:
**detection is handled by something deterministic; explaining it is left to
the model.** On AWS, "something deterministic" is my string matching; on GCP
it's Google's API. What the model does on both sides is identical —
prioritize, explain clearly, and be honest when the dollar amounts are small.
"""

import os

# ⚠️ Requires the extra dependency google-cloud-recommender (see requirements.txt)
from google.cloud import recommender_v1

# Cost/idle-resource recommenders. IDs taken from the official list:
# https://docs.cloud.google.com/recommender/docs/recommenders
_RECOMMENDERS = (
    ('google.compute.address.IdleResourceRecommender',      '閒置的靜態 IP 位址'),
    ('google.compute.disk.IdleResourceRecommender',         '閒置的永久磁碟'),
    ('google.compute.instance.IdleResourceRecommender',     '閒置的 VM'),
    ('google.compute.instance.MachineTypeRecommender',      'VM 機型過大（rightsizing）'),
    ('google.compute.commitment.UsageCommitmentRecommender', '可用承諾使用折扣（CUD）省錢'),
    ('google.resourcemanager.projectUtilization.Recommender', '整個專案幾乎沒在用'),
)

# Location wildcard "-": **tested, doesn't work** (2026-09-01).
# The API returns a flat `400 InvalidArgument: Invalid location: -` — a clear
# rejection, not a permissions issue — so the original "try the wildcard first,
# fall back to enumerating locations" dual path has been removed; only the
# enumeration path remains.
#
# ⚠️ The first verification attempt actually returned 403, which looked like
# "the wildcard isn't supported" but was really the quota project not being
# set plus the API not being enabled. The two failure modes look alike; drawing
# a conclusion at that point would have gotten the right answer for the wrong
# reason.

# Locations to query. Most recommenders scope to a zone or region, and sweeping
# all 100+ zones would blow up the call count, so this only lists locations
# where this project actually has resources, and is adjustable via an env var.
#
# ⚠️ **Missing a single location here means silently missing every
# recommendation for it.** During 2026-09-01 testing, the original default was
# missing `us-east1-c` — which happened to be where this project's only VM and
# only disk live, so at the time the whole list scanned zero compute
# resources. Remember to come back and add locations here when adding
# resources to a new region.
_FALLBACK_LOCATIONS = [
    s.strip() for s in os.environ.get(
        'ADVICE_LOCATIONS',
        'global,asia-east1,asia-east1-a,asia-east1-b,asia-east1-c,'
        'us-central1,us-central1-a,us-east1,us-east1-c',
    ).split(',') if s.strip()
]

# Max items to take per recommender, so a single category doesn't flood the prompt.
MAX_PER_RECOMMENDER = int(os.environ.get('ADVICE_MAX_PER_RECOMMENDER', '10'))

# Character cap for the whole listing. Truncate past this, and **explicitly say
# in the prompt how many items were cut** — a silently truncated list reads
# identically to a complete one, which is a lesson learned from the AWS version.
MAX_LISTING_CHARS = int(os.environ.get('ADVICE_MAX_LISTING_CHARS', '12000'))


def _money_to_float(money):
    """google.type.Money -> float. Savings are negative in the API; kept as-is here, not made absolute."""
    if not money:
        return 0.0
    return float(getattr(money, 'units', 0) or 0) + float(getattr(money, 'nanos', 0) or 0) / 1e9


def _fetch_one(client, project_id, recommender_id, location):
    parent = (f'projects/{project_id}/locations/{location}'
              f'/recommenders/{recommender_id}')
    return list(client.list_recommendations(parent=parent))


def fetch_recommendations(project_id):
    """Returns [(recommender label, recommendation)], only the ones not yet resolved.

    Raises on failure; it's up to build_advice_section to decide how to handle it.

    🔴 The except clauses here are deliberately split into two categories and must
    not be merged back into one bare except (fixed 2026-09-01):

    "This recommender doesn't exist at this location" is expected (happens when a
    zone-scoped recommender is queried at a region), so it's fine to skip. But
    "no permission" or "API not enabled" is not expected — if those get skipped
    too, the whole batch silently returns an empty list, and the caller then
    prints "no open recommendations right now." **Insufficient permissions and
    genuinely having no recommendations look exactly the same in the output.**

    This isn't a hypothetical concern: on the first real test on 2026-09-01, the
    quota project wasn't set and the API wasn't enabled, causing all 42
    (recommender x location) combinations to 403 — yet the script reported
    "fetch succeeded, 0 items."
    """
    from google.api_core import exceptions as gexc

    # These mean "this combination just has nothing to query" — expected, skip.
    _SKIPPABLE = (gexc.InvalidArgument, gexc.NotFound)
    # These mean "the environment isn't set up right" — must be allowed to blow up,
    # never silently treated as "no recommendations."
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
                # Uncategorized error: don't swallow it, don't abort the whole batch,
                # but make sure it's visible in the logs.
                print(f'[advice] ⚠️ {recommender_id} @ {location}: '
                      f'{type(e).__name__}: {e}')

        for r in recs:
            state = getattr(getattr(r, 'state_info', None), 'state', None)
            # Only recommendations that haven't been accepted/dismissed yet; no need
            # to nag about ones already handled every single week.
            if state is not None and state != recommender_v1.RecommendationStateInfo.State.ACTIVE:
                continue
            found.append((label, r))

    total = len(_RECOMMENDERS) * len(_FALLBACK_LOCATIONS)
    print(f'[advice] 掃過 {len(_RECOMMENDERS)} 個 recommender × '
          f'{len(_FALLBACK_LOCATIONS)} 個 location，'
          f'{skipped} 個組合不適用，取得 {len(found)} 筆建議')

    # Last-resort guard: if **every single** combination was skipped, that's not
    # "no recommendations," it's "never actually queried anything successfully" —
    # most likely _FALLBACK_LOCATIONS is entirely wrong. In that case, returning
    # an empty list would make the caller print "no open recommendations right
    # now," which is the same shape of bug as the old bare-except problem above.
    if total and skipped == total:
        raise RuntimeError(
            f'{total} 個 (recommender × location) 組合全部不適用，'
            f'沒有任何一次呼叫成功——請檢查 ADVICE_LOCATIONS：{_FALLBACK_LOCATIONS}')

    return found


def format_recommendations(items, lang):
    """Renders the recommendations into a listing for the prompt, returns (text, omitted_count)."""
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
            # Negative means savings; flip to a positive "estimated monthly savings" for readability.
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
    """Returns (markdown section, warning). Never raises.

    A Recommender failure shouldn't cost you the whole digest — the digest is the
    product, this section is a bonus. But this repo's sister project already got
    burned once by this (an except meant to guard against errors ended up
    swallowing a parser error, silently mailing out a permanently blank section
    for weeks), so failures here are returned as a warning for the caller to
    print, rather than being silently absorbed in this function.

    "Never raises" covers the model call too. It used to cover only the
    Recommender fetch, so a failed or cut-off Gemini reply for this section
    sent the error email in place of the whole digest.
    """
    try:
        return _build_advice_section(lang, project_id, invoke_llm)
    except Exception as e:                                      # noqa: BLE001
        return '', f'account advice skipped: {type(e).__name__}: {e}'


def _build_advice_section(lang, project_id, invoke_llm):
    builder = _PROMPTS.get(lang) or _PROMPTS['zh-TW']

    if not project_id:
        return '', 'account advice skipped: 沒有 GCP_PROJECT_ID'

    items = fetch_recommendations(project_id)

    if not items:
        # This is good news, not a failure; but it still needs to be said, otherwise
        # "no section" and "nothing this week" look identical in the email.
        heading = '## 你的專案：優化與改善建議' if lang != 'en' else '## Your project: what to improve'
        body    = ('Google Cloud Recommender 目前對這個專案沒有任何待處理建議。'
                   if lang != 'en' else
                   'Google Cloud Recommender currently has no open recommendations for this project.')
        return f'\n\n---\n\n{heading}\n\n{body}\n', ''

    listing, omitted = format_recommendations(items, lang)
    heading = '## 你的專案：優化與改善建議' if lang != 'en' else '## Your project: what to improve'
    body    = invoke_llm(builder(listing, project_id, omitted))
    return f'\n\n---\n\n{heading}\n\n{body}\n', ''
