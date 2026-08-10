<script>
  import { onMount } from 'svelte'
  import { get, post, patch, del, SCREEN_TYPES } from '../lib/api.js'
  import { fmtAgo, screenLabel } from '../lib/format.js'

  let { targets = [], settings, onchange } = $props()

  let catalog = $state({ movies: [], sites: [], regions: [], refreshed_at: null })
  let refreshing = $state(false)
  let saving = $state(false)
  let message = $state(null)
  let error = $state(null)

  // 입력 폼
  let movie = $state('')
  let freeText = $state(false) // 예매 오픈 전 영화는 목록에 없어 직접 입력해야 한다
  let region = $state('서울')
  let chosenSites = $state(new Set())
  let types = $state(new Set())

  onMount(loadCatalog)

  $effect(() => {
    // 기본 상영관 필터를 폼 초기값으로 쓴다 — 매번 같은 필터를 고르는 수고를 덜어준다.
    if (settings?.default_screen_types?.length && types.size === 0 && !touchedTypes) {
      types = new Set(settings.default_screen_types)
    }
  })
  let touchedTypes = false

  async function loadCatalog() {
    try {
      catalog = await get('/api/catalog')
      if (!catalog.regions.includes(region)) region = catalog.regions[0] ?? ''
    } catch (exc) {
      error = exc.message
    }
  }

  async function refreshCatalog() {
    refreshing = true
    error = null
    try {
      const result = await post('/api/catalog/refresh')
      await loadCatalog()
      message = `영화 ${result.movies}편 · 극장 ${result.sites}곳을 다시 받았습니다`
    } catch (exc) {
      error = exc.message
    } finally {
      refreshing = false
    }
  }

  const sitesInRegion = $derived(catalog.sites.filter((s) => s.region === region))

  function toggleSite(name) {
    const next = new Set(chosenSites)
    next.has(name) ? next.delete(name) : next.add(name)
    chosenSites = next
  }

  function toggleType(name) {
    touchedTypes = true
    const next = new Set(types)
    next.has(name) ? next.delete(name) : next.add(name)
    types = next
  }

  async function add() {
    message = null
    error = null
    if (!movie.trim()) {
      error = '영화를 고르거나 이름을 입력하세요'
      return
    }
    if (chosenSites.size === 0) {
      error = '극장을 하나 이상 고르세요'
      return
    }
    saving = true
    try {
      const result = await post('/api/targets', {
        movie: movie.trim(),
        sites: [...chosenSites],
        screen_types: [...types],
      })
      const parts = []
      if (result.created.length) parts.push(`${result.created.length}개 추가`)
      if (result.duplicates.length)
        parts.push(`이미 있음: ${result.duplicates.join(', ')}`)
      message = parts.join(' · ')
      chosenSites = new Set()
      onchange?.()
    } catch (exc) {
      error = exc.message
    } finally {
      saving = false
    }
  }

  async function toggleEnabled(target) {
    await patch(`/api/targets/${target.id}`, { enabled: !target.enabled })
    onchange?.()
  }

  async function editTypes(target) {
    const answer = prompt(
      '상영관 필터를 쉼표로 구분해 입력하세요 (비우면 전체 상영관):',
      target.screen_types.join(', ')
    )
    if (answer === null) return
    const next = answer.split(',').map((s) => s.trim()).filter(Boolean)
    await patch(`/api/targets/${target.id}`, { screen_types: next })
    onchange?.()
  }

  async function remove(target) {
    if (!confirm(`${target.mov_nm} · ${target.site_nm}을(를) 감시 목록에서 지울까요?`)) return
    await del(`/api/targets/${target.id}`)
    onchange?.()
  }
</script>

<div class="panel stack">
  <div class="spread">
    <h2>감시 대상 추가</h2>
    <div class="row small muted">
      목록 갱신 {fmtAgo(catalog.refreshed_at)}
      <button class="ghost" onclick={refreshCatalog} disabled={refreshing}>
        {refreshing ? '받는 중…' : '영화·극장 목록 다시 받기'}
      </button>
    </div>
  </div>

  <div class="form">
    <div class="field">
      <label for="movie">영화</label>
      {#if freeText}
        <input id="movie" bind:value={movie} placeholder="예: 아바타 파이어 앤 애쉬" />
      {:else}
        <select id="movie" bind:value={movie}>
          <option value="">— 예매 가능한 영화 —</option>
          {#each catalog.movies as m (m.mov_no)}
            <option value={m.mov_nm}>
              {m.mov_nm}{m.atkt_rate ? ` (예매율 ${m.atkt_rate}%)` : ''}
            </option>
          {/each}
        </select>
      {/if}
      <label class="small muted check">
        <input type="checkbox" bind:checked={freeText} />
        아직 예매가 열리지 않은 영화 (이름 직접 입력)
      </label>
    </div>

    <div class="field">
      <label for="region">극장</label>
      <select id="region" bind:value={region}>
        {#each catalog.regions as r (r)}
          <option value={r}>{r}</option>
        {/each}
      </select>
      <div class="row sites">
        {#each sitesInRegion as site (site.site_no)}
          <button
            class="chip"
            class:on={chosenSites.has(site.site_nm)}
            onclick={() => toggleSite(site.site_nm)}>{site.site_nm}</button>
        {/each}
      </div>
      {#if chosenSites.size}
        <div class="small muted">선택: {[...chosenSites].join(', ')}</div>
      {/if}
    </div>

    <div class="field">
      <label for="types-imax">상영관 필터</label>
      <div class="row">
        {#each SCREEN_TYPES as name, i (name)}
          <button
            id={i === 0 ? 'types-imax' : undefined}
            class="chip"
            class:on={types.has(name)}
            onclick={() => toggleType(name)}>{name}</button>
        {/each}
      </div>
      <div class="small muted">
        고르면 <strong>그 상영관 상영이 있는 날짜만</strong> 알립니다. 비워 두면 날짜
        추가만 봅니다. 부분 일치라 "IMAX" 하나로 IMAX LASER 2D·IMAX관이 모두 걸립니다.
      </div>
    </div>
  </div>

  <div class="row">
    <button class="primary" onclick={add} disabled={saving}>
      {saving ? '추가 중…' : `추가${chosenSites.size > 1 ? ` (${chosenSites.size}곳)` : ''}`}
    </button>
    {#if message}<span class="small ok-text">{message}</span>{/if}
    {#if error}<span class="small err-text">{error}</span>{/if}
  </div>
</div>

<div class="panel">
  <h2 style="margin-bottom: 8px">감시 목록 ({targets.length})</h2>
  {#if targets.length === 0}
    <div class="empty">아직 감시 대상이 없습니다.</div>
  {:else}
    <table>
      <thead>
        <tr>
          <th>영화</th>
          <th>극장</th>
          <th>상영관 필터</th>
          <th>상태</th>
          <th>마지막 확인</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each targets as target (target.id)}
          <tr class:off={!target.enabled}>
            <td>
              {target.mov_nm}
              {#if target.mov_nm !== target.movie_query}
                <span class="small muted">(등록: {target.movie_query})</span>
              {/if}
            </td>
            <td>{target.site_nm}</td>
            <td>
              <button class="ghost small" onclick={() => editTypes(target)}>
                {screenLabel(target.screen_types)}
              </button>
            </td>
            <td>
              {#if !target.enabled}
                <span class="badge">중지</span>
              {:else if target.fail_count > 0}
                <span class="badge warn">실패 {target.fail_count}회</span>
              {:else if target.status === 'not_open'}
                <span class="badge accent">오픈 전</span>
              {:else}
                <span class="badge ok">{target.tracked_dates.length}일</span>
              {/if}
            </td>
            <td class="small muted">{fmtAgo(target.last_ok)}</td>
            <td class="row" style="justify-content: flex-end">
              <button class="ghost small" onclick={() => toggleEnabled(target)}>
                {target.enabled ? '중지' : '재개'}
              </button>
              <button class="ghost small danger" onclick={() => remove(target)}>삭제</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<style>
  .form {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
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
  .check {
    display: flex;
    align-items: center;
    gap: 5px;
  }
  .check input {
    width: auto;
  }
  .sites {
    gap: 4px;
    max-height: 148px;
    overflow-y: auto;
    padding: 2px;
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
  tr.off {
    opacity: 0.55;
  }
  .ok-text {
    color: var(--ok);
  }
  .err-text {
    color: var(--accent);
  }
  td button.small,
  td .small {
    font-size: 12px;
    padding: 1px 7px;
  }
</style>
