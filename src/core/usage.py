"""Provider usage normalization. Missing measurements are not zero measurements."""

from decimal import Decimal, InvalidOperation

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def count(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def money(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and 0 <= number < Decimal("1e18") else None
    except (ValueError, InvalidOperation):
        return None


def mapping(value):
    return value if isinstance(value, dict) else {}


def text(value):
    return value[:200] if isinstance(value, str) and value else None


def claude_usage(data):
    data = mapping(data)
    raw = mapping(data.get("usage"))
    result = dict(
        zip(
            TOKEN_FIELDS,
            (
                count(raw.get(k))
                for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            ),
        )
    )
    reported = money(data.get("total_cost_usd"))
    result.update(
        provider_cost_usd=str(reported) if reported is not None else None, cost_scope="session", fresh_session=False
    )
    models = {}
    for model, values in list(mapping(data.get("modelUsage")).items())[:100]:
        if text(model):
            values = mapping(values)
            models[model] = dict(
                zip(
                    TOKEN_FIELDS,
                    (
                        count(values.get(k))
                        for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
                    ),
                )
            )
    result["model_usage"] = models
    return result


def inclusive_usage(raw, *, codex=False):
    """OpenAI/Codex input includes cache hits. Output already includes reasoning."""
    raw = mapping(raw)
    details = mapping(raw.get("prompt_tokens_details"))
    total_input = count(raw.get("input_tokens" if codex else "prompt_tokens"))
    cache_read = count(raw.get("cached_input_tokens") if codex else details.get("cached_tokens"))
    # Codex exec and Chat Completions have no separately charged cache-write
    # category in their published usage schemas. Preserve one when supplied by
    # an OpenAI-compatible endpoint; do not add cached input to input again.
    cache_write = count(raw.get("cache_write_tokens", 0) if codex else details.get("cache_write_tokens", 0))
    plain = None
    if all(v is not None for v in (total_input, cache_read, cache_write)):
        if cache_read + cache_write <= total_input:
            plain = total_input - cache_read - cache_write
        else:
            cache_read = cache_write = None  # malformed overlapping categories
    return dict(
        input_tokens=plain,
        output_tokens=count(raw.get("output_tokens" if codex else "completion_tokens")),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        provider_cost_usd=None,
        cost_scope="call",
        model_usage={},
    )


def ollama_usage(data):
    data = mapping(data)
    # Current Ollama reports cache hits as a subset of prompt_eval_count.
    # Older servers omit it. Missing cache measurements remain unknown.
    return inclusive_usage(
        {
            "prompt_tokens": data.get("prompt_eval_count"),
            "completion_tokens": data.get("eval_count"),
            "prompt_tokens_details": {"cached_tokens": data.get("prompt_eval_cached_count")},
        }
    )


def total(usage):
    values = [usage.get(k) for k in TOKEN_FIELDS]
    return count(sum(v for v in values if v is not None)) if any(v is not None for v in values) else None


def table_cost(usage, provider, model, prices):
    """USD decimal, exact provider/model only; no wildcard, alias or missing rate."""
    rates = mapping(mapping(prices).get(provider)).get(model)
    if not isinstance(rates, dict):
        return None
    amount = Decimal(0)
    for field in TOKEN_FIELDS:
        tokens = count(usage.get(field))
        rate = money(rates.get(field.removesuffix("_tokens")))
        if tokens is None or rate is None:
            return None
        amount += Decimal(tokens) * rate / 1_000_000
    return amount
