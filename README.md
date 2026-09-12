# AI 매매 다크랩

사람이 매매 전략을 쓰지 않는다. 종목 선정·매수·매도·보유 판단을 전부 LLM 이 내리고,
그 판단이 어떻게 끝났는지를 보고 **스스로 생각을 고쳐 쓴다**.

"다크랩(Dark Lab)"은 사람이 들어가지 않아도 불 꺼놓고 돌아가는 무인 실험실을 뜻한다.
이 랩도 같다 — 사람은 판단에 개입하지 않고, 아침에 기록만 읽는다. 사람 손이 남는 곳은 킬스위치뿐이다.

## 사람이 정한 것 / AI 가 정하는 것

| 사람이 정한 것 (`config.py`, `policy.md` 의 경계 섹션) | AI 가 정하는 것 |
|---|---|
| 안정 60% : 공격 40% 자본 분리 | 어떤 종목이 '안정'이고 '공격'인지 |
| 한 종목당 슬리브의 20% 상한 | 무엇을 얼마나 살지 |
| **종목당 손절 -15%** | 언제 팔지, 언제 그냥 둘지 (단, -15% 안에서) |
| 당일 -7% 시 랩 정지 | 이번 사이클에 아무것도 안 할지 |
| 이겨야 할 벤치마크 (S&P500) | 그것을 어떻게 이길지 |
| 관찰 유니버스 | 그 안에서 무엇을 볼지 |
| 사이클 주기 | |

`lab/policy.md` 가 트레이더의 유일한 기억이다. **v0 은 의도적으로 비어 있다** — 사람이 전략을
안 써줬기 때문이다. 한 달 운용 뒤 `autoresearch` 가 실제 기록을 읽고 이 파일을 채워 나간다.

## 설치

```bash
pip3 install --user anthropic mlflow requests
cp .env.example .env   # 키를 넣는다. 없어도 전 구간이 돈다.
export PYTHONPATH=src

python3 -m ai_trader.check   # 무엇이 되고 무엇이 안 되는지 먼저 본다 (조회만, 주문 안 냄)

python3 -m ai_trader.dashboard --lan   # 폰에서 보는 대시보드 (조회 전용)
```

`.env` 는 `config.py` 가 import 시점에 자동으로 읽는다. 셸에 이미 있는 값이 파일보다 우선이다.

**증권사 키만 넣어도 전 구간이 돈다.** `ANTHROPIC_API_KEY` 가 없으면 판단이 무작위 스텁으로
떨어지는 게 아니라, 이식해 온 퀀트 점수(`quant.py`)가 판단을 맡는다 — 기록에
`brain="quant-fallback"` 이 찍힌다. 자세한 것은 아래 [LLM 키가 없을 때](#llm-키가-없을-때).

`.env` 에 넣는 값:

| 변수 | 용도 | 없으면 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 판단 엔진 | **퀀트 점수로 매매** (`quant-fallback`) |
| `AI_TRADER_QUANT_BUY` / `_SELL` | 퀀트 대역의 매수·매도선 | 65 / 40 |
| `TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET` / `TOSS_ACCOUNT` | 토스 시세·주문 | 합성 시장으로 대체 |
| `KIWOOM_MODE` (`demo`/`real`) | 키움 모의투자 / 실계좌 | `demo` |
| `APP_KEY_MOCK` / `APP_SECRET_MOCK` | 키움 모의투자 키 | 국내 주문 거절 |
| `APP_KEY` / `APP_SECRET` | 키움 실계좌 키 (`KIWOOM_MODE=real` 일 때만) | 국내 주문 거절 |
| `AI_TRADER_FETCH_WORKERS` | 관측 팩 병렬 조회 수 (기본 8) | 8 |
| `GS_QUANT_PATH` | gs-quant 소스 경로 (선택) | stdlib 로 같은 값 계산 |
| `AI_TRADER_LIVE=1` | 실계좌 주문 허용 | 실주문 안 나감 |

키가 없으면: 시장은 `SyntheticFeed`(시드 고정 랜덤워크), 판단은 `offline-stub` 으로 대체된다.
스텁은 **AI 판단이 아니며**, 모든 기록에 `brain: "offline-stub"` 이 찍혀 절대 섞이지 않는다.

## 실행

```bash
python3 -m ai_trader.selfcheck                 # 자체 점검 22항목
python3 -m ai_trader.loop --paper --once       # 한 사이클: 관측 → 판단 → 집행 → 기록
python3 -m ai_trader.loop --paper              # 무인 연속 운용 (15분 주기, 합성 시장)
python3 -m ai_trader.loop --paper --live-data  # 모의투자: 토스 실시세 + 페이퍼 계좌
python3 -m ai_trader.loop --live --live-data   # 실계좌 라우팅: 국내→키움, 해외→토스
python3 -m ai_trader.benchmark               # S&P500 지수 수집 (초과수익 계산용)
python3 -m ai_trader.backtest --days 30      # 한 달 운용 (기본: 내려받은 실제 일봉)
python3 -m ai_trader.backtest --days 30 --source synthetic   # 랜덤워크 = 배선 점검용
python3 -m ai_trader.autoresearch --iters 3  # 자기개선 루프 (기본: 실제 일봉)

# 실데이터 · 멀티 타임프레임 · 워크포워드
python3 -m ai_trader.download --market kr,us --top 300      # 일봉 전종목 + 인트라데이 상위 300
python3 -m ai_trader.download --market kr,us --all          # 인트라데이도 전종목 (~16,000, 수십 분)
python3 -m ai_trader.walkforward --train-years 3 --blind-years 1 --live-years 1
```

실계좌로 넘어갈 때는 두 개를 다 켜야 한다 — `--live` 플래그 **그리고** `AI_TRADER_LIVE=1`.
하나만으로는 주문이 나가지 않는다.

## 모의투자

토스증권 OpenAPI 에는 모의투자 서버가 없다. 실계좌용 엔드포인트 하나뿐이다.
**키움증권 REST 에는 있다** (`mockapi.kiwoom.com`). 그래서 모의투자 경로가 두 가지다.

| 실행 | 시세 | 계좌 | 주문 |
|---|---|---|---|
| `--paper` | 합성 랜덤워크 | 자체 장부 | 안 나감 |
| `--paper --live-data` | **토스 실시세** | 자체 장부 | 안 나감 |
| `--live --live-data` | 토스 실시세 | **시장별 실계좌** | **나감** (아래 표) |

`--live-data` 는 시세 조회 토큰만 쓴다 — 주문 API 를 아예 부르지 않으므로 키가 새어도 체결되지 않는다.

### 주문은 시장마다 다른 증권사로 간다

| 종목 | 시장 | 창구 | 돈이 걸리나 |
|---|---|---|---|
| `005930` (6자리 숫자) | KR | **키움증권** | `KIWOOM_MODE=demo` 면 **안 걸림** (모의투자 서버) |
| `AAPL` (영문 티커) | US | **토스증권** | 걸림 — 토스는 모의투자 서버가 없다 |

`RoutedBroker` 가 종목마다 창구를 고른다. 회계·가드레일(슬리브·20% 상한·손절 -15%·킬스위치)은
창구와 무관하게 `PaperBroker` 가 그대로 강제하고, 통과한 주문만 증권사로 나간다.

- 키움 `demo` 주문은 돈이 걸리지 않으므로 `AI_TRADER_LIVE` 없이 나간다. `real` 은 있어야 나간다.
- 토스 주문은 언제나 `AI_TRADER_LIVE=1` 이 있어야 나간다.
- **키가 없는 시장의 주문은 거절된다.** 조용히 넘어가지 않는다 — 안 나간 주문을 체결된 것처럼
  장부에 적으면 그 뒤 모든 숫자가 거짓말이 된다. 한 시장 키만 있으면 그 시장만 돈다.
- 시세는 양쪽 다 토스가 준다. 이식한 키움 클라이언트에 일봉 조회 TR(`ka10081`)이 없어서인데,
  붙이면 국내 시세도 키움으로 옮길 수 있다.

기본 유니버스는 국내 20종목이다. `AI_TRADER_UNIVERSE` 에 영문 티커를 섞으면 해외도 같이 본다
(예: `AI_TRADER_UNIVERSE=005930,000660,AAPL,NVDA`). 무엇을 살지는 여전히 brain 이 정한다.

**주말에는 안 돈다.** 실시세를 쓰는 순간 장 운영 시간을 따른다. 매 사이클 앞에서
`GET /api/v1/market-calendar/KR` 로 개장일인지 확인하고, KRX 정규장(09:00~15:30 KST)
밖이면 사이클을 건너뛰고 `research/incidents.jsonl` 에 `market_closed` 를 남긴다.
장 마감 중의 가격은 전일 종가라서, 그걸 판단에 먹이면 있지도 않은 매매를 만들어낸다.

달력 API 를 못 읽으면 요일로 대체하고 그 사실을 기록한다 — 이때는 **공휴일을 못 거른다**.
합성 시장(`--paper` 단독)에는 주말이 없다. 언제든 돈다.

## 지표와 퀀트 점수 — 측정이지 판단이 아니다

`invest` 프로젝트에서 기술지표·퀀트 채점·상관관계를 가져왔다. 다만 **역할을 바꿔서** 가져왔다.

원본에서 퀀트 점수는 매매 판단의 주체였다 (`score >= 65` 면 매수, 점수로 비중까지 계산).
여기서는 그 역할을 떼었다 — 판단 주체는 `brain` 하나뿐이고, 점수는 brain 이 보는 관측치 중 하나다.
그래서 원본의 `verdict`("강매수"/"매수"/"관망"), `buy`(bool), `position_size_pct()` 는 옮기지 않았다.
점수를 주문으로 바꾸는 순간 판단 주체가 둘이 되고, 그러면 어느 쪽을 믿을지 알 수 없다.

| 모듈 | 내용 | 출처 |
|---|---|---|
| `indicators.py` | RSI·MACD·볼린저·ADX·OBV·정배열·거래량 급증·매물대 공백 | `invest/signals.py` |
| `quant.py` | 5요소 × 20점 = 0~100. 추세(trend)·눌림목(dip) 두 관점 | `invest/quant.py` (판단부 제거) |
| `correlation.py` | 보유 종목 상관계수·덩어리·집중도. gs-quant 있으면 사용 | `invest/portfolio.py` (매수 차단부 제거) |
| `kiwoom.py` | 키움 REST 국내주식 주문 + 호가단위 검증 | `invest/kiwoom.py` |

selfcheck 가 이 경계를 지킨다 — 관측 팩 전체를 재귀로 훑어 `verdict`·`buy`·`sell`·`action`·
`recommendation`·`order` 키가 하나라도 있으면 실패한다. (`macd.signal` 은 지표 선 이름이라 예외다.)

지표를 계산하려면 고가·저가가 필요하다. 그래서 모든 피드의 캔들을
`{date, open, high, low, close, volume}` 으로 통일했다 — 종가만 넘기면 ADX 와 매물대가 조용히 죽는다.

`invest` 의 판단·전략층(`exits.py` `screener.py` `plan.py` `review.py` `analyst.py` `guard.py`
`server.py` `fundamentals.py` `fx.py` `youtube.py`)은 옮기지 않았다. 사람이 쓴 매매 규칙이라
이 랩의 전제와 정면으로 부딪힌다.

## 대시보드

```bash
python3 -m ai_trader.dashboard          # 127.0.0.1:8765 (이 PC 만)
python3 -m ai_trader.dashboard --lan    # 같은 와이파이의 폰에서 접속
```

stdlib 만 쓴다 — 빌드 도구도 프레임워크도 CDN 도 없다. 실행하면 토큰이 박힌 주소가 뜬다
(`AI_TRADER_DASH_TOKEN` 으로 고정 가능). 계좌 잔고가 보이므로 토큰 없이는 API 가 401 이다.

화면은 기록을 읽고, **자동매매 토글과 Run 버튼이 매매 루프를 띄우고 세운다.**
주문 자체는 대시보드가 만들지 않는다 — 무엇을 사고 팔지는 루프가 정하고,
실주문이 나가는지는 `AI_TRADER_LIVE` 가 정한다. **버튼은 그 환경변수를 바꾸지 못한다.**

- 토글 ON → `python3 -m ai_trader.loop` 를 띄운다. 어떤 모드로 뜨는지는 화면에 적힌다
  (합성 / 토스 실시세+자체 장부 / 실계좌). `AI_TRADER_LIVE=1` 이 아니면 절대 `--live` 로 뜨지 않는다.
- Run → 지금 한 사이클만 돌린다. 연속 운용 중이면 장부가 겹치므로 거절한다.
- 프로세스는 하나만 돈다. 두 개가 같은 `lab/paper_state.json` 을 쓰면 서로의 체결을 덮어쓴다.
- 상태를 바꾸는 것은 POST 로만 받는다. 링크를 여는 것만으로 매매가 시작되지 않는다.
- 터미널에서도 같은 일을 한다: `python3 -m ai_trader.engine start|stop|status|once`

| 탭 | 내용 | 데이터원 |
|---|---|---|
| Command | 라이브 데스크(평가·손익·예수금·포지션), 가드레일, 전략별 성과, 지수·환율, AI 판단과 근거, 뉴스, 인시던트 | `lab/paper_state.json`, `research/{cycles,decisions,backtests,incidents}.jsonl`, `benchmark.quotes()`, `data.news()` |
| Radar | 퀀트 점수 순위 + 지표(RSI·정배열·ADX·매물대) | `data.observe()` |
| Portfolio | 슬리브별 자산, 보유 종목, 상관관계, **실시간 거래 기록**(체결·거부·손절) | `paper_state.json`, `decisions.jsonl`, `correlation` |
| Control | 연결 점검, 사람이 정한 경계, 유니버스 | `check.describe()`, `config` |

거래 기록은 체결(`FILLED`)·거부(`REJECTED`)·오류(`ERROR`)를 한 타임라인에 담고, 손절
가드레일이 낸 주문은 따로 표시된다(`brain=guardrail`).

**만들지 않은 화면이 있다.** 첨부받은 화면 중 DART 재무제표(상세실적)와 섹터 분류는 이 레포에
데이터원이 없다. 숫자를 지어내지 않으려고 화면 자체를 만들지 않고, Control 탭에 그 사실을 적었다.
지수·환율은 못 받으면 0 이 아니라 `unavailable` 로 표시된다.

## LLM 키가 없을 때

판단 주체가 세 단계로 떨어진다. 어느 단계였는지는 모든 기록의 `brain` 필드에 남는다.

| `brain` | 언제 | 무엇으로 판단하나 |
|---|---|---|
| `claude-opus-5` | `ANTHROPIC_API_KEY` 있음 | LLM 이 `policy.md` 를 들고 판단 |
| `quant-fallback` | 키 없음 + 관측 팩에 퀀트 점수 있음 | **퀀트 점수 0~100** (매수 ≥65, 매도 <40) |
| `offline-stub` | 키 없음 + 점수도 없음 | 결정론적 해시. **판단 아님 — 배선 점검용** |

`quant-fallback` 은 스텁이 아니다. 실제로 규칙대로 매매한다 — 점수 높은 순으로 훑어 매수선을
넘으면 사고, 보유 중 매도선 아래로 떨어지면 판다. 슬리브는 일변동성으로 가른다.

**다만 이 셋(`AI_TRADER_QUANT_BUY`/`_SELL`/`_AGGRESSIVE_VOL`)은 사람이 정한 임계값이고,
이 랩의 전제("사람이 전략을 안 쓴다")의 예외다.** 그래서 두 가지를 지킨다:

- `quant-fallback` 기록은 `policy.md` 학습 근거로 **쓰이지 않는다**. 사람이 정한 임계값의
  결과로 LLM 의 생각을 고치면 앞뒤가 안 맞는다.
- LLM 이 "과거 내 판단"으로 되돌려받는 기록에서도 빠진다. 손절 가드레일(`guardrail`) 기록도 같다.

`ANTHROPIC_API_KEY` 를 넣는 순간 판단 주체는 다시 LLM 하나가 되고, 임계값은 아예 읽히지 않는다.

## 실계좌 장부 동기화

`--live` 로 시작하면 사이클을 돌기 전에 증권사에서 보유·현금을 끌어와 내부 장부를 맞춘다.
못 읽으면 **중단한다.** 내부 장부가 실계좌와 어긋나면 슬리브 회계·20% 상한·손절 -15% 가
전부 허구 위에서 계산되기 때문이다. 모른 채로 실매매하지 않는다.

슬리브는 증권사에 없는 개념이라 기존 장부의 배정을 유지하고, 처음 보는 종목은 `AGGRESSIVE`
로 둔다(더 좁은 상한이 걸리는 쪽). 슬리브별 현금은 총현금을 사람이 정한 6:4 로 나눈다.

## 손절 -15% 와 벤치마크

두 가지가 `config.py` 와 `policy.md` 의 경계 섹션에 못박혀 있다. AI 는 이 둘을 못 바꾼다.

- **종목당 손절 -15%** (`STOP_LOSS_PCT`). 평가손실이 -15% 에 닿은 보유는 brain 에게 묻지 않고
  전량 정리된다. 손절은 판단보다 **먼저** 실행되므로 "조금만 더 버틴다"가 성립하지 않는다.
  가격을 모르는 종목은 팔지 않는다 — 모른 채로 손실을 단정하지 않는다.
  매도가 거절되면 기록하고 다음 사이클에 다시 걸린다.
- **벤치마크 S&P500** (`BENCHMARK=^GSPC`). 모든 백테스트 결과에 `vs_benchmark` 가 붙는다.
  지수 데이터가 없으면 `alpha=0` 으로 채우지 않고 `unavailable` 로 남긴다 —
  벤치마크 없는 초과수익은 거짓말이다.

지수를 이기는 것은 목표이지 보장이 아니다. 이 레포가 하는 일은 이기는 것이 아니라 **재는 것**이고,
이겼는지 아닌지는 `research/backtests.jsonl` 의 `alpha_pct` 가 말한다.

## 백테스트는 무엇을 재생하는가

| `--source` | 재생 대상 | 쓸 곳 |
|---|---|---|
| `history` (기본) | `data/bars_1d.parquet` — 내려받은 실제 일봉 | 진짜 백테스트 |
| `synthetic` | 시드 고정 랜덤워크 | 배선 점검. **시장이 아니다** |
| `toss` | 토스 실시세를 그 자리에서 받아 재생 | 최신 구간 확인 |

`autoresearch` 도 같은 소스를 쓴다. 기본이 `history` 인 이유는 하나다 —
랜덤워크에 최적화된 정책은 학습이 아니라 노이즈 과적합이다.

## 자기개선 루프 (오토리서치)

Karpathy 의 `autoresearch` 를 그대로 옮겼다. 고칠 수 있는 파일은 하나, 예산은 고정, 지표는 하나.

```
현재 policy.md → 30일 백테스트 → risk_adjusted 기준선
   ↓
연구원 LLM 이 실제 운용 기록(사이클·판단·사고·폐기된 가설)만 근거로 policy.md 전문을 다시 씀
   ↓
같은 예산으로 재평가 → 개선되면 KEEP(이전 버전은 lab/policy_history/ 로), 아니면 REVERT
   ↓
research/experiments.jsonl 에 판정과 사유를 남김
```

`risk_adjusted = 총수익% / (일변동성% + 최대낙폭% + 1)` — 크게 벌어도 크게 흔들리면 점수를 못 받는다.
경계 섹션을 건드린 제안은 백테스트도 돌리지 않고 자동 기각된다.

## 기록

| 파일 | 내용 |
|---|---|
| `research/decisions.jsonl` | 개별 판단 — 종목, 액션, 슬리브, 수량, 확신, **근거**, 체결/거부 사유 |
| `research/cycles.jsonl` | 사이클 요약 — 시장관, 배운 것, 자산, 토큰 사용량 |
| `research/backtests.jsonl` | 구간 운용 성과 |
| `research/experiments.jsonl` | 자기개선 실험 — 바꾼 것, 점수, KEEP/REVERT 사유 |
| `research/incidents.jsonl` | 킬스위치 발동, 주문 오류, 사이클 크래시 |
| `research/daily_log.md` | 사람이 아침에 읽는 보고서 |
| `research/autoresearch.md` | 실험 판정표 |
| `lab/mlflow.db` | MLflow — `mlflow ui --backend-store-uri sqlite:///lab/mlflow.db` |

## 설계상 지켜지는 것

- **백테스트는 뉴스를 쓰지 않는다.** 오늘 헤드라인을 과거 봉에 먹이면 미래를 훔쳐보는 것이다.
  실시간 뉴스는 라이브 사이클에서만 쓴다.
- **판단을 못 하면 아무것도 하지 않는다.** 네트워크 실패·레이트리밋·거부 시 추측 매매 대신 무거래.
- **랩은 멈추지 않는다.** 뉴스 수집 실패, 주문 거부, 사이클 크래시 모두 기록하고 다음 사이클로 간다.
- **가드레일이 LLM 보다 먼저 실행된다.** 실계좌 주문은 슬리브 회계와 상한을 통과한 뒤에야 나간다.

## 데이터: 무료 소스의 실제 천장

2026-09 기준 Yahoo 에서 실측한 값이다. **추정이 아니라 받아본 결과다.**

| 타임프레임 | 5년 요청 | 실제 확보 | 판정 |
|---|---|---|---|
| 15분봉 | 1,825일 | **60일** | 3% — 다년 백테스트 불가 |
| 1시간봉 | 1,825일 | **730일** | 40% |
| 4시간봉 | 1,825일 | **730일** (1시간봉 파생) | 40% |
| 일봉 | 1,825일 | **5년+** | 충족 |

즉 *3년 학습 + 1년 블라인드 + 1년 실전* 을 4h/1h/15m 으로 돌리는 것은 무료 데이터로 불가능하다.
`walkforward` 는 이 사실을 숨기지 않는다 — 데이터가 없는 구간은 `확보 불가` 로 건너뛰고,
빠진 타임프레임을 일봉으로 대체하지 않으며, 구간마다 실제로 쓴 TF 와 빠진 TF 를 같이 적는다.

**5년치 15분봉을 정말 원한다면** 유료 소스가 필요하다: Alpha Vantage Premium(미장 인트라데이
20년+), Polygon.io, Databento, 또는 증권사 유료 피드(국장). `download.py` 의 `fetch()` 하나만
갈아끼우면 나머지는 그대로 돈다.

## 유니버스 규모

| 시장 | 종목 수 | 출처 |
|---|---|---|
| 국장 (코스피+코스닥) | 2,802 | KIND 상장법인목록 (로그인 불필요) |
| 미장 (NASDAQ+기타) | 13,216 | nasdaqtrader 심볼 디렉터리 |

전종목 일봉 ≈ 14분 / 1GB, 타임프레임당 비슷. 인트라데이까지 전부 받으면 ~6GB.
기본값은 거래대금 상위 N 종목으로 좁힌다 — 매매 전략이 아니라 체결 가능성 필터다.

## 워크포워드가 강제하는 것

- 세 구간은 시간순으로만 흐르고 서로 겹치지 않는다 (self-check 가 검사한다).
- **정책은 학습 구간이 끝난 뒤에만 고친다.** 블라인드·실전 구간에서는 손대지 않는다.
- 어느 시점에서도 그 시각 이후에 마감된 봉은 보이지 않는다 (self-check 가 검사한다).
