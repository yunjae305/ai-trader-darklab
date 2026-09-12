"""Dark Lab 운영 상수. 여기에는 매매 규칙이 들어가지 않는다.

사람이 정하는 것은 '돈의 경계'뿐이고, 무엇을 언제 사고 파는지는 brain 이 정한다.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESEARCH = ROOT / "research"
LAB = ROOT / "lab"
POLICY = LAB / "policy.md"
STATE = LAB / "paper_state.json"

# --- 자본 배분: 안정 6 : 공격 4 (사람이 정한 유일한 배분 제약) ---
SLEEVES: dict[str, float] = {"STABLE": 0.60, "AGGRESSIVE": 0.40}

START_CASH = float(os.getenv("AI_TRADER_START_CASH", "10_000_000".replace("_", "")))

# --- 가드레일: 전략이 아니라 사고 방지 장치. AI 가 넘을 수 없는 선. ---
MAX_POSITION_PCT = float(os.getenv("AI_TRADER_MAX_POSITION_PCT", "0.20"))  # 슬리브 대비
DAILY_LOSS_KILL_PCT = float(os.getenv("AI_TRADER_DAILY_LOSS_KILL", "0.07"))  # 당일 -7% → 랩 정지
MAX_ORDERS_PER_CYCLE = int(os.getenv("AI_TRADER_MAX_ORDERS", "8"))
# 종목당 손절. 평가손실이 이 선을 넘으면 brain 에게 묻지 않고 전량 판다.
# 사람이 정한 경계다 — AI 는 이 값을 못 바꾸고, 이 선을 넘겨 버티는 선택도 못 한다.
STOP_LOSS_PCT = float(os.getenv("AI_TRADER_STOP_LOSS", "-15.0"))
# 이겨야 하는 대상. 성과는 이 지수 대비 초과수익으로 잰다.
BENCHMARK = os.getenv("AI_TRADER_BENCHMARK", "^GSPC")  # S&P500
LIVE_TRADING = os.getenv("AI_TRADER_LIVE", "0") == "1"  # 1 이어야만 실주문

# --- 관찰 대상 유니버스. AI 가 이 안에서 스스로 고른다(선정도 AI 몫). ---
UNIVERSE: list[str] = [s.strip() for s in os.getenv(
    "AI_TRADER_UNIVERSE",
    "005930,000660,373220,207940,005380,035420,035720,068270,105560,051910,"
    "247540,086520,196170,058470,277810,042700,022100,095340,900140,053610",
).split(",") if s.strip()]

# --- 토스증권 OpenAPI ---
TOSS_BASE = "https://openapi.tossinvest.com"
TOSS_WS = "wss://openapi-ws.tossinvest.com/ws/v1"
TOSS_CLIENT_ID = os.getenv("TOSS_CLIENT_ID", "")
TOSS_CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET", "")
TOSS_ACCOUNT = os.getenv("TOSS_ACCOUNT", "")

# --- 키움증권 REST (국내 주문). 토스와 달리 모의투자 서버가 따로 있다. ---
KIWOOM_MODE = os.getenv("KIWOOM_MODE", "demo")  # demo=모의투자 / real=실계좌
_KW_SUFFIX = "" if KIWOOM_MODE == "real" else "_MOCK"
KIWOOM_KEY = os.getenv(f"APP_KEY{_KW_SUFFIX}", "")
KIWOOM_SECRET = os.getenv(f"APP_SECRET{_KW_SUFFIX}", "")

# --- 판단 엔진 ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
BRAIN_MODEL = os.getenv("AI_TRADER_MODEL", "claude-opus-5")
RESEARCH_MODEL = os.getenv("AI_TRADER_RESEARCH_MODEL", "claude-opus-5")

CYCLE_SECONDS = int(os.getenv("AI_TRADER_CYCLE_SECONDS", "900"))  # 15분
# mlflow 3.x 는 파일 스토어를 폐기했다 — 로컬 sqlite 가 기본.
MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI") or f"sqlite:///{ROOT / 'lab' / 'mlflow.db'}"
MLFLOW_EXPERIMENT = os.getenv("MLFLOW_EXPERIMENT", "ai-trader-darklab")


def have_broker_keys() -> bool:
    return bool(TOSS_CLIENT_ID and TOSS_CLIENT_SECRET and TOSS_ACCOUNT)


def have_kiwoom_keys() -> bool:
    return bool(KIWOOM_KEY and KIWOOM_SECRET)


def have_brain_key() -> bool:
    return bool(ANTHROPIC_API_KEY)
