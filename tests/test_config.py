"""Configuration loading, interpolation, and the promotion gates."""

from __future__ import annotations

import pytest

from liqengine.config.loader import interpolate, load_registry
from liqengine.config.models import (
    ChainConfig,
    InclusionStrategy,
    PendingSourceKind,
    VenueConfig,
    VenueStatus,
)
from liqengine.core.errors import ConfigError

CONFIG_ROOT = "config"


# --------------------------------------------------------------------------- interpolation


def test_interpolate_prefers_environment_over_fallback() -> None:
    assert interpolate("${A:-fallback}", {"A": "real"}) == "real"


def test_interpolate_uses_fallback_when_unset() -> None:
    assert interpolate("${A:-fallback}", {}) == "fallback"


def test_interpolate_treats_empty_environment_value_as_unset() -> None:
    # An exported-but-blank variable is a half-configured .env, not an intentional empty URL.
    assert interpolate("${A:-fallback}", {"A": ""}) == "fallback"


def test_interpolate_raises_on_missing_variable_without_fallback() -> None:
    with pytest.raises(ConfigError, match="MISSING_VAR"):
        interpolate("${MISSING_VAR}", {})


def test_interpolate_reports_every_missing_variable_at_once() -> None:
    with pytest.raises(ConfigError) as exc:
        interpolate("${A}/${B}", {})
    assert "A" in str(exc.value) and "B" in str(exc.value)


# --------------------------------------------------------------------------- shipped config


def test_shipped_config_loads_with_no_environment() -> None:
    """The tree must run unconfigured, on public endpoints, with no .env present."""
    registry = load_registry(CONFIG_ROOT, environ={})
    assert set(registry.venues) == {
        "aave_v3_arbitrum",
        "aave_v3_base",
        "aave_v3_ethereum",
        "morpho_base",
    }


def test_every_venue_resolves_to_a_matching_chain_and_protocol() -> None:
    registry = load_registry(CONFIG_ROOT, environ={})
    for resolved in registry.resolved():
        assert resolved.protocol.chain == resolved.chain.key


def test_arbitrum_is_a_latency_race_with_no_public_mempool() -> None:
    """Decision D6: bidding does not exist on an FCFS sequencer. Guard against a silent edit."""
    registry = load_registry(CONFIG_ROOT, environ={})
    arbitrum = registry.chains["arbitrum"]
    assert arbitrum.inclusion_strategy is InclusionStrategy.LATENCY_RACE
    assert arbitrum.pending_source is PendingSourceKind.SEQUENCER_FEED
    assert arbitrum.gas.reverts_cost_gas


def test_l2_venues_declare_a_sequencer_uptime_feed() -> None:
    """Without it we simulate liquidations that revert during the post-downtime grace period."""
    registry = load_registry(CONFIG_ROOT, environ={})
    for key in ("arbitrum", "base"):
        assert registry.chains[key].sequencer_uptime_feed is not None, key


def test_no_venue_ships_able_to_sign() -> None:
    registry = load_registry(CONFIG_ROOT, environ={})
    assert not any(v.may_sign for v in registry.venues.values())


def test_unknown_venue_lists_the_known_ones() -> None:
    registry = load_registry(CONFIG_ROOT, environ={})
    with pytest.raises(ConfigError, match="aave_v3_arbitrum"):
        registry.venue("nope")


def test_min_status_filter_excludes_lower_stages() -> None:
    registry = load_registry(CONFIG_ROOT, environ={})
    assert registry.resolved(min_status=VenueStatus.SHADOW) == []
    assert len(registry.resolved(min_status=VenueStatus.PHASE0)) == 4


# --------------------------------------------------------------------------- model invariants


def _endpoint(name: str, url: str, role: str = "quorum") -> dict[str, object]:
    return {"name": name, "http_url": url, "role": role}


def _chain(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "key": "testchain",
        "chain_id": 999,
        "name": "Test",
        "block_time_ms": 1000,
        "inclusion_strategy": "priority_auction",
        "pending_source": "public_mempool",
        "endpoints": [_endpoint("a", "https://a.example"), _endpoint("b", "https://b.example")],
    }
    return base | overrides


def test_chain_rejects_duplicate_voting_urls() -> None:
    """A quorum across one node agrees by construction and cannot detect disagreement."""
    with pytest.raises(ValueError, match="duplicate URLs"):
        ChainConfig.model_validate(
            _chain(
                endpoints=[
                    _endpoint("a", "https://same.example"),
                    _endpoint("b", "https://same.example"),
                ]
            )
        )


def test_chain_allows_a_backup_to_share_a_url_with_a_voter() -> None:
    # Backups never vote and never bind a signing decision, so sharing a URL is harmless.
    ChainConfig.model_validate(
        _chain(
            endpoints=[
                _endpoint("a", "https://same.example"),
                _endpoint("b", "https://other.example"),
                _endpoint("bulk", "https://same.example", role="backup"),
            ]
        )
    )


def test_chain_rejects_two_authoritative_endpoints() -> None:
    with pytest.raises(ValueError, match="at most one authoritative"):
        ChainConfig.model_validate(
            _chain(
                endpoints=[
                    _endpoint("a", "https://a.example", role="authoritative"),
                    _endpoint("b", "https://b.example", role="authoritative"),
                ]
            )
        )


def test_chain_rejects_quorum_larger_than_the_voting_pool() -> None:
    with pytest.raises(ValueError, match="quorum needs"):
        ChainConfig.model_validate(
            _chain(
                quorum={"min_agreeing": 3},
                endpoints=[_endpoint("a", "https://a.example")],
            )
        )


def test_chain_rejects_subscribe_without_a_websocket_url() -> None:
    with pytest.raises(ValueError, match="subscribe support"):
        ChainConfig.model_validate(
            _chain(
                endpoints=[
                    {"name": "a", "http_url": "https://a.example", "supports_subscribe": True},
                    _endpoint("b", "https://b.example"),
                ]
            )
        )


def test_chain_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        ChainConfig.model_validate(_chain(inclusion_stratgy="latency_race"))


def test_signing_status_requires_capital_caps() -> None:
    with pytest.raises(ValueError, match="max_position_base"):
        VenueConfig.model_validate(
            {"key": "v", "chain": "c", "protocol": "p", "status": "canary"}
        )


def test_signing_status_accepts_configured_caps() -> None:
    venue = VenueConfig.model_validate(
        {
            "key": "v",
            "chain": "c",
            "protocol": "p",
            "status": "canary",
            "max_position_base": 500_00000000,
            "max_daily_loss_base": 50_00000000,
        }
    )
    assert venue.may_sign
