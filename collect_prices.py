#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
핏프라이스 일일 가격 수집기
- 메인: 네이버 쇼핑 검색 API (무료, 즉시 발급, 합법, 최저가 lprice 제공)
- 옵션: 쿠팡 파트너스 API (파트너스 승인 후, 쿠팡 현재가 + 수익용 딥링크)
- 매일 1회 실행 → products.json의 각 상품 가격을 prices.csv 에 1행씩 누적
- 6개월(약 180행)이 쌓이면 그대로 가격 추이 차트 데이터가 됩니다.

환경변수(깃허브 Actions Secrets 또는 .env):
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET            (필수)
  COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY          (선택)
"""

import os, csv, json, time, hmac, hashlib, datetime, urllib.parse, urllib.request, ssl

BASE = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(BASE, "products.json")
OUTPUT_CSV    = os.path.join(BASE, "prices.csv")
TODAY = datetime.date.today().isoformat()

# ---------------------------------------------------------------------------
# 네이버 쇼핑 검색 API
# ---------------------------------------------------------------------------
def naver_lowest(query: str):
    """검색어로 네이버 쇼핑 최저가 1건 반환. {price, name, mall, url, product_id} 또는 None"""
    cid, secret = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not (cid and secret):
        return None
    url = "https://openapi.naver.com/v1/search/shop.json?" + urllib.parse.urlencode(
        {"query": query, "display": 1, "sort": "asc"}  # sort=asc → 가격 낮은순
    )
    req = urllib.request.Request(url, headers={
        "X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret,
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  [네이버 오류] {query}: {e}")
        return None
    items = data.get("items") or []
    if not items:
        return None
    it = items[0]
    name = it["title"].replace("<b>", "").replace("</b>", "")
    return {
        "price": int(it["lprice"]),
        "name": name,
        "mall": it.get("mallName", ""),
        "url": it.get("link", ""),
        "product_id": it.get("productId", ""),
        "image": it.get("image", ""),
    }

# ---------------------------------------------------------------------------
# 쿠팡 파트너스 API (선택) — 승인 후 키를 넣으면 자동 활성화
# ---------------------------------------------------------------------------
COUPANG_DOMAIN = "https://api-gateway.coupang.com"

def _coupang_auth(method: str, path_with_query: str, access: str, secret: str) -> str:
    path, _, query = path_with_query.partition("?")
    signed_date = time.strftime("%y%m%dT%H%M%SZ", time.gmtime())
    message = signed_date + method + path + query
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"CEA algorithm=HmacSHA256, access-key={access}, signed-date={signed_date}, signature={signature}"

def coupang_search(query: str):
    """쿠팡 파트너스 검색 1건. {price, name, url, product_id, is_rocket} 또는 None"""
    access, secret = os.environ.get("COUPANG_ACCESS_KEY"), os.environ.get("COUPANG_SECRET_KEY")
    if not (access and secret):
        return None
    path = "/v2/providers/affiliate_open_api/apis/openapi/products/search?" + urllib.parse.urlencode(
        {"keyword": query, "limit": 1}
    )
    auth = _coupang_auth("GET", path, access, secret)
    req = urllib.request.Request(COUPANG_DOMAIN + path, headers={
        "Authorization": auth, "Content-Type": "application/json;charset=UTF-8",
    })
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  [쿠팡 오류] {query}: {e}")
        return None
    rows = (data.get("data") or {}).get("productData") or []
    if not rows:
        return None
    it = rows[0]
    return {
        "price": int(it["productPrice"]),
        "name": it.get("productName", ""),
        "url": it.get("productUrl", ""),       # 이 URL 자체가 파트너스 추적 링크(수익용)
        "product_id": it.get("productId", ""),
        "is_rocket": it.get("isRocket", False),
        "image": it.get("productImage", ""),
    }

# ---------------------------------------------------------------------------
# 메인 루프
# ---------------------------------------------------------------------------
HEADER = ["date", "tracking_id", "category", "source", "price", "name", "mall", "url", "product_id", "image"]

def append_rows(rows):
    # 기존 파일이 구버전(이미지 컬럼 없음)이면 새 헤더로 자동 변환 후 이어쓰기
    existing = []
    if os.path.exists(OUTPUT_CSV):
        with open(OUTPUT_CSV, newline="", encoding="utf-8-sig") as f:
            data = list(csv.reader(f))
        if data:
            old = data[0]
            if old == HEADER:
                with open(OUTPUT_CSV, "a", newline="", encoding="utf-8-sig") as f:
                    csv.writer(f).writerows(rows)
                return
            for r in data[1:]:                      # 옛 행을 새 컬럼 순서로 재배치(없는 값은 빈칸)
                d = dict(zip(old, r))
                existing.append([d.get(k, "") for k in HEADER])
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(existing)
        w.writerows(rows)

def main():
    if not os.path.exists(PRODUCTS_FILE):
        raise SystemExit(f"products.json 이 없습니다: {PRODUCTS_FILE}")
    products = json.load(open(PRODUCTS_FILE, encoding="utf-8"))
    rows, ok = [], 0
    for p in products:
        tid, cat, query = p["id"], p.get("category", ""), p["query"]
        print(f"· {tid} ({query})")

        n = naver_lowest(query)
        if n:
            rows.append([TODAY, tid, cat, "naver", n["price"], n["name"], n["mall"], n["url"], n["product_id"], n["image"]])
            ok += 1
            print(f"    네이버 최저가 {n['price']:,}원 ({n['mall']})")

        c = coupang_search(query)
        if c:
            rows.append([TODAY, tid, cat, "coupang", c["price"], c["name"], "쿠팡", c["url"], c["product_id"], c["image"]])
            ok += 1
            print(f"    쿠팡 {c['price']:,}원")

        time.sleep(0.3)  # rate limit 보호

    if rows:
        append_rows(rows)
    print(f"\n완료: {len(products)}개 상품 / {ok}건 수집 → {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
