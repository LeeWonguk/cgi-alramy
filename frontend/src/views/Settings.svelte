<script>
  import { patch, post, SCREEN_TYPES, WEBHOOK_LABELS } from '../lib/api.js'

  let { server, user, isOwner, onchange, onguide } = $props()

  // 지원하는 웹훅. 종류를 바꾸면 문구가 그 서비스 문법으로 변환돼 나간다.
  const KINDS = [
    {
      id: 'slack',
      label: WEBHOOK_LABELS.slack,
      hint: 'https://hooks.slack.com/services/T…/B…/…',
    },
    {
      id: 'discord',
      label: WEBHOOK_LABELS.discord,
      hint: 'https://discord.com/api/webhooks/123…/토큰',
    },
  ]

  // ── 내 설정 ──
  let mine = $state(null)
  let webhook = $state('')
  let webhookTouched = $state(false)
  let webhookKind = $state(null)
  let savingMine = $state(false)

  // ── 서버 설정 (소유자만) ──
  let draft = $state(null)
  let savingServer = $state(false)

  let message = $state(null)
  let error = $state(null)

  // 서버 값이 처음 도착했을 때만 폼을 채운다 — 5초 폴링이 입력 중인 값을 덮으면 안 된다.
  $effect(() => {
    if (user?.settings && mine === null) mine = { ...user.settings }
  })
  $effect(() => {
    if (user && webhookKind === null) webhookKind = user.webhook_kind ?? 'slack'
  })
  $effect(() => {
    if (server && draft === null) draft = { ...server }
  })

  const kindInfo = $derived(KINDS.find((k) => k.id === webhookKind) ?? KINDS[0])
  const kindDirty = $derived(!!webhookKind && webhookKind !== user?.webhook_kind)

  const mineDirty = $derived.by(() => {
    if (!mine || !user?.settings) return false
    return (
      JSON.stringify(mine) !== JSON.stringify(user.settings) ||
      webhookTouched ||
      kindDirty
    )
  })
  const serverDirty = $derived.by(() => {
    if (!draft || !server) return false
    return JSON.stringify(draft) !== JSON.stringify(server)
  })

  function toggleType(name) {
    const current = new Set(mine.default_screen_types)
    current.has(name) ? current.delete(name) : current.add(name)
    mine.default_screen_types = [...current]
  }

  /** 붙여넣은 주소에서 서비스를 알아내 종류를 맞춰준다 (서버도 한 번 더 본다). */
  function onWebhookInput() {
    webhookTouched = true
    const url = webhook.trim()
    if (/^https?:\/\/(\w+\.)*discord(app)?\.com\//.test(url)) webhookKind = 'discord'
    else if (/^https?:\/\/hooks\.slack\.com\//.test(url)) webhookKind = 'slack'
  }

  async function saveMine() {
    savingMine = true
    message = null
    error = null
    try {
      const payload = { ...mine }
      if (webhookTouched) payload.webhook_url = webhook
      if (webhookTouched || kindDirty) payload.webhook_kind = webhookKind
      const saved = await patch('/api/me/settings', payload)
      webhookTouched = false
      webhook = ''
      webhookKind = saved.webhook_kind ?? webhookKind
      message = '내 설정을 저장했습니다'
      onchange?.()
    } catch (exc) {
      error = exc.message
    } finally {
      savingMine = false
    }
  }

  async function saveServer() {
    savingServer = true
    message = null
    error = null
    try {
      const result = await patch('/api/settings', draft)
      draft = { ...result.settings }
      message = '서버 설정을 저장했습니다 — 다음 확인부터 적용됩니다'
      onchange?.()
    } catch (exc) {
      error = exc.message
    } finally {
      savingServer = false
    }
  }

  async function testNotify() {
    message = null
    error = null
    try {
      const result = await post('/api/test-notify')
      const where = result.personal ? '' : ' (서버 기본 웹훅)'
      message = `${result.label}으로 테스트 메시지를 보냈습니다${where}`
    } catch (exc) {
      // 502면 서버가 전송에 실패한 것이다 — 주소나 채널 권한 문제가 대부분이다.
      error =
        exc.status === 502
          ? '전송에 실패했습니다 — 웹훅 주소와 종류를 확인하세요'
          : exc.message
    }
  }
</script>

{#if mine}
  <div class="panel stack">
    <h2>내 설정</h2>

    <div class="fields">
      <label class="field">
        <span>며칠 이내만 알림</span>
        <input type="number" min="0" bind:value={mine.lookahead_days} />
        <small class="muted">0이면 제한 없음. 오늘+N일 이내에 열린 날짜만 알립니다.</small>
      </label>

      <label class="check">
        <input type="checkbox" bind:checked={mine.include_showtimes} />
        <span>알림에 상영관·시간 요약 첨부</span>
      </label>
    </div>

    <div class="field">
      <span class="label">새 감시 대상의 기본 상영관 필터</span>
      <div class="row">
        {#each SCREEN_TYPES as name (name)}
          <button
            class="chip"
            class:on={mine.default_screen_types.includes(name)}
            onclick={() => toggleType(name)}>{name}</button>
        {/each}
      </div>
      <small class="muted">감시 대상을 추가할 때 폼에 미리 채워지는 값입니다.</small>
    </div>

    <div class="field">
      <span class="label">알림 받을 곳</span>
      <div class="row">
        {#each KINDS as kind (kind.id)}
          <button
            class="chip"
            class:on={webhookKind === kind.id}
            onclick={() => (webhookKind = kind.id)}>{kind.label}</button>
        {/each}
        {#if user.has_webhook}
          <span class="badge">
            {KINDS.find((k) => k.id === user.webhook_kind)?.label ?? user.webhook_kind}
            웹훅 저장됨
          </span>
        {:else}
          <span class="badge">서버 기본 웹훅 사용 중</span>
        {/if}
      </div>
      <small class="muted">
        같은 문구를 고른 서비스의 문법(굵게·링크)으로 바꿔 보냅니다.
      </small>
    </div>

    <label class="field">
      <span>내 {kindInfo.label} 웹훅 주소</span>
      <input
        type="url"
        placeholder={user.has_webhook
          ? '설정되어 있습니다 — 바꾸려면 새 주소를 입력하세요'
          : `${kindInfo.hint}  (비우면 서버 기본 웹훅)`}
        bind:value={webhook}
        oninput={onWebhookInput} />
      <small class="muted">
        내 감시의 알림은 여기로 갑니다. 비워 두면 서버의 기본 웹훅(<code>.env</code>의
        <code>SLACK_WEBHOOK_URL</code> · <code>DISCORD_WEBHOOK_URL</code>)으로 갑니다.
        저장된 주소는 보안을 위해 다시 보여주지 않습니다.
        {#if onguide}
          <button class="linkish" onclick={onguide}>발급 방법 보기 →</button>
        {/if}
      </small>
    </label>

    <div class="row">
      <button class="primary" onclick={saveMine} disabled={savingMine || !mineDirty}>
        {savingMine ? '저장 중…' : '내 설정 저장'}
      </button>
      <button class="ghost" onclick={testNotify}>테스트 메시지 보내기</button>
      {#if message}<span class="small ok-text">{message}</span>{/if}
      {#if error}<span class="small err-text">{error}</span>{/if}
    </div>
  </div>
{/if}

{#if isOwner && draft}
  <div class="panel stack">
    <div class="spread">
      <h2>서버 설정</h2>
      <span class="badge accent">소유자만</span>
    </div>
    <div class="small muted">
      브라우저와 스케줄러가 하나씩뿐이라 이 값들은 <strong>모든 사용자에게</strong>
      함께 적용됩니다.
    </div>

    <div class="fields">
      <label class="field">
        <span>확인 간격 (초)</span>
        <input type="number" min="3" step="1" bind:value={draft.poll_interval_seconds} />
        <small class="muted">
          최소 3초. 60의 약수(3·5·10·15·20·30·60)를 쓰면 확인 시각이 매분 같은 자리에
          고정됩니다. <code>./install.sh</code>를 다시 실행할 필요는 없습니다.
        </small>
      </label>

      <label class="field">
        <span>브라우저 세션 재기동 (분)</span>
        <input type="number" min="1" bind:value={draft.session_recycle_minutes} />
        <small class="muted">
          Chromium을 상주시키므로 주기적으로 갈아줍니다 — 메모리 누적을 여기서 끊습니다.
        </small>
      </label>

      <label class="check">
        <input type="checkbox" bind:checked={draft.headless} />
        <span>브라우저를 화면 없이 실행 (headless)</span>
      </label>
    </div>
    <small class="muted">
      CGV가 headless를 막기 시작하면 체크를 해제하세요 — 창이 잠깐 떴다 사라지지만
      통과율이 더 높습니다. 바꾼 값은 세션이 다시 뜰 때 적용됩니다.
    </small>

    <div class="row">
      <button class="primary" onclick={saveServer} disabled={savingServer || !serverDirty}>
        {savingServer ? '저장 중…' : '서버 설정 저장'}
      </button>
    </div>
  </div>
{/if}

<div class="panel small muted stack">
  <h3>참고</h3>
  <div>
    감시 대상과 설정의 출처는 Postgres입니다. <code>config.toml</code>은 최초 1회
    시드로만 쓰이므로, 지금 파일을 고쳐도 반영되지 않습니다.
  </div>
  <div>
    CGV는 한 영화의 날짜를 몇 분에 걸쳐 순차적으로 엽니다. 확인 간격을 아무리
    좁혀도 이 반영 지연은 줄어들지 않습니다.
  </div>
</div>

<style>
  .fields {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
    gap: 14px;
    align-items: start;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .field > span,
  .label {
    font-size: 12px;
    color: var(--muted);
    font-weight: 600;
  }
  .check {
    display: flex;
    align-items: center;
    gap: 7px;
  }
  .check input {
    width: auto;
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
  /* 안내 문구 안에 섞여 들어가는 링크형 버튼 — 탭 이동이라 <a>가 아니다. */
  button.linkish {
    background: none;
    border: none;
    padding: 0;
    color: var(--accent);
    font: inherit;
    font-weight: 600;
    white-space: nowrap;
  }
  button.linkish:hover {
    background: none;
    text-decoration: underline;
  }
  .ok-text {
    color: var(--ok);
  }
  .err-text {
    color: var(--accent);
  }
  small {
    font-size: 11.5px;
    line-height: 1.5;
  }
</style>
