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
| 당일 -7% 시 랩 정지 | 언제 팔지, 언제 그냥 둘지 |
| 관찰 유니버스 | 그 안에서 무엇을 볼지 |
| 사이클 주기 | 이번 사이클에 아무것도 안 할지 |

`lab/policy.md` 가 트레이더의 유일한 기억이다. **v0 은 의도적으로 비어 있다** — 사람이 전략을
안 써줬기 때문이다. 한 달 운용 뒤 `autoresearch` 가 실제 기록을 읽고 이 파일을 채워 나간다.

## 설치

```bash
pip3 install --user anthropic mlflow requests
cp .env.example .env   # 키를 넣는다. 없어도 전 구간이 돈다.
export PYTHONPATH=src
```

`.env` 에 넣는 값:

| 변수 | 용도 | 없으면 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 판단 엔진 | `offline-stub` 으로 대체 (판단 아님) |
| `TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET` / `TOSS_ACCOUNT` | 토스 시세·주문 | 합성 시장으로 대체 |
| `KIWOOM_MODE` (`demo`/`real`) | 키움 모의투자 / 실계좌 | `demo` |
| `APP_KEY_MOCK` / `APP_SECRET_MOCK` | 키움 모의투자 키 | `--kiwoom` 불가 |
| `APP_KEY` / `APP_SECRET` | 키움 실계좌 키 (`KIWOOM_MODE=real` 일 때만) | `--kiwoom` 불가 |
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
python3 -m ai_trader.loop --kiwoom --live-data # 키움 모의계좌로 실제 주문 (돈 안 걸림)
python3 -m ai_trader.backtest --days 30      # 한 달 페이퍼 운용
python3 -m ai_trader.autoresearch --iters 3  # 자기개선 루프

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
| `--paper` | 합성 랜덤워크 | 페이퍼 | 안 나감 |
| `--paper --live-data` | **토스 실시세** | 페이퍼 (자체 장부) | 안 나감 |
| `--kiwoom --live-data` | 토스 실시세 | **키움 모의계좌** | **나감 (돈 안 걸림)** |
| `--live` + `AI_TRADER_LIVE=1` | 토스 실시세 | 토스 실계좌 | **나감 (돈 걸림)** |

`--live-data` 는 시세 조회 토큰만 쓴다 — 주문 API 를 아예 부르지 않으므로 키가 새어도 체결되지 않는다.
`--kiwoom` 은 `KIWOOM_MODE=demo` 면 모의투자 서버로 주문이 실제로 나간다. 체결·잔고를 증권사가
처리하므로 자체 페이퍼 장부보다 진짜에 가깝다. `KIWOOM_MODE=real` 은 `AI_TRADER_LIVE=1` 이 있어야만 나간다.

주문은 키움, 시세는 토스로 갈린다 — 이식한 키움 클라이언트에 일봉 조회 TR 이 없기 때문이다.
`kiwoom.py` 에 `ka10081` 을 붙이면 한쪽으로 합칠 수 있다.

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
