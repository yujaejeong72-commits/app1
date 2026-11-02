"""둘의 하루 · Streamlit 커플 다이어리

배포: 이 파일과 requirements.txt를 GitHub 저장소에 올린 뒤 Streamlit Community
Cloud에서 app.py를 선택합니다. 의존성 파일 이름은 반드시 requirements.txt입니다.
Supabase 무료 프로젝트의 SQL Editor에서 아래 SETUP_SQL을 한 번 실행하고,
Streamlit 앱 Settings > Secrets에 다음 값을 저장합니다(절대 GitHub에 올리지 않음).

SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "YOUR_SERVER_ONLY_SERVICE_ROLE_KEY"
APP_URL = "https://YOUR_APP.streamlit.app"

QR은 APP_URL로 접속하는 용도이며 초대 코드나 비밀번호는 포함하지 않습니다.
두 사람은 같은 초대 코드, 각자 다른 이름과 비밀번호로 로그인합니다.
관리자용 키는 서버에서만 사용됩니다. 테이블 RLS를 활성화하고 공개 정책은
만들지 않습니다. 일기는 DB에 평문으로 저장되므로 운영자는 조회할 수 있습니다.
비밀번호 분실 복구/방 삭제 기능은 미제공입니다. 테스트 후 실제 사용하세요.
"""
import hashlib
import html
import hmac
import io
import secrets
from datetime import datetime, timezone, timedelta

import qrcode
import streamlit as st
from supabase import create_client

SETUP_SQL = """
create table if not exists public.couple_rooms (
 id uuid primary key default gen_random_uuid(),
 code_hash text unique not null,
 created_at timestamptz not null default now()
);
create table if not exists public.couple_members (
 id uuid primary key default gen_random_uuid(),
 room_id uuid not null references public.couple_rooms(id) on delete cascade,
 slot integer not null check (slot in (1,2)),
 name text not null check (char_length(name) between 1 and 12),
 salt text not null,
 password_hash text not null,
 unique(room_id, slot), unique(room_id, name)
);
create table if not exists public.couple_entries (
 id uuid primary key default gen_random_uuid(),
 author_id uuid not null references public.couple_members(id) on delete cascade,
 entry_date date not null,
 mood text not null,
 body text not null check (char_length(body) between 1 and 180),
 updated_at timestamptz not null default now(),
 read_at timestamptz,
 read_by uuid references public.couple_members(id),
 unique(author_id, entry_date)
);
alter table public.couple_rooms enable row level security;
alter table public.couple_members enable row level security;
alter table public.couple_entries enable row level security;
revoke all on public.couple_rooms, public.couple_members, public.couple_entries from anon, authenticated;
"""

st.set_page_config(page_title="둘의 하루", page_icon="💌", layout="centered", initial_sidebar_state="collapsed")
st.markdown("""<style>
.stApp {background:#fff8fa;}
.block-container {max-width:460px;padding:0.65rem 0.9rem 0.5rem;}
header[data-testid="stHeader"] {height:0;background:transparent;}
[data-testid="stToolbar"] {display:none;}
[data-testid="stVerticalBlock"] {gap:0.38rem;}
[data-testid="stHorizontalBlock"] {gap:0.5rem;}
h1 {font-size:1.35rem!important;padding:0!important;}
p {margin-bottom:0.25rem;}
[data-testid="stCaptionContainer"] {font-size:0.74rem;}
textarea {font-size:16px!important;}
[data-testid="stTextInput"] input {font-size:16px!important;}
.stButton button,.stFormSubmitButton button {min-height:2.2rem;border-radius:13px;}
.stButton button[kind="primary"],.stFormSubmitButton button[kind="primary"] {background:#c94d75;border-color:#c94d75;}
[data-testid="stForm"] {padding:0.55rem;border-color:#f1d7df;border-radius:16px;}
[data-testid="stAlert"] {padding:0.5rem;}
@media(max-width:480px){.block-container{padding-top:0.5rem;} }
</style>""", unsafe_allow_html=True)
KST = timezone(timedelta(hours=9))
MOODS = ["🥰 설렘", "😊 행복", "😌 평온", "😢 우울", "😤 속상", "😴 피곤"]


def now():
    return datetime.now(timezone.utc).isoformat()


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310000).hex()


def room_hash(code):
    return hashlib.sha256(code.strip().upper().replace("-", "").encode()).hexdigest()


def utc_label(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(KST).strftime("%m/%d %H:%M")
    except (ValueError, AttributeError):
        return "방금"


@st.cache_resource
def database(url, key):
    return create_client(url, key)


def rows(table, **filters):
    query = db.table(table).select("*")
    for key, value in filters.items():
        query = query.eq(key, value)
    return query.execute().data


def authentication():
    st.caption("둘만의 짧은 기록 · 초대 코드로 연결하세요")
    mode = st.radio("시작 방법", ["로그인", "상대방 초대로 가입", "새 커플 만들기"], horizontal=True, label_visibility="collapsed")
    with st.form("login"):
        code = st.text_input("초대 코드", placeholder="새 커플 만들기에서는 자동 생성", max_chars=32) if mode != "새 커플 만들기" else ""
        name = st.text_input("내 이름", max_chars=12)
        password = st.text_input("내 비밀번호 · 8자 이상", type="password", max_chars=128)
        submit = st.form_submit_button("함께 시작하기", type="primary", use_container_width=True)
    if not submit:
        return
    name = name.strip()
    if not name or len(password) < 8 or (mode != "새 커플 만들기" and not code.strip()):
        st.warning("이름, 비밀번호 8자 이상, 초대 코드를 확인해 주세요.")
        return
    # 잘못된 로그인은 세션 기준 지수 지연. 공개 대규모 서비스에는 별도 인증/속도 제한 권장.
    import time
    if time.time() < st.session_state.get("retry_after", 0):
        st.warning("잠시 후 다시 시도해 주세요.")
        return
    try:
        if mode == "새 커플 만들기":
            code = secrets.token_hex(8).upper()
            room = db.table("couple_rooms").insert({"code_hash": room_hash(code)}).execute().data[0]
            slot = 1
        else:
            rooms = rows("couple_rooms", code_hash=room_hash(code))
            if not rooms:
                raise ValueError("초대 코드 또는 로그인 정보를 확인해 주세요.")
            room = rooms[0]
        members = rows("couple_members", room_id=room["id"])
        if mode == "로그인":
            member = next((m for m in members if m["name"] == name), None)
            if not member or not hmac.compare_digest(member["password_hash"], password_hash(password, member["salt"])):
                raise ValueError("초대 코드 또는 로그인 정보를 확인해 주세요.")
        else:
            if len(members) >= 2:
                raise ValueError("이미 두 사람이 연결된 커플입니다.")
            if any(m["name"] == name for m in members):
                raise ValueError("서로 다른 이름을 사용해 주세요.")
            slot = next(s for s in (1, 2) if not any(m["slot"] == s for m in members))
            salt = secrets.token_hex(16)
            member = db.table("couple_members").insert({"room_id": room["id"], "slot": slot, "name": name, "salt": salt, "password_hash": password_hash(password, salt)}).execute().data[0]
        st.session_state["identity"] = {k: member[k] for k in ("id", "room_id", "name")}
        st.session_state["invite_code"] = code.strip().upper().replace("-", "")
        st.session_state["failed_logins"] = 0
        st.rerun()
    except ValueError as exc:
        failures = st.session_state.get("failed_logins", 0) + 1
        st.session_state["failed_logins"] = failures
        st.session_state["retry_after"] = time.time() + min(60, 2 ** min(failures, 6))
        st.error(str(exc))
    except Exception:
        st.error("연결 또는 가입에 실패했습니다. DB 설정을 확인하거나, 가입이 완료됐다면 로그인해 주세요.")


st.title("💌 둘의 하루")
try:
    url = st.secrets.get("SUPABASE_URL", "")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", "")
except Exception:
    url = key = ""
if not url or not key:
    st.info("최초 설정이 필요해요. 아래 안내대로 저장소를 연결해 주세요.")
    st.markdown("1. Supabase 프로젝트 생성 → SQL Editor에서 아래 SQL 실행\n2. GitHub에 두 파일 업로드 → Streamlit Community Cloud 배포\n3. 앱 Settings → Secrets에 아래 설정 입력")
    st.code(SETUP_SQL, language="sql")
    st.code('SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"\nSUPABASE_SERVICE_ROLE_KEY = "YOUR_SERVER_ONLY_SERVICE_ROLE_KEY"\nAPP_URL = "https://YOUR_APP.streamlit.app"', language="toml")
    st.caption("키는 GitHub에 올리지 마세요. 공개 가입 서비스가 아닌 개인 커플용 소규모 앱입니다.")
    st.stop()

try:
    db = database(url, key)
except Exception:
    st.error("Supabase 연결 설정을 확인해 주세요.")
    st.stop()
if "identity" not in st.session_state:
    authentication()
    st.stop()

me = st.session_state.identity
head1, head2 = st.columns([4, 1])
with head1:
    st.caption(f"{me['name']}님의 작은 마음을 전해 보세요")
with head2:
    with st.popover("⚙️"):
        st.caption("상대방은 아래 코드로 가입하세요. 코드는 둘만 공유하세요.")
        st.code(st.session_state.get("invite_code", ""), language=None)
        public_url = str(st.secrets.get("APP_URL", "")).strip()
        if public_url.startswith("https://"):
            image = qrcode.make(public_url)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            st.image(buffer.getvalue(), width=180)
            st.download_button("접속 QR 저장", buffer.getvalue(), "couple-qr.png", "image/png")
            st.caption("QR 접속 후 각자 로그인하세요.")
        else:
            st.caption("Secrets에 HTTPS APP_URL을 설정하면 QR이 표시됩니다.")
        if st.button("로그아웃", use_container_width=True):
            st.session_state.clear()
            st.rerun()

try:
    members = rows("couple_members", room_id=me["room_id"])
    # 세션 계정이 DB에서 제거되었으면 접근 중단.
    if not any(m["id"] == me["id"] for m in members):
        st.session_state.clear()
        st.rerun()
    partner = next((m for m in members if m["id"] != me["id"]), None)
    today = datetime.now(KST).date()
    day = st.date_input("기록 날짜", value=today, max_value=today, label_visibility="collapsed")
    mine_list = rows("couple_entries", author_id=me["id"], entry_date=day.isoformat())
    mine = mine_list[0] if mine_list else None
    read_label = ("✓ 상대가 읽었어요 · " + utc_label(mine["read_at"])) if mine and mine["read_at"] else ("아직 읽기 전이에요" if mine else "오늘의 마음을 남겨 주세요")
    st.caption(f"내 기록 · {read_label}")
    draft_key = f"{me['id']}_{day.isoformat()}"
    with st.form("diary_" + draft_key, clear_on_submit=False):
        mood = st.selectbox("지금 기분", MOODS, index=MOODS.index(mine["mood"]) if mine and mine["mood"] in MOODS else 0, label_visibility="collapsed")
        body = st.text_area("내 일기", value=mine["body"] if mine else "", height=90, max_chars=180, placeholder="오늘 너에게 전하고 싶은 마음… (180자)", label_visibility="collapsed")
        saved = st.form_submit_button("💗 마음 저장하기", type="primary", use_container_width=True)
    if saved:
        if not body.strip():
            st.warning("한 줄이라도 마음을 적어 주세요.")
        else:
            db.table("couple_entries").upsert({"author_id": me["id"], "entry_date": day.isoformat(), "mood": mood, "body": body.strip(), "updated_at": now(), "read_at": None, "read_by": None}, on_conflict="author_id,entry_date").execute()
            st.toast("저장했어요. 수정하면 읽음 표시가 초기화돼요. 💗")
            st.rerun()

    partner_list = rows("couple_entries", author_id=partner["id"], entry_date=day.isoformat()) if partner else []
    entry = partner_list[0] if partner_list else None
    partner_name = partner["name"] if partner else "상대방"
    st.caption(f"💬 {partner_name}의 마음")
    with st.container(border=True):
        if entry:
            st.caption(f"{entry['mood']} · {utc_label(entry['updated_at'])}")
            # 일반 텍스트 출력: HTML/Markdown 주입 방지. 길이 180자로 화면 높이 제한.
            st.markdown('<div style="white-space:pre-wrap;overflow-wrap:anywhere;font-size:0.9rem;line-height:1.4">' + html.escape(entry["body"]) + "</div>", unsafe_allow_html=True)
        else:
            st.caption("아직 이 날짜의 일기가 없어요." if partner else "설정 ⚙️에서 초대 코드를 공유해 주세요.")
    left, right = st.columns([3, 1])
    with left:
        if entry:
            checked = st.checkbox("읽었어요 💕", value=bool(entry["read_at"]), key="read_" + entry["id"] + "_" + entry["updated_at"])
            if checked != bool(entry["read_at"]):
                # 수정된 새 버전에는 이전 버전을 읽은 체크가 덮어써지지 않음.
                result = db.table("couple_entries").update({"read_at": now() if checked else None, "read_by": me["id"] if checked else None}).eq("id", entry["id"]).eq("author_id", partner["id"]).eq("updated_at", entry["updated_at"]).execute()
                if not result.data:
                    st.toast("상대가 일기를 수정했어요. 새 내용을 확인해 주세요.")
                st.rerun()
    with right:
        if st.button("↻", help="상대방 일기·읽음 상태 새로고침", use_container_width=True):
            st.rerun()
except Exception:
    st.error("기록을 불러오거나 저장하지 못했어요. 연결·SQL 설정을 확인하고 ↻ 새로고침해 주세요.")
