#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prices.csv  →  app_data.js / app_data.json  (앱이 읽는 가공 데이터)

수집 스크립트(collect_prices.py)가 매일 쌓은 prices.csv를 읽어 상품별로
  - 현재가, 역대 최저/최고/평균가, "역대 최저가 여부", 전일 대비 하락률
  - 날짜별 가격 추이(여러 판매처가 있으면 그날의 최저가를 채택)
를 계산해서 앱이 바로 쓸 수 있는 형태로 내보냅니다.

사용법:
  python build_app_data.py                 # prices.csv 읽어 현재 폴더에 출력
  python build_app_data.py 입력.csv 출력폴더

출력:
  app_data.js   → window.FITPRICE_DATA 전역변수 (HTML이 file://로 바로 로드 가능)
  app_data.json → 동일 데이터의 순수 JSON (DB/다른 용도용)
"""
import csv, json, sys, os, datetime
from collections import defaultdict

CAT_ORDER = ["protein", "creatine", "booster", "guard", "etc"]

def main():
    inp    = sys.argv[1] if len(sys.argv) > 1 else "prices.csv"
    outdir = sys.argv[2] if len(sys.argv) > 2 else "."
    if not os.path.exists(inp):
        raise SystemExit(f"입력 CSV가 없습니다: {inp}\n먼저 수집을 한 번 돌려 prices.csv를 만드세요.")

    rows = list(csv.DictReader(open(inp, encoding="utf-8-sig")))
    # tracking_id → { date → [그날 수집된 여러 판매처 행] }
    by_id = defaultdict(lambda: defaultdict(list))
    for r in rows:
        tid = (r.get("tracking_id") or "").strip()
        d   = (r.get("date") or "").strip()
        if not tid or not d:
            continue
        try:
            price = int(float(r["price"]))
        except (KeyError, ValueError, TypeError):
            continue
        by_id[tid][d].append({
            "price": price,
            "source": r.get("source", ""),
            "mall":  r.get("mall", ""),
            "url":   r.get("url", ""),
            "name":  r.get("name", ""),
            "image": r.get("image", ""),
            "category": r.get("category", ""),
        })

    products = []
    for tid, daymap in by_id.items():
        hist, last = [], None
        for d in sorted(daymap):
            cheapest = min(daymap[d], key=lambda x: x["price"])   # 그날의 최저가 채택
            hist.append({"d": d, "price": cheapest["price"]})
            last = cheapest
        prices = [h["price"] for h in hist]
        if not prices:
            continue
        latest = prices[-1]
        mn, mx = min(prices), max(prices)
        avg = round(sum(prices) / len(prices))
        prev = prices[-2] if len(prices) > 1 else None
        drop = round((prev - latest) / prev * 100) if prev and prev > 0 else 0
        name = last["name"] or tid
        products.append({
            "id": tid,
            "category": last["category"],
            "brand": name.split()[0] if name else "",
            "name": name,
            "image": last.get("image", ""),
            "mall": last["mall"],
            "url": last["url"],
            "price": latest,
            "min": mn, "max": mx, "avg": avg,
            "isAllTimeLow": latest <= mn,
            "dropPct": drop if drop > 0 else 0,
            "days": len(hist),
            "hist": hist,
        })

    order = {c: i for i, c in enumerate(CAT_ORDER)}
    products.sort(key=lambda p: (order.get(p["category"], 99), p["name"]))

    os.makedirs(outdir, exist_ok=True)
    today = datetime.date.today().isoformat()
    with open(os.path.join(outdir, "app_data.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": today, "products": products}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(outdir, "app_data.js"), "w", encoding="utf-8") as f:
        f.write("window.FITPRICE_DATA = " + json.dumps(products, ensure_ascii=False) + ";\n")
        f.write("window.FITPRICE_UPDATED = " + json.dumps(today) + ";\n")

    days = max((p["days"] for p in products), default=0)
    print(f"상품 {len(products)}개 처리 완료 → app_data.js, app_data.json")
    print(f"누적 수집 일수(최대): {days}일  /  기준일: {today}")
    if days < 2:
        print("※ 아직 1일치라 추이 그래프는 점 1개로 보입니다. 매일 쌓이면 선이 그려집니다.")

if __name__ == "__main__":
    main()
