"""A broken login must be classified as an auth error, whatever the CLI calls it.

On 2026-09-05 the CLI started failing with "Failed to authenticate: OAuth
session expired and could not be refreshed". The classifier only knew
"authentication", "401" and "not logged in", so every turn was retried as a
generic error, no token refresh was attempted, no auth_error stop_reason was
returned, and the ops alert never fired. These tests pin the wording that was
missed and the wording that must keep working, and make sure transient and
usage failures do not get misfiled as auth.
"""

import pytest

from src.llm.claude_code import _is_auth_error


@pytest.mark.parametrize("text", [
    # the exact stderr from the 2026-09-05 outage
    "Failed to authenticate: OAuth session expired and could not be refreshed",
    # what the classifier already caught, and must keep catching
    "Error: 401 Unauthorized",
    "Authentication failed. Please run /login",
    "Not logged in. Please run /login",
    # wording seen in other CLI versions
    "OAuth token expired",
    "Please log in to continue",
    "Unauthenticated request",
])
def test_login_failures_are_auth_errors(text):
    assert _is_auth_error(text.lower())


@pytest.mark.parametrize("text", [
    # transient / capacity / signal failures are retried or downgraded, never
    # treated as a dead login
    "API Error: 529 Overloaded. This is a server-side issue, usually temporary",
    "Exit code 143",
    "You've reached your usage limit. Resets at 3pm",
    "No conversation found with session ID abc",
    "",
])
def test_other_failures_are_not_auth_errors(text):
    assert not _is_auth_error(text.lower())
