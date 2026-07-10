#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
핏프라이스 일일 가격 수집기
- 메인: 네이버 쇼핑 검색 API (무료, 즉시 발급, 합법, 최저가 lprice 제공)
- 옵션: 쿠팡 파트너스 API (파트너스 승인 후, 쿠팡 현재가 + 수익용 딥링크)
- 매일 1회 실행 → products.json의 각 상품 가격을 prices.csv 에 1행씩 누적

[v2 데이터 정제]
- 검색 결과 상위 30개 중 '진짜 같은 상품'만 채택 (샘플/체험분, 용량 불일치,
  캡슐↔파우더 형태 불일치, 타 브랜드 혼동, 비정상 저가 제외)
- 실행 시마다 기존 prices.csv도 같은 기준으로 자동 청소
  (같은 날 중복 실행분 제거 + 과거 오염 행 제거) — 멱등이라 여러 번 돌아도 안전
- 수집 커버리지(성공/실패 상품) 로그 출력

[v2.2]
- 검색 결과가 없거나 전부 검증 탈락하면 쿼리 마지막 단어(맛 이름 등)를 빼고 1회 재시도
  (용량·숫자 토큰은 빼지 않음, 검증은 항상 원래 쿼리 기준)
- collect_report.json 생성: 상품별 수집 상태와 실패 원인(검색 0건 / 탈락 사유 샘플)
  → 저장소에 커밋되므로 로그를 열지 않아도 쿼리 개선 대상을 파악 가능

환경변수(깃허브 Actions Secrets 또는 .env):
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET            (필수)
  COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY          (선택)
"""

import os, re, csv, json, time, hmac, hashlib, datetime, urllib.parse, urllib.request, ssl

BASE = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(BASE, "products.json")
OUTPUT_CSV    = os.path.join(BASE, "prices.csv")
TODAY = datetime.date.today().isoformat()

# ---------------------------------------------------------------------------
# 상품 검증(정제) — 수집·과거 데이터 청소에 공통 사용
# ---------------------------------------------------------------------------
BAD_WORDS = ["샘플", "체험", "증정", "사은품", "트라이얼", "1포", "낱개", "소분"]

# 카테고리별 최저 정상가(원) — 이보다 싸면 스틱/샘플류로 간주
MIN_PRICE = {"protein": 3000, "creatine": 2000, "booster": 2000, "guard": 500, "etc": 500}

# 브랜드 혼동 감지용(정규화: 소문자·공백 제거 후 부분일치)
KNOWN_BRANDS = [
    "마이프로틴", "옵티멈뉴트리션", "옵티멈", "디마티즈", "bsn", "머슬팜", "칼로바이",
    "식스스타", "뉴트리코스트", "알라니뉴", "프로틴웍스", "가든오브라이프", "보충닷컴",
    "뉴트라바이오", "바디닥터스", "퀘스트", "나우푸드", "셀루코어", "크레아핏",
    "머슬테크", "유니버셜", "프로메라", "고스트", "삼대오백", "블렌더보틀",
    "베어그립", "험블", "렙스", "인저", "sbd",
]

_VOL = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|mg|ml|g|l)(?![a-zA-Z])", re.I)

def _norm(s):
    return re.sub(r"\s+", "", (s or "")).lower()

def volumes_g(text):
    """텍스트에서 용량 토큰을 모두 g 단위로 추출 (ml≈g 취급)"""
    out = []
    for m in _VOL.finditer(text or ""):
        v, u = float(m.group(1)), m.group(2).lower()
        if u in ("kg", "l"):
            v *= 1000
        elif u == "mg":
            v /= 1000
        out.append(v)
    return out

def _brands_in(text):
    t = _norm(text)
    return {b for b in KNOWN_BRANDS if b in t}

def validate(query, cat, name, price):
    """검색 결과가 추적 상품과 같은 상품인지 검증. (ok, 사유) 반환"""
    n = name or ""
    for w in BAD_WORDS:
        if w in n:
            return False, f"제외 키워드({w})"
    try:
        price = int(float(price))
    except (ValueError, TypeError):
        return False, "가격 파싱 실패"
    if price < MIN_PRICE.get(cat, 500):
        return False, f"비정상 저가({price}원)"
    # 브랜드 혼동: 쿼리에 브랜드가 있는데 결과가 '다른' 브랜드만 담고 있으면 제외
    qb, nb = _brands_in(query), _brands_in(n)
    if qb and nb and not (qb & nb):
        return False, f"브랜드 불일치({'/'.join(sorted(nb))})"
    # 용량 검증: 쿼리에 50g 이상 용량이 명시된 경우만
    qv = [v for v in volumes_g(query) if v >= 50]
    if qv:
        target = max(qv)
        nv = volumes_g(n)
        big = [v for v in nv if v >= 50]
        if big:
            if not any(0.5 * target <= v <= 2.2 * target for v in big):
                return False, "용량 불일치"
        elif nv and max(nv) < 0.3 * target:
            return False, "용량 미달(샘플 추정)"
        elif re.search(r"\d+\s*(정|캡슐|개입)", n):
            return False, "형태 불일치(캡슐/정)"
    return True, ""

# ---------------------------------------------------------------------------
# 네이버 쇼핑 검색 API
# ---------------------------------------------------------------------------
def naver_search(query: str):
    """네이버 쇼핑 검색(가격 낮은순 30개). 원시 아이템 리스트 반환, 오류/키 없음이면 None"""
    cid, secret = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not (cid and secret):
        return None
    url = "https://openapi.naver.com/v1/search/shop.json?" + urllib.parse.urlencode(
        {"query": query, "display": 30, "sort": "asc"}  # sort=asc → 가격 낮은순
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
    return data.get("items") or []

def pick_valid(orig_query: str, cat: str, items):
    """아이템 중 검증(원래 쿼리 기준) 통과 최저가 1건 + 탈락 사유 샘플 반환"""
    valid, rejects = [], []
    for it in items:
        name = it["title"].replace("<b>", "").replace("</b>", "")
        try:
            price = int(it["lprice"])
        except (ValueError, TypeError, KeyError):
            continue
        ok, why = validate(orig_query, cat, name, price)
        if not ok:
            if len(rejects) < 3:
                rejects.append({"name": name[:60], "price": price, "why": why})
            continue
        valid.append({
            "price": price, "name": name,
            "mall": it.get("mallName", ""), "url": it.get("link", ""),
            "product_id": it.get("productId", ""), "image": it.get("image", ""),
        })
    if not valid:
        return None, rejects
    # 쿼리 브랜드(첫 단어)가 이름에 들어간 후보를 우선 (가격 낮은순이라 첫 매치가 최저가)
    brand = _norm(orig_query.split()[0]) if orig_query.split() else ""
    if brand:
        for v in valid:
            if brand in _norm(v["name"]):
                return v, rejects
    return valid[0], rejects

def simplify_query(query: str):
    """마지막 단어(맛 이름 등)를 뺀 완화 쿼리. 용량/숫자 토큰이거나 너무 짧으면 None"""
    toks = query.split()
    if len(toks) < 3:
        return None
    last = toks[-1]
    if re.search(r"\d", last):        # 용량·수량 등 숫자 포함 토큰은 유지
        return None
    return " ".join(toks[:-1])

def naver_best(query: str, cat: str):
    """검증 통과 최저가 1건 + 진단 정보. (result, diag) 반환"""
    items = naver_search(query)
    if items is None:
        return None, {"status": "api_error_or_no_key"}
    best, rejects = pick_valid(query, cat, items)
    if best:
        return best, {"status": "ok"}
    diag = {"status": "no_result" if not items else "all_rejected",
            "found": len(items), "rejected_sample": rejects}
    # 재시도: 마지막 단어(맛 등) 빼고 다시 검색 — 검증은 원래 쿼리 기준 유지
    sq = simplify_query(query)
    if sq:
        time.sleep(0.2)
        items2 = naver_search(sq)
        if items2:
            best2, rejects2 = pick_valid(query, cat, items2)
            if best2:
                return best2, {"status": "ok_simplified", "used_query": sq}
            diag = {"status": "all_rejected", "found": len(items2),
                    "used_query": sq, "rejected_sample": (rejects or rejects2)[:3]}
    return None, diag

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

REPORT_FILE = os.path.join(BASE, "collect_report.json")

def coupang_search(query: str, cat: str):
    """쿠팡 파트너스 검색 — 상위 5개 중 검증 통과한 1건. 없으면 None"""
    access, secret = os.environ.get("COUPANG_ACCESS_KEY"), os.environ.get("COUPANG_SECRET_KEY")
    if not (access and secret):
        return None
    path = "/v2/providers/affiliate_open_api/apis/openapi/products/search?" + urllib.parse.urlencode(
        {"keyword": query, "limit": 5}
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
    for it in (data.get("data") or {}).get("productData") or []:
        name = it.get("productName", "")
        try:
            price = int(it["productPrice"])
        except (ValueError, TypeError, KeyError):
            continue
        ok, _ = validate(query, cat, name, price)
        if not ok:
            continue
        return {
            "price": price, "name": name,
            "url": it.get("productUrl", ""),   # 이 URL 자체가 파트너스 추적 링크(수익용)
            "product_id": it.get("productId", ""),
            "is_rocket": it.get("isRocket", False),
            "image": it.get("productImage", ""),
        }
    return None

# ---------------------------------------------------------------------------
# CSV 입출력 + 과거 데이터 청소
# ---------------------------------------------------------------------------
HEADER = ["date", "tracking_id", "category", "source", "price", "name", "mall", "url", "product_id", "image"]

def clean_history(products):
    """기존 prices.csv를 정리: 같은 날 중복 실행분 제거 + 검증 실패(오염) 행 제거.
    구버전 헤더도 새 헤더로 자동 변환. 멱등."""
    if not os.path.exists(OUTPUT_CSV):
        return
    qmap = {p["id"]: (p.get("category", ""), p["query"]) for p in products}
    with open(OUTPUT_CSV, newline="", encoding="utf-8-sig") as f:
        rows_in = list(csv.DictReader(f))
    total_in = len(rows_in)
    # 같은 (날짜, 상품, 소스)는 마지막 수집분만 유지 → 하루 여러 번 돌린 중복 제거
    seen = {}
    for r in rows_in:
        d, tid = (r.get("date") or "").strip(), (r.get("tracking_id") or "").strip()
        if not d or not tid:
            continue
        seen[(d, tid, (r.get("source") or "").strip())] = r
    dups = total_in - len(seen)
    kept, polluted = [], 0
    for (d, tid, src), r in sorted(seen.items()):
        cat, query = qmap.get(tid, ((r.get("category") or ""), None))
        if query:
            ok, why = validate(query, cat, r.get("name", ""), r.get("price", 0))
            if not ok:
                polluted += 1
                continue
        kept.append([r.get(k, "") for k in HEADER])
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(kept)
    if dups or polluted:
        print(f"기존 데이터 청소: 중복 {dups}건 + 오염(잘못된 상품 매칭) {polluted}건 제거 → {len(kept)}행 유지")

def append_rows(rows):
    exists = os.path.exists(OUTPUT_CSV)
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(HEADER)
        w.writerows(rows)

# ---------------------------------------------------------------------------
# 메인 루프
# ---------------------------------------------------------------------------
def main():
    if not os.path.exists(PRODUCTS_FILE):
        raise SystemExit(f"products.json 이 없습니다: {PRODUCTS_FILE}")
    products = json.load(open(PRODUCTS_FILE, encoding="utf-8"))

    clean_history(products)   # 과거 오염/중복 정리 (멱등)

    rows, ok, failed = [], 0, []
    report = {"date": TODAY, "failures": {}}
    for p in products:
        tid, cat, query = p["id"], p.get("category", ""), p["query"]
        print(f"· {tid} ({query})")
        got = False

        n, diag = naver_best(query, cat)
        if n:
            rows.append([TODAY, tid, cat, "naver", n["price"], n["name"], n["mall"], n["url"], n["product_id"], n["image"]])
            ok += 1; got = True
            extra = f" (완화 쿼리: {diag.get('used_query')})" if diag.get("status") == "ok_simplified" else ""
            print(f"    네이버 최저가 {n['price']:,}원 ({n['mall']}){extra}")

        c = coupang_search(query, cat)
        if c:
            rows.append([TODAY, tid, cat, "coupang", c["price"], c["name"], "쿠팡", c["url"], c["product_id"], c["image"]])
            ok += 1; got = True
            print(f"    쿠팡 {c['price']:,}원")

        if not got:
            failed.append(tid)
            report["failures"][tid] = {"query": query, **diag}
        time.sleep(0.3)  # rate limit 보호

    if rows:
        append_rows(rows)

    report["collected"] = len(products) - len(failed)
    report["total"] = len(products)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print(f"\n완료: {len(products)}개 상품 중 {len(products) - len(failed)}개 수집 / 총 {ok}건 → {OUTPUT_CSV}")
    if failed:
        print(f"미수집 {len(failed)}개: {', '.join(failed)}")
        print(f"※ 상품별 실패 원인은 {os.path.basename(REPORT_FILE)} 참고 (검색 0건인지 검증 탈락인지 구분됨)")

if __name__ == "__main__":
    main()
