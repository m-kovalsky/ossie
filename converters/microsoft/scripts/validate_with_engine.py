# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Validate a .bim against a live Analysis Services engine.

    export OSSIE_MICROSOFT_FABRIC_WORKSPACE=<workspace guid>
    python scripts/validate_with_engine.py tests/fixtures/sales_model.bim

Publishes the model to the workspace with generated sample data, refreshes it
so the engine compiles the DAX, evaluates every measure, then deletes it.
Requires a real workspace on a real capacity; see the README.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ossie_microsoft.engine import (  # noqa: E402
    EngineUnavailableError,
    validate_with_engine,
)

FABRIC_SCOPE = "https://api.fabric.microsoft.com"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api"


def _token(env_var, resource):
    """Use an explicit token when given, otherwise fall back to the Azure CLI."""

    token = os.environ.get(env_var)
    if token:
        return token
    if not shutil.which("az"):
        raise EngineUnavailableError(
            f"set {env_var}, or install the Azure CLI and run 'az login'"
        )
    result = subprocess.run(  # noqa: S603
        ["az", "account", "get-access-token", "--resource", resource, "--output", "json"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise EngineUnavailableError(
            f"could not get a token for {resource}: {result.stderr.strip()}"
        )
    return json.loads(result.stdout)["accessToken"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path, help="the .bim file to validate")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Fabric workspace id (default: $OSSIE_MICROSOFT_FABRIC_WORKSPACE)",
    )
    parser.add_argument("--name", default=None, help="semantic model display name")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the published model in the workspace for inspection",
    )
    args = parser.parse_args(argv)

    workspace = args.workspace or os.environ.get("OSSIE_MICROSOFT_FABRIC_WORKSPACE")

    try:
        if not workspace:
            raise EngineUnavailableError(
                "no workspace configured; set OSSIE_MICROSOFT_FABRIC_WORKSPACE "
                "to a Fabric workspace id"
            )
        bim = json.loads(args.model.read_text(encoding="utf-8"))
        result = validate_with_engine(
            bim,
            workspace=workspace,
            fabric_token=_token("OSSIE_MICROSOFT_FABRIC_TOKEN", FABRIC_SCOPE),
            powerbi_token=_token("OSSIE_MICROSOFT_POWERBI_TOKEN", POWERBI_SCOPE),
            name=args.name,
            keep=args.keep,
        )
    except EngineUnavailableError as exc:
        print(f"engine validation is not configured: {exc}", file=sys.stderr)
        return 2

    report = {
        "model": str(args.model),
        "stage": result.stage,
        "error": result.error,
        "findings": [
            {
                "kind": finding.kind,
                "object": finding.object,
                "error": finding.error,
                "value": finding.value,
            }
            for finding in result.findings
        ],
    }
    print(json.dumps(report, indent=2, default=str))
    return 0 if result.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
