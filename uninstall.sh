#!/usr/bin/env bash
# CGV 알림기 LaunchAgent를 해제합니다. DB와 로그는 남겨둡니다.
set -euo pipefail

LABEL="com.lwg.cgv-web"
OLD_LABEL="com.lwg.cgv-watch"   # 매분 watch.py를 띄우던 예전 job
AGENTS="$HOME/Library/LaunchAgents"

# `$LABEL.*.plist`는 job을 초 위치마다 나눠 깔던 시절의 잔재까지 걷어내는 용도다.
found=0
for plist in "$AGENTS/$LABEL.plist" "$AGENTS/$LABEL".*.plist \
             "$AGENTS/$OLD_LABEL.plist" "$AGENTS/$OLD_LABEL".*.plist; do
  [ -e "$plist" ] || continue
  found=1
  name="$(basename "$plist" .plist)"
  launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true
  rm -f "$plist"
  echo "내리고 삭제했습니다: $name"
done

if [ "$found" -eq 0 ]; then
  echo "등록되어 있지 않았습니다: $LABEL"
fi

echo "Postgres의 데이터와 logs/는 그대로 두었습니다."
