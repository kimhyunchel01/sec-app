"""미국 공시 조회 웹앱 (SEC EDGAR)

- 한글 종목명(일부 글자 가능)·영문명·티커로 검색
- 최근 공시 목록 + 공시 종류 한국어 설명 + SEC 원문 링크
- Streamlit Secrets에 SEC_USER_AGENT = "이름 이메일" 필요
"""

import datetime as dt
import html
import json
import os
import re
import threading

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="미국 공시 조회", page_icon="📄", layout="centered")

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"

# 공시 종류별 한국어 설명
FORM_DESC = {
    "10-K": "연간 보고서",
    "10-Q": "분기 보고서",
    "8-K": "주요 사항 수시 공시",
    "20-F": "외국 기업 연간 보고서",
    "40-F": "캐나다 기업 연간 보고서",
    "6-K": "외국 기업 수시 공시",
    "3": "내부자 최초 지분 보고",
    "4": "내부자 지분 변동 보고",
    "5": "내부자 연간 지분 보고",
    "144": "내부자 주식 매도 예정 신고",
    "SC 13G": "5% 이상 지분 보유 보고(단순 투자)",
    "SCHEDULE 13G": "5% 이상 지분 보유 보고(단순 투자)",
    "SC 13D": "5% 이상 지분 보유 보고(경영 참여 목적)",
    "SCHEDULE 13D": "5% 이상 지분 보유 보고(경영 참여 목적)",
    "DEF 14A": "주주총회 안건 자료(위임장 권유)",
    "DEFA14A": "주주총회 추가 자료",
    "PRE 14A": "주주총회 안건 자료 예비본",
    "ARS": "주주용 연차 보고서",
    "S-1": "신규 증권 발행 신고서",
    "S-3": "간이 증권 발행 신고서",
    "S-4": "합병 관련 증권 신고서",
    "S-8": "임직원 주식 보상 등록",
    "424B2": "증권 발행 설명서",
    "424B3": "증권 발행 설명서",
    "424B4": "증권 발행 설명서",
    "424B5": "증권 발행 설명서",
    "FWP": "증권 발행 보충 자료",
    "11-K": "임직원 주식 보상 제도 연간 보고",
    "SD": "분쟁 광물 보고",
    "8-A12B": "증권 거래소 등록",
    "25-NSE": "상장 폐지 통지",
    "CORRESP": "SEC 질의에 대한 회사 답변",
    "UPLOAD": "SEC 질의 서한",
    "CERT": "거래소 상장 인증",
    "IRANNOTICE": "이란 관련 거래 공지",
    "NT 10-K": "연간 보고서 제출 지연 통지",
    "NT 10-Q": "분기 보고서 제출 지연 통지",
}


def describe_form(form: str) -> str:
    if form in FORM_DESC:
        return FORM_DESC[form]
    if form.endswith("/A") and form[:-2] in FORM_DESC:
        return FORM_DESC[form[:-2]] + " (정정)"
    return "기타 공시"


def normalize(text: str) -> str:
    """띄어쓰기·점·쉼표 제거, 소문자화"""
    return re.sub(r"[\s.,\-]", "", str(text)).lower()


def get_user_agent() -> str | None:
    try:
        ua = st.secrets.get("SEC_USER_AGENT")
    except Exception:
        ua = None
    return ua or os.environ.get("SEC_USER_AGENT")


def sec_fetch(url: str) -> requests.Response:
    headers = {"User-Agent": get_user_agent(), "Accept-Encoding": "gzip, deflate"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp


def sec_get(url: str) -> dict:
    return sec_fetch(url).json()


def get_secret(name: str, default=None):
    try:
        val = st.secrets.get(name)
    except Exception:
        val = None
    return val if val not in (None, "") else os.environ.get(name, default)


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def load_companies() -> pd.DataFrame:
    data = sec_get(TICKERS_URL)
    df = pd.DataFrame(data.values())
    df = df.rename(columns={"cik_str": "cik", "ticker": "ticker", "title": "name"})
    df["ticker"] = df["ticker"].str.upper()
    return df[["cik", "ticker", "name"]]


@st.cache_data(ttl=60 * 60, show_spinner=False)
def load_kr_names() -> pd.DataFrame:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kr_names.csv")
    try:
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    except FileNotFoundError:
        return pd.DataFrame(columns=["kr_name", "ticker"])
    df.columns = ["kr_name", "ticker"] + list(df.columns[2:])
    df = df.dropna(subset=["kr_name", "ticker"])
    df["kr_name"] = df["kr_name"].str.strip()
    df["ticker"] = df["ticker"].str.strip().str.upper()
    return df[["kr_name", "ticker"]]


@st.cache_data(ttl=60 * 10, show_spinner=False)
def load_filings(cik: int) -> pd.DataFrame:
    data = sec_get(SUBMISSIONS_URL.format(cik=cik))
    recent = data.get("filings", {}).get("recent", {})
    df = pd.DataFrame(recent)
    if df.empty:
        return df
    for col in ["reportDate", "primaryDocument", "primaryDocDescription"]:
        if col not in df.columns:
            df[col] = ""

    def make_link(row):
        acc = str(row["accessionNumber"]).replace("-", "")
        doc = row["primaryDocument"]
        if doc:
            return ARCHIVE_URL.format(cik=cik, acc=acc, doc=doc)
        return FILING_INDEX_URL.format(cik=cik, acc=acc)

    df["link"] = df.apply(make_link, axis=1)
    df["desc"] = df["form"].apply(describe_form)
    return df


def search(query: str, companies: pd.DataFrame, kr: pd.DataFrame) -> pd.DataFrame:
    """검색 결과: ticker, cik, name(영문), kr_name 목록"""
    q = normalize(query)
    if not q:
        return companies.iloc[0:0]

    kr = kr.copy()
    kr["norm"] = kr["kr_name"].apply(normalize)
    kr_hit = kr[kr["norm"].str.contains(q, regex=False)]
    # 정확히 일치하는 한글명을 앞에
    kr_hit = kr_hit.assign(exact=kr_hit["norm"] == q).sort_values("exact", ascending=False)
    tickers = list(dict.fromkeys(kr_hit["ticker"]))

    comp = companies.copy()
    comp["norm_t"] = comp["ticker"].apply(normalize)
    exact_t = comp[comp["norm_t"] == q]["ticker"].tolist()
    if len(q) >= 2:
        name_hit = comp[comp["name"].apply(normalize).str.contains(q, regex=False)]["ticker"].tolist()
    else:
        name_hit = []

    ordered = list(dict.fromkeys(exact_t + tickers + name_hit))[:30]
    result = comp.set_index("ticker").loc[[t for t in ordered if t in set(comp["ticker"])]].reset_index()
    return result[["ticker", "cik", "name"]]


def display_name(ticker: str, eng_name: str, kr: pd.DataFrame) -> str:
    hit = kr[kr["ticker"] == ticker]
    base = hit.iloc[0]["kr_name"] if not hit.empty else eng_name
    return f"{base}({ticker})"


# ---------------- 공시 본문 ----------------
PAGE_CHARS = 10_000  # 한 화면(=번역 1회)에 표시할 글자 수
SKIP_DOC = re.compile(r"^(R\d+\.htm|FilingSummary|.*\.(xsd|jpg|jpeg|png|gif|zip|js|css|json)$)", re.I)


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def list_documents(cik: int, accession: str, primary: str) -> list[dict]:
    """공시 1건에 포함된 문서 목록 (본문 + 첨부). 첫 항목이 본문."""
    acc = accession.replace("-", "")
    base = FILING_INDEX_URL.format(cik=cik, acc=acc)
    docs = []
    try:
        items = sec_get(base + "index.json").get("directory", {}).get("item", [])
        for it in items:
            name = it.get("name", "")
            if not name or SKIP_DOC.match(name) or name.endswith("-index.htm") or name.endswith("-index-headers.html"):
                continue
            if not re.search(r"\.(htm|html|txt|xml)$", name, re.I):
                continue
            docs.append({"name": name, "url": base + name})
    except Exception:
        pass
    # 본문을 맨 앞으로
    if primary:
        prim_name = primary.split("/")[-1]
        docs = [d for d in docs if d["name"] != prim_name]
        docs.insert(0, {"name": primary, "url": base + primary})
    return docs


def doc_label(name: str, is_primary: bool) -> str:
    if is_primary:
        return f"본문 · {name}"
    low = name.lower()
    if re.search(r"ex[-_]?99", low):
        return f"첨부(보도자료 등) · {name}"
    if re.search(r"ex[-_]?(31|32)", low):
        return f"첨부(경영진 인증서) · {name}"
    if re.search(r"ex[-_]?\d+", low):
        return f"첨부 · {name}"
    return name


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False, max_entries=50)
def fetch_text(url: str) -> str:
    """SEC 문서에서 글자만 추출"""
    raw = sec_fetch(url).content
    if url.lower().endswith(".txt"):
        text = raw.decode("utf-8", errors="ignore")
    else:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "head", "ix:header"]):
            tag.decompose()
        for tag in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
            tag.decompose()
        text = soup.get_text("\n")
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out, blank = [], 0
    for ln in lines:
        if ln:
            out.append(ln)
            blank = 0
        else:
            blank += 1
            if blank == 1:
                out.append("")
    return "\n".join(out).strip()


def split_pages(text: str, size: int = PAGE_CHARS) -> list[str]:
    """줄 경계를 지키며 size 글자 단위로 나눔"""
    pages, cur, cur_len = [], [], 0
    for ln in text.split("\n"):
        if cur_len + len(ln) > size and cur:
            pages.append("\n".join(cur))
            cur, cur_len = [], 0
        while len(ln) > size:  # 매우 긴 한 줄
            pages.append(ln[:size])
            ln = ln[size:]
        cur.append(ln)
        cur_len += len(ln) + 1
    if cur:
        pages.append("\n".join(cur))
    return pages or [""]


def show_text_box(text: str, height: int = 450):
    st.html(
        f"<div style='height:{height}px;overflow-y:auto;white-space:pre-wrap;"
        "font-size:14px;line-height:1.6;padding:10px;border:1px solid #ddd;"
        f"border-radius:6px'>{html.escape(text)}</div>"
    )


# ---------------- 한국어 번역·요약 (Claude API) ----------------
DEFAULT_MODEL = "claude-sonnet-5-5"
SUMMARY_MAX_CHARS = 300_000
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_cache.json")

TRANSLATE_SYSTEM = (
    "당신은 미국 증권 공시 전문 번역가입니다. 영어 원문을 한국어로 번역합니다.\n"
    "- 원문과 1:1로 대응시키고, 임의 의역·누락·추가를 하지 않습니다.\n"
    "- 숫자, 금액, 날짜, 비율은 원문 그대로 정확히 옮깁니다. 통화 단위는 원문 표기를 유지합니다.\n"
    "- 문체는 평서문(~합니다)을 사용합니다.\n"
    "- 회사명·인명은 처음 나올 때 '한국어(영어)' 형식으로 씁니다.\n"
    "- 표는 원문 구조를 유지해 줄 단위로 옮깁니다.\n"
    "- 번역문만 출력하고 설명이나 인사말은 쓰지 않습니다."
)

SUMMARY_SYSTEM = (
    "당신은 미국 증권 공시를 한국 개인 투자자에게 설명하는 분석가입니다.\n"
    "주어진 공시 원문을 한국어로 요약합니다.\n"
    "형식:\n"
    "1. 한 줄 요약\n"
    "2. 핵심 내용 (불릿 5~10개, 중요한 숫자·날짜 포함)\n"
    "3. 투자자가 눈여겨볼 점 (불릿 2~4개)\n"
    "규칙: 원문에 없는 내용은 쓰지 않습니다. 숫자는 원문 그대로 씁니다. "
    "매수·매도 권유는 하지 않습니다. 원문이 잘려 있으면 마지막에 '※ 원문 앞부분만 요약함'이라고 적습니다. "
    "문체는 평서문(~합니다)을 사용합니다."
)


@st.cache_resource
def ai_store():
    """번역·요약 결과 저장소(모든 이용자 공유) + 일일 사용 횟수"""
    data = {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        pass
    return {"results": data, "usage": {}, "lock": threading.Lock()}


def save_store(store):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(store["results"], f, ensure_ascii=False)
    except Exception:
        pass


def today_key() -> str:
    return (dt.datetime.utcnow() + dt.timedelta(hours=9)).strftime("%Y-%m-%d")  # 한국 시간 기준


def escape_md(text: str) -> str:
    """화면 표시용: $ 기호가 수식으로 바뀌는 것 방지"""
    return text.replace("$", "\\$")


def claude_stream(system: str, user_text: str, max_tokens: int):
    import anthropic

    client = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
    params = dict(
        model=get_secret("CLAUDE_MODEL", DEFAULT_MODEL),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_text}],
    )
    # 생각(추론) 단계를 끄면 비용이 줄어듭니다. 모델이 지원하지 않으면 끄지 않고 다시 시도합니다.
    try:
        stream_ctx = client.messages.stream(thinking={"type": "disabled"}, **params)
        stream = stream_ctx.__enter__()
    except Exception as e:
        if "thinking" not in str(e).lower() and "400" not in str(e):
            raise
        stream_ctx = client.messages.stream(**params)
        stream = stream_ctx.__enter__()
    try:
        for chunk in stream.text_stream:
            yield chunk
    finally:
        stream_ctx.__exit__(None, None, None)


def run_ai(key: str, system: str, user_text: str, max_tokens: int):
    """저장된 결과가 있으면 재사용, 없으면 Claude 호출(화면에 실시간 표시)"""
    store = ai_store()
    if key in store["results"]:
        st.markdown(escape_md(store["results"][key]))
        st.caption("저장된 결과를 다시 표시했습니다 (추가 비용 없음).")
        return

    limit = int(get_secret("DAILY_LIMIT", 30))
    with store["lock"]:
        used = store["usage"].get(today_key(), 0)
        if used >= limit:
            st.warning(f"오늘 사용 한도({limit}회)를 모두 사용했습니다. 내일 다시 이용하십시오.")
            return
        store["usage"][today_key()] = used + 1

    parts = []

    def gen():
        for chunk in claude_stream(system, user_text, max_tokens):
            parts.append(chunk)
            yield escape_md(chunk)

    try:
        st.write_stream(gen())
    except Exception as e:
        st.error("번역·요약 중 오류가 발생했습니다. 잠시 후 다시 시도하십시오.")
        st.caption(f"오류 내용: {e}")
        return
    result = "".join(parts)
    if result.strip():
        store["results"][key] = result
        save_store(store)
    st.caption(f"오늘 사용: {store['usage'][today_key()]}/{limit}회")


def ai_unlocked() -> bool:
    pw = get_secret("APP_PASSWORD")
    if not pw:
        return True
    if st.session_state.get("ai_ok"):
        return True
    entered = st.text_input("번역·요약 비밀번호", type="password", key="ai_pw")
    if entered:
        if entered == pw:
            st.session_state["ai_ok"] = True
            return True
        st.error("비밀번호가 맞지 않습니다.")
    return False


# ---------------- 화면 ----------------
st.title("📄 미국 공시 조회")
st.caption("미국 증권거래위원회(SEC) 공시를 한글 종목명으로 찾아봅니다.")

if not get_user_agent():
    st.error("관리자 설정 필요: Streamlit Secrets에 SEC_USER_AGENT 값을 입력하십시오.")
    st.stop()

try:
    with st.spinner("종목 목록을 불러오는 중..."):
        companies = load_companies()
except Exception as e:
    st.error("SEC 종목 목록을 불러오지 못했습니다. 잠시 후 다시 시도하십시오.")
    st.caption(f"오류 내용: {e}")
    st.stop()

kr_names = load_kr_names()

query = st.text_input("종목명 또는 티커", placeholder="예: 애플, 엔비, 테슬라, MSFT")

if not query:
    st.info("종목명을 입력하십시오. 한글명 일부만 입력해도 됩니다.")
    st.stop()

results = search(query, companies, kr_names)
if results.empty:
    st.warning("검색 결과가 없습니다. 영문 회사명이나 티커(예: AAPL)로 검색해 보십시오.")
    st.stop()

labels = [display_name(r.ticker, r.name, kr_names) + f" · {r.name}" for r in results.itertuples()]
if len(results) == 1:
    idx = 0
else:
    idx = st.selectbox(
        f"검색 결과 {len(results)}개", range(len(results)), format_func=lambda i: labels[i]
    )

sel = results.iloc[idx]
st.subheader(display_name(sel["ticker"], sel["name"], kr_names))
st.caption(sel["name"])

try:
    with st.spinner("공시 목록을 불러오는 중..."):
        filings = load_filings(int(sel["cik"]))
except Exception as e:
    st.error("공시 목록을 불러오지 못했습니다. 잠시 후 다시 시도하십시오.")
    st.caption(f"오류 내용: {e}")
    st.stop()

if filings.empty:
    st.info("최근 공시가 없습니다.")
    st.stop()

forms = filings["form"].value_counts().index.tolist()
form_labels = {f: f"{f} ({describe_form(f)})" for f in forms}
chosen = st.multiselect(
    "공시 종류 선택 (비워 두면 전체)", forms, format_func=lambda f: form_labels[f]
)
count = st.slider("표시 개수", 10, 200, 30, step=10)

view = filings if not chosen else filings[filings["form"].isin(chosen)]
view = view.head(count)

st.write(f"총 {len(view)}건")
table = pd.DataFrame(
    {
        "제출일": view["filingDate"],
        "종류": view["form"],
        "설명": view["desc"],
        "원문": view["link"],
    }
)
st.dataframe(
    table,
    hide_index=True,
    width="stretch",
    column_config={"원문": st.column_config.LinkColumn("원문", display_text="열기")},
)

with st.expander("공시 종류 설명 보기"):
    st.markdown(
        "- **10-K**: 연간 보고서 (1년 실적·사업 현황)\n"
        "- **10-Q**: 분기 보고서\n"
        "- **8-K**: 실적 발표·경영진 변경 등 주요 사항 수시 공시\n"
        "- **4**: 임원·대주주의 주식 매매 보고\n"
        "- **DEF 14A**: 주주총회 안건·임원 보수 자료\n"
        "- **/A**가 붙은 것: 이전 공시의 정정본"
    )

# ---------------- 공시 본문 보기 · 번역 · 요약 ----------------
st.divider()
st.subheader("공시 내용 보기")

if view.empty:
    st.stop()

opts = list(range(len(view)))
pick = st.selectbox(
    "공시 선택",
    opts,
    format_func=lambda i: f"{view.iloc[i]['filingDate']} · {view.iloc[i]['form']} ({view.iloc[i]['desc']})",
)
filing = view.iloc[pick]
cik_int = int(sel["cik"])

docs = list_documents(cik_int, filing["accessionNumber"], filing["primaryDocument"])
if not docs:
    st.info("앱에서 볼 수 있는 문서가 없습니다. 위 표의 '열기'로 SEC 원문을 확인하십시오.")
    st.stop()

if len(docs) > 1:
    if filing["form"].startswith("8-K"):
        st.caption("8-K는 실적 등 주요 내용이 '첨부(보도자료 등)'에 들어 있는 경우가 많습니다.")
    d_idx = st.selectbox(
        "문서 선택",
        range(len(docs)),
        format_func=lambda i: doc_label(docs[i]["name"], i == 0 and bool(filing["primaryDocument"])),
    )
else:
    d_idx = 0
doc = docs[d_idx]

try:
    with st.spinner("문서를 불러오는 중... (큰 문서는 10~30초 걸릴 수 있습니다)"):
        full_text = fetch_text(doc["url"])
except Exception as e:
    st.error("문서를 불러오지 못했습니다. 위 표의 '열기'로 SEC 원문을 확인하십시오.")
    st.caption(f"오류 내용: {e}")
    st.stop()

if not full_text:
    st.info("글자로 된 내용이 없는 문서입니다.")
    st.stop()

pages = split_pages(full_text)
if len(pages) > 1:
    page_no = st.number_input(
        f"쪽 번호 (전체 {len(pages)}쪽, 1쪽당 약 {PAGE_CHARS:,}자)", 1, len(pages), 1, step=1
    )
else:
    page_no = 1
page_text = pages[page_no - 1]

mode = st.radio("보기 방식", ["원문", "한국어 번역", "한국어 요약"], horizontal=True)

if mode == "원문":
    show_text_box(page_text)
    st.caption(f"[SEC 원문 열기]({doc['url']})")
else:
    if not get_secret("ANTHROPIC_API_KEY"):
        st.info("번역·요약 기능이 아직 설정되지 않았습니다. (관리자: Secrets에 ANTHROPIC_API_KEY 입력)")
    elif ai_unlocked():
        model = get_secret("CLAUDE_MODEL", DEFAULT_MODEL)
        if mode == "한국어 번역":
            st.caption(f"현재 {page_no}쪽을 번역합니다. 다른 쪽은 쪽 번호를 바꾼 뒤 다시 누르십시오.")
            key = f"tr|{model}|{doc['url']}|{page_no}|{PAGE_CHARS}"
            if key in ai_store()["results"] or st.button("이 쪽 번역하기", type="primary"):
                run_ai(key, TRANSLATE_SYSTEM, page_text, max_tokens=16000)
        else:
            cut = len(full_text) > SUMMARY_MAX_CHARS
            if cut:
                st.caption(f"문서가 길어 앞부분 {SUMMARY_MAX_CHARS:,}자만 요약합니다.")
            key = f"sum|{model}|{doc['url']}"
            if key in ai_store()["results"] or st.button("문서 요약하기", type="primary"):
                src = full_text[:SUMMARY_MAX_CHARS]
                header = f"회사: {sel['name']} ({sel['ticker']})\n공시 종류: {filing['form']}\n제출일: {filing['filingDate']}\n"
                if cut:
                    header += "(원문이 길어 앞부분만 제공됨)\n"
                run_ai(key, SUMMARY_SYSTEM, header + "\n" + src, max_tokens=4000)
        st.caption("AI 번역·요약은 오류가 있을 수 있습니다. 중요한 내용은 원문으로 확인하십시오.")

st.caption("자료 출처: SEC EDGAR. 투자 판단의 책임은 이용자에게 있습니다.")
