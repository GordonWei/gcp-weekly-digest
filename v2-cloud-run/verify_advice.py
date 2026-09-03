"""
帳號建議區段的驗證腳本 —— 部署前跑這支，不要直接推上去。

寫這支的原因：`account_context.py` 最初是在**沒有可用憑證**的情況下寫的
（本機當時 `gcloud` token 過期，而重新登入會切換掉別的專案正在用的 default 帳號，
所以當下刻意不動認證）。也就是說裡面有幾件事是照官方文件寫的、但沒有實測過。
這支腳本就是去把那幾件事問清楚。

用法：

    gcloud auth application-default login --account=<YOUR_ACCOUNT>
    GCP_PROJECT_ID=<YOUR_PROJECT_ID> python3 verify_advice.py

它只做讀取，不會寄信、不會寫 GCS、不會呼叫 Gemini（除非加 --llm）。
"""

import os
import sys

PROJECT = os.environ.get('GCP_PROJECT_ID', '')


def main():
    if not PROJECT:
        sys.exit('請設 GCP_PROJECT_ID')

    print(f'專案：{PROJECT}\n')

    # ① 套件裝了沒
    try:
        from google.cloud import recommender_v1  # noqa: F401
    except ImportError:
        sys.exit('❌ 缺 google-cloud-recommender，先 pip install -r requirements.txt')
    print('① google-cloud-recommender 可匯入 ✅')

    import account_context as ac

    # ② location 萬用字元到底能不能用 —— 這是寫的時候最大的未知數。
    #    官方 API 參考沒有講，所以程式裡是「先試萬用字元、失敗退回逐一列舉」。
    #    這裡直接把答案問出來，確認之後可以把沒走到的那條路徑刪掉。
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

    # ③ 實際抓一次，看有沒有權限、以及有沒有東西
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

    # ④ render 出來的 prompt 長什麼樣（不呼叫模型）
    listing, omitted = ac.format_recommendations(items, 'zh-TW')
    print(f'\n④ 清單 render：{len(listing)} 字元，未列出 {omitted} 筆')
    print('─' * 60)
    print(listing[:1500] or '（空的——代表這個專案目前沒有待處理建議，這是好消息）')
    print('─' * 60)

    # ⑤ 要看模型實際會寫什麼，加 --llm
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
