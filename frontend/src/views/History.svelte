<script>
  import { onMount } from 'svelte'
  import { get } from '../lib/api.js'
  import { fmtDateTime, fmtAgo, fmtDuration, fmtDate } from '../lib/format.js'

  let { isOwner = false } = $props()

  // 확인 사이클과 로그에는 모든 사용자의 감시가 섞여 있어 소유자만 볼 수 있다.
  const SECTIONS = $derived([
    { id: 'alerts', label: '알림' },
    ...(isOwner
      ? [
          { id: 'cycles', label: '확인 이력' },
          { id: 'logs', label: '로그' },
        ]
      : []),
  ])

  const KINDS = {
    new_dates: { text: '새 날짜', cls: 'new' },
    open: { text: '예매 오픈', cls: 'accent' },
    fetch_error: { text: '조회 실패', cls: 'warn' },
    config_error: { text: '설정 오류', cls: 'warn' },
    connect_error: { text: '접속 실패', cls: 'warn' },
  }

  let section = $state('alerts')
  let alerts = $state([])
  let cycles = $state([])
  let logs = $state([])
  let error = $state(null)
  let loading = $state(false)

  async function load() {
    loading = true
    error = null
    try {
      if (section === 'alerts') alerts = await get('/api/alerts?limit=50')
      else if (section === 'cycles') cycles = await get('/api/cycles?limit=50')
      else logs = (await get('/api/logs?lines=300')).lines
    } catch (exc) {
      error = exc.message
    } finally {
      loading = false
    }
  }

  onMount(load)
  $effect(() => {
    section // 섹션을 바꾸면 그때 받아온다
    load()
  })
</script>

<div class="panel stack">
  <div class="spread">
    <div class="row" style="gap: 2px">
      {#each SECTIONS as item (item.id)}
        <button
          class="seg"
          class:active={section === item.id}
          onclick={() => (section = item.id)}>{item.label}</button>
      {/each}
    </div>
    <button class="ghost small" onclick={load} disabled={loading}>
      {loading ? '불러오는 중…' : '새로 고침'}
    </button>
  </div>

  {#if error}
    <div class="small err-text">{error}</div>
  {/if}

  {#if section === 'alerts'}
    {#if alerts.length === 0}
      <div class="empty">아직 보낸 알림이 없습니다.</div>
    {:else}
      <div class="stack">
        {#each alerts as alert (alert.id)}
          {@const kind = KINDS[alert.kind] ?? { text: alert.kind, cls: '' }}
          <div class="alert">
            <div class="spread">
              <div class="row" style="gap: 6px">
                <span class="badge {kind.cls}">{kind.text}</span>
                {#if alert.mov_nm}
                  <strong class="small">{alert.mov_nm}</strong>
                  <span class="small muted">CGV {alert.site_nm}</span>
                {/if}
                {#if alert.dates?.length}
                  <span class="small muted">
                    {alert.dates.map(fmtDate).join(', ')}
                  </span>
                {/if}
              </div>
              <div class="row small muted" style="gap: 8px">
                {#if alert.delivered}
                  <span class="badge ok">전송됨</span>
                {:else}
                  <span class="badge warn">
                    전송 실패{alert.attempts > 1 ? ` · ${alert.attempts}회 시도` : ''}
                  </span>
                {/if}
                <span title={fmtDateTime(alert.created_at)}>{fmtAgo(alert.created_at)}</span>
              </div>
            </div>
            <pre>{alert.body}</pre>
          </div>
        {/each}
      </div>
    {/if}
  {:else if section === 'cycles'}
    {#if cycles.length === 0}
      <div class="empty">확인 기록이 없습니다.</div>
    {:else}
      <table>
        <thead>
          <tr>
            <th>시각</th>
            <th>계기</th>
            <th>대상</th>
            <th>요청</th>
            <th>새 날짜</th>
            <th>소요</th>
            <th>결과</th>
          </tr>
        </thead>
        <tbody>
          {#each cycles as cycle (cycle.id)}
            <tr>
              <td class="small">{fmtDateTime(cycle.started_at)}</td>
              <td class="small muted">{cycle.trigger}</td>
              <td>{cycle.targets_checked}</td>
              <td>{cycle.requests}</td>
              <td>
                {#if cycle.new_dates > 0}
                  <span class="badge new">{cycle.new_dates}</span>
                {:else}
                  <span class="muted">0</span>
                {/if}
              </td>
              <td class="small muted">{fmtDuration(cycle.duration_ms)}</td>
              <td class="small">
                {#if cycle.ok}
                  <span class="badge ok">정상</span>
                {:else if cycle.ok === false}
                  <span class="badge warn" title={cycle.error}>실패</span>
                {:else}
                  <span class="badge">진행 중</span>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  {:else}
    <pre class="logs">{logs.join('\n')}</pre>
  {/if}
</div>

<style>
  button.seg {
    border-radius: 0;
    border-right-width: 0;
  }
  button.seg:first-child {
    border-radius: 7px 0 0 7px;
  }
  button.seg:last-child {
    border-radius: 0 7px 7px 0;
    border-right-width: 1px;
  }
  button.seg.active {
    background: var(--accent);
    border-color: transparent;
    color: #fff;
    font-weight: 600;
  }
  .alert {
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 9px 11px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .alert pre {
    background: var(--panel-2);
    border-radius: 7px;
    padding: 8px 10px;
  }
  .logs {
    background: var(--panel-2);
    border-radius: 8px;
    padding: 10px 12px;
    max-height: 62vh;
    overflow: auto;
    line-height: 1.5;
  }
  .err-text {
    color: var(--accent);
  }
</style>
