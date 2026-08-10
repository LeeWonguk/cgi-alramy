/** Flask API 호출 헬퍼. 서버가 낸 error 문구를 그대로 예외 메시지로 쓴다. */

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }

  /** 로그인이 필요하다 (세션 없음·만료) */
  get unauthorized() {
    return this.status === 401
  }

  /** 로그인은 했지만 권한이 없다 (승인 대기·소유자 전용) */
  get forbidden() {
    return this.status === 403
  }
}

async function unwrap(res) {
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new ApiError(data.error || `HTTP ${res.status}`, res.status)
  return data
}

function send(method, path, body) {
  return fetch(path, {
    method,
    headers: { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(unwrap)
}

export const get = (path) => fetch(path).then(unwrap)
export const post = (path, body) => send('POST', path, body ?? {})
export const patch = (path, body) => send('PATCH', path, body)
export const del = (path) => send('DELETE', path)

/** 알려진 상영관 종류. 부분 일치라 이 단어 하나로 파생 상영관까지 걸린다. */
export const SCREEN_TYPES = ['IMAX', '4DX', 'SCREENX', 'DOLBY', 'CINE de CHEF']
