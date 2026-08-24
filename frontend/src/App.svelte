<script>
  import { onMount } from 'svelte'
  import { get, post } from './lib/api.js'
  import { fmtAgo } from './lib/format.js'
  import Login from './views/Login.svelte'
  import Dashboard from './views/Dashboard.svelte'
  import Targets from './views/Targets.svelte'
  import Lookup from './views/Lookup.svelte'
  import History from './views/History.svelte'
  import Settings from './views/Settings.svelte'
  import Users from './views/Users.svelte'
  import WebhookGuide from './views/WebhookGuide.svelte'

  // 서버가 30초마다 확인하므로 5초 폴링이면 거의 실시간이다.
  // DB 읽기라 비용이 없어 SSE의 장기 연결 관리를 감당할 이유가 없다.
  const REFRESH_MS = 5000

  let session = $state(null) // /api/me 응답
  let authState = $state('loading') // loading | anonymous | pending | ready
  let tab = $state('dashboard')
  let data = $state(null)
  let error = $state(null)
  let checking = $state(false)
  let now = $state(Date.now())

  const user = $derived(session?.user)
  const isOwner = $derived(!!user?.is_owner)

  const tabs = $derived([
    { id: 'dashboard', label: '대시보드' },
    { id: 'targets', label: '감시 대상' },
    { id: 'lookup', label: '상영표 조회' },
    { id: 'history', label: '이력' },
    ...(isOwner ? [{ id: 'users', label: '사용자' }] : []),
    { id: 'settings', label: '설정' },
    { id: 'webhooks', label: '웹훅 설정법' },
  ])

  async function loadSession() {
    try {
      session = await get('/api/me')
      authState = session.user.status === 'approved' ? 'ready' : 'pending'
    } catch (exc) {
      if (exc.unauthorized) {
        session = null
        authState = 'anonymous'
      } else {
        error = exc.message
        authState = 'anonymous'
      }
    }
  }

  async function refresh() {
    if (authState !== 'ready') return
    try {
      data = await get('/api/dashboard')
      error = null
    } catch (exc) {
      if (exc.unauthorized) {
        // 세션이 끊겼다 — 다시 로그인 화면으로.
        session = null
        authState = 'anonymous'
        return
      }
      error = exc.message
    }
  }

  async function checkNow() {
    checking = true
    try {
      const result = await post('/api/check-now')
      if (result.error) error = result.error
      await refresh()
    } catch (exc) {
      error = exc.message
    } finally {
      checking = false
    }
  }

  async function logout() {
    await post('/api/auth/logout').catch(() => {})
    location.href = '/'
  }

  onMount(() => {
    loadSession().then(refresh)
    const poll = setInterval(refresh, REFRESH_MS)
    const tick = setInterval(() => (now = Date.now()), 1000)
    return () => {
      clearInterval(poll)
      clearInterval(tick)
    }
  })

  const poller = $derived(data?.poller)

  const countdown = $derived.by(() => {
    if (!poller?.next_check_at) return null
    const left = Math.ceil((new Date(poller.next_check_at).getTime() - now) / 1000)
    return left > 0 ? left : 0
  })

  const health = $derived.by(() => {
    if (error) return { dot: 'bad', text: '서버에 연결할 수 없습니다' }
    if (!data) return { dot: '', text: '불러오는 중' }
    if (!poller?.running) return { dot: 'bad', text: '폴링이 멈춰 있습니다' }
    if (data.worker?.last_error) return { dot: 'warn', text: '최근 오류 있음' }
    return { dot: 'ok', text: '정상' }
  })
</script>

{#if authState === 'loading'}
  <div class="splash muted">불러오는 중…</div>
{:else if authState === 'anonymous'}
  <Login />
{:else if authState === 'pending'}
  <div class="splash">
    <div class="panel pending">
      <h1>승인을 기다리는 중입니다</h1>
      <p class="muted small">
        <strong>{user.nickname ?? user.provider}</strong> 계정으로 로그인했습니다.
        소유자가 승인하면 감시 대상을 만들고 알림을 받을 수 있습니다.
      </p>
      <div class="row" style="justify-content: center">
        <button onclick={loadSession}>다시 확인</button>
        <button class="ghost" onclick={logout}>로그아웃</button>
      </div>
    </div>
  </div>
{:else}
  <header>
    <div class="bar">
      <div class="spread">
        <div class="row" style="gap: 10px">
          <h1>🎟 CGV 예매 알림기</h1>
          <span class="row small muted" style="gap: 5px"
                title={data?.worker?.last_error ?? ''}>
            <span class="dot {health.dot}"></span>{health.text}
          </span>
        </div>
        <div class="row">
          {#if poller}
            <span class="small muted">
              {poller.interval_seconds}초 간격 ·
              {#if countdown !== null}다음 확인 {countdown}초{:else}대기 중{/if}
            </span>
          {/if}
          {#if isOwner}
            <button class="primary" onclick={checkNow} disabled={checking}>
              {checking ? '확인 중…' : '지금 확인'}
            </button>
          {/if}
          <div class="row who small" style="gap: 6px">
            {#if user.profile_image}
              <img class="avatar" src={user.profile_image} alt="" />
            {/if}
            <span>{user.nickname ?? user.provider}</span>
            {#if isOwner}<span class="badge accent">소유자</span>{/if}
            <button class="ghost small" onclick={logout}>로그아웃</button>
          </div>
        </div>
      </div>

      <nav class="row">
        {#each tabs as item (item.id)}
          <button
            class="tab"
            class:active={tab === item.id}
            onclick={() => (tab = item.id)}>{item.label}</button>
        {/each}
        {#if isOwner}
          <span class="small muted" style="margin-left: auto">
            마지막 확인 {fmtAgo(data?.last_cycle?.finished_at)}
          </span>
        {/if}
      </nav>
    </div>
  </header>

  <main>
    {#if error}
      <div class="panel error">
        <strong>오류:</strong> {error}
      </div>
    {/if}

    {#if tab === 'dashboard'}
      <Dashboard {data} {isOwner} onchange={refresh} />
    {:else if tab === 'targets'}
      <Targets
        targets={data?.targets ?? []}
        settings={user.settings}
        onchange={refresh} />
    {:else if tab === 'lookup'}
      <Lookup />
    {:else if tab === 'history'}
      <History {isOwner} />
    {:else if tab === 'users'}
      <Users me={user} />
    {:else if tab === 'settings'}
      <Settings
        server={data?.settings}
        {user}
        {isOwner}
        onchange={() => loadSession().then(refresh)}
        onguide={() => (tab = 'webhooks')} />
    {:else if tab === 'webhooks'}
      <WebhookGuide {user} onsettings={() => (tab = 'settings')} />
    {/if}
  </main>
{/if}

<style>
  header {
    position: sticky;
    top: 0;
    z-index: 10;
    background: var(--bg);
    border-bottom: 1px solid var(--line);
  }
  /* 본문과 같은 폭·같은 여백으로 맞춘다 — 창이 넓어도 어긋나 보이지 않게. */
  .bar {
    max-width: 1100px;
    margin: 0 auto;
    padding: 12px 20px 0;
  }
  nav {
    margin-top: 10px;
    gap: 2px;
  }
  button.tab {
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 7px 12px;
    color: var(--muted);
    font-weight: 550;
  }
  button.tab:hover {
    background: transparent;
    color: var(--text);
  }
  button.tab.active {
    color: var(--text);
    border-bottom-color: var(--accent);
  }
  .who {
    padding-left: 10px;
    margin-left: 4px;
    border-left: 1px solid var(--line);
  }
  .avatar {
    width: 22px;
    height: 22px;
    border-radius: 50%;
    object-fit: cover;
  }
  main {
    max-width: 1100px;
    margin: 0 auto;
    padding: 18px 20px 60px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .error {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .splash {
    min-height: 100vh;
    display: grid;
    place-items: center;
    padding: 24px;
  }
  .pending {
    width: min(420px, 100%);
    text-align: center;
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 28px 26px;
  }
  .pending h1 {
    font-size: 18px;
  }
  .pending p {
    margin: 0;
  }
</style>
