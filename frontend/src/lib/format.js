/** 표시용 포맷. 서버(watch.py)의 fmt_date·fmt_time과 같은 모양을 낸다. */

const WEEKDAYS = '월화수목금토일'

/** '20260811' -> '8/11(화)' */
export function fmtDate(ymd) {
  if (!ymd || ymd.length !== 8) return ymd ?? ''
  const y = +ymd.slice(0, 4)
  const m = +ymd.slice(4, 6)
  const d = +ymd.slice(6, 8)
  const date = new Date(y, m - 1, d)
  if (Number.isNaN(date.getTime())) return ymd
  // JS의 getDay()는 일요일이 0이다 — 월요일 시작으로 옮긴다.
  return `${m}/${d}(${WEEKDAYS[(date.getDay() + 6) % 7]})`
}

/** '20260811' -> '2026-08-11' */
export function isoDate(ymd) {
  if (!ymd || ymd.length !== 8) return ymd ?? ''
  return `${ymd.slice(0, 4)}-${ymd.slice(4, 6)}-${ymd.slice(6, 8)}`
}

export function fmtTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function fmtDateTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('ko-KR', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

/** '방금', '12초 전', '3분 전' — 마지막 확인이 살아 있는지 한눈에 보려고. */
export function fmtAgo(iso) {
  if (!iso) return '기록 없음'
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (seconds < 0) return '방금'
  if (seconds < 5) return '방금'
  if (seconds < 60) return `${seconds}초 전`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}분 전`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}시간 전`
  return `${Math.floor(hours / 24)}일 전`
}

export function fmtDuration(ms) {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(2)}초`
}

/** 필터 표기 — 비어 있으면 '전체 상영관'. */
export function screenLabel(types) {
  return types?.length ? types.join('/') : '전체 상영관'
}
