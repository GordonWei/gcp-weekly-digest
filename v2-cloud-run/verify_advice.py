"""
Verification script for the account advice section — run this before
deploying, don't just push straight to prod.

Why this script exists: `account_context.py` was originally written
**without working credentials** (the local `gcloud` token had expired, and
re-authenticating would have switched the default account used by another
project, so re-auth was deliberately avoided at the time). That means a few
things in it were written per the official docs but never actually tested.
This script goes and settles those open questions.

Usage:

    gcloud auth application-default login --account=<YOUR_ACCOUNT>
    GCP_PROJECT_ID=<YOUR_PROJECT_ID> python3 verify_advice.py

It only reads — it won't send email, won't write to GCS, and won't call
Gemini (unless you add --llm).
"""

import os
import sys

PROJECT = os.environ.get('GCP_PROJECT_ID', '')


def main():
    if not PROJECT:
        sys.exit('請設 GCP_PROJECT_ID')

    print(f'專案：{PROJECT}\n')

    # ① Is the package installed
    try:
        from google.cloud import recommender_v1  # noqa: F401
    except ImportError:
        sys.exit('❌ 缺 google-cloud-recommender，先 pip install -r requirements.txt')
    print('① google-cloud-recommender 可匯入 ✅')

    import account_context as ac

    # ② Whether the location wildcard actually works -- this was the biggest
    #    unknown when writing this. The official API reference doesn't say, so
    #    the code tries the wildcard first and falls back to enumerating
    #    locations on failure. This settles the question directly so the unused
    #    path can be deleted afterward.
    from google.cloud import recommender_v1
    client = recommender_v1.RecommenderClient()
    probe  = 'google.compute.address.IdleResourceRecommender'
    try:
        list(client.list_recommendations(
            parent=f'projects/{PROJECT}/locations/-/recommenders/{probe}'))
        print('② location 萬用字元 "-"：可用 ✅ '
              '（可以把 account_context.py 的 fallback 路徑刪掉）')
    except Exception as e:
        print(f'② location 萬用字元 "-"：不可用 ❌ {type(e).__name__}: {e}')
        print(f'   → 會走 fallback 逐一列舉：{ac._FALLBACK_LOCATIONS}')
        print('   ⚠️ 確認這份清單涵蓋你實際有資源的 zone/region，否則會漏。')

    # ③ Do an actual fetch to check permissions and whether there's anything there
    try:
        items = ac.fetch_recommendations(PROJECT)
    except Exception as e:
        print(f'\n③ 抓取失敗 ❌ {type(e).__name__}: {e}')
        print('   常見原因：Recommender API 沒啟用，或缺 roles/recommender.viewer')
        print('   啟用：gcloud services enable recommender.googleapis.com '
              f'--project={PROJECT}')
        return
    print(f'\n③ 抓取成功 ✅ 共 {len(items)} 筆待處理建議')

    by = {}
    for label, _ in items:
        by[label] = by.get(label, 0) + 1
    for label, n in by.items():
        print(f'   - {label}：{n} 筆')

    # ④ What the rendered prompt looks like (without calling the model)
    listing, omitted = ac.format_recommendations(items, 'zh-TW')
    print(f'\n④ 清單 render：{len(listing)} 字元，未列出 {omitted} 筆')
    print('─' * 60)
    print(listing[:1500] or '（空的——代表這個專案目前沒有待處理建議，這是好消息）')
    print('─' * 60)

    # ⑤ To see what the model actually writes, add --llm
    if '--llm' in sys.argv:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import main as digest
        digest.CONFIG['GCP_PROJECT_ID'] = PROJECT
        section, warn = ac.build_advice_section('zh-TW', PROJECT, digest._invoke_gemini)
        print(f'\n⑤ warn: {warn or "(none)"}')
        print(section)
    else:
        print('\n⑤ 想看 Gemini 實際寫出來的內容，加上 --llm 再跑一次')


if __name__ == '__main__':
    main()
