#!/usr/bin/env python3
"""validate_member.py - fail-closed check of one fleet member file (an A2A v1.0 Agent Card).

What it does: reads `fleet/members/<github-login>.json`, checks that it is a valid A2A v1.0
Agent Card (name, description, version, supportedInterfaces OR a documentationUrl for
static cards, skills, capabilities) AND that it carries the Palo Alto AI Lab fleet
extension `https://palo-alto.ai/fleet/ext/v0` with the five declarations JOIN.md asks for:
owner, escalation, permissions, consent, tier. Also refuses anything that looks like a secret.

Input:  path(s) to member JSON files (or `--all` = every file under fleet/members/).
Output: one line per file `OK <login>` or `FAIL <login>: <reason>`; exit 0 only if all OK.
`--max-tier UNVERIFIED` (used by CI on join PRs) refuses any tier above it: a joiner may not
declare OBSERVER or higher; maintainers set those on merge. The secret check is a heuristic
over known token shapes, not a guarantee: a human still reads every card before merge.
Who calls it: the contributor before opening a PR (JOIN.md step 3), CI on every PR
(.github/workflows/fleet-validate.yml), build_members.py before regenerating MEMBERS.md.
What breaks it: a card that is not JSON, a filename that does not match the owner login,
a missing extension field, a secret-looking string. All of those are the point.
How to repair: read the FAIL reason, fix the file, re-run. stdlib only, no network.
Rail: 0 tokens, deterministic.
"""
import json
import os
import re
import sys

EXT_URI = "https://palo-alto.ai/fleet/ext/v0"
TIERS = ("UNVERIFIED", "OBSERVER", "VERIFIED", "TRUSTED", "CORE")
# A joining agent may only ask for UNVERIFIED (CI passes --max-tier UNVERIFIED on join PRs);
# higher tiers are set by maintainers on merge.
JOIN_MAX_TIER = "UNVERIFIED"
SKILL_ID_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
LOGIN_RX = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,38}$")
SECRET_RX = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})"
)
URL_RX = re.compile(r"^https://[^\s]+$")
MEMBERS_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "members")


def _fail(reason):
    raise ValueError(reason)


def _req(obj, key, typ, where):
    if key not in obj:
        _fail(f"{where}.{key} missing")
    if not isinstance(obj[key], typ):
        _fail(f"{where}.{key} must be {typ.__name__}")
    if typ is str and not obj[key].strip():
        _fail(f"{where}.{key} is empty")
    return obj[key]


def validate_card(card, expected_login, max_tier=None):
    """Raise ValueError on the first defect; return the owner login when valid.
    max_tier: highest tier the card may declare (None = any of TIERS)."""
    if not isinstance(card, dict):
        _fail("top level must be a JSON object (the Agent Card)")
    _req(card, "name", str, "card")
    _req(card, "description", str, "card")
    _req(card, "version", str, "card")
    ifaces = card.get("supportedInterfaces")
    if ifaces is not None:
        if not isinstance(ifaces, list):
            _fail("card.supportedInterfaces must be a list")
        for i, it in enumerate(ifaces):
            url = _req(it, "url", str, f"card.supportedInterfaces[{i}]")
            if not URL_RX.match(url):
                _fail(f"card.supportedInterfaces[{i}].url must be https://")
            _req(it, "protocolBinding", str, f"card.supportedInterfaces[{i}]")
    doc = card.get("documentationUrl")
    if not ifaces and not doc:
        _fail("static card: documentationUrl (your repo URL) is required when supportedInterfaces is empty")
    if doc is not None and not URL_RX.match(doc):
        _fail("card.documentationUrl must be https://")
    skills = _req(card, "skills", list, "card")
    if not skills:
        _fail("card.skills must list at least one skill (what can your agent DO for a member?)")
    seen = set()
    for i, sk in enumerate(skills):
        sid = _req(sk, "id", str, f"card.skills[{i}]")
        if not SKILL_ID_RX.match(sid):
            _fail(f"card.skills[{i}].id must be a slug [a-z0-9-], got {sid!r}")
        _req(sk, "name", str, f"card.skills[{i}]")
        _req(sk, "description", str, f"card.skills[{i}]")
        if sid in seen:
            _fail(f"card.skills[{i}].id duplicated: {sid}")
        seen.add(sid)
    caps = _req(card, "capabilities", dict, "card")
    exts = caps.get("extensions")
    if not isinstance(exts, list):
        _fail("card.capabilities.extensions must be a list")
    fleet = [e for e in exts if isinstance(e, dict) and e.get("uri") == EXT_URI]
    if len(fleet) != 1:
        _fail(f"exactly one extension with uri {EXT_URI} required, found {len(fleet)}")
    params = _req(fleet[0], "params", dict, "fleet-ext")
    owner = _req(params, "owner", dict, "fleet-ext.params")
    login = _req(owner, "github", str, "fleet-ext.params.owner").lower()
    if not LOGIN_RX.match(login):
        _fail(f"owner.github '{login}' is not a GitHub login")
    if expected_login is not None and login != expected_login:
        _fail(f"file name '{expected_login}.json' must equal owner.github '{login}'")
    esc = _req(params, "escalation", dict, "fleet-ext.params")
    _req(esc, "contact", str, "fleet-ext.params.escalation")
    _req(esc, "when", list, "fleet-ext.params.escalation")
    if not esc["when"]:
        _fail("escalation.when must name at least one condition that wakes the human")
    perms = _req(params, "permissions", dict, "fleet-ext.params")
    _req(perms, "may_share", list, "fleet-ext.params.permissions")
    never = _req(perms, "never_shares", list, "fleet-ext.params.permissions")
    if not never:
        _fail("permissions.never_shares must not be empty (at minimum: secrets, private data of third parties)")
    consent = _req(params, "consent", str, "fleet-ext.params")
    if login not in consent.lower():
        _fail("consent must be written by the owner and contain the owner login")
    tier = _req(params, "tier", str, "fleet-ext.params")
    if tier not in TIERS:
        _fail(f"tier must be one of {TIERS}")
    if max_tier is not None and TIERS.index(tier) > TIERS.index(max_tier):
        _fail(f"tier {tier} may not be self-declared; a joining card declares {max_tier}, the lab raises it on merge")
    for key in ("tasks_completed", "latency", "ratings"):
        if key in params:
            _fail(f"{key} is reputation data; it is computed by the lab, not declared")
    blob = json.dumps(card)
    m = SECRET_RX.search(blob)
    if m:
        _fail(f"secret-looking string present ({m.group(0)[:8]}...); cards never carry credentials")
    return login


def validate_file(path, max_tier=None):
    base = os.path.basename(path)
    if not base.endswith(".json"):
        _fail("member file must be <github-login>.json")
    expected = base[:-5]
    if expected != expected.lower():
        _fail("file name must be lowercase")
    with open(path, encoding="utf-8") as fh:
        try:
            card = json.load(fh)
        except json.JSONDecodeError as exc:
            _fail(f"not valid JSON: {exc}")
    return validate_card(card, expected, max_tier)


def main(argv):
    args = list(argv[1:])
    max_tier = None
    if "--max-tier" in args:
        i = args.index("--max-tier")
        max_tier = args[i + 1] if i + 1 < len(args) else ""
        if max_tier not in TIERS:
            print(f"FAIL --max-tier must be one of {TIERS}")
            return 1
        del args[i:i + 2]
    paths = args
    if paths == ["--all"] or not paths:
        paths = sorted(
            os.path.join(MEMBERS_DIR, f) for f in os.listdir(MEMBERS_DIR) if f.endswith(".json")
        )
    bad = 0
    for p in paths:
        try:
            login = validate_file(p, max_tier)
            print(f"OK {login}")
        except (ValueError, OSError) as exc:
            bad += 1
            print(f"FAIL {os.path.basename(p)}: {exc}")
    print(f"checked {len(paths)} file(s), failed {bad}")
    return 1 if bad or not paths else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
