"""Dark Lab 운영 상수. 여기에는 매매 규칙이 들어가지 않는다.

사람이 정하는 것은 '돈의 경계'뿐이고, 무엇을 언제 사고 파는지는 brain 이 정한다.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


# 이 프로세스가 .env 에서 올린 키들. 자식 프로세스를 띄울 때 빼주면 자식이
# 파일을 다시 읽는다 — 오래 떠 있는 부모의 옛 값이 따라가지 않게 하려는 것이다.
ENV_FILE_KEYS: set[str] = set()


def _load_env(path: Path | None = None) -> bool:
    """`.env` 를 환경변수로 올린다. python-dotenv 없이 stdlib 만 쓴다.

    이 파일의 상수들이 import 시점에 os.getenv 로 읽히므로, 로딩은 그 전에 끝나야 한다.
    이미 설정된 값은 덮지 않는다 — 셸에서 준 값이 파일보다 우선이다.
    """
    path = Path(path) if path else ROOT / ".env"
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in os.environ:   # 셸에서 준 값이 파일보다 우선이다
            os.environ[key] = value.strip().strip('"').strip("'")
            ENV_FILE_KEYS.add(key)
    return True


ENV_LOADED = _load_env()

RESEARCH = ROOT / "research"
LAB = ROOT / "lab"
POLICY = LAB / "policy.md"
STATE = LAB / "paper_state.json"
MOCK_STATE = LAB / "mock_state.json"

# --- 자본 배분: 안정 6 : 공격 4 (사람이 정한 유일한 배분 제약) ---
SLEEVES: dict[str, float] = {"STABLE": 0.60, "AGGRESSIVE": 0.40}
# 총 모의자본에서 시장별로 실제 투입할 수 있는 상한. 현금은 공통 원화 장부에 남는다.
MARKET_WEIGHTS: dict[str, float] = {"KR": 0.50, "US": 0.50}
USD_KRW = float(os.getenv("AI_TRADER_USD_KRW", "1400"))

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

# --- LLM 키가 없을 때의 판단 임계값 (quant-fallback 전용) ---
# 이 랩의 전제는 '사람이 전략을 안 쓴다'이고 이 세 값은 그 예외다. ANTHROPIC_API_KEY 가
# 있으면 아예 쓰이지 않는다. 원본(invest/quant.py)의 매수선 65를 그대로 가져왔다.
QUANT_BUY_ABOVE = float(os.getenv("AI_TRADER_QUANT_BUY", "65"))
QUANT_SELL_BELOW = float(os.getenv("AI_TRADER_QUANT_SELL", "40"))
QUANT_AGGRESSIVE_VOL = float(os.getenv("AI_TRADER_QUANT_AGGRESSIVE_VOL", "2.5"))  # 일변동성 %
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

# --- DART 전자공시 (재무제표 상세실적) ---
DART_API_KEY = os.getenv("DART_API_KEY", "")

# --- 텔레그램 알림. 루프는 사람 없이 도니까 사고가 나면 이 경로로만 닿는다. ---
# 토큰은 봇을 조종할 수 있는 자격증명이다 — .env 에만 두고 코드에 적지 않는다.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- 판단 엔진 ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
BRAIN_MODEL = os.getenv("AI_TRADER_MODEL", "claude-opus-5")
RESEARCH_MODEL = os.getenv("AI_TRADER_RESEARCH_MODEL", "claude-opus-5")

# --- 판단 백엔드: auto(기본) | api | cli | codex | off ---
# auto는 API → Claude CLI → Codex CLI 순서다. 로컬 CLI는 로그인된 구독 한도를 쓴다.
BRAIN_BACKEND = os.getenv("AI_TRADER_BRAIN", "auto")
BRAIN_CLI = os.getenv("AI_TRADER_CLAUDE_CLI", "claude")
CODEX_CLI = os.getenv("AI_TRADER_CODEX_CLI", "codex")
CODEX_MODEL = os.getenv("AI_TRADER_CODEX_MODEL", "")  # 비우면 ~/.codex/config.toml 설정 사용
BRAIN_CLI_TIMEOUT = int(os.getenv("AI_TRADER_BRAIN_TIMEOUT", "240"))
# Claude 한도에 걸린 뒤 다시 찔러보기까지 기다리는 시간. 한도는 몇 시간이면 풀리는데
# 루프는 며칠씩 돈다 — 영원히 Codex 에 남지 않게 한다.
BRAIN_QUOTA_COOLDOWN = int(os.getenv("AI_TRADER_QUOTA_COOLDOWN", "3600"))

CYCLE_SECONDS = int(os.getenv("AI_TRADER_CYCLE_SECONDS", "900"))  # 15분
# 사이클 사이를 자면서 보내지 않고 호가창을 들여다보는 주기. LLM 을 부르지 않으므로
# 초 단위로 돌아도 비용이 0이다. 판단은 느리게, 반응은 빠르게 — 사람이 하는 방식이다.
WATCH_SECONDS = int(os.getenv("AI_TRADER_WATCH_SECONDS", "3"))
# 보유가 이만큼 움직이면 다음 사이클을 기다리지 않고 LLM 을 깨운다 (%).
WATCH_JOLT_PCT = float(os.getenv("AI_TRADER_WATCH_JOLT_PCT", "3.0"))
# 익절 문턱. 여기 닿으면 판단을 앞당긴다 — 파는 것은 brain 이 정한다.
TAKE_PROFIT_PCT = float(os.getenv("AI_TRADER_TAKE_PROFIT", "10.0"))
# mlflow 3.x 는 파일 스토어를 폐기했다 — 로컬 sqlite 가 기본.
MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI") or f"sqlite:///{ROOT / 'lab' / 'mlflow.db'}"
MLFLOW_EXPERIMENT = os.getenv("MLFLOW_EXPERIMENT", "ai-trader-darklab")


def have_broker_keys() -> bool:
    # accountSeq 는 키가 아니다. 비어 있으면 TossVenue.account_seq() 가 계좌 목록에서 찾는다.
    return bool(TOSS_CLIENT_ID and TOSS_CLIENT_SECRET)


MARKETS = ("KR", "US")


def market_of(symbol: str) -> str:
    """종목이 어느 시장인지. 국내는 6자리 숫자, 해외는 영문 티커다.

    주문이 어느 증권사로 갈지가 여기서 갈린다 — 국내는 키움, 해외는 토스.
    모호하면 추측하지 않고 세운다. 잘못 라우팅된 주문은 엉뚱한 계좌에서 체결된다.
    """
    s = (symbol or "").strip().upper()
    if re.fullmatch(r"\d{6}", s):
        return "KR"
    if re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", s):
        return "US"
    raise ValueError(f"어느 시장인지 알 수 없는 종목 표기: {symbol!r}")


def have_kiwoom_keys() -> bool:
    return bool(KIWOOM_KEY and KIWOOM_SECRET)


def have_brain_key() -> bool:
    """API 키로 판단할 수 있나. CLI 경로는 have_brain() 을 봐라."""
    return bool(ANTHROPIC_API_KEY)


def brain_backend() -> str:
    """우선 시도할 판단 경로: 'api' | 'cli' | 'codex' | 'none'.

    auto는 API 키, Claude CLI, Codex CLI 순서다. Claude CLI가 한도 초과로 실패하면
    실행 시점에 Codex CLI로 한 번 더 시도한다.
    """
    want = BRAIN_BACKEND.lower()
    if want == "off":
        return "none"
    if want in ("api", "cli", "codex"):
        if want == "api":
            return "api" if ANTHROPIC_API_KEY else "none"
        if want == "cli":
            return "cli" if shutil.which(BRAIN_CLI) else "none"
        return "codex" if shutil.which(CODEX_CLI) else "none"
    if ANTHROPIC_API_KEY:
        return "api"
    if shutil.which(BRAIN_CLI):
        return "cli"
    return "codex" if shutil.which(CODEX_CLI) else "none"


def brain_name() -> str:
    """지금 판단을 실제로 하는 것의 이름. 기록·화면·콘솔이 같은 값을 써야 한다.

    이 계산이 호출부에 흩어져 있던 동안 mlflow 만 C.BRAIN_MODEL 을 그대로 적어,
    codex 로 돌린 실험이 기록에는 claude 로 남았다. 실험 비교가 거짓이 된다.
    """
    return {"api": BRAIN_MODEL,
            "cli": f"{BRAIN_MODEL}(cli→codex)",
            "codex": f"{CODEX_MODEL or 'codex'}(cli)",
            }.get(brain_backend(), "quant-fallback(LLM 없음)")


def cli_path(name: str) -> str:
    """CLI 를 띄울 때 넘길 경로. 이름만 넘기면 Windows 에서 죽는다.

    npm 이 깐 CLI 는 Windows 에서 'claude.CMD' 라는 배치 파일이다. shutil.which 는
    PATHEXT 를 붙여 찾아내지만, subprocess 는 CreateProcess 를 쓰므로 확장자 없는
    이름으로는 WinError 2 가 난다 — 가용성 검사는 통과하는데 실행만 죽었다.
    그래서 which 가 찾아낸 그 파일을 그대로 넘긴다.
    """
    return shutil.which(name) or name


def have_codex() -> bool:
    return bool(shutil.which(CODEX_CLI))


def have_brain() -> bool:
    return brain_backend() != "none"
