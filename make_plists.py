#!/usr/bin/env python3
"""웹 서버를 상주시키는 LaunchAgent plist를 만든다.

install.sh가 호출한다. 만든 plist 경로를 stdout으로 출력하고,
사람이 읽을 요약은 stderr로 보낸다.

    make_plists.py <프로젝트 경로> <python 경로> <LaunchAgents 경로> <라벨>

예전에는 launchd가 매분 watch.py를 띄우고, 1분을 초 단위로 쪼개는 일을
watch.py --sweep이 맡았다(launchd는 분 단위 그리드로만 동작해서 Second 키를
넣어도 무시한다). 이제 서버가 상주하며 스스로 간격을 지키므로 launchd가 할
일은 "프로세스를 살려 두는 것"뿐이다 — 확인 간격은 plist가 아니라 DB의
설정값이라서, 간격을 바꿔도 이 스크립트를 다시 돌릴 필요가 없다.
"""

from __future__ import annotations

import pathlib
import sys

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py}</string>
        <string>{root}/web/app.py</string>
        <string>--host</string>
        <string>{host}</string>
        <string>--port</string>
        <string>{port}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{root}</string>
    <key>RunAtLoad</key>
    <true/>
    <!-- 서버가 죽으면 launchd가 다시 띄운다. 확인 간격은 서버가 스스로 지킨다. -->
    <key>KeepAlive</key>
    <true/>
    <!-- 재시작이 폭주하지 않게 최소 간격을 둔다 (기본 10초). -->
    <key>ThrottleInterval</key>
    <integer>15</integer>
    <key>StandardOutPath</key>
    <string>{root}/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>{root}/logs/launchd.err.log</string>
    <!-- Background는 타이머 병합(coalescing) 대상이라 실행이 몇 초씩 늦는다.
         확인 시각을 정확히 지키려고 Standard로 둔다. -->
    <key>ProcessType</key>
    <string>Standard</string>
</dict>
</plist>
"""

HOST = "127.0.0.1"  # 인증이 없으므로 로컬에만 바인딩한다
PORT = 8787


def main() -> int:
    root, py, agents, label = (
        pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3]), sys.argv[4]
    )

    path = agents / f"{label}.plist"
    path.write_text(
        PLIST_TEMPLATE.format(label=label, py=py, root=root, host=HOST, port=PORT),
        encoding="utf-8",
    )
    print(path)
    print(f"주소:     http://{HOST}:{PORT}", file=sys.stderr)
    print("간격:     DB 설정값 (웹의 '설정' 탭에서 바꿉니다)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
