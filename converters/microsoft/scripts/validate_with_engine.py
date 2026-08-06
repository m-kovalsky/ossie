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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ossie_microsoft.engine import (  # noqa: E402
    EngineUnavailableError,
    validate_bim_on_engine,
)


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

    try:
        result = validate_bim_on_engine(
            args.model, workspace=args.workspace, name=args.name, keep=args.keep
        )
    except EngineUnavailableError as exc:
        print(f"engine validation is not configured: {exc}", file=sys.stderr)
        return 2

    report = {
        "model": str(args.model),
        "deployed": result.deployed,
        "refreshed": result.refreshed,
        "errors": [
            {"stage": issue.stage, "source": issue.source, "message": issue.message}
            for issue in result.errors
        ],
        "measures": [
            {
                "object": f"{value.table}[{value.measure}]",
                "expression": value.expression,
                "value": value.value,
            }
            for value in result.values
        ],
    }
    print(json.dumps(report, indent=2, default=str))
    return 0 if result.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
