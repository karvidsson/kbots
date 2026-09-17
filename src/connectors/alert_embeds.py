"""One bounded, editable incident card. No protocol identifiers in visible text."""

import base64
import re
import uuid

import discord

from src.core.alert_diagnosis import human_diagnosis, incident_label, public_prose, public_text

RED = 0xFF4444
GREY = 0x888888
AMBER = 0xE0A020


def status_nonce(receipt):
    # Discord permits 25 characters. This reversible 22-character value remains
    # protocol metadata, never a rendered footer or message body.
    return base64.urlsafe_b64encode(uuid.UUID(receipt["id"]).bytes).decode().rstrip("=")


def diagnosis_sections(text):
    text = human_diagnosis(text)
    headings = re.compile(
        r"(?im)^(?:#{1,4}[ \t]*)?(?:\*\*)?"
        r"(verdict|observations|observed facts|cause|suspected cause|fix|proposed fix|missing evidence)"
        r"[ \t]*:?(?:\*\*)?[ \t]*:?[ \t]*(?:\n|(?=\S)|$)"
    )
    matches = list(headings.finditer(text))
    aliases = {
        "observations": "verdict",
        "observed facts": "verdict",
        "suspected cause": "cause",
        "proposed fix": "fix",
    }
    parts = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end].strip()
        key = aliases.get(match[1].lower(), match[1].lower())
        if value and key not in parts:
            parts[key] = value
    # Keep unstructured legacy output as evidence without claiming it is a cause.
    if not matches and text:
        parts["verdict"] = text.splitlines()[0]
    return parts


def incident_embed(source, receipt, step, status=""):
    config = source["config"]
    label = receipt.get("issue_title") or (
        incident_label(source, {"name": receipt["issue_name"]})
        if receipt.get("issue_name")
        else public_text(config.get("app", "Application"), 80)
    )
    link = None
    if config.get("host") and config.get("project"):
        link = f"{config['host']}/project/{config['project']}/error_tracking/{receipt['issue_id']}"
    compact = receipt.get("setup_test") or receipt.get("drill") or receipt.get("sample_drill_status") == "drill"
    held = step == "result" and not receipt["success"]
    embed = discord.Embed(title=public_text(label, 80), url=link, colour=AMBER if held else GREY if compact else RED)
    if step != "result":
        embed.description = public_prose(status, 350)
    elif held:
        embed.description = "Diagnosis is held. More evidence or access is needed."
        embed.add_field(name="Cause", value="No cause confirmed.", inline=False)
        embed.add_field(name="Fix", value="No changes proposed.", inline=False)
        embed.add_field(name="Missing evidence", value=public_prose(receipt["result"], 700), inline=False)
    elif compact:
        verdict = (
            "Setup check received. Alert path works."
            if receipt.get("setup_test")
            else "Drill received. Alert path works; no fix needed."
            if receipt.get("drill")
            else "Drill sample received. Alert path works; trigger identity is unconfirmed."
        )
        embed.description = (
            verdict
            + "\n"
            + public_prose(receipt.get("source_summary") or "Source: no in-app source frame was available.", 300)
        )
    else:
        parts = diagnosis_sections(receipt["result"])
        embed.description = public_prose(
            " ".join(parts.get("verdict", "Diagnosis complete. Review the proposed fix.").split()), 280
        )
        for name, key, fallback in (
            ("Cause", "cause", "No separate cause was established."),
            ("Fix", "fix", "No separate fix was proposed."),
            ("Missing evidence", "missing evidence", "The diagnosis did not specify missing evidence."),
        ):
            value = parts.get(key, fallback)
            if key == "missing evidence" and receipt.get("evidence", {}).get("status") == "timed_out":
                value = "Stack trace was not yet available after a 90-second wait. " + value
            embed.add_field(name=name, value=public_prose(value, 700), inline=False)
    service = "PostHog" if config.get("service") == "posthog" else public_text(config.get("service", "Alerts"), 30)
    kind = receipt.get("kind", "issue").removeprefix("$error_tracking_issue_")
    kind = kind if kind in {"created", "reopened", "spiking"} else "issue"
    embed.set_footer(text=f"{service} · {kind} · issue {receipt['issue_id'][:8]}")
    return embed


def incident_message(embed, channel):
    member = getattr(getattr(channel, "guild", None), "me", None)
    allowed = getattr(channel, "guild", None) is None or (
        member is not None and channel.permissions_for(member).embed_links is True
    )
    if allowed:
        return {"content": "", "embed": embed}
    # Preserve delivery where an installation cannot embed. No permission mutation.
    lines = [embed.title, embed.description]
    lines.extend(f"{field.name}: {field.value}" for field in embed.fields)
    lines.append(embed.footer.text)
    return {"content": public_prose("\n\n".join(lines), 1900), "embed": None}
