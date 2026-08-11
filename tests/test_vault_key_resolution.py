"""Vault key lookup — exact match wins, normalised match rescues the rest.

The bug this covers: a token saved as `cloudflare-api-token` while every
Cloudflare tool looks up `secrets/cloudflare-api-token`. The value was in the
vault the whole time; the tools reported "not configured" and two agents lost
~20 minutes each to it. The vault is a flat dict — `secrets/` is part of the
key string, not a namespace — so there is no hierarchy to get wrong, only a
string to mistype, and the codebase uses three conventions at once.
"""

import importlib

import pytest

from src.vault.fernet import FernetVault, _normalise_key

vault_manage = importlib.import_module("vault-manage")


def _vault(**secrets) -> FernetVault:
    """An unlocked, in-memory vault. No file, no passphrase, no encryption."""
    v = FernetVault(vault_path="/nonexistent/secrets.enc")
    v._secrets = dict(secrets)
    v._unlocked = True
    return v


# --- normalisation ---------------------------------------------------------

@pytest.mark.parametrize("key", [
    "secrets/cloudflare-api-token",
    "cloudflare-api-token",
    "CLOUDFLARE_API_TOKEN",
    "Cloudflare-Api-Token",
    "secrets/CLOUDFLARE_API_TOKEN",
    "  secrets/cloudflare-api-token  ",
])
def test_spellings_of_one_name_normalise_alike(key):
    assert _normalise_key(key) == "cloudflare-api-token"


def test_normalisation_does_not_merge_distinct_names():
    """Only spelling is ignored — different secrets stay different."""
    distinct = ["discord-token", "active-discord-token", "discord-token-atlas",
                "github-token", "secrets/gemini-api-key", "secrets/groq-api-key"]
    assert len({_normalise_key(k) for k in distinct}) == len(distinct)


def test_secrets_prefix_stripped_only_at_the_front():
    # A key that merely contains the word must not be mangled.
    assert _normalise_key("my-secrets/thing") == "my-secrets/thing"


# --- lookup ----------------------------------------------------------------

def test_the_reported_bug_now_resolves():
    """Saved bare, looked up prefixed — the exact failure that was reported."""
    v = _vault(**{"cloudflare-api-token": "cf-tok"})
    assert v.get("secrets/cloudflare-api-token") == "cf-tok"


def test_resolves_across_every_convention():
    stored_prefixed = _vault(**{"secrets/notion-api-key": "n1"})
    assert stored_prefixed.get("NOTION_API_KEY") == "n1"

    stored_upper = _vault(**{"TAVILY_API_KEY": "t1"})
    assert stored_upper.get("secrets/tavily-api-key") == "t1"

    stored_bare = _vault(**{"github-token": "g1"})
    assert stored_bare.get("secrets/github-token") == "g1"


def test_exact_match_always_wins():
    """Normalisation must never change what an already-working lookup returns."""
    v = _vault(**{"secrets/github-token": "prefixed", "github-token": "bare"})
    assert v.get("secrets/github-token") == "prefixed"
    assert v.get("github-token") == "bare"


def test_ambiguous_match_refuses_to_guess(caplog):
    """Two stored spellings, neither exact: handing back either could be the
    wrong credential, so return None and name them."""
    v = _vault(**{"github-token": "bare", "GITHUB_TOKEN": "upper"})
    with caplog.at_level("WARNING"):
        assert v.get("secrets/github-token") is None
    assert "ambiguous" in caplog.text
    assert "github-token" in caplog.text and "GITHUB_TOKEN" in caplog.text


def test_genuinely_missing_key_is_still_none():
    v = _vault(**{"discord-token": "d"})
    assert v.get("secrets/cloudflare-api-token") is None
    assert v.get("") is None


def test_empty_stored_value_is_not_overridden_by_a_normalised_match():
    """An exact hit on an empty string is still an exact hit, not a miss."""
    v = _vault(**{"secrets/x-key": "", "X_KEY": "fallback"})
    assert v.get("secrets/x-key") == ""


def test_lookup_does_not_mutate_the_vault():
    v = _vault(**{"cloudflare-api-token": "cf"})
    before = dict(v._secrets)
    v.get("secrets/cloudflare-api-token")
    assert v._secrets == before          # resolves, never rewrites


# --- vault-manage.py save-time guard ---------------------------------------

def test_canonical_for_catches_the_mistake_at_save_time():
    assert vault_manage.canonical_for("cloudflare-api-token") == "secrets/cloudflare-api-token"
    assert vault_manage.canonical_for("CLOUDFLARE_API_TOKEN") == "secrets/cloudflare-api-token"
    assert vault_manage.canonical_for("GITHUB_TOKEN") == "github-token"


def test_canonical_for_stays_quiet_on_correct_and_custom_keys():
    """Must not second-guess a right answer, nor any dynamically-built key."""
    for good in vault_manage.ALL_KEYS:
        assert vault_manage.canonical_for(good) is None
    for custom in ("discord-token-atlas", "acme-api-token",
                   "secrets/my-own-thing", "active-discord-token"):
        assert vault_manage.canonical_for(custom) is None


def test_every_canonical_key_is_uniquely_spelt():
    """ALL_KEYS must not contain two names that normalise alike — that would
    make canonical_for ambiguous and the suggestion unreliable."""
    normalised = [_normalise_key(k) for k in vault_manage.ALL_KEYS]
    assert len(set(normalised)) == len(normalised)


def test_canonical_keys_are_the_ones_core_actually_reads():
    """A key missing here is a key an operator has to guess."""
    for expected in ("secrets/cloudflare-api-token", "secrets/notion-api-key",
                     "secrets/gemini-api-key", "secrets/tavily-api-key",
                     "secrets/serpapi-key", "secrets/slack-bot-token",
                     "secrets/telegram-bot-token", "discord-token", "github-token"):
        assert expected in vault_manage.ALL_KEYS
