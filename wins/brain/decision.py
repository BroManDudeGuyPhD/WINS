"""
wins/brain/decision.py
Core Claude decision engine.
Implements the tiered Haiku → Sonnet → Opus model approach from WINS.md.
Set USE_MOCK_BRAIN=true to run without an Anthropic API key.
"""
import json
from decimal import Decimal

from wins.shared.config import (
    ANTHROPIC_API_KEY, HAIKU_MODEL, SONNET_MODEL, OPUS_MODEL,
    MIN_CONFIDENCE_TO_TRADE, USE_MOCK_BRAIN,
)
from wins.shared.models import DecisionOutput, SignalBundle
from wins.shared.logger import get_logger
from wins.brain.prompts import SYSTEM_PROMPT, build_user_message
from wins.brain.mock_decision import mock_decision

log = get_logger("brain")

# Lazy-init — avoids import-time crash when no API key is set
_client = None

def _get_client():
    global _client
    if _client is None:
        import anthropic
        if not ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. "
                "Set USE_MOCK_BRAIN=true to run without it."
            )
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def summarise_raw_signals(raw_text: str) -> str:
    """
    Uses Haiku to compress noisy social/news text before passing to Sonnet.
    Called only when raw text is long (> 2000 chars).
    """
    if len(raw_text) <= 2000:
        return raw_text

    client = _get_client()
    import anthropic
    msg = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=512,
        system="You summarise raw crypto news and social media data into 3-5 bullet points. Be factual and brief.",
        messages=[{"role": "user", "content": f"Summarise:\n\n{raw_text[:8000]}"}],
    )
    return msg.content[0].text


# Return type: (decision, model_used, input_tokens, output_tokens, cache_read_tokens)
BrainResult = tuple[DecisionOutput | None, str, int, int, int]


def make_decision(
    bundle: SignalBundle,
    account_state: dict | None = None,
    as_of: str | None = None,
) -> BrainResult:
    """
    Route to mock or real Claude based on USE_MOCK_BRAIN env flag.
    Returns (decision, model_used, input_tokens, output_tokens, cache_read_tokens).
    decision is None only on unrecoverable error.
    """
    if USE_MOCK_BRAIN:
        log.debug(f"[MOCK MODE] Generating mock decision for {bundle.token}")
        return mock_decision(bundle), "mock", 0, 0, 0

    return _claude_decision(bundle, use_opus=False, account_state=account_state, as_of=as_of)


def _claude_decision(
    bundle: SignalBundle,
    use_opus: bool = False,
    account_state: dict | None = None,
    as_of: str | None = None,
) -> BrainResult:
    """Send a SignalBundle to Claude and parse the structured JSON response."""
    import anthropic

    model = OPUS_MODEL if use_opus else SONNET_MODEL
    client = _get_client()

    # Pre-compress noisy text fields with Haiku
    bundle_dict = bundle.model_dump()
    bundle_dict["news_summary"]   = summarise_raw_signals(bundle.news_summary)
    bundle_dict["social_summary"] = summarise_raw_signals(bundle.social_summary)

    user_message = build_user_message(bundle_dict, account_state=account_state, as_of=as_of)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},   # prompt caching
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as exc:
        log.error(f"Anthropic API error for {bundle.token}: {exc}")
        return None, model, 0, 0, 0

    raw_text = response.content[0].text.strip()

    try:
        raw_json = json.loads(raw_text)
    except json.JSONDecodeError:
        log.error(f"Claude returned non-JSON for {bundle.token}: {raw_text[:500]}")
        return None, model, 0, 0, 0

    try:
        decision = DecisionOutput(**raw_json)
    except Exception as exc:
        log.error(f"DecisionOutput validation failed for {bundle.token}: {exc} | raw={raw_json}")
        return None, model, 0, 0, 0

    usage = response.usage
    input_tokens      = getattr(usage, "input_tokens", 0) or 0
    output_tokens     = getattr(usage, "output_tokens", 0) or 0
    cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0

    log.info(
        f"Decision [{model}] {bundle.token}: action={decision.action} "
        f"confidence={decision.confidence} macro_gate={decision.macro_gate} "
        f"risk_flag={decision.risk_flag} | "
        f"tokens in={input_tokens} out={output_tokens} cache_read={cache_read_tokens}"
    )

    # Escalate to Opus only for very high-confidence catalyst signals
    # Threshold raised to 0.92 (from 0.85) and gated on signal_type=catalyst
    # to avoid runaway Opus spend on routine momentum calls
    if (
        not use_opus
        and decision.confidence >= Decimal("0.92")
        and decision.signal_type.value == "catalyst"
    ):
        log.info(
            f"High-confidence catalyst ({decision.confidence}) on {bundle.token} "
            "— escalating to Opus."
        )
        return _claude_decision(bundle, use_opus=True, account_state=account_state, as_of=as_of)

    return decision, model, input_tokens, output_tokens, cache_read_tokens
