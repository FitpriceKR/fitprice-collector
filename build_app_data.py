#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prices.csv  →  app_data.js / app_data.json  (앱이 읽는 가공 데이터)

수집 스크립트(collect_prices.py)가 매일 쌓은 prices.csv를 읽어 상품별로
  - 현재가, 역대 최저/최고/평균가, "역대 최저가 여부", 직전 수집일 대비 하락률
  - 날짜별 가격 추이(여러 판매처가 있으면 그날의 최저가를 채택)
  - 쿠팡 수집분이 있으면 coupang {price, url} (파트너스 딥링크) 첨부
  - 수집 커버리지 메타(FITPRICE_META): 마지막 수집일, 수집 성공/미수집 상품
를 계산해서 앱이 바로 쓸 수 있는 형태로 내보냅니다.

사용법:
  python build_app_data.py                 # prices.csv 읽어 현재 폴더에 출력
  python build_app_data.py 입력.csv 출력폴더
"""
import csv, json, sys, os, datetime
from collections import defaultdict

CAT_ORDER = ["protein", "creatine", "booster", "guard", "supplement", "etc"]

def main():
    inp    = sys.argv[1] if len(sys.argv) > 1 else "prices.csv"
    outdir = sys.argv[2] if len(sys.argv) > 2 else "."
    if not os.path.exists(inp):
        raise SystemExit(f"입력 CSV가 없습니다: {inp}\n먼저 수집을 한 번 돌려 prices.csv를 만드세요.")

    # products.json: 커버리지 메타 + 카테고리/세부분류 최신화 용도
    pj = []
    pj_path = os.path.join(os.path.dirname(os.path.abspath(inp)), "products.json")
    if not os.path.exists(pj_path):
        pj_path = "products.json"
    if os.path.exists(pj_path):
        try:
            pj = json.load(open(pj_path, encoding="utf-8"))
        except Exception:
            pj = []
    all_ids  = [p["id"] for p in pj]
    cat_map  = {p["id"]: p.get("category", "") for p in pj}
    sub_map  = {p["id"]: p.get("subcat", "") for p in pj}
    cp_map   = {p["id"]: p.get("cp_url", "") for p in pj}   # 쿠팡 파트너스 수동 링크

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
        if all_ids and tid not in cat_map:
            continue   # 추적 목록에서 빠진 상품(예: 카테고리 개편으로 제외)은 앱에 노출 안 함
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
        prod = {
            "id": tid,
            "category": cat_map.get(tid) or last["category"],
            "subcat": sub_map.get(tid, ""),
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
        }
        # 마지막 수집일에 쿠팡 데이터가 있으면 첨부(구매 버튼이 파트너스 링크로 전환됨)
        last_day = sorted(daymap)[-1]
        cp = next((x for x in daymap[last_day] if x["source"] == "coupang"), None)
        if cp:
            prod["coupang"] = {"price": cp["price"], "url": cp["url"]}
        elif cp_map.get(tid):
            prod["coupang"] = {"url": cp_map[tid]}   # API 승인 전 수동 파트너스 링크(가격 없음)
        products.append(prod)

    order = {c: i for i, c in enumerate(CAT_ORDER)}
    products.sort(key=lambda p: (order.get(p["category"], 99), p["name"]))

    # ---------- 수집 커버리지 메타 ----------
    last_date = max((p["hist"][-1]["d"] for p in products), default=None)
    fresh     = sum(1 for p in products if p["hist"][-1]["d"] == last_date)
    stale_ids = [p["id"] for p in products if p["hist"][-1]["d"] != last_date]
    never_ids = [i for i in all_ids if i not in by_id]
    meta = {
        "lastDate": last_date,
        "total": len(all_ids) or len(products),
        "freshCount": fresh,
        "staleIds": stale_ids,   # 추적 이력은 있는데 최근에 수집 안 된 상품
        "neverIds": never_ids,   # 한 번도 수집 못 한 상품
    }

    os.makedirs(outdir, exist_ok=True)
    today = datetime.date.today().isoformat()
    with open(os.path.join(outdir, "app_data.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": today, "meta": meta, "products": products}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(outdir, "app_data.js"), "w", encoding="utf-8") as f:
        f.write("window.FITPRICE_DATA = " + json.dumps(products, ensure_ascii=False) + ";\n")
        f.write("window.FITPRICE_UPDATED = " + json.dumps(today) + ";\n")
        f.write("window.FITPRICE_META = " + json.dumps(meta, ensure_ascii=False) + ";\n")

    days = max((p["days"] for p in products), default=0)
    print(f"상품 {len(products)}개 처리 완료 → app_data.js, app_data.json")
    print(f"누적 수집 일수(최대): {days}일  /  기준일: {today}")
    print(f"커버리지: 마지막 수집일 {last_date} 기준 {fresh}/{meta['total']}개 최신")
    if stale_ids:
        print(f"  · 최근 수집 누락: {', '.join(stale_ids)}")
    if never_ids:
        print(f"  · 수집 이력 없음: {', '.join(never_ids)}")
    if days < 2:
        print("※ 아직 1일치라 추이 그래프는 점 1개로 보입니다. 매일 쌓이면 선이 그려집니다.")

if __name__ == "__main__":
    main()
