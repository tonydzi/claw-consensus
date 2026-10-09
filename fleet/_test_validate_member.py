#!/usr/bin/env python3
"""_test_validate_member.py - red-first tests for validate_member.py.

Every test takes the known-good card (fleet/members/tonydzi.json), breaks ONE thing, and asserts
the validator names that defect. A validator that never goes red is a fake validator.
Run: python fleet/_test_validate_member.py  -> prints N/N ok, exit 0; exit 1 on any miss.
"""
import copy
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import validate_member as vm  # noqa: E402

with open(os.path.join(HERE, "members", "tonydzi.json"), encoding="utf-8") as _fh:
    GOOD = json.load(_fh)


def ext(card):
    return card["capabilities"]["extensions"][0]["params"]


def expect_fail(card, login, needle):
    try:
        vm.validate_card(card, login)
    except ValueError as exc:
        assert needle in str(exc), "wrong reason: %r (wanted %r)" % (str(exc), needle)
        return
    raise AssertionError("validator stayed green, wanted %r" % needle)


def t_good():
    assert vm.validate_card(copy.deepcopy(GOOD), "tonydzi") == "tonydzi"


def t_not_object():
    expect_fail(["x"], "tonydzi", "JSON object")


def t_missing_name():
    c = copy.deepcopy(GOOD)
    del c["name"]
    expect_fail(c, "tonydzi", "card.name missing")


def t_empty_description():
    c = copy.deepcopy(GOOD)
    c["description"] = "  "
    expect_fail(c, "tonydzi", "description is empty")


def t_static_card_needs_doc_url():
    c = copy.deepcopy(GOOD)
    del c["documentationUrl"]
    expect_fail(c, "tonydzi", "documentationUrl")


def t_interface_must_be_https():
    c = copy.deepcopy(GOOD)
    c["supportedInterfaces"] = [{"url": "http://1.2.3.4:8080", "protocolBinding": "JSONRPC"}]
    expect_fail(c, "tonydzi", "must be https")


def t_hosted_card_ok_without_doc():
    c = copy.deepcopy(GOOD)
    del c["documentationUrl"]
    c["supportedInterfaces"] = [{"url": "https://agent.example.com", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}]
    assert vm.validate_card(c, "tonydzi") == "tonydzi"


def t_no_skills():
    c = copy.deepcopy(GOOD)
    c["skills"] = []
    expect_fail(c, "tonydzi", "at least one skill")


def t_dup_skill_id():
    c = copy.deepcopy(GOOD)
    c["skills"].append(dict(c["skills"][0]))
    expect_fail(c, "tonydzi", "duplicated")


def t_missing_extension():
    c = copy.deepcopy(GOOD)
    c["capabilities"]["extensions"] = []
    expect_fail(c, "tonydzi", "exactly one extension")


def t_two_extensions():
    c = copy.deepcopy(GOOD)
    c["capabilities"]["extensions"].append(copy.deepcopy(c["capabilities"]["extensions"][0]))
    expect_fail(c, "tonydzi", "found 2")


def t_owner_not_login():
    c = copy.deepcopy(GOOD)
    ext(c)["owner"]["github"] = "not a login!"
    expect_fail(c, "tonydzi", "not a GitHub login")


def t_filename_mismatch():
    expect_fail(copy.deepcopy(GOOD), "someone-else", "must equal owner.github")


def t_owner_case_insensitive():
    c = copy.deepcopy(GOOD)
    ext(c)["owner"]["github"] = "TonyDzi"
    assert vm.validate_card(c, "tonydzi") == "tonydzi"


def t_escalation_when_empty():
    c = copy.deepcopy(GOOD)
    ext(c)["escalation"]["when"] = []
    expect_fail(c, "tonydzi", "escalation.when")


def t_never_shares_empty():
    c = copy.deepcopy(GOOD)
    ext(c)["permissions"]["never_shares"] = []
    expect_fail(c, "tonydzi", "never_shares")


def t_consent_without_login():
    c = copy.deepcopy(GOOD)
    ext(c)["consent"] = "sure, list it"
    expect_fail(c, "tonydzi", "contain the owner login")


def t_join_may_not_self_declare_high_tier():
    for tier in ("OBSERVER", "VERIFIED", "TRUSTED", "CORE"):
        c = copy.deepcopy(GOOD)
        ext(c)["tier"] = tier
        try:
            vm.validate_card(c, "tonydzi", max_tier="UNVERIFIED")
        except ValueError as exc:
            assert "may not be self-declared" in str(exc), str(exc)
        else:
            raise AssertionError("green on self-declared " + tier)
    c = copy.deepcopy(GOOD)
    ext(c)["tier"] = "UNVERIFIED"
    assert vm.validate_card(c, "tonydzi", max_tier="UNVERIFIED") == "tonydzi"


def t_main_max_tier_flag():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "tonydzi.json")  # GOOD declares CORE
        with open(p, "w") as fh:
            json.dump(GOOD, fh)
        assert vm.main(["x", "--max-tier", "UNVERIFIED", p]) == 1
        assert vm.main(["x", "--max-tier", "CORE", p]) == 0
        assert vm.main(["x", "--max-tier", "ADMIN", p]) == 1


def t_skill_id_must_be_slug():
    for bad in ("Fleet Review", "a|b", "x" * 70, "../etc"):
        c = copy.deepcopy(GOOD)
        c["skills"][0]["id"] = bad
        expect_fail(c, "tonydzi", "must be a slug")


def t_registry_escapes_links_and_images():
    sys.path.insert(0, HERE)
    import build_members as bm
    out = bm.esc("![x](https://evil.example/a.png) <img src=x> | #h")
    assert "https://" not in out and "<img" not in out and "![" not in out and "#h" not in out, out
    assert out.count("|") == out.count("\|"), out


def t_bad_tier():
    c = copy.deepcopy(GOOD)
    ext(c)["tier"] = "ADMIN"
    expect_fail(c, "tonydzi", "tier must be one of")


def t_reputation_not_self_declared():
    c = copy.deepcopy(GOOD)
    ext(c)["ratings"] = 5
    expect_fail(c, "tonydzi", "reputation data")


def t_secret_in_card():
    c = copy.deepcopy(GOOD)
    c["description"] += " token " + "ghp_" + "a" * 30  # built at runtime so repo leak-scans stay quiet
    expect_fail(c, "tonydzi", "secret-looking")


def t_private_key_in_card():
    c = copy.deepcopy(GOOD)
    ext(c)["consent"] += " -----BEGIN " + "OPENSSH PRIVATE " + "KEY-----"  # same reason
    expect_fail(c, "tonydzi", "secret-looking")


def t_file_not_json():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "bob.json")
        with open(p, "w") as fh:
            fh.write("{not json")
        try:
            vm.validate_file(p)
        except ValueError as exc:
            assert "not valid JSON" in str(exc)
        else:
            raise AssertionError("green on broken JSON")


def t_file_uppercase_name():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "Bob.json")
        with open(p, "w") as fh:
            json.dump(GOOD, fh)
        try:
            vm.validate_file(p)
        except ValueError as exc:
            assert "lowercase" in str(exc)
        else:
            raise AssertionError("green on uppercase filename")


def t_main_exit_codes():
    with tempfile.TemporaryDirectory() as d:
        good = os.path.join(d, "tonydzi.json")
        with open(good, "w") as fh:
            json.dump(GOOD, fh)
        bad = os.path.join(d, "eve.json")  # owner tonydzi != eve
        with open(bad, "w") as fh:
            json.dump(GOOD, fh)
        assert vm.main(["x", good]) == 0
        assert vm.main(["x", good, bad]) == 1
        assert vm.main(["x"]) in (0, 1)  # --all over the real members dir runs without crashing


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    bad = 0
    for t in tests:
        try:
            t()
            print("ok   " + t.__name__)
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print("FAIL %s: %s" % (t.__name__, exc))
    print("%d/%d ok" % (len(tests) - bad, len(tests)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
