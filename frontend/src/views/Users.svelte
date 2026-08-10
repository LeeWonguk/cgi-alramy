<script>
  import { onMount } from 'svelte'
  import { get, patch, del } from '../lib/api.js'
  import { fmtAgo, fmtDateTime } from '../lib/format.js'

  let { me } = $props()

  let users = $state([])
  let error = $state(null)
  let loading = $state(false)

  const STATUS = {
    approved: { text: '사용 중', cls: 'ok' },
    pending: { text: '승인 대기', cls: 'warn' },
    blocked: { text: '차단됨', cls: '' },
  }

  async function load() {
    loading = true
    error = null
    try {
      users = await get('/api/users')
    } catch (exc) {
      error = exc.message
    } finally {
      loading = false
    }
  }

  onMount(load)

  async function setStatus(user, status) {
    error = null
    try {
      await patch(`/api/users/${user.id}`, { status })
      await load()
    } catch (exc) {
      error = exc.message
    }
  }

  async function remove(user) {
    if (
      !confirm(
        `${user.nickname ?? user.provider} 계정을 삭제할까요?\n` +
          '이 계정의 감시 대상과 기준선도 함께 사라집니다.\n' +
          '접근만 막으려면 차단을 쓰세요.'
      )
    )
      return
    error = null
    try {
      await del(`/api/users/${user.id}`)
      await load()
    } catch (exc) {
      error = exc.message
    }
  }

  const pending = $derived(users.filter((u) => u.status === 'pending'))
</script>

<div class="panel stack">
  <div class="spread">
    <h2>사용자 ({users.length})</h2>
    <button class="ghost small" onclick={load} disabled={loading}>
      {loading ? '불러오는 중…' : '새로 고침'}
    </button>
  </div>

  {#if error}<div class="small err-text">{error}</div>{/if}

  {#if pending.length}
    <div class="notice small">
      <strong>{pending.length}명이 승인을 기다리고 있습니다.</strong>
      승인하면 그 계정은 자기 감시 대상을 만들고 알림을 받을 수 있습니다.
      다른 사람의 감시는 서로 보이지 않습니다.
    </div>
  {/if}

  <table>
    <thead>
      <tr>
        <th>계정</th>
        <th>로그인</th>
        <th>상태</th>
        <th>Slack</th>
        <th>마지막 로그인</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      {#each users as user (user.id)}
        {@const status = STATUS[user.status] ?? { text: user.status, cls: '' }}
        <tr>
          <td>
            <div class="row" style="gap: 7px">
              {#if user.profile_image}
                <img class="avatar" src={user.profile_image} alt="" />
              {/if}
              <div>
                <div>
                  {user.nickname ?? '(이름 없음)'}
                  {#if user.is_owner}<span class="badge accent">소유자</span>{/if}
                  {#if user.id === me?.id}<span class="badge">나</span>{/if}
                </div>
                {#if user.email}
                  <div class="small muted">{user.email}</div>
                {/if}
              </div>
            </div>
          </td>
          <td class="small muted">{user.provider}</td>
          <td><span class="badge {status.cls}">{status.text}</span></td>
          <td class="small muted">
            {user.has_slack_webhook ? '개인 웹훅' : '기본 웹훅'}
          </td>
          <td class="small muted" title={fmtDateTime(user.last_login_at)}>
            {fmtAgo(user.last_login_at)}
          </td>
          <td class="row" style="justify-content: flex-end">
            {#if !user.is_owner}
              {#if user.status !== 'approved'}
                <button class="ghost small" onclick={() => setStatus(user, 'approved')}>
                  승인
                </button>
              {/if}
              {#if user.status !== 'blocked'}
                <button class="ghost small" onclick={() => setStatus(user, 'blocked')}>
                  차단
                </button>
              {/if}
              <button class="ghost small danger" onclick={() => remove(user)}>삭제</button>
            {/if}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>

  <div class="small muted">
    차단은 데이터를 남기고 접근만 막습니다. 삭제는 그 계정의 감시 대상과 기준선까지
    지우며 되돌릴 수 없습니다.
  </div>
</div>

<style>
  .avatar {
    width: 26px;
    height: 26px;
    border-radius: 50%;
    object-fit: cover;
    flex: none;
  }
  .notice {
    background: var(--warn-soft);
    color: var(--warn);
    border-radius: 8px;
    padding: 8px 11px;
  }
  .err-text {
    color: var(--accent);
  }
  td button.small {
    font-size: 12px;
    padding: 1px 7px;
  }
</style>
