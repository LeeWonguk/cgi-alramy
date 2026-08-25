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
  let rowsText = $state('')
  let minConsecutive = $state(0) // 0·1 = 개별 좌석, 2+ = 나란히 붙은 N석
  let autoBook = $state(false) // 좌석 확보 시 자동 선점
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
    savingWatch = true
    try {
      await post('/api/seat-watches', {
        movie: movie.trim(),
        site: site.trim(),
        scn_ymd: ymd.replaceAll('-', ''),
        screen_types: [...types],
        rows: rowsText.split(/[,\s]+/).map((r) => r.trim()).filter(Boolean),
        min_consecutive: Number(minConsecutive) || 0,
        auto_book: autoBook,
        party_size: Number(partySize) || 1,
        ticket_spec: autoBook ? { adult: Number(partySize) || 1 } : {},
      })
      watchMsg = autoBook ? '좌석 감시를 추가했습니다 (자동 예매 켜짐)' : '좌석 감시를 추가했습니다'
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

  function fmtYmd(s) {
    // 20260825 → 2026-08-25
    return s?.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}` : s
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
      {/if}
      <div class="small muted">
        {#if !account.linked}
          먼저 위에서 CGV 계정을 연동하세요.
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
            <td>{fmtYmd(w.scn_ymd)}</td>
            <td>{w.screen_types.length ? w.screen_types.join(', ') : '전체'}</td>
            <td>{w.rows.length ? w.rows.join(', ') : '전 열'}</td>
            <td>{w.min_consecutive >= 2 ? `${w.min_consecutive}석` : '개별'}</td>
            <td>
              {#if w.auto_book}
                <span class="badge accent">성인 {w.party_size}</span>
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
