# 운영 절차

돌아가는 것은 셋이다. 역할이 다르니 켜고 끄는 방법도 다르다.

| 무엇 | 어떻게 뜨나 | 죽으면 |
|---|---|---|
| 텔레그램 봇 | systemd (`darklab-bot`) | 10초 뒤 자동 재시작 |
| 대시보드 | systemd (`darklab-dash`) | 10초 뒤 자동 재시작 |
| **매매 루프** | **사람이 Run 버튼** | 다시 켜지지 않는다 — 일부러 그렇게 뒀다 |

매매 루프만 서비스가 아니다. 매매를 시작하는 결정은 사람이 화면 앞에서 내린다.
알림(체결·손절·킬스위치)은 매매 루프가 직접 보낸다 — 봇이 죽어도 알림은 온다.

---

## 1. 서비스 켜기 (WSL 안, 보통은 이미 떠 있다)

```bash
systemctl --user start darklab-bot darklab-dash
systemctl --user status darklab-bot darklab-dash --no-pager
```

둘 다 `active (running)` 이면 된다.

코드를 고친 뒤에는 다시 띄워야 반영된다:

```bash
systemctl --user restart darklab-bot darklab-dash
```

로그:

```bash
tail -f ~/ai_trader/lab/telegramctl.log     # 봇
tail -f ~/ai_trader/lab/dashboard.log       # 대시보드
```

`Linger=yes` 라 WSL 재시작에도 자동으로 뜬다. 확인: `loginctl show-user $USER -p Linger`

---

## 2. 폰에서 보기 (Windows 쪽, WSL 재시작마다 다시)

WSL2 는 NAT 라 폰에서 WSL 내부 IP 로 직접 못 온다. Windows 가 받아서 넘겨줘야 한다.

WSL 에서 현재 내부 IP 와 접속 주소를 확인:

```bash
hostname -I | awk '{print $1}'                          # ← 아래 <WSL_IP> 에 넣는다
grep AI_TRADER_DASH_TOKEN ~/ai_trader/.env | cut -d= -f2 # ← 접속 토큰
```

**PowerShell을 관리자로 열고**:

```powershell
netsh interface portproxy reset
netsh interface portproxy add v4tov4 listenport=8765 listenaddress=0.0.0.0 connectport=8765 connectaddress=<WSL_IP>
New-NetFirewallRule -DisplayName "DarkLab Dashboard" -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow
```

확인:

```powershell
netsh interface portproxy show v4tov4
ipconfig | findstr IPv4        # 폰이 쓸 Windows IP
```

폰에서 `http://<Windows_IP>:8765/?t=<토큰>`.
집 밖에서도 보려면 Tailscale 켜고 `100.x.x.x` 주소로 같은 포트에 붙는다.

**영구 해결**: `C:\Users\<사용자>\.wslconfig` 에 아래를 넣고 `wsl --shutdown`.
그러면 WSL 이 Windows IP 를 그대로 써서 portproxy 자체가 필요 없어진다.

```
[wsl2]
networkingMode=mirrored
```

---

## 3. 매매 루프 켜기

**평소에는 대시보드의 Run 버튼**을 쓴다. 터미널에서 직접 하려면:

```bash
cd ~/ai_trader
PYTHONPATH=src python3 -m ai_trader.engine status    # 지금 도는지
PYTHONPATH=src python3 -m ai_trader.engine start     # 시작
PYTHONPATH=src python3 -m ai_trader.engine once      # 한 사이클만
PYTHONPATH=src python3 -m ai_trader.engine stop      # 정지
```

텔레그램으로는 `/stop` 만 된다. 켜는 명령은 일부러 없다.

어느 모드로 뜰지는 `.env` 가 정한다. `status` 의 `would_start_as` 로 미리 볼 수 있다.

```
KIWOOM_MODE=demo + 키움 키   →  모의 (국내 키움 모의주문 / 해외 로컬원장)
AI_TRADER_LIVE=1            →  실계좌. 이 값은 버튼이 못 바꾼다
```

장이 닫혀 있으면 사이클을 건너뛴다. 국장 09:00–15:30 KST.

---

## 4. 점검

```bash
cd ~/ai_trader
PYTHONPATH=src python3 -m ai_trader.check        # 연결 상태 (조회만, 주문 안 냄)
PYTHONPATH=src python3 -m ai_trader.selfcheck    # 돈이 걸린 경계들
```

`selfcheck` 는 임시 디렉터리에서 돌고 알림도 끈다 — 실제 기록을 더럽히지 않는다.

---

## 5. 전부 새로 올리는 순서

```bash
# 1) WSL 안
systemctl --user restart darklab-bot darklab-dash
systemctl --user status  darklab-bot darklab-dash --no-pager
hostname -I | awk '{print $1}'        # ← 이 IP 를 2번에 쓴다

# 2) Windows PowerShell (관리자) — 2장의 명령

# 3) 확인
cd ~/ai_trader && PYTHONPATH=src python3 -m ai_trader.check
```

그 다음 텔레그램에 `/status`, 대시보드 열고 **Run**.

---

## 막혔을 때

| 증상 | 볼 곳 |
|---|---|
| 텔레그램 명령 무응답 | `systemctl --user status darklab-bot` |
| 대시보드 안 열림 | `systemctl --user status darklab-dash` / `curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8765/?t=<토큰>"` |
| 폰에서만 안 됨 | portproxy 가 옛 WSL IP 를 가리킨다 — 2번을 다시 |
| 알림이 안 옴 | `python3 -m ai_trader.check` 의 `알림` 줄 |
| 키움 429 | 정상. 전송 계층이 물러섰다 다시 시도한다 |
| 체결이 났는데 화면에 없음 | **주문 현황**(증권사 원장)과 **거래 기록**(우리 장부)을 대조 — 주문번호가 열쇠다 |
