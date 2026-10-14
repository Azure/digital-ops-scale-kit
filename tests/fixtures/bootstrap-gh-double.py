"""Closed verification replies for the bootstrap boundary tests."""

import json
import os
import re
import sys
from pathlib import Path

arguments = sys.argv[1:]
if os.environ.get("GH_TELEMETRY") != "false":
    sys.exit(99)
if arguments == ["version"]:
    print("gh version 2.95.0 (fixture)")
    sys.exit(0)
if arguments[:2] != ["attestation", "verify"]:
    sys.exit(99)
log = Path(os.environ["TEST_GH_LOG"])
previous = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
responses = json.loads(Path(os.environ["TEST_RESPONSES"]).read_text(encoding="utf-8"))
with log.open("a", encoding="utf-8") as output:
    output.write(json.dumps(arguments) + "\n")
if len(previous) >= len(responses):
    sys.exit(99)
response = responses[len(previous)]
if response.get("exit", 0):
    sys.exit(response["exit"])
if "raw" in response:
    print(response["raw"])
    sys.exit(0)
observations = response["observations"]
if "--template" in arguments:
    fields = (
        "subjectAlternativeName", "issuer", "sourceRepositoryURI", "sourceRepositoryDigest",
        "sourceRepositoryRef", "buildSignerDigest", "buildConfigURI", "buildConfigDigest", "runnerEnvironment",
    )
    for observation in observations:
        verification = observation.get("verificationResult", {})
        certificate = verification.get("signature", {}).get("certificate", {})
        # Certificate identities are ASCII. JSON string quoting matches Go's %q for these inputs.
        print("\t".join(json.dumps(value) for value in (
            verification.get("mediaType"), *(certificate.get(key) for key in fields),
        )))
elif "--jq" in arguments:
    query = arguments[arguments.index("--jq") + 1]
    expected = dict(re.findall(r'\.([A-Za-z]+) == "([^"]*)"', query))
    accepted = bool(observations)
    for observation in observations:
        verification = observation.get("verificationResult", {})
        values = {**verification, **verification.get("signature", {}).get("certificate", {})}
        accepted = accepted and all(values.get(key) == value for key, value in expected.items())
    print("true" if accepted else "false")
else:
    sys.exit(99)
