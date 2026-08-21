<!--
  웹훅 발급 방법 안내. 설정 탭에서 넘어오는 문서 화면이라 API를 부르지 않는다.
  Slack·Discord 두 서비스의 화면 이름은 자주 바뀌므로, 각 단계에 "어디를 눌러야
  하는지"보다 "무엇을 찾아야 하는지"를 적는다.
-->
<script>
  import { WEBHOOK_LABELS } from '../lib/api.js'

  let { user, onsettings } = $props()

  const current = $derived(user?.webhook_kind ?? 'slack')
  const label = $derived(WEBHOOK_LABELS[current] ?? current)

  // 처음 열었을 때는 지금 쓰는 종류를 펼쳐 둔다.
  let tab = $state(null)
  $effect(() => {
    if (tab === null) tab = current
  })
</script>

<div class="panel stack">
  <div class="spread">
    <h2>웹훅 설정법</h2>
    {#if onsettings}
      <button class="ghost small" onclick={onsettings}>설정 탭으로 →</button>
    {/if}
  </div>
  <div class="small muted">
    알림은 <strong>웹훅 URL</strong> 하나로 나갑니다. Slack이든 Discord든 절차는
    같습니다 — <em>채널에 글을 쓸 수 있는 주소를 발급받아</em> 설정 탭에 붙여넣는
    것입니다. 같은 문구를 고른 서비스의 문법으로 바꿔 보내므로, 종류만 맞게
    골라두면 굵은 글씨와 예매 링크가 제대로 보입니다.
  </div>
  <div class="small muted">
    지금 내 알림은
    {#if user?.has_webhook}
      <span class="badge ok">{label} 개인 웹훅</span>으로 갑니다.
    {:else}
      <span class="badge">서버 기본 웹훅</span>으로 갑니다 — 아래 절차로 내 웹훅을
      넣으면 나만 받을 수 있습니다.
    {/if}
  </div>

  <div class="row">
    <button class="chip" class:on={tab === 'slack'} onclick={() => (tab = 'slack')}>
      Slack
    </button>
    <button class="chip" class:on={tab === 'discord'} onclick={() => (tab = 'discord')}>
      Discord
    </button>
    <button class="chip" class:on={tab === 'compare'} onclick={() => (tab = 'compare')}>
      비교 · 문제 해결
    </button>
  </div>
</div>

{#if tab === 'slack'}
  <div class="panel stack">
    <div class="spread">
      <h3>Slack — Incoming Webhook 발급</h3>
      <span class="badge">3~5분</span>
    </div>
    <div class="small muted">
      Slack은 채널 하나마다 주소가 하나씩 나옵니다. 워크스페이스에 앱을 설치할
      권한이 없으면 관리자 승인이 필요합니다.
    </div>

    <ol class="steps">
      <li>
        <strong>Slack 앱을 만듭니다.</strong>
        <a href="https://api.slack.com/apps" target="_blank" rel="noreferrer">
          api.slack.com/apps
        </a>
        → <em>Create New App</em> → <em>From scratch</em>.
        <div class="note">
          이름은 <code>CGV 알림기</code>처럼 알아볼 수 있게 두고, 알림을 받을
          워크스페이스를 고른 뒤 <em>Create App</em>.
        </div>
      </li>
      <li>
        <strong>Incoming Webhooks를 켭니다.</strong>
        왼쪽 <em>Features</em> → <em>Incoming Webhooks</em> →
        <em>Activate Incoming Webhooks</em> 토글을 <em>On</em>.
        <div class="note">
          토글을 켜지 않으면 아래의 웹훅 추가 버튼이 나타나지 않습니다.
        </div>
      </li>
      <li>
        <strong>채널에 웹훅을 붙입니다.</strong>
        같은 화면 아래쪽 <em>Add New Webhook to Workspace</em> → 알림 받을 채널
        선택 → <em>허용(Allow)</em>.
        <div class="note">
          비공개 채널·DM도 고를 수 있습니다. 목록에 없으면 그 채널에서
          <code>/invite @CGV 알림기</code>로 앱을 초대한 뒤 다시 시도하세요.
        </div>
      </li>
      <li>
        <strong>주소를 복사합니다.</strong> 표에 생긴 <em>Webhook URL</em>의
        <em>Copy</em>.
        <pre class="url">https://hooks.slack.com/services/&#123;워크스페이스ID&#125;/&#123;앱ID&#125;/&#123;토큰&#125;</pre>
      </li>
      <li>
        <strong>설정 탭에 넣습니다.</strong>
        <em>알림 받을 곳</em>을 <em>Slack</em>으로 두고 주소를 붙여넣은 뒤
        <em>내 설정 저장</em> → <em>테스트 메시지 보내기</em>.
        <div class="note">
          고른 채널에 <code>✅ CGV 알림기 연결 테스트</code>가 오면 끝입니다.
        </div>
      </li>
    </ol>

    <div class="callout">
      <strong>채널을 바꾸고 싶다면</strong> 이 주소는 발급할 때 고른 채널에
      고정입니다. 같은 앱에서 <em>Add New Webhook to Workspace</em>로 새 주소를
      받아 설정 탭의 주소를 갈아 주세요.
    </div>
  </div>
{/if}

{#if tab === 'discord'}
  <div class="panel stack">
    <div class="spread">
      <h3>Discord — 채널 웹후크 발급</h3>
      <span class="badge">1~2분</span>
    </div>
    <div class="small muted">
      Discord는 앱을 만들 필요가 없습니다. 채널 설정에서 바로 주소가 나옵니다 —
      대신 그 서버에서 <strong>웹후크 관리</strong> 권한(보통 서버 관리자)이
      필요합니다.
    </div>

    <ol class="steps">
      <li>
        <strong>채널 설정을 엽니다.</strong> 알림 받을 채널 이름에 마우스를 올려
        <em>톱니바퀴(채널 편집)</em>를 누릅니다 — 채널을 우클릭해
        <em>채널 편집</em>을 골라도 됩니다.
        <div class="note">
          모바일 앱은 채널 길게 누르기 → <em>편집</em> → <em>웹후크</em>입니다.
        </div>
      </li>
      <li>
        <strong>연동 → 웹후크로 들어갑니다.</strong>
        <em>연동(Integrations)</em> → <em>웹후크(Webhooks)</em> →
        <em>새 웹후크(New Webhook)</em>.
        <div class="note">
          메뉴가 보이지 않으면 권한이 없는 것입니다. 서버 관리자에게 주소를
          발급해 달라고 부탁하세요 — 주소만 받으면 됩니다.
        </div>
      </li>
      <li>
        <strong>이름과 채널을 확인합니다.</strong> 이름은 알림에 보낸 사람으로
        표시됩니다 (<code>CGV 알림기</code> 권장). 아래 <em>채널</em>이 알림 받을
        채널인지 확인하세요.
      </li>
      <li>
        <strong>주소를 복사합니다.</strong> <em>웹후크 URL 복사(Copy Webhook
        URL)</em> — 새로 만든 웹후크는 <em>변경 사항 저장</em>까지 눌러야
        남습니다.
        <pre class="url">https://discord.com/api/webhooks/1234567890123456789/XXXXXXXXXXXXXXXXXXXX</pre>
      </li>
      <li>
        <strong>설정 탭에 넣습니다.</strong>
        <em>알림 받을 곳</em>을 <em>Discord</em>로 고르고 주소를 붙여넣은 뒤
        <em>내 설정 저장</em> → <em>테스트 메시지 보내기</em>.
        <div class="note">
          주소를 붙여넣으면 종류는 자동으로 <em>Discord</em>로 맞춰집니다.
        </div>
      </li>
    </ol>

    <div class="callout">
      <strong>스레드로 받고 싶다면</strong> 주소 끝에
      <code>?thread_id=&lt;스레드 ID&gt;</code>를 붙이면 그 스레드로 들어갑니다.
      스레드 ID는 스레드 우클릭 → <em>ID 복사</em>(개발자 모드 필요)로 얻습니다.
    </div>
  </div>
{/if}

{#if tab === 'compare'}
  <div class="panel stack">
    <h3>두 서비스의 차이</h3>
    <table>
      <thead>
        <tr>
          <th></th>
          <th>Slack</th>
          <th>Discord</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>발급 위치</td>
          <td>api.slack.com/apps (앱을 만들어야 함)</td>
          <td>채널 편집 → 연동 → 웹후크</td>
        </tr>
        <tr>
          <td>필요 권한</td>
          <td>워크스페이스 앱 설치(관리자 승인이 걸릴 수 있음)</td>
          <td>서버의 웹후크 관리 권한</td>
        </tr>
        <tr>
          <td>주소 모양</td>
          <td><code>hooks.slack.com/services/…</code></td>
          <td><code>discord.com/api/webhooks/…</code></td>
        </tr>
        <tr>
          <td>채널 변경</td>
          <td>새 웹훅을 발급해야 함</td>
          <td>웹후크 설정에서 채널만 바꾸면 됨</td>
        </tr>
        <tr>
          <td>한 메시지 길이</td>
          <td>넉넉함 (약 4만 자)</td>
          <td>2,000자 — 넘치면 잘려서 갑니다</td>
        </tr>
        <tr>
          <td>강조 문법</td>
          <td><code>*굵게*</code> · <code>&lt;주소|라벨&gt;</code></td>
          <td><code>**굵게**</code> · <code>[라벨](주소)</code></td>
        </tr>
      </tbody>
    </table>
    <div class="small muted">
      강조 문법은 서버가 알아서 바꿔 씁니다. 종류를 잘못 골라 두면
      <code>*새 예매 날짜 오픈*</code>처럼 별표가 그대로 보이거나 링크가 깨지는데,
      주소를 붙여넣을 때 서비스를 알아내 종류를 맞추므로 대개 저절로 맞습니다.
    </div>
  </div>

  <div class="panel stack">
    <h3>알림이 오지 않을 때</h3>
    <dl class="faq">
      <dt>테스트 메시지가 실패한다</dt>
      <dd>
        주소를 다시 복사해 붙여넣어 보세요. 앞뒤 공백이나 잘린 토큰이 가장 흔한
        원인입니다. Slack은 앱을 지우거나 채널을 없애면 주소가 죽고, Discord는
        웹후크를 삭제하면 즉시 죽습니다 — 그 경우 새로 발급해야 합니다.
      </dd>
      <dt>테스트는 오는데 예매 알림이 안 온다</dt>
      <dd>
        <em>감시 대상</em> 탭에서 그 조합이 <em>사용 중</em>인지, 상영관 필터가
        너무 좁지 않은지 보세요. 첫 관측은 기준선만 잡고 알리지 않습니다 — 그
        다음에 늘어난 날짜부터 알림이 갑니다. <em>이력</em> 탭에
        <code>전송 실패</code>가 쌓여 있으면 웹훅 쪽 문제입니다.
      </dd>
      <dt>주소가 유출된 것 같다</dt>
      <dd>
        웹훅 주소를 아는 사람은 누구나 그 채널에 글을 쓸 수 있습니다. Slack은 앱의
        <em>Incoming Webhooks</em> 화면에서, Discord는 채널의 <em>웹후크</em>
        화면에서 지우면 그 주소는 즉시 무효가 됩니다. 새로 발급해 설정 탭에서
        갈아 주세요.
      </dd>
      <dt>주소를 비우면 어떻게 되나</dt>
      <dd>
        설정 탭의 주소를 지우고 저장하면 서버 기본 웹훅으로 돌아갑니다
        (<code>.env</code>의 <code>SLACK_WEBHOOK_URL</code> ·
        <code>DISCORD_WEBHOOK_URL</code>). 기본 웹훅은 서버를 다시 띄울 때
        읽으므로, 소유자가 <code>.env</code>를 고쳤다면
        <code>./install.sh</code>를 다시 실행해야 반영됩니다.
      </dd>
    </dl>
  </div>
{/if}

<style>
  .chip {
    padding: 3px 12px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 550;
  }
  .chip.on {
    background: var(--accent);
    border-color: transparent;
    color: #fff;
    font-weight: 600;
  }
  .chip.on:hover {
    background: var(--accent);
    filter: brightness(1.08);
  }

  .steps {
    margin: 0;
    padding-left: 22px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .steps li {
    padding-left: 2px;
  }
  .steps strong {
    font-weight: 650;
  }
  .steps em {
    font-style: normal;
    font-weight: 600;
    color: var(--text);
    background: var(--panel-2);
    border-radius: 4px;
    padding: 0 4px;
  }
  .note {
    margin-top: 4px;
    font-size: 12px;
    color: var(--muted);
  }
  .url {
    margin-top: 6px;
    padding: 7px 9px;
    background: var(--panel-2);
    border-radius: 7px;
    color: var(--muted);
    overflow-x: auto;
  }
  .callout {
    font-size: 12.5px;
    padding: 10px 12px;
    border-radius: 9px;
    background: var(--warn-soft);
    color: var(--warn);
  }
  code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    background: var(--panel-2);
    border-radius: 4px;
    padding: 0 4px;
  }

  .faq {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .faq dt {
    font-weight: 650;
  }
  .faq dd {
    margin: 3px 0 0;
    font-size: 12.5px;
    color: var(--muted);
  }
  td {
    font-size: 12.5px;
  }
  td:first-child {
    color: var(--muted);
    white-space: nowrap;
  }
</style>
