<script>
  import { post } from '../lib/api.js'
  import { fmtDate, fmtAgo, fmtDuration, screenLabel } from '../lib/format.js'

  let { data, isOwner = false, onchange } = $props()

  const targets = $derived(data?.targets ?? [])
  // 사이클 요약은 서버 전체 정보라 소유자에게만 내려온다.
  const cycle = $derived(isOwner ? data?.last_cycle : null)

  // 처음 본 순간을 기록해 뒤늦게 추가된 날짜에 '새로'를 붙인다.
  // 서버는 "무엇이 새 날짜였는지"를 알림에만 담으므로 화면에서 직접 판단한다.
  const NEW_FOR_MS = 5 * 60 * 1000
  let firstSeen = new Map() // `${targetId}:${date}` -> 처음 본 시각
  let baselined = new Set() // 첫 스냅샷은 기준선이라 새로 표시하지 않는다

  $effect(() => {
    const stamp = Date.now()
    for (const target of targets) {
      const isFirst = !baselined.has(target.id)
      baselined.add(target.id)
      for (const date of target.tracked_dates) {
        const key = `${target.id}:${date}`
        if (!firstSeen.has(key)) firstSeen.set(key, isFirst ? 0 : stamp)
      }
    }
  })

  function isNew(targetId, date) {
    const at = firstSeen.get(`${targetId}:${date}`)
    return !!at && Date.now() - at < NEW_FOR_MS
  }

  let expanded = $state(new Set())

  function toggle(key) {
    const next = new Set(expanded)
    next.has(key) ? next.delete(key) : next.add(key)
    expanded = next
  }

  function showtimesFor(target, date) {
    return target.showtimes?.find((s) => s.date === date)
  }

  async function resetBaseline(target) {
    if (!confirm(`${target.mov_nm} · ${target.site_nm}의 기준선을 지울까요?\n다음 확인은 알림 없이 현재 상태만 저장합니다.`)) return
    await post(`/api/targets/${target.id}/reset`)
    onchange?.()
  }

  function statusBadge(target) {
    if (!target.enabled) return { cls: '', text: '중지' }
    if (target.fail_count > 0) return { cls: 'warn', text: `조회 실패 ${target.fail_count}회` }
    if (target.status === 'not_open') return { cls: 'accent', text: '예매 오픈 전' }
    if (target.status === 'unknown') return { cls: '', text: '확인 대기' }
    return { cls: 'ok', text: '추적 중' }
  }
</script>

{#if cycle}
  <div class="panel spread small">
    <div class="row" style="gap: 14px">
      <span><strong>{cycle.targets_checked}</strong>개 대상 확인</span>
      <span class="muted">CGV 요청 {cycle.requests}건</span>
      <span class="muted">소요 {fmtDuration(cycle.duration_ms)}</span>
      {#if cycle.new_dates > 0}
        <span class="badge new">새 날짜 {cycle.new_dates}개</span>
      {/if}
      {#if cycle.error}
        <span class="badge warn">{cycle.error}</span>
      {/if}
    </div>
    <span class="muted">{fmtAgo(cycle.finished_at)} · {cycle.trigger}</span>
  </div>
{/if}

{#if targets.length === 0}
  <div class="panel empty">
    감시 중인 조합이 없습니다. <strong>감시 대상</strong> 탭에서 추가하세요.
  </div>
{/if}

<div class="grid cards">
  {#each targets as target (target.id)}
    {@const status = statusBadge(target)}
    <div class="panel card" class:off={!target.enabled}>
      <div class="spread">
        <div>
          <h2>{target.mov_nm}</h2>
          <div class="small muted">CGV {target.site_nm}</div>
        </div>
        <div class="row" style="gap: 6px">
          <span class="badge accent">{screenLabel(target.screen_types)}</span>
          <span class="badge {status.cls}">{status.text}</span>
        </div>
      </div>

      {#if target.last_error && target.fail_count > 0}
        <div class="small warn-line">{target.last_error}</div>
      {/if}

      {#if target.tracked_dates.length === 0}
        <div class="small muted">
          {#if target.status === 'not_open'}
            아직 예매가 열리지 않았습니다 — 열리는 순간 알립니다.
          {:else if target.screen_types.length}
            {screenLabel(target.screen_types)} 상영이 아직 없습니다
            (극장에 열린 날짜는 {target.dates.length}일).
          {:else}
            열린 날짜가 없습니다.
          {/if}
        </div>
      {:else}
        <div class="row dates">
          {#each target.tracked_dates as date (date)}
            {@const key = `${target.id}:${date}`}
            {@const times = showtimesFor(target, date)}
            <button
              class="chip"
              class:fresh={isNew(target.id, date)}
              class:open={expanded.has(key)}
              class:plain={!times}
              disabled={!times}
              title={times ? '상영 시간표 보기' : '저장된 시간표가 없습니다'}
              onclick={() => toggle(key)}>{fmtDate(date)}</button>
          {/each}
        </div>

        {#each target.tracked_dates as date (date)}
          {@const key = `${target.id}:${date}`}
          {@const times = showtimesFor(target, date)}
          {#if expanded.has(key) && times}
            <div class="times">
              <div class="small muted">
                {fmtDate(date)} · 조회 {fmtAgo(times.fetched_at)}
              </div>
              {#each times.groups as group (group.label)}
                <div class="small">
                  <strong>{group.label}</strong>
                  <span class="muted">{group.times.join(', ')}</span>
                </div>
              {/each}
            </div>
          {/if}
        {/each}
      {/if}

      <div class="spread small muted footer">
        <span>
          마지막 확인 {fmtAgo(target.last_ok)}
          {#if target.screen_types.length}
            · 극장 전체 {target.dates.length}일 중 {target.tracked_dates.length}일 해당
          {/if}
        </span>
        <button class="ghost small" onclick={() => resetBaseline(target)}>기준선 초기화</button>
      </div>
    </div>
  {/each}
</div>

<style>
  .cards {
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  }
  .card {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .card.off {
    opacity: 0.6;
  }
  .dates {
    gap: 5px;
  }
  .chip {
    padding: 2px 9px;
    border-radius: 999px;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
  }
  .chip.plain {
    opacity: 0.75;
  }
  .chip.open {
    border-color: var(--accent);
    color: var(--accent);
  }
  .chip.fresh {
    background: var(--new-soft);
    border-color: var(--new);
    color: var(--new);
    font-weight: 650;
  }
  .times {
    background: var(--panel-2);
    border-radius: 8px;
    padding: 8px 10px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .warn-line {
    color: var(--warn);
  }
  .footer {
    margin-top: auto;
    padding-top: 4px;
    border-top: 1px solid var(--line);
  }
  .footer button {
    padding: 2px 8px;
    font-size: 12px;
  }
</style>
