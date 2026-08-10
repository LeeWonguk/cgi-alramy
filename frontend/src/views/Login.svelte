<script>
  import { onMount } from 'svelte'
  import { get } from '../lib/api.js'

  let providers = $state([])
  let baseUrl = $state('')
  let error = $state(null)
  let loading = $state(true)

  onMount(async () => {
    // 로그인 실패는 서버가 ?login_error=…로 돌려보낸다. 한 번 읽고 주소를 정리한다.
    const params = new URLSearchParams(location.search)
    if (params.has('login_error')) {
      error = params.get('login_error')
      history.replaceState(null, '', location.pathname)
    }
    try {
      const data = await get('/api/auth/providers')
      providers = data.providers
      baseUrl = data.base_url
    } catch (exc) {
      error = exc.message
    } finally {
      loading = false
    }
  })

  const ready = $derived(providers.filter((p) => p.configured))
  const missing = $derived(providers.filter((p) => !p.configured))
</script>

<div class="wrap">
  <div class="panel card">
    <div class="brand">🎟</div>
    <h1>CGV 예매 알림기</h1>
    <p class="muted small">
      감시 대상과 알림은 계정별로 따로 관리됩니다. 로그인해 주세요.
    </p>

    {#if error}
      <div class="error small">{error}</div>
    {/if}

    {#if loading}
      <div class="muted small">불러오는 중…</div>
    {:else}
      <div class="buttons">
        {#each ready as provider (provider.provider)}
          <a class="btn {provider.provider}" href={provider.login_url}>
            {provider.label}로 로그인
          </a>
        {/each}
      </div>

      {#if ready.length === 0}
        <div class="setup small">
          <strong>아직 로그인이 설정되지 않았습니다.</strong>
          <p class="muted">
            네이버·카카오 개발자 콘솔에서 앱을 만들고 <code>.env</code>에 키를 채운 뒤
            서버를 다시 띄우세요. 콜백 주소는 아래와 정확히 같아야 합니다.
          </p>
          <ul class="muted">
            {#each providers as provider (provider.provider)}
              <li>
                <strong>{provider.label}</strong> —
                <code>{baseUrl}/api/auth/{provider.provider}/callback</code>
                <br />필요한 키: <code>{provider.missing.join(', ')}</code>
              </li>
            {/each}
          </ul>
        </div>
      {:else if missing.length}
        <div class="muted small">
          설정되지 않음: {missing.map((p) => p.label).join(', ')}
        </div>
      {/if}
    {/if}

    <div class="foot muted small">
      처음 로그인한 계정이 <strong>소유자</strong>가 되고, 이후 계정은 소유자의
      승인을 받아야 들어올 수 있습니다.
    </div>
  </div>
</div>

<style>
  .wrap {
    min-height: 100vh;
    display: grid;
    place-items: center;
    padding: 24px;
  }
  .card {
    width: min(420px, 100%);
    display: flex;
    flex-direction: column;
    gap: 12px;
    text-align: center;
    padding: 28px 26px;
  }
  .brand {
    font-size: 34px;
    line-height: 1;
  }
  h1 {
    font-size: 19px;
  }
  p {
    margin: 0;
  }
  .buttons {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 4px;
  }
  .btn {
    display: block;
    padding: 11px 14px;
    border-radius: 8px;
    font-weight: 650;
    text-decoration: none;
    border: 1px solid transparent;
  }
  .btn.naver {
    background: #03c75a;
    color: #fff;
  }
  .btn.kakao {
    background: #fee500;
    color: #191600;
  }
  .btn:hover {
    filter: brightness(1.06);
  }
  .error {
    background: var(--accent-soft);
    color: var(--accent);
    border-radius: 8px;
    padding: 8px 10px;
    text-align: left;
  }
  .setup {
    text-align: left;
    background: var(--panel-2);
    border-radius: 9px;
    padding: 11px 13px;
  }
  .setup ul {
    margin: 8px 0 0;
    padding-left: 18px;
    line-height: 1.9;
  }
  .setup code {
    word-break: break-all;
  }
  .foot {
    border-top: 1px solid var(--line);
    padding-top: 11px;
    margin-top: 4px;
  }
</style>
