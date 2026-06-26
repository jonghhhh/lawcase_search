# -*- coding: utf-8 -*-
"""
대법원 사법정보공개포털(종합법률정보) 판례 검색 수집기

기존 Playwright 방식(화면 클릭/팝업)은 SPA 구조 때문에 셀렉터가 자주 깨졌다.
이 스크립트는 포털이 내부적으로 호출하는 검색 API
    POST /pgp/pgp1011/selectJdcpctSrchRsltLst.on
를 그대로 호출한다.
 - 로그인 불필요
 - '요지 전체보기'를 클릭하지 않아도 요지(jdcpctSumrCtt)가 응답에 포함됨
 - 법원종류 / 판례등급 / 사건종류 / 선고구분 필터를 코드로 지정 가능
"""

import re
import time
from urllib.parse import quote
import requests
import pandas as pd

# ────────────────────────────────────────────────────────────
# 1) 검색 설정
# ────────────────────────────────────────────────────────────
KEYWORD = "임금 체불"     # 검색어
PAGE_SIZE = 100          # 한 번에 가져올 건수 (최대 100 권장)
MAX_RECORDS = None       # 최대 수집 건수 (None = 전체)

# 목록 API의 요지/판시사항은 약 260자 미리보기로 '잘려서' 내려온다.
# True 로 두면 판례별 상세 API를 한 번 더 호출해 '요지 전문'을 가져온다(권장).
# 건수가 많으면 그만큼 느려지므로(레코드당 호출 2회) 필요에 맞게 조절.
FETCH_FULL_TEXT = True

# ── 필터 (원하는 항목만 리스트에 넣기 / 빈 리스트 = '전체') ──
# 아래 한글 라벨 중 골라서 넣으면 된다. 여러 개 선택 가능.
COURT_FILTER      = []   # 법원종류 : "대법원", "고등법원", "하급심"
GRADE_FILTER      = []   # 판례등급 : "전원합의체", "간행판결", "미간행판결", "변경·폐기"
CASE_TYPE_FILTER  = []   # 사건종류 : "민사", "형사", "가사", "특허", "조세", "행정"
DECISION_FILTER   = []   # 선고구분 : "판결", "결정"

# 예) 대법원 + 고등법원의, 간행판결 중, 형사·행정 사건만:
#   COURT_FILTER     = ["대법원", "고등법원"]
#   GRADE_FILTER     = ["간행판결"]
#   CASE_TYPE_FILTER = ["형사", "행정"]

# ────────────────────────────────────────────────────────────
# 2) 필터 라벨 → 코드 매핑 (포털 API 분석으로 확인된 값)
# ────────────────────────────────────────────────────────────
COURT_CODES = {"대법원": "01", "고등법원": "03", "하급심": "04"}
GRADE_CODES = {"전원합의체": "01", "간행판결": "02", "미간행판결": "03", "변경·폐기": "04"}
CASE_TYPE_CODES = {"민사": "01", "형사": "02", "가사": "03", "특허": "04", "조세": "05", "행정": "06"}
DECISION_CODES = {"판결": "01", "결정": "02"}

# 판례등급 기본 코드 묶음 (포털이 '전체' 검색 시 보내는 값)
DEFAULT_JDCPCT_GR_CD = "111|112|130|141|180|182|232|235"

API_URL = "https://portal.scourt.go.kr/pgp/pgp1011/selectJdcpctSrchRsltLst.on"
DETAIL_URL = "https://portal.scourt.go.kr/pgp/pgp1011/selectJdcpctSumrInf.on"

# 상세 API의 본문 구분 코드
CTXT_HOLDING = "01"   # 판시사항
CTXT_SUMMARY = "02"   # 판결요지

HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://portal.scourt.go.kr",
    "Referer": "https://portal.scourt.go.kr/pgp/index.on?m=PGP1011M01&l=N&c=900",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

TAG_RE = re.compile(r"<[^>]+>")


def clean(text):
    """<strong> 등 HTML 태그 제거 + 공백 정리."""
    if not text:
        return ""
    text = TAG_RE.sub("", text)
    text = text.replace("\xa0", " ")
    # 줄 단위로 좌우 공백 정리
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln != "").strip()


def labels_to_code(labels, mapping):
    """선택한 한글 라벨 리스트를 '01|03' 형태의 코드 문자열로."""
    bad = [l for l in labels if l not in mapping]
    if bad:
        raise ValueError(f"알 수 없는 필터 라벨: {bad} (가능: {list(mapping)})")
    return "|".join(mapping[l] for l in labels)


def build_payload(page_no, total_count=""):
    return {
        "dma_searchParam": {
            "srchwd": KEYWORD,
            "sort": "jis_jdcpc_instn_dvs_cd_s asc, $relevance desc, "
                    "prnjdg_ymd_o desc, jdcpct_gr_cd_s asc",
            "sortType": "정확도",
            "searchRange": "",
            "tpcJdcpctCsAlsYn": "",
            "csNoLstCtt": "",
            "csNmLstCtt": "",
            "prvsRefcCtt": "",
            "searchScope": "",
            "jisJdcpcInstnDvsCd": "",
            "jdcpctCdcsCd": "",
            "prnjdgYmdFrom": "",
            "prnjdgYmdTo": "",
            "grpJdcpctGrCd": "",
            "cortNm": "",
            "pageNo": page_no,
            # ↓↓↓ 필터 (그룹 필드) ↓↓↓
            "jisJdcpcInstnDvsCdGrp": labels_to_code(COURT_FILTER, COURT_CODES),       # 법원종류
            "grpJdcpctGrCdGrp": labels_to_code(GRADE_FILTER, GRADE_CODES),            # 판례등급
            "jdcpctCdcsCdGrp": labels_to_code(CASE_TYPE_FILTER, CASE_TYPE_CODES),     # 사건종류
            "adjdTypCdGrp": labels_to_code(DECISION_FILTER, DECISION_CODES),          # 선고구분
            "pageSize": str(PAGE_SIZE),
            "reSrchFlag": "",
            "befSrchwd": KEYWORD,
            "preSrchConditions": "",
            "initYn": "N",
            "totalCount": str(total_count),
            "jdcpctGrCd": DEFAULT_JDCPCT_GR_CD,
            "category": "jdcpct",
            "isKwdSearch": "N",
        }
    }


def fmt_date(yyyymmdd):
    s = (yyyymmdd or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}.{s[4:6]}.{s[6:8]}"
    return s


def fetch_full_text(session, srno, ctxt_cd):
    """판례 상세 API에서 본문 전문을 가져온다.
    응답 dlt_sumrInf 는 [1],[2]... 항목별로 나뉘어 있어 이어 붙인다."""
    if not srno:
        return ""
    payload = {"dma_searchParam": {"jisCntntsSrno": int(srno), "jdcpctCtxtDvsCd": ctxt_cd}}
    r = session.post(DETAIL_URL, json=payload, timeout=30)
    r.raise_for_status()
    segs = (r.json().get("data") or {}).get("dlt_sumrInf") or []
    return "\n".join(clean(s.get("jdcpctSumrCtt", "")) for s in segs).strip()


def parse_item(session, item, page_no, idx):
    srno = item.get("jisCntntsSrno", "")
    case_no = clean(item.get("csNoLstCtt", ""))

    # 요지/판시사항: 기본은 목록 미리보기(잘림), 옵션이 켜져 있으면 전문으로 교체
    holding = clean(item.get("dcdcsCtt", ""))      # 판시사항(미리보기)
    summary = clean(item.get("jdcpctSumrCtt", ""))  # 요지(미리보기)
    if FETCH_FULL_TEXT:
        # 전문이 미리보기보다 길 때만 교체(전문이 비어있으면 미리보기 유지)
        full_summary = fetch_full_text(session, srno, CTXT_SUMMARY)
        full_holding = fetch_full_text(session, srno, CTXT_HOLDING)
        if len(full_summary) > len(summary):
            summary = full_summary
        if len(full_holding) > len(holding):
            holding = full_holding

    return {
        "검색어": KEYWORD,
        "페이지": page_no,
        "순번": idx,
        "법원": item.get("cortNm", ""),
        "사건번호": case_no,
        "사건명": clean(item.get("csNmLstCtt", "")),
        "선고일자": fmt_date(item.get("prnjdgYmd", "")),
        "선고구분": item.get("adjdTypNm", ""),
        "판례등급": item.get("grpJdcpctGrNm", ""),
        "공보": clean(item.get("jdcpctPublcCtt", "")),
        "판시사항": holding,
        "요지전체": summary,
        "일련번호": srno,
        # 포털은 SPA라 판례별 고정 URL이 없다. 사건번호로 재검색되는 링크를 제공.
        "검색링크": (
            "https://portal.scourt.go.kr/pgp/index.on?m=PGP1011M01&l=N&c=900&q="
            + quote(case_no) if case_no else ""
        ),
    }


def main():
    session = requests.Session()
    session.headers.update(HEADERS)

    # 첫 페이지로 전체 건수 확인
    resp = session.post(API_URL, json=build_payload(1), timeout=30)
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    total = int(data.get("totalCount") or 0)

    if total == 0:
        print("검색 결과가 없습니다. (검색어/필터를 확인하세요)")
        return

    limit = total if MAX_RECORDS is None else min(total, MAX_RECORDS)
    last_page = (limit + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"총 {total:,}건 중 {limit:,}건 수집 시작 (페이지 {last_page}개)")

    rows = []
    for page_no in range(1, last_page + 1):
        if page_no == 1:
            items = data.get("dlt_jdcpctRslt") or []
        else:
            r = session.post(API_URL, json=build_payload(page_no, total), timeout=30)
            r.raise_for_status()
            items = (r.json().get("data") or {}).get("dlt_jdcpctRslt") or []

        if not items:
            print(f"{page_no}페이지: 결과 없음 → 중단")
            break

        for i, it in enumerate(items, start=1):
            rows.append(parse_item(session, it, page_no, i))
            if len(rows) >= limit:
                break

        print(f"{page_no}/{last_page} 페이지 수집 ({len(rows):,}건 누적)")
        if len(rows) >= limit:
            break
        time.sleep(0.4)  # 서버 부담 완화

    df = pd.DataFrame(rows)

    base = f"scourt_{KEYWORD}_요지"
    df.to_excel(f"{base}.xlsx", index=False)
    df.to_csv(f"{base}.csv", index=False, encoding="utf-8-sig")

    print(f"\n완료: {len(df):,}건 저장")
    print(f"  - {base}.xlsx")
    print(f"  - {base}.csv")
    print(df[["법원", "사건번호", "선고일자", "사건명"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
