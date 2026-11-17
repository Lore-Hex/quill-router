"""The CloudTrail metric filters in infra/aws_security_alarms.tf, held to what
they must and must not count.

The patterns are rendered from the Terraform the way the resources compose
them and evaluated against CloudTrail-shaped records. The evaluator follows
the CloudWatch semantics these patterns rely on, each confirmed against the
live service with `aws logs test-metric-filter` on 2026-11-16: `*` is a
wildcard on either side of a string, a comparison (= or !=) on a field that
is absent never matches, and NOT EXISTS matches only an absent field."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ALARMS = ROOT / "infra" / "aws_security_alarms.tf"
IMPORTS = ROOT / "infra" / "imports.tf"
BASELINE = ROOT / "scripts" / "deploy" / "aws_security_baseline.sh"

ACCOUNT = "330422590279"
SIGNER_KEY = f"arn:aws:kms:eu-west-1:{ACCOUNT}:key/14d1c5b4-0b95-4c5c-995c-9511ff1038ab"
OTHER_KEY = f"arn:aws:kms:eu-west-1:{ACCOUNT}:key/00000000-0000-4000-8000-000000000000"
RELEASE_SIGNER = f"arn:aws:iam::{ACCOUNT}:user/jperla"
ENCLAVE_ROLE = f"arn:aws:sts::{ACCOUNT}:assumed-role/quill-enclave-role/i-0123456789abcdef0"


# --- rendering the Terraform -------------------------------------------------

def _tf() -> str:
    return ALARMS.read_text(encoding="utf-8")


def _block(kind: str, name: str) -> str:
    tf = _tf()
    start = tf.index(f'resource "{kind}" "{name}" {{')
    end = tf.find("\n}\n", start)
    assert end > start
    return tf[start:end]


def _unescape(value: str) -> str:
    return value.replace('\\"', '"')


def _string_locals() -> dict[str, str]:
    found = dict(re.findall(r'^\s+(\w+)\s+= "(.*)"$', _tf(), re.M))
    found["aws_account_id"] = ACCOUNT
    return found


def _resolve(value: str, scalars: dict[str, str]) -> str:
    for _ in range(5):
        value = re.sub(r"\$\{local\.(\w+)\}", lambda m: scalars[m.group(1)], value)
    assert "${" not in value, value
    return _unescape(value)


def _list_local(name: str, scalars: dict[str, str]) -> list[str]:
    match = re.search(rf"^  {name} = (?:join\(\" \|\| \", )?\[\n(.*?)\n  \]", _tf(), re.M | re.S)
    assert match, name
    items = re.findall(r'^\s+"(.*)",$', match.group(1), re.M)
    assert items, name
    return [_resolve(item, scalars) for item in items]


def _scalars() -> dict[str, str]:
    scalars = _string_locals()
    key_match = " || ".join(_list_local("enclave_bundle_signer_key_match", scalars))
    scalars["enclave_bundle_signer_key_match"] = key_match
    return scalars


def _signer_patterns() -> tuple[str, str]:
    scalars = _scalars()
    sign = _resolve(scalars["enclave_bundle_signer_sign"], scalars)
    release = _list_local("enclave_bundle_release_signature", scalars)
    not_release = _list_local("enclave_bundle_not_release_signature", scalars)

    use = _block("aws_cloudwatch_log_metric_filter", "enclave_bundle_signer_use")
    rel = _block("aws_cloudwatch_log_metric_filter", "enclave_bundle_release_sign")
    assert 'pattern        = "{ ${local.enclave_bundle_signer_sign} && (${join(" || ", local.enclave_bundle_not_release_signature)}) }"' in use
    assert 'pattern        = "{ ${local.enclave_bundle_signer_sign} && ${join(" && ", local.enclave_bundle_release_signature)} }"' in rel
    return (
        "{ " + sign + " && (" + " || ".join(not_release) + ") }",
        "{ " + sign + " && " + " && ".join(release) + " }",
    )


def _unauthorized_pattern() -> str:
    block = _block("aws_cloudwatch_log_metric_filter", "unauthorized_api_calls")
    raw = re.search(r'^  pattern = "(.*)"$', block, re.M)
    assert raw
    return _unescape(raw.group(1).replace("${data.aws_caller_identity.config.account_id}", ACCOUNT))


# --- a CloudWatch JSON filter evaluator for the subset these patterns use ----

_TOKEN = re.compile(r'\s*(\{|\}|\(|\)|&&|\|\||!=|=|NOT EXISTS|IS NULL|"[^"]*"|\$[\w.\[\]]+)')
_ABSENT = object()


def _tokens(pattern: str) -> list[str]:
    out, pos = [], 0
    while pos < len(pattern):
        if pattern[pos:].strip() == "":
            break
        match = _TOKEN.match(pattern, pos)
        assert match, pattern[pos:]
        out.append(match.group(1))
        pos = match.end()
    return out


def _lookup(event: Any, selector: str) -> Any:
    value = event
    for part in re.findall(r"\w+|\[\d+\]", selector[2:]):
        if part.startswith("["):
            index = int(part[1:-1])
            if not isinstance(value, list) or index >= len(value):
                return _ABSENT
            value = value[index]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return _ABSENT
    return value


def _glob(value: Any, literal: str) -> bool:
    if not isinstance(value, str):
        return False
    regex = ".*".join(re.escape(piece) for piece in literal.split("*"))
    return re.fullmatch(regex, value, re.S) is not None


def matches(pattern: str, event: dict[str, Any]) -> bool:
    tokens = _tokens(pattern)
    assert tokens[0] == "{" and tokens[-1] == "}", pattern
    pos = 1

    def take() -> str:
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def comparison() -> bool:
        if tokens[pos] == "(":
            take()
            value = disjunction()
            assert take() == ")"
            return value
        selector, op = take(), take()
        value = _lookup(event, selector)
        if op == "NOT EXISTS":
            return value is _ABSENT
        if op == "IS NULL":
            return value is None
        literal = take()[1:-1]
        if value is _ABSENT or value is None:
            return False
        return _glob(value, literal) if op == "=" else not _glob(value, literal)

    def conjunction() -> bool:
        value = comparison()
        while tokens[pos] == "&&":
            take()
            value = comparison() and value
        return value

    def disjunction() -> bool:
        value = conjunction()
        while tokens[pos] == "||":
            take()
            value = conjunction() or value
        return value

    result = disjunction()
    assert pos == len(tokens) - 1, tokens[pos:]
    return result


# --- CloudTrail-shaped records ------------------------------------------------

def _release_signature() -> dict[str, Any]:
    """A successful bundle signature as tools/aws_sealed_bundle.py makes it,
    shaped like the 2026-11-16 release records."""
    return {
        "eventSource": "kms.amazonaws.com",
        "eventName": "Sign",
        "userIdentity": {"type": "IAMUser", "arn": RELEASE_SIGNER, "accountId": ACCOUNT},
        "requestParameters": {
            "keyId": SIGNER_KEY,
            "messageType": "DIGEST",
            "signingAlgorithm": "ECDSA_SHA_384",
        },
        "resources": [{"accountId": ACCOUNT, "type": "AWS::KMS::Key", "ARN": SIGNER_KEY}],
    }


def _denied_sign(principal: str, key: str = SIGNER_KEY) -> dict[str, Any]:
    """A denied Sign. KMS records no requestParameters or resources for it;
    the key is named only inside errorMessage."""
    return {
        "eventSource": "kms.amazonaws.com",
        "eventName": "Sign",
        "userIdentity": {"type": "AssumedRole", "arn": principal, "accountId": ACCOUNT},
        "errorCode": "AccessDenied",
        "errorMessage": (
            f"User: {principal} is not authorized to perform: kms:Sign on resource: "
            f"{key} with an explicit deny in a resource-based policy"
        ),
        "requestParameters": None,
    }


def _variant(**changes: Any) -> dict[str, Any]:
    event = _release_signature()
    for path, value in changes.items():
        target = event
        *parents, leaf = path.split("__")
        for parent in parents:
            target = target[parent]
        if value is _ABSENT:
            del target[leaf]
        else:
            target[leaf] = value
    return event


def _classify(event: dict[str, Any]) -> str:
    unexpected, release = _signer_patterns()
    hits = (matches(unexpected, event), matches(release, event))
    return {(True, False): "unexpected", (False, True): "release", (False, False): "neither"}[hits]


# --- the bundle signer --------------------------------------------------------

def test_a_routine_release_signature_does_not_email_and_is_still_counted() -> None:
    assert _classify(_release_signature()) == "release"
    by_alias = _variant(requestParameters__keyId="alias/quill-enclave-bundle-signer")
    assert _classify(by_alias) == "release"


def test_the_release_exception_names_one_identity_and_one_method_shape() -> None:
    scalars = _scalars()
    assert _resolve(scalars["enclave_bundle_release_signer"], scalars) == RELEASE_SIGNER
    assert _list_local("enclave_bundle_release_signature", scalars) == [
        "($.errorCode NOT EXISTS)",
        f'($.userIdentity.arn = "{RELEASE_SIGNER}")',
        '($.requestParameters.messageType = "DIGEST")',
        '($.requestParameters.signingAlgorithm = "ECDSA_SHA_384")',
    ]
    sign = _resolve(scalars["enclave_bundle_signer_sign"], scalars)
    assert sign.startswith('($.eventSource = "kms.amazonaws.com") && ($.eventName = "Sign") && (')


def test_every_signer_sign_lands_in_exactly_one_metric() -> None:
    """The unexpected filter's exceptions are the release filter's conditions
    negated, so no Sign on the key falls between the two."""
    scalars = _string_locals()
    release = _list_local("enclave_bundle_release_signature", scalars)
    negated = set(_list_local("enclave_bundle_not_release_signature", scalars))
    for condition in release:
        field = re.match(r"\((\$[\w.]+) ", condition)
        assert field, condition
        if condition.endswith("NOT EXISTS)"):
            assert f'({field.group(1)} = "*")' in negated
        else:
            assert condition.replace(" = ", " != ") in negated
            assert f"({field.group(1)} NOT EXISTS)" in negated


def test_another_principal_or_another_shape_still_emails() -> None:
    other_principal = _variant(userIdentity={"type": "AssumedRole", "arn": ENCLAVE_ROLE})
    lookalike = _variant(userIdentity__arn=f"arn:aws:iam::{ACCOUNT}:user/jperla-ci")
    no_identity = _variant(userIdentity={"type": "AWSService"})
    raw_message = _variant(requestParameters__messageType="RAW")
    default_message = _variant(requestParameters__messageType=_ABSENT)
    other_algorithm = _variant(requestParameters__signingAlgorithm="ECDSA_SHA_256")
    errored = _variant(errorCode="ThrottlingException")
    for event in (other_principal, lookalike, no_identity, raw_message, default_message, other_algorithm, errored):
        assert _classify(event) == "unexpected", event


def test_every_denied_sign_emails_including_the_release_signers() -> None:
    assert _classify(_denied_sign(ENCLAVE_ROLE)) == "unexpected"
    assert _classify(_denied_sign(RELEASE_SIGNER)) == "unexpected"
    # The CLI-created filter matched only requestParameters.keyId, which a
    # denied Sign does not carry, so it missed both of these.
    unexpected, _ = _signer_patterns()
    assert f'($.errorMessage = "*kms:Sign on resource: {SIGNER_KEY}*")' in unexpected


def test_other_keys_and_other_calls_on_the_signer_count_nowhere() -> None:
    other_key = _variant(requestParameters__keyId=OTHER_KEY, resources=[{"ARN": OTHER_KEY}])
    public_key_read = _variant(eventName="GetPublicKey")
    for event in (other_key, public_key_read, _denied_sign(ENCLAVE_ROLE, OTHER_KEY)):
        assert _classify(event) == "neither", event


def test_signer_patterns_fit_cloudwatch_and_alarm_as_intended() -> None:
    for pattern in _signer_patterns():
        assert len(pattern) <= 1024
    use = _block("aws_cloudwatch_metric_alarm", "enclave_bundle_signer_use")
    for line in (
        'alarm_name          = "TR-EnclaveBundleSignerUse"',
        'metric_name         = "EnclaveBundleSignerUse"',
        "period              = 60",
        "threshold           = 1",
        'comparison_operator = "GreaterThanOrEqualToThreshold"',
        "alarm_actions       = [aws_sns_topic.tr_security_alarms.arn]",
    ):
        assert line in use, line
    assert _setting(use, "period") == "60" and _setting(use, "threshold") == "1"
    assert "Do not mute" not in use
    burst = _block("aws_cloudwatch_metric_alarm", "enclave_bundle_release_sign_burst")
    for line in (
        'metric_name         = "EnclaveBundleReleaseSign"',
        "period              = 3600",
        "threshold           = 4",
        'comparison_operator = "GreaterThanThreshold"',
        "alarm_actions       = [aws_sns_topic.tr_security_alarms.arn]",
    ):
        assert line in burst, line
    assert _setting(burst, "period") == "3600" and _setting(burst, "threshold") == "4"
    imports = IMPORTS.read_text(encoding="utf-8")
    assert 'id = "/aws/cloudtrail/quill:quill-enclave-bundle-signer-use"' in imports
    assert 'id = "TR-EnclaveBundleSignerUse"' in imports


def _setting(block: str, name: str) -> str:
    """One top-level argument's exact value, so threshold = 40 is not threshold = 4."""
    found = re.findall(rf"(?m)^\s*{name}\s*=\s*(\S+)\s*$", block)
    assert len(found) == 1, (name, found)
    return found[0]


# --- unauthorized API calls ---------------------------------------------------

def _denial(arn: str | None, event_name: str = "DescribeKey") -> dict[str, Any]:
    event: dict[str, Any] = {"eventName": event_name, "errorCode": "AccessDenied"}
    if arn is not None:
        event["userIdentity"] = {"type": "AssumedRole", "arn": arn}
    return event


def test_service_linked_role_noise_is_dropped_and_everything_else_counts() -> None:
    pattern = _unauthorized_pattern()
    dropped = (
        _denial(f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSServiceRoleForConfig/ConfigResourceCompositionSession"),
        _denial(f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSServiceRoleForAccessAnalyzer/access-analyzer"),
    )
    counted = (
        _denial(f"arn:aws:sts::{ACCOUNT}:assumed-role/tr-router-github-deploy/GitHubActions", "GetRole"),
        _denial(ENCLAVE_ROLE, "Sign"),
        _denial("arn:aws:sts::111111111111:assumed-role/AWSServiceRoleForConfig/x"),
        _denial(None, "AssumeRoleWithWebIdentity"),
        {**_denial(None), "errorCode": "Client.UnauthorizedOperation"},
    )
    for event in dropped:
        assert not matches(pattern, event), event
    for event in counted:
        assert matches(pattern, event), event


def test_unauthorized_pattern_matches_the_baseline_drift_check() -> None:
    script = BASELINE.read_text(encoding="utf-8")
    line = re.search(r"^UNAUTH_PATTERN='(.*)'$", script, re.M)
    assert line
    assert line.group(1).replace("'\"$ACCOUNT\"'", ACCOUNT) == _unauthorized_pattern()


def test_the_evaluator_rejects_what_cloudwatch_rejects() -> None:
    """Self-checks of the evaluator on the semantics the patterns lean on."""
    absent = copy.deepcopy(_release_signature())
    del absent["requestParameters"]["messageType"]
    assert not matches('{ ($.requestParameters.messageType != "DIGEST") }', absent)
    assert matches("{ ($.requestParameters.messageType NOT EXISTS) }", absent)
    assert matches('{ ($.errorMessage = "*on resource: arn*") }', _denied_sign(ENCLAVE_ROLE))
    assert not matches('{ ($.errorCode = "*") }', _release_signature())
