<script>
  import { onMount } from 'svelte'
  import { get, post, SCREEN_TYPES } from '../lib/api.js'
  import { fmtDate, isoDate } from '../lib/format.js'

  let catalog = $state({ movies: [], sites: [], regions: [] })
  let movNo = $state('')
  let siteNo = $state('')
  let region = $state('서울')
  let types = $state(new Set())

  let dates = $state(null)
  let loading = $state(false)
  let error = $state(null)

  let selected = $state(null)
  let showtimes = $state(null)
  let loadingTimes = $state(false)

  onMount(async () => {
    try {
      catalog = await get('/api/catalog')
      if (!catalog.regions.includes(region)) region = catalog.regions[0] ?? ''
    } catch (exc) {
      error = exc.message
    }
  })

  const sitesInRegion = $derived(catalog.sites.filter((s) => s.region === region))
  const movieName = $derived(catalog.movies.find((m) => m.mov_no === movNo)?.mov_nm ?? '')
  const siteName = $derived(catalog.sites.find((s) => s.site_no === siteNo)?.site_nm ?? '')

  function toggleType(name) {
    const next = new Set(types)
    next.has(name) ? next.delete(name) : next.add(name)
    types = next
    if (selected) loadShowtimes(selected) // 필터를 바꾸면 열려 있던 날짜를 다시 본다
  }

  async function lookup() {
    if (!movNo || !siteNo) {
      error = '영화와 극장을 고르세요'
      return
    }
    loading = true
    error = null
    dates = null
    selected = null
    showtimes = null
    try {
      const result = await post('/api/lookup', { mov_no: movNo, site_no: siteNo })
      dates = result.dates
    } catch (exc) {
      error = exc.message
    } finally {
      loading = false
    }
  }

  async function loadShowtimes(date) {
    selected = date
    loadingTimes = true
    showtimes = null
    error = null
    try {
      showtimes = await post('/api/lookup/showtimes', {
        mov_no: movNo,
        site_no: siteNo,
        date,
        screen_types: [...types],
      })
    } catch (exc) {
      error = exc.message
    } finally {
      loadingTimes = false
    }
  }

  async function addToWatch() {
    if (!movieName || !siteName) return
    const result = await post('/api/targets', {
      movie: movieName,
      sites: [siteName],
      screen_types: [...types],
    })
    error = result.created.length
      ? null
      : `이미 감시 중입니다: ${movieName} · ${siteName}`
    if (result.created.length) alert(`감시 목록에 추가했습니다: ${movieName} · ${siteName}`)
  }
</script>

<div class="panel stack">
  <div class="spread">
    <h2>상영표 직접 조회</h2>
    <span class="small muted">감시 등록 없이 CGV에서 바로 가져옵니다</span>
  </div>

  <div class="form">
    <div class="field">
      <label for="lk-movie">영화</label>
      <select id="lk-movie" bind:value={movNo}>
        <option value="">— 선택 —</option>
        {#each catalog.movies as m (m.mov_no)}
          <option value={m.mov_no}>
            {m.mov_nm}{m.atkt_rate ? ` (예매율 ${m.atkt_rate}%)` : ''}
          </option>
        {/each}
      </select>
    </div>

    <div class="field">
      <label for="lk-region">지역</label>
      <select id="lk-region" bind:value={region}>
        {#each catalog.regions as r (r)}
          <option value={r}>{r}</option>
        {/each}
      </select>
    </div>

    <div class="field">
      <label for="lk-site">극장</label>
      <select id="lk-site" bind:value={siteNo}>
        <option value="">— 선택 —</option>
        {#each sitesInRegion as s (s.site_no)}
          <option value={s.site_no}>{s.site_nm}</option>
        {/each}
      </select>
    </div>
  </div>

  <div class="row">
    <span class="small muted">상영관 필터</span>
    {#each SCREEN_TYPES as name (name)}
      <button class="chip" class:on={types.has(name)} onclick={() => toggleType(name)}>
        {name}
      </button>
    {/each}
  </div>

  <div class="row">
    <button class="primary" onclick={lookup} disabled={loading}>
      {loading ? '조회 중…' : '조회'}
    </button>
    {#if dates}
      <button onclick={addToWatch} disabled={!movieName || !siteName}>
        이 조합을 감시에 추가
      </button>
    {/if}
    {#if error}<span class="small err-text">{error}</span>{/if}
  </div>
</div>

{#if dates}
  <div class="panel stack">
    <div class="spread">
      <h2>{movieName} · CGV {siteName}</h2>
      <span class="small muted">예매 가능 {dates.length}일</span>
    </div>

    {#if dates.length === 0}
      <div class="empty">예매 가능한 날짜가 없습니다.</div>
    {:else}
      <div class="row dates">
        {#each dates as date (date)}
          <button
            class="chip"
            class:on={selected === date}
            title={isoDate(date)}
            onclick={() => loadShowtimes(date)}>{fmtDate(date)}</button>
        {/each}
      </div>
      <div class="small muted">날짜를 누르면 상영 시간표를 가져옵니다.</div>
    {/if}

    {#if loadingTimes}
      <div class="times small muted">시간표를 가져오는 중…</div>
    {:else if showtimes}
      <div class="times">
        <div class="spread small muted">
          <span>{fmtDate(showtimes.date)}</span>
          <span>상영 {showtimes.count}회</span>
        </div>
        {#if showtimes.groups.length === 0}
          <div class="small">
            {types.size ? `${[...types].join('/')} 상영이 없습니다.` : '상영 정보가 없습니다.'}
          </div>
        {:else}
          {#each showtimes.groups as group (group.label)}
            <div class="group">
              <strong class="small">{group.label}</strong>
              <div class="row times-row">
                {#each group.times as t (t)}
                  <span class="time">{t}</span>
                {/each}
              </div>
            </div>
          {/each}
        {/if}
      </div>
    {/if}
  </div>
{/if}

<style>
  .form {
    display: grid;
    grid-template-columns: 2fr 1fr 1.4fr;
    gap: 12px;
  }
  @media (max-width: 720px) {
    .form {
      grid-template-columns: 1fr;
    }
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .field label {
    font-size: 12px;
    color: var(--muted);
    font-weight: 600;
  }
  .chip {
    padding: 2px 9px;
    border-radius: 999px;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
  }
  .chip.on {
    background: var(--accent);
    border-color: transparent;
    color: #fff;
    font-weight: 600;
  }
  .dates {
    gap: 5px;
  }
  .times {
    background: var(--panel-2);
    border-radius: 8px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .group {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .times-row {
    gap: 4px;
  }
  .time {
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 5px;
    padding: 1px 6px;
  }
  .err-text {
    color: var(--accent);
  }
</style>
