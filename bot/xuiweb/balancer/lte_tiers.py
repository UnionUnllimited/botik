"""LTE trigger tiers: each trigger word = cascade level with its own baseline."""

from __future__ import annotations

from .countries import _RENEWAL_DATE_IN_NAME_RE, is_lte_tag
from .settings import DEFAULT_LTE_TRIGGERS

# list[tuple[trigger_word, baseline_ms]] — priority top to bottom


def _tier_probe_text(tag: str) -> str:
    """Tag text for tier matching — keep [TEST]/[NEW] brackets, strip only renewal dates."""
    return _RENEWAL_DATE_IN_NAME_RE.sub('', str(tag or '')).strip()


def match_lte_trigger(
    tag: str,
    lte_trigger_tiers: list[tuple[str, int]] | None,
) -> tuple[str, int] | None:
    """
    Return (trigger_word, baseline_ms) for the highest-priority matching trigger.
    Priority = order in admin list (first wins).
    """
    tiers = lte_trigger_tiers or [(w, 1500) for w in DEFAULT_LTE_TRIGGERS]
    text_lower = _tier_probe_text(tag).lower()
    for word, baseline_ms in tiers:
        item = str(word or '').strip()
        if item and item.lower() in text_lower:
            return item, baseline_ms
    return None


def classify_lte_tier(tag: str, lte_trigger_tiers: list[tuple[str, int]] | None = None) -> str | None:
    """Tier key = trigger word (used as bucket key in cascade)."""
    matched = match_lte_trigger(tag, lte_trigger_tiers)
    return matched[0] if matched else None


def split_lte_by_tier(
    lte_outbounds: list[dict],
    lte_trigger_tiers: list[tuple[str, int]] | None = None,
) -> dict[str, list[dict]]:
    tiers = lte_trigger_tiers or [(w, 1500) for w in DEFAULT_LTE_TRIGGERS]
    buckets: dict[str, list[dict]] = {word: [] for word, _ in tiers}
    fallback_key = tiers[0][0] if tiers else 'LTE'
    for ob in lte_outbounds:
        key = classify_lte_tier(ob.get('tag', ''), tiers) or fallback_key
        if key not in buckets:
            buckets[key] = []
        buckets[key].append(ob)
    return buckets


def active_lte_tiers(
    buckets: dict[str, list[dict]],
    lte_trigger_tiers: list[tuple[str, int]] | None = None,
) -> list[str]:
    """Active tiers in configured priority order."""
    tiers = lte_trigger_tiers or [(w, 1500) for w in DEFAULT_LTE_TRIGGERS]
    ordered = [word for word, _ in tiers if buckets.get(word)]
    for word in buckets:
        if word not in ordered and buckets[word]:
            ordered.append(word)
    return ordered


def lte_tier_summary(
    lte_outbounds: list[dict],
    lte_trigger_tiers: list[tuple[str, int]] | None = None,
) -> dict[str, int]:
    buckets = split_lte_by_tier(lte_outbounds, lte_trigger_tiers)
    return {k: len(v) for k, v in buckets.items() if v}


def lte_trigger_words(lte_trigger_tiers: list[tuple[str, int]] | None) -> list[str]:
    tiers = lte_trigger_tiers or [(w, 1500) for w in DEFAULT_LTE_TRIGGERS]
    return [word for word, _ in tiers if str(word).strip()]


def is_lte_host(tag: str, lte_trigger_tiers: list[tuple[str, int]] | None = None) -> bool:
    return match_lte_trigger(tag, lte_trigger_tiers) is not None or is_lte_tag(
        tag, lte_trigger_words(lte_trigger_tiers),
    )
