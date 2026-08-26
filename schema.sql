-- CGV 알림기 스키마. store.init_db()가 매번 실행하므로 전부 멱등이어야 한다.

-- 로그인 사용자. provider는 naver · kakao, 그리고 개발모드의 local이다
-- (local은 비밀번호 없이 이름만으로 들어오는 계정 — auth.local_login_state 참고).
--   신원은 (provider, provider_user_id)로 잡는다. 카카오는 이메일이 선택 동의라
--   사용자가 거부하면 아예 오지 않고, 이메일을 키로 쓰면 같은 이메일의 다른
--   provider 계정과 뭉개진다. 이메일·닉네임은 표시용으로만 둔다.
--   status: pending(승인 대기) | approved | blocked
CREATE TABLE IF NOT EXISTS users (
    id                   serial PRIMARY KEY,
    provider             text NOT NULL,
    provider_user_id     text NOT NULL,
    nickname             text,
    email                text,
    profile_image        text,
    status               text    NOT NULL DEFAULT 'pending',
    is_owner             boolean NOT NULL DEFAULT false,
    -- 알림 웹훅. 비어 있으면 .env의 전역 웹훅으로 보낸다.
    --   webhook_kind: slack | discord — 같은 문구를 서비스별 문법으로 바꿔 보낸다.
    webhook_url          text,
    webhook_kind         text NOT NULL DEFAULT 'slack',
    -- 사용자별 취향. 확인 간격·headless 같은 서버 공용 설정은 settings에 남는다.
    include_showtimes    boolean NOT NULL DEFAULT true,
    lookahead_days       integer NOT NULL DEFAULT 0,
    default_screen_types text[]  NOT NULL DEFAULT '{}',
    created_at           timestamptz NOT NULL DEFAULT now(),
    last_login_at        timestamptz,
    UNIQUE (provider, provider_user_id)
);

-- 소유자는 한 명뿐이다 — 첫 로그인 계정이 가져간다.
CREATE UNIQUE INDEX IF NOT EXISTS users_single_owner_idx
    ON users ((is_owner)) WHERE is_owner;

-- 전역 설정. config.toml은 최초 1회 시드로만 쓰이고, 이후 진짜 출처는 이 표다.
--   poll_interval_seconds · include_showtimes · lookahead_days · headless
--   default_screen_types · session_recycle_minutes
--   config_error_signature · global_fail_count  (운영 상태값)
CREATE TABLE IF NOT EXISTS settings (
    key        text PRIMARY KEY,
    value      jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 감시 대상. config.toml의 sites = [...] 배열은 (영화, 극장) 한 행씩으로 펼친다.
-- 예전 state.json 키 "영화|극장"과 1:1로 대응해 비교 로직이 단순해진다.
CREATE TABLE IF NOT EXISTS watch_targets (
    id           serial PRIMARY KEY,
    movie_query  text   NOT NULL,
    site_query   text   NOT NULL,
    screen_types text[] NOT NULL DEFAULT '{}',
    enabled      boolean     NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (movie_query, site_query)
);

-- 이전 관측 결과. 여기 없는 대상은 "첫 관측"으로 취급해 알림 없이 기준선만 잡는다.
--   status: unknown(아직 확인 못 함) | not_open(예매 오픈 전) | tracking(추적 중)
--   dates         — 비교 기준이 되는 날짜 (필터가 없을 때). lookahead_days를
--                   쓰면 그 범위 안의 날짜만 담는다 — 범위 밖 날짜를 여기 넣으면
--                   나중에 범위 안으로 들어와도 "이미 아는 날짜"가 되어 그 날짜의
--                   알림이 영구히 사라진다. 필터가 있으면 열린 전체 날짜가 들어온다.
--   matched_dates — screen_types 필터에 걸린 날짜 (필터가 있을 때의 비교 기준)
CREATE TABLE IF NOT EXISTS watch_state (
    target_id     integer PRIMARY KEY REFERENCES watch_targets(id) ON DELETE CASCADE,
    status        text    NOT NULL DEFAULT 'unknown',
    mov_no        text,
    site_no       text,
    mov_nm        text,
    site_nm       text,
    dates         text[]  NOT NULL DEFAULT '{}',
    matched_dates text[]  NOT NULL DEFAULT '{}',
    screen_types  text[]  NOT NULL DEFAULT '{}',
    fail_count    integer NOT NULL DEFAULT 0,
    last_ok       timestamptz,
    last_error    text,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- 날짜별 상영 시간표 캐시. payload는 CGV 응답 원본 배열을 그대로 담는다.
-- (컬럼명 rows는 SQL 키워드와 헷갈리기 쉬워 payload로 둔다.)
CREATE TABLE IF NOT EXISTS showtimes (
    target_id  integer     NOT NULL REFERENCES watch_targets(id) ON DELETE CASCADE,
    scn_ymd    text        NOT NULL,
    payload    jsonb       NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (target_id, scn_ymd)
);

-- 알림 이력. delivered=false로 먼저 남기고 웹훅 전송이 성공하면 true로 올린다.
-- 대상이 삭제돼도 이력은 남겨야 하므로 target_id는 SET NULL.
CREATE TABLE IF NOT EXISTS alerts (
    id           serial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    kind         text        NOT NULL,
    target_id    integer REFERENCES watch_targets(id) ON DELETE SET NULL,
    mov_nm       text,
    site_nm      text,
    dates        text[]  NOT NULL DEFAULT '{}',
    body         text    NOT NULL,
    delivered    boolean NOT NULL DEFAULT false,
    delivered_at timestamptz
);

CREATE INDEX IF NOT EXISTS alerts_created_at_idx ON alerts (created_at DESC);

-- 폴링 사이클 이력. 대시보드의 "마지막 확인"과 소요 시간 추이가 여기서 나온다.
CREATE TABLE IF NOT EXISTS poll_cycles (
    id              bigserial PRIMARY KEY,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    ok              boolean,
    trigger         text    NOT NULL DEFAULT 'schedule',  -- schedule | manual | cli
    targets_checked integer NOT NULL DEFAULT 0,
    requests        integer NOT NULL DEFAULT 0,
    new_dates       integer NOT NULL DEFAULT 0,
    error           text
);

CREATE INDEX IF NOT EXISTS poll_cycles_started_at_idx ON poll_cycles (started_at DESC);

-- 영화·극장 목록 캐시. 감시 대상 편집 화면이 이름 대신 코드를 확정해 저장하도록 쓴다.
CREATE TABLE IF NOT EXISTS catalog_movies (
    mov_no       text PRIMARY KEY,
    mov_nm       text NOT NULL,
    atkt_rate    numeric,
    refreshed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS catalog_sites (
    site_no      text PRIMARY KEY,
    site_nm      text NOT NULL,
    region       text,
    refreshed_at timestamptz NOT NULL DEFAULT now()
);

-- ── 소유권 ──────────────────────────────────────────────────────────────────
-- CREATE TABLE IF NOT EXISTS는 이미 있는 표를 건드리지 않으므로, 나중에 붙인
-- 컬럼·제약은 ALTER로 따로 적용해야 한다. 아래도 전부 멱등이다.

ALTER TABLE watch_targets ADD COLUMN IF NOT EXISTS
    owner_id integer REFERENCES users(id) ON DELETE CASCADE;

-- alerts.target_id는 ON DELETE SET NULL이라 대상을 지우면 소유자를 잃는다.
-- 이력은 남아야 하므로 소유자를 직접 들고 있는다.
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS
    owner_id integer REFERENCES users(id) ON DELETE SET NULL;

-- 전송이 실패하면 상태를 밀지 않아 다음 확인에서 같은 알림을 다시 시도한다.
-- 웹훅이 계속 죽어 있으면 30초마다 똑같은 행이 쌓이므로, 새 행을 만드는 대신
-- 아직 못 보낸 같은 알림의 시도 횟수를 올린다.
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS
    attempts integer NOT NULL DEFAULT 1;

-- 같은 영화×극장을 사용자마다 따로 감시할 수 있어야 한다.
ALTER TABLE watch_targets
    DROP CONSTRAINT IF EXISTS watch_targets_movie_query_site_query_key;
CREATE UNIQUE INDEX IF NOT EXISTS watch_targets_owner_movie_site_idx
    ON watch_targets (owner_id, movie_query, site_query);

CREATE INDEX IF NOT EXISTS watch_targets_owner_idx ON watch_targets (owner_id);
CREATE INDEX IF NOT EXISTS alerts_owner_idx ON alerts (owner_id, created_at DESC);

-- ── 웹훅: Slack 전용 → Slack·Discord ────────────────────────────────────────
-- 예전 이름 slack_webhook_url을 Discord도 담을 수 있게 webhook_url로 옮긴다.
-- RENAME에는 IF NOT EXISTS가 없으므로 옮길 컬럼이 남아 있을 때만 실행한다.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = 'slack_webhook_url')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'users' AND column_name = 'webhook_url')
    THEN
        ALTER TABLE users RENAME COLUMN slack_webhook_url TO webhook_url;
    END IF;
END $$;

ALTER TABLE users ADD COLUMN IF NOT EXISTS webhook_url  text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS webhook_kind text NOT NULL DEFAULT 'slack';

-- ── CGV 로그인 자격증명 (Phase 1) ───────────────────────────────────────────
-- 감시 대상 CGV 계정의 아이디·비밀번호를 사용자별로 보관한다. 로그인은
-- oidc.cgv.co.kr/cjone/cjoneLogin에 비밀번호의 SHA-256(hex)을 보내는 방식이라
-- 서버가 원문 비밀번호를 다시 볼 일은 없지만, CGV 클라이언트가 매 로그인마다
-- 원문을 직접 해시하므로 우리도 원문을 되찾을 수 있어야 한다. 그래서 단방향
-- 해시가 아니라 되돌릴 수 있는 암호문으로 저장한다 (secretbox.encrypt).
--   password_enc — Fernet 암호문. 키는 .env의 CGV_CRED_KEY (secretbox 참고).
--   status: unlinked(아직 로그인 안 해 봄) | linked(마지막 로그인 성공)
--           | error(마지막 로그인 실패 — last_error에 사유)
CREATE TABLE IF NOT EXISTS cgv_accounts (
    owner_id      integer PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    cgv_user_id   text        NOT NULL,
    password_enc  bytea       NOT NULL,
    status        text        NOT NULL DEFAULT 'unlinked',
    last_login_at timestamptz,
    last_error    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- 로그인 성공 시 발급되는 세션 쿠키(accessToken·refresh_token 등)를 암호문으로
-- 보관한다. 폴링마다 캡차를 풀지 않도록 재사용하고, accessToken이 만료되면
-- refresh_token으로 갱신한다. 둘 다 만료되면 지우고 다시 로그인한다.
ALTER TABLE cgv_accounts ADD COLUMN IF NOT EXISTS session_enc bytea;

-- ── 좌석 감시 (Phase 1) ──────────────────────────────────────────────────────
-- 특정 날짜의 회차에서 원하는 열(row)에 빈좌석이 생기는지 본다. 감시 대상(영화×
-- 극장)과 달리 **날짜가 고정**이고, screen_types로 상영관을, rows로 열을 좁힌다.
--   rows        — 감시할 열 이름들(예: {A,B}). 비면 전 열.
--   screen_types — IMAX 등 상영관 필터. 비면 그 날짜의 모든 회차.
CREATE TABLE IF NOT EXISTS seat_watches (
    id           serial PRIMARY KEY,
    owner_id     integer REFERENCES users(id) ON DELETE CASCADE,
    movie_query  text   NOT NULL,
    site_query   text   NOT NULL,
    scn_ymd      text   NOT NULL,
    screen_types text[] NOT NULL DEFAULT '{}',
    rows         text[] NOT NULL DEFAULT '{}',
    enabled      boolean     NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, movie_query, site_query, scn_ymd, screen_types, rows)
);

CREATE INDEX IF NOT EXISTS seat_watches_owner_idx ON seat_watches (owner_id);

-- 좌석 감시의 직전 관측. available은 회차별로 비어 있던 좌석 라벨 집합을 담는다:
--   { "<scnsNo>|<scnSseq>": ["A6","B1", ...], ... }
-- 여기 없는 회차·좌석은 "처음 본 것"이라 기준선만 잡고 알리지 않는다.
CREATE TABLE IF NOT EXISTS seat_watch_state (
    seat_watch_id integer PRIMARY KEY REFERENCES seat_watches(id) ON DELETE CASCADE,
    available     jsonb       NOT NULL DEFAULT '{}',
    last_ok       timestamptz,
    last_error    text,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- 연속(나란히 붙은) 좌석 감시. min_consecutive석 이상 나란히 빈자리가 새로
-- 생기면 알린다. 0·1이면 개별 좌석 감시(기본).
ALTER TABLE seat_watches ADD COLUMN IF NOT EXISTS
    min_consecutive integer NOT NULL DEFAULT 0;

-- ── 자동 예매(좌석 선점) (Phase 1 auto-book) ─────────────────────────────────
-- 좌석 감시에 자동 선점 옵션을 붙인다. auto_book이 켜지면 빈자리(또는 연속 블록)가
-- 감지될 때 인원수만큼 좌석을 골라 CGV에 임시 선점(seatTempPrmp)까지 하고, 결제
-- 확정은 사람이 한다. party_size는 잡을 좌석 수, ticket_spec은 권종별 수량이다.
--   ticket_spec 예: {"adult": 2}  (권종 코드 매핑은 booking.py)
ALTER TABLE seat_watches ADD COLUMN IF NOT EXISTS
    auto_book   boolean NOT NULL DEFAULT false;
ALTER TABLE seat_watches ADD COLUMN IF NOT EXISTS
    party_size  integer NOT NULL DEFAULT 1;
ALTER TABLE seat_watches ADD COLUMN IF NOT EXISTS
    ticket_spec jsonb   NOT NULL DEFAULT '{}';

-- 선점 시도 이력. 대상이 삭제돼도 이력은 남기므로 seat_watch_id는 SET NULL,
-- 소유자도 SET NULL로 남긴다.
--   status: pending(시도 중) | held(선점 성공) | failed(실패) | expired(만료)
--           | cancelled(취소)
--   mov_atkt_no          — 선점 성공 시 CGV가 준 예매번호
--   hold_expires_at      — 선점 만료 시각(이때까지 결제해야 한다)
CREATE TABLE IF NOT EXISTS booking_attempts (
    id             bigserial PRIMARY KEY,
    seat_watch_id  integer REFERENCES seat_watches(id) ON DELETE SET NULL,
    owner_id       integer REFERENCES users(id) ON DELETE SET NULL,
    showtime_key   text,
    mov_nm         text,
    site_nm        text,
    scn_ymd        text,
    start_hhmm     text,
    seat_labels    text[] NOT NULL DEFAULT '{}',
    seat_loc_nos   text[] NOT NULL DEFAULT '{}',
    mov_atkt_no    text,
    amount         integer,
    status         text NOT NULL DEFAULT 'pending',
    hold_expires_at timestamptz,
    last_error     text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS booking_attempts_owner_idx
    ON booking_attempts (owner_id, created_at DESC);

-- ── 좌석 감시에 상영 시간(회차) 지정 ─────────────────────────────────────────
-- 지금까지 좌석 감시는 (영화×극장×날짜)라 그 날짜의 모든 회차를 봤다. 특정 회차만
-- 보려면 scn_time(HH:MM)을 지정한다. 비어 있으면 예전처럼 모든 회차를 본다.
ALTER TABLE seat_watches ADD COLUMN IF NOT EXISTS scn_time text NOT NULL DEFAULT '';

-- 같은 날짜의 서로 다른 회차를 따로 감시할 수 있도록, 유일성에 scn_time을 넣는다.
-- 기존 인라인 UNIQUE(scn_time 없음)를 지우고 새 유니크 인덱스로 바꾼다(멱등).
DO $$
DECLARE c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'seat_watches'::regclass AND contype = 'u' LOOP
        EXECUTE 'ALTER TABLE seat_watches DROP CONSTRAINT ' || quote_ident(c.conname);
    END LOOP;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS seat_watches_uniq_idx
    ON seat_watches (owner_id, movie_query, site_query, scn_ymd, scn_time,
                     screen_types, rows);


-- ── 좌석 감시에 상영 시간 '범위' 지정 ────────────────────────────────────────
-- scn_time은 회차 하나를 콕 집는 값이라, **아직 상영표가 열리지 않은** 영화에는
-- 쓸 수 없다 — 화면의 회차 드롭다운이 그 조합의 실제 회차로 채워지기 때문이다.
-- 미상영 영화를 미리 걸어 두려면 "이 시간대 안이면 아무 회차나"라고 말할 수
-- 있어야 한다. 그 시간대가 scn_time_from ~ scn_time_to (둘 다 'HH:MM')이다.
--
-- 범위 안에 회차가 여럿이면 **늦은 회차부터** 선점한다 (seats.select_showtimes).
-- to < from이면 자정을 넘긴 것으로 본다: 22:00~02:00 = 22:00~26:00.
-- CGV가 심야 회차를 '2530'처럼 24시 이상으로 주는 표기와 같은 뜻이다.
ALTER TABLE seat_watches ADD COLUMN IF NOT EXISTS
    scn_time_from text NOT NULL DEFAULT '';
ALTER TABLE seat_watches ADD COLUMN IF NOT EXISTS
    scn_time_to   text NOT NULL DEFAULT '';

-- 같은 날짜에 시간대만 다른 감시를 따로 둘 수 있어야 한다 — 유일성에도 넣는다.
--
-- 이 파일은 서버가 뜰 때마다 통째로 실행된다(store.init_db). 그래서 예전 인덱스를
-- DROP하고 다시 만드는 대신 **이름을 새로 쓴다** — IF NOT EXISTS가 두 번째
-- 기동부터 통째로 건너뛰므로 매번 인덱스를 다시 세우지 않는다. 컬럼이 늘기만 한
-- 인덱스라 기존 행이 새 조건을 어길 일은 없다.
CREATE UNIQUE INDEX IF NOT EXISTS seat_watches_uniq_range_idx
    ON seat_watches (owner_id, movie_query, site_query, scn_ymd, scn_time,
                     scn_time_from, scn_time_to, screen_types, rows);

DROP INDEX IF EXISTS seat_watches_uniq_idx;


-- ── 알림이 어느 좌석 감시에서 나왔는지 ───────────────────────────────────────
-- 미전송 알림을 다시 시도할 때 새 행을 만들지 않고 기존 행을 재사용하는데
-- (store.record_alert), 그 "같은 알림" 판정에 좌석 감시가 빠져 있었다. 좌석
-- 알림은 target_id가 없고 dates가 [scn_ymd] 하나뿐이라, **한 사람이 같은 날짜에
-- 건 좌석 감시가 둘이면** 서로의 미전송 알림을 덮어쓴다 — 한쪽 알림이 조용히
-- 사라진다. 어느 감시에서 나왔는지를 행에 남겨 판정에 함께 넣는다.
-- 감시가 지워져도 이력은 남아야 하므로 SET NULL이다.
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS
    seat_watch_id integer REFERENCES seat_watches(id) ON DELETE SET NULL;


-- ── 자동 결제(카카오페이) ────────────────────────────────────────────────────
-- 선점까지 해 두고 사람이 CGV 앱을 다시 열어 결제수단부터 고르는 게 실제로는
-- 느렸다. auto_pay를 켜면 선점에 이어 결제 화면에서 **카카오페이를 고르고 약관에
-- 동의해 결제를 요청**하는 데까지 간다. 돈이 실제로 나가는 마지막 승인(카카오톡
-- 인증·비밀번호)은 여전히 사람이 자기 기기에서 한다 — 시스템이 대신할 수 없고,
-- 대신해서도 안 된다. 그래서 알림에 **카카오페이 결제 링크**를 실어 보낸다.
--
-- pay_method는 지금 'kakaopay' 하나뿐이지만, 결제수단이 늘 수 있으니 컬럼으로
-- 둔다(booking.PAY_METHODS).
ALTER TABLE seat_watches ADD COLUMN IF NOT EXISTS
    auto_pay   boolean NOT NULL DEFAULT false;
ALTER TABLE seat_watches ADD COLUMN IF NOT EXISTS
    pay_method text NOT NULL DEFAULT 'kakaopay';

-- 결제 요청 결과. 링크는 몇 분 만에 죽으므로 만료 시각을 함께 남긴다 —
-- 이력 화면에서 "이미 지난 링크"를 눌러 보게 두면 안 된다.
--   pay_url        — 사람이 휴대폰에서 열어 결제를 마칠 카카오페이 주소
--   pay_expires_at — 그 링크가 만료되는 시각(카카오페이가 알려준 값)
--   pay_error      — 선점은 됐는데 결제 요청까지 못 간 사유
ALTER TABLE booking_attempts ADD COLUMN IF NOT EXISTS pay_method     text;
ALTER TABLE booking_attempts ADD COLUMN IF NOT EXISTS pay_url        text;
ALTER TABLE booking_attempts ADD COLUMN IF NOT EXISTS pay_expires_at timestamptz;
ALTER TABLE booking_attempts ADD COLUMN IF NOT EXISTS pay_error      text;
