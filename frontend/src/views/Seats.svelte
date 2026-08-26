<script>
  import { onMount } from 'svelte'
  import { get, post, put, del, SCREEN_TYPES } from '../lib/api.js'
  import { fmtAgo } from '../lib/format.js'

  let { onchange } = $props()

  // ── CGV 계정 ──
  let account = $state({ linked: false, status: 'none' })
  let cgvId = $state('')
  let cgvPw = $state('')
  let savingAccount = $state(false)
  let accountMsg = $state(null)
  let accountErr = $state(null)

  // ── 좌석 감시 ──
  let watches = $state([])
  let catalog = $state({ movies: [], sites: [], regions: [] })
  let region = $state('서울')
  let movie = $state('')
  let site = $state('')
  let ymd = $state('') // YYYY-MM-DD (입력) → 저장 시 YYYYMMDD로 보냄
  // 회차 고르는 방법: 'all' 모든 회차 · 'one' 특정 회차 · 'range' 시간대
  // 'range'는 아직 상영표가 열리지 않은 영화를 미리 걸어 둘 때 쓴다 — 회차
  // 드롭다운이 실제 회차로 채워지므로 미상영이면 'one'을 고를 수가 없다.
  let timeMode = $state('all')
  let scnTime = $state('') // 상영 시간(HH:MM). 비우면 모든 회차
  let timeFrom = $state('18:00') // 시간대 시작
  let timeTo = $state('23:59') // 시간대 끝 (시작보다 이르면 자정 넘김)
  let times = $state([]) // 선택한 영화·극장·날짜의 회차 시간 목록
  let loadingTimes = $state(false)
  let rowsText = $state('')
  let minConsecutive = $state(0) // 0·1 = 개별 좌석, 2+ = 나란히 붙은 N석
  let autoBook = $state(false) // 좌석 확보 시 자동 선점
  let autoPay = $state(false) // 선점에 이어 카카오페이 결제까지 요청
  let partySize = $state(1) // 잡을 좌석 수(인원)
  let types = $state(new Set())
  let savingWatch = $state(false)
  let watchMsg = $state(null)
  let watchErr = $state(null)

  let bookings = $state([])

  onMount(async () => {
    await Promise.all([loadAccount(), loadWatches(), loadCatalog(), loadBookings()])
  })

  async function loadBookings() {
    try {
      bookings = await get('/api/bookings')
    } catch (exc) {
      /* 이력 조회 실패는 조용히 */
    }
  }

  async function loadAccount() {
    try {
      account = await get('/api/cgv-account')
    } catch (exc) {
      accountErr = exc.message
    }
  }

  async function loadWatches() {
    try {
      watches = await get('/api/seat-watches')
    } catch (exc) {
      watchErr = exc.message
    }
  }

  async function loadCatalog() {
    try {
      catalog = await get('/api/catalog')
      if (!catalog.regions.includes(region)) region = catalog.regions[0] ?? ''
    } catch (exc) {
      watchErr = exc.message
    }
  }

  const sitesInRegion = $derived(catalog.sites.filter((s) => s.region === region))

  // 영화·극장·날짜가 모두 정해지면 그 조합의 회차 시간 목록을 받아 드롭다운을 채운다.
  $effect(() => {
    const m = catalog.movies.find((x) => x.mov_nm === movie)
    const s = catalog.sites.find((x) => x.site_nm === site)
    if (!m || !s || !ymd) {
      times = []
      return
    }
    loadTimes(m.mov_no, s.site_no, ymd.replaceAll('-', ''))
  })

  async function loadTimes(movNo, siteNo, ymdDigits) {
    loadingTimes = true
    try {
      const res = await post('/api/lookup/showtimes', {
        mov_no: movNo,
        site_no: siteNo,
        date: ymdDigits,
      })
      // groups: [{label, times:[HH:MM,...]}] → 시간만 모아 정렬·중복제거
      const all = (res.groups ?? []).flatMap((g) => g.times ?? [])
      times = [...new Set(all)].sort()
      if (scnTime && !times.includes(scnTime)) scnTime = '' // 없는 시간이면 초기화
      // 회차가 하나도 없으면 '특정 회차'는 고를 수 없다 — 시간대로 넘겨준다.
      if (!times.length && timeMode === 'one') timeMode = 'range'
    } catch (exc) {
      times = [] // 조회 실패 시 '모든 회차'로만 진행
    } finally {
      loadingTimes = false
    }
  }

  async function saveAccount() {
    accountMsg = null
    accountErr = null
    if (!cgvId.trim() || !cgvPw) {
      accountErr = 'CGV 아이디와 비밀번호를 입력하세요'
      return
    }
    savingAccount = true
    try {
      const result = await put('/api/cgv-account', {
        cgv_user_id: cgvId.trim(),
        password: cgvPw,
      })
      account = result.account
      cgvPw = '' // 원문은 화면에 남기지 않는다
      accountMsg = result.logged_in
        ? '저장하고 로그인까지 확인했습니다'
        : '저장했습니다. 로그인은 아직 확인되지 않았습니다 (CGV 상태에 따라 다음 확인에서 재시도합니다).'
      onchange?.()
    } catch (exc) {
      accountErr = exc.message
    } finally {
      savingAccount = false
    }
  }

  async function unlinkAccount() {
    if (!confirm('저장된 CGV 계정을 지울까요? 좌석 감시는 로그인이 없으면 확인되지 않습니다.'))
      return
    await del('/api/cgv-account')
    await loadAccount()
    accountMsg = '삭제했습니다'
  }

  function toggleType(name) {
    const next = new Set(types)
    next.has(name) ? next.delete(name) : next.add(name)
    types = next
  }

  async function addWatch() {
    watchMsg = null
    watchErr = null
    if (!movie.trim() || !site.trim() || !ymd) {
      watchErr = '영화·극장·날짜를 모두 지정하세요'
      return
    }
    if (timeMode === 'range' && (!timeFrom || !timeTo)) {
      watchErr = '시간대의 시작과 끝을 모두 지정하세요'
      return
    }
    if (timeMode === 'one' && !scnTime) {
      watchErr = '회차를 고르거나 다른 방법을 선택하세요'
      return
    }
    savingWatch = true
    try {
      await post('/api/seat-watches', {
        movie: movie.trim(),
        site: site.trim(),
        scn_ymd: ymd.replaceAll('-', ''),
        screen_types: [...types],
        rows: rowsText.split(/[,\s]+/).map((r) => r.trim()).filter(Boolean),
        scn_time: timeMode === 'one' ? scnTime : '',
        scn_time_from: timeMode === 'range' ? timeFrom : '',
        scn_time_to: timeMode === 'range' ? timeTo : '',
        min_consecutive: Number(minConsecutive) || 0,
        auto_book: autoBook,
        auto_pay: autoBook && autoPay,
        party_size: Number(partySize) || 1,
        ticket_spec: autoBook ? { adult: Number(partySize) || 1 } : {},
      })
      watchMsg = autoBook
        ? `좌석 감시를 추가했습니다 (자동 예매 켜짐${autoPay ? ' · 카카오페이 결제까지' : ''})`
        : '좌석 감시를 추가했습니다'
      rowsText = ''
      await loadWatches()
    } catch (exc) {
      watchErr = exc.message
    } finally {
      savingWatch = false
    }
  }

  async function removeWatch(w) {
    if (!confirm(`${w.movie_query} · ${w.site_query} · ${w.scn_ymd} 좌석 감시를 지울까요?`))
      return
    await del(`/api/seat-watches/${w.id}`)
    await loadWatches()
  }

  /** 감시가 어느 회차를 보는지 한 줄로. 시간대면 늦은 회차 우선이라고 알린다. */
  function fmtWhen(w) {
    if (w.scn_time) return w.scn_time
    if (w.scn_time_from && w.scn_time_to) {
      const overnight = w.scn_time_to < w.scn_time_from ? '+1' : ''
      return `${w.scn_time_from}~${w.scn_time_to}${overnight} ↓늦은순`
    }
    return '모든 회차'
  }

  function fmtYmd(s) {
    // 20260825 → 2026-08-25
    return s?.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}` : s
  }

  function payLinkAlive(b) {
    // 카카오페이 결제 링크는 몇 분 만에 죽는다. 죽은 링크를 눌러 보게 두면
    // 결제가 안 되는 이유를 오해하게 되므로, 만료가 지났으면 링크를 감춘다.
    //
    // **선점 만료도 함께 본다.** 실측에서 선점은 5분 남짓, 링크는 15분을 버텼다 —
    // 링크만 보면 좌석이 풀린 뒤에도 "결제하기"를 내주게 된다.
    if (!b.pay_url) return false
    const deadlines = [b.pay_expires_at, b.hold_expires_at]
      .filter(Boolean)
      .map((t) => new Date(t).getTime())
    if (!deadlines.length) return true
    return Math.min(...deadlines) > Date.now()
  }

  const statusBadge = $derived.by(() => {
    switch (account.status) {
      case 'linked':
        return { cls: 'ok', text: '로그인됨' }
      case 'error':
        return { cls: 'warn', text: '로그인 오류' }
      case 'unlinked':
        return { cls: 'accent', text: '미확인' }
      default:
        return { cls: '', text: '없음' }
    }
  })
</script>

<div class="panel stack">
  <div class="spread">
    <h2>CGV 계정</h2>
    {#if account.linked}
      <span class="badge {statusBadge.cls}">{statusBadge.text}</span>
    {/if}
  </div>
  <p class="small muted">
    좌석 감시는 CGV 로그인이 있어야 좌석 배치도를 볼 수 있습니다. 아이디·비밀번호는
    <strong>암호화해</strong> 보관하며 화면·알림에 노출되지 않습니다. 로그인은 최초
    1회 캡차를 자동으로 처리하고, 이후에는 저장된 세션을 재사용합니다.
  </p>

  {#if account.linked}
    <div class="row small">
      <span>연동 계정: <strong>{account.cgv_user_id}</strong></span>
      {#if account.last_login_at}
        <span class="muted">마지막 로그인 {fmtAgo(account.last_login_at)}</span>
      {/if}
    </div>
    {#if account.status === 'error' && account.last_error}
      <div class="small err-text">최근 오류: {account.last_error}</div>
    {/if}
  {/if}

  <div class="form2">
    <div class="field">
      <label for="cgv-id">CGV(CJ ONE) 아이디</label>
      <input id="cgv-id" bind:value={cgvId} placeholder="6~12자" autocomplete="off" />
    </div>
    <div class="field">
      <label for="cgv-pw">비밀번호</label>
      <input
        id="cgv-pw"
        type="password"
        bind:value={cgvPw}
        placeholder={account.linked ? '바꿀 때만 입력' : ''}
        autocomplete="new-password" />
    </div>
  </div>
  <div class="row">
    <button class="primary" onclick={saveAccount} disabled={savingAccount}>
      {savingAccount ? '저장·로그인 확인 중…' : account.linked ? '자격증명 갱신' : '저장하고 로그인'}
    </button>
    {#if account.linked}
      <button class="ghost danger" onclick={unlinkAccount}>계정 삭제</button>
    {/if}
    {#if accountMsg}<span class="small ok-text">{accountMsg}</span>{/if}
    {#if accountErr}<span class="small err-text">{accountErr}</span>{/if}
  </div>
</div>

<div class="panel stack">
  <h2>좌석 감시 추가</h2>
  <p class="small muted">
    특정 날짜 회차에서 원하는 <strong>열</strong>에 빈자리(취소표 포함)가 생기면
    알립니다. 첫 확인은 기준선만 잡고, 이후 새로 생긴 좌석만 알립니다.
  </p>

  <div class="form">
    <div class="field">
      <label for="s-movie">영화</label>
      <select id="s-movie" bind:value={movie}>
        <option value="">— 예매 가능한 영화 —</option>
        {#each catalog.movies as m (m.mov_no)}
          <option value={m.mov_nm}>{m.mov_nm}</option>
        {/each}
      </select>
    </div>

    <div class="field">
      <label for="s-region">극장</label>
      <select id="s-region" bind:value={region}>
        {#each catalog.regions as r (r)}
          <option value={r}>{r}</option>
        {/each}
      </select>
      <select bind:value={site}>
        <option value="">— 극장 선택 —</option>
        {#each sitesInRegion as s (s.site_no)}
          <option value={s.site_nm}>{s.site_nm}</option>
        {/each}
      </select>
    </div>

    <div class="field">
      <label for="s-date">날짜</label>
      <input id="s-date" type="date" bind:value={ymd} />
    </div>

    <div class="field">
      <label for="s-timemode">상영 시간</label>
      <select id="s-timemode" bind:value={timeMode}>
        <option value="all">모든 회차</option>
        <option value="one" disabled={!times.length}>특정 회차</option>
        <option value="range">시간대로 (미상영 영화도 가능)</option>
      </select>

      {#if timeMode === 'one'}
        <select
          class="stacked"
          aria-label="회차"
          bind:value={scnTime}
          disabled={!times.length}
        >
          <option value="">회차를 고르세요</option>
          {#each times as t (t)}
            <option value={t}>{t}</option>
          {/each}
        </select>
      {:else if timeMode === 'range'}
        <div class="range">
          <input type="time" aria-label="시간대 시작" bind:value={timeFrom} />
          <span class="tilde">~</span>
          <input type="time" aria-label="시간대 끝" bind:value={timeTo} />
        </div>
      {/if}

      <div class="small muted">
        {#if timeMode === 'range'}
          이 시간대의 회차를 봅니다. 상영표가 아직 열리지 않은 영화도 미리 걸어 둘 수
          있습니다 — 열리는 순간부터 확인합니다. 자동 예매를 켜 두면 시간대 안에서
          <strong>가장 늦은 회차부터</strong> 좌석을 잡습니다.
          {#if timeTo && timeFrom && timeTo < timeFrom}
            <br />끝이 시작보다 이르므로 <strong>자정을 넘긴 것</strong>으로 봅니다
            ({timeFrom} ~ 다음날 {timeTo}).
          {/if}
        {:else if loadingTimes}
          회차 불러오는 중…
        {:else if !ymd || !movie || !site}
          영화·극장·날짜를 고르면 회차가 표시됩니다.
        {:else if !times.length}
          이 조합의 회차를 불러오지 못했습니다 — 아직 상영표가 열리지 않았다면
          '시간대로'를 고르세요.
        {:else if timeMode === 'all'}
          그 날짜의 모든 회차를 봅니다.
        {/if}
      </div>
    </div>

    <div class="field">
      <label for="s-rows">열 필터</label>
      <input id="s-rows" bind:value={rowsText} placeholder="예: A, B (비우면 전 열)" />
      <div class="small muted">쉼표·공백으로 구분. 비우면 모든 열의 빈좌석을 봅니다.</div>
    </div>

    <div class="field">
      <label for="s-consec">연속 좌석</label>
      <select id="s-consec" bind:value={minConsecutive}>
        <option value={0}>개별 좌석 (한 자리라도)</option>
        <option value={2}>2석 연속 (나란히)</option>
        <option value={3}>3석 연속</option>
        <option value={4}>4석 연속</option>
        <option value={5}>5석 연속</option>
      </select>
      <div class="small muted">
        고르면 <strong>나란히 붙은</strong> 그만큼의 빈자리가 새로 생겼을 때만 알립니다
        (통로로 끊긴 자리는 연속으로 치지 않습니다).
      </div>
    </div>

    <div class="field">
      <label for="s-types">상영관 필터</label>
      <div class="row">
        {#each SCREEN_TYPES as name, i (name)}
          <button
            id={i === 0 ? 's-types' : undefined}
            class="chip"
            class:on={types.has(name)}
            onclick={() => toggleType(name)}>{name}</button>
        {/each}
      </div>
      <div class="small muted">비우면 그 날짜의 모든 회차를 봅니다.</div>
    </div>

    <div class="field">
      <label for="s-auto">자동 예매</label>
      <label class="check small">
        <input
          id="s-auto"
          type="checkbox"
          bind:checked={autoBook}
          disabled={!account.linked} />
        좌석이 확보되면 <strong>자동으로 선점</strong>합니다
      </label>
      {#if autoBook}
        <div class="row" style="align-items: center; gap: 6px">
          <span class="small muted">인원</span>
          <select bind:value={partySize}>
            {#each [1, 2, 3, 4, 5] as n (n)}
              <option value={n}>성인 {n}명</option>
            {/each}
          </select>
        </div>
        <label class="check small">
          <input id="s-autopay" type="checkbox" bind:checked={autoPay} />
          선점에 이어 <strong>카카오페이 결제까지 요청</strong>하고 결제 링크를 보냅니다
        </label>
      {/if}
      <div class="small muted">
        {#if !account.linked}
          먼저 위에서 CGV 계정을 연동하세요.
        {:else if autoBook && autoPay}
          ⚠️ 실제로 좌석을 잡고 카카오페이 결제창까지 띄웁니다. <strong>마지막 승인은
          직접</strong> 하셔야 합니다 — 알림으로 온 링크를 휴대폰에서 열어 카카오페이
          인증을 마치면 결제가 끝납니다. 링크는 몇 분 뒤 만료됩니다.
        {:else}
          ⚠️ 실제로 좌석을 잡습니다. <strong>결제 확정은 직접</strong> 하셔야 하며,
          선점 후 알림의 안내대로 만료 전에 결제를 완료하세요.
        {/if}
      </div>
    </div>
  </div>

  <div class="row">
    <button class="primary" onclick={addWatch} disabled={savingWatch}>
      {savingWatch ? '추가 중…' : '추가'}
    </button>
    {#if watchMsg}<span class="small ok-text">{watchMsg}</span>{/if}
    {#if watchErr}<span class="small err-text">{watchErr}</span>{/if}
  </div>
</div>

<div class="panel">
  <h2 style="margin-bottom: 8px">좌석 감시 목록 ({watches.length})</h2>
  {#if watches.length === 0}
    <div class="empty">아직 좌석 감시가 없습니다.</div>
  {:else}
    <table>
      <thead>
        <tr>
          <th>영화</th>
          <th>극장</th>
          <th>날짜</th>
          <th>상영관</th>
          <th>열</th>
          <th>연속</th>
          <th>자동예매</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each watches as w (w.id)}
          <tr class:off={!w.enabled}>
            <td>{w.movie_query}</td>
            <td>{w.site_query}</td>
            <td>{fmtYmd(w.scn_ymd)} <span class="small muted">{fmtWhen(w)}</span></td>
            <td>{w.screen_types.length ? w.screen_types.join(', ') : '전체'}</td>
            <td>{w.rows.length ? w.rows.join(', ') : '전 열'}</td>
            <td>{w.min_consecutive >= 2 ? `${w.min_consecutive}석` : '개별'}</td>
            <td>
              {#if w.auto_book}
                <span class="badge accent">성인 {w.party_size}</span>
                {#if w.auto_pay}<span class="badge">카카오페이</span>{/if}
              {:else}
                <span class="small muted">끔</span>
              {/if}
            </td>
            <td class="row" style="justify-content: flex-end">
              <button class="ghost small danger" onclick={() => removeWatch(w)}>삭제</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

{#if bookings.length}
  <div class="panel">
    <h2 style="margin-bottom: 8px">자동 예매 이력</h2>
    <table>
      <thead>
        <tr>
          <th>영화</th>
          <th>극장</th>
          <th>일시</th>
          <th>좌석</th>
          <th>상태</th>
        </tr>
      </thead>
      <tbody>
        {#each bookings as b (b.id)}
          <tr>
            <td>{b.mov_nm}</td>
            <td>{b.site_nm}</td>
            <td>{fmtYmd(b.scn_ymd)} {b.start_hhmm}</td>
            <td>{b.seat_labels.join(', ')}</td>
            <td>
              {#if b.status === 'held'}
                <span class="badge ok">선점됨 — 결제 필요</span>
                {#if b.pay_url}
                  {#if payLinkAlive(b)}
                    <a class="small" href={b.pay_url} target="_blank" rel="noreferrer">
                      카카오페이로 결제
                    </a>
                  {:else}
                    <span class="small muted">결제 링크 만료됨</span>
                  {/if}
                {:else if b.pay_error}
                  <span class="small muted" title={b.pay_error}>자동 결제 실패</span>
                {/if}
              {:else if b.status === 'failed'}
                <span class="badge warn" title={b.last_error}>실패</span>
              {:else if b.status === 'expired'}
                <span class="badge">만료</span>
              {:else}
                <span class="badge">{b.status}</span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

<style>
  .form {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 16px;
  }
  .form2 {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .field > label:first-child {
    font-size: 12px;
    color: var(--muted);
    font-weight: 600;
  }
  /* 회차를 '시간대로' 고를 때의 시작~끝 입력 */
  .range {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .range input {
    flex: 1;
    min-width: 0;
  }
  .tilde {
    color: var(--muted);
  }
  /* 모드 선택 아래에 딸려 나오는 두 번째 컨트롤 */
  .stacked {
    width: 100%;
  }
  .chip {
    padding: 2px 9px;
    border-radius: 999px;
    font-size: 12px;
  }
  .chip.on {
    background: var(--accent);
    border-color: transparent;
    color: #fff;
    font-weight: 600;
  }
  .ok-text {
    color: var(--ok);
  }
  .err-text {
    color: var(--accent);
  }
  td button.small {
    font-size: 12px;
    padding: 1px 7px;
  }
</style>
