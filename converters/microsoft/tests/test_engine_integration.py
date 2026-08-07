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

"""Validation against a live Analysis Services engine.

These tests need a real Fabric workspace on a real capacity, so they skip
unless one is configured. See the README for what to set.

They deploy real semantic models and consume capacity, so they are not part of
the default test run and are deliberately not wired into CI.
"""

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ossie_microsoft import convert_ossie_to_semantic_model, validate_with_engine
from ossie_microsoft.engine import FABRIC_API, _request, deploy, evaluate, refresh

WORKSPACE = os.environ.get("OSSIE_MICROSOFT_FABRIC_WORKSPACE")
if not WORKSPACE:
    pytest.skip(
        "set OSSIE_MICROSOFT_FABRIC_WORKSPACE to run live engine validation",
        allow_module_level=True,
    )

FABRIC_SCOPE = "https://api.fabric.microsoft.com"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api"

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "sales_model.bim"


def _token(env_var, resource):
    token = os.environ.get(env_var)
    if token:
        return token
    if not shutil.which("az"):
        pytest.skip(f"set {env_var}, or install the Azure CLI and run 'az login'")
    result = subprocess.run(  # noqa: S603
        ["az", "account", "get-access-token", "--resource", resource, "--output", "json"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"could not get a token for {resource}")
    return json.loads(result.stdout)["accessToken"]


@pytest.fixture(scope="module")
def fabric_token():
    return _token("OSSIE_MICROSOFT_FABRIC_TOKEN", FABRIC_SCOPE)


@pytest.fixture(scope="module")
def powerbi_token():
    return _token("OSSIE_MICROSOFT_POWERBI_TOKEN", POWERBI_SCOPE)


@pytest.fixture
def validate(fabric_token, powerbi_token):
    def run(bim, name, keep=False):
        return validate_with_engine(
            bim,
            workspace=WORKSPACE,
            fabric_token=fabric_token,
            powerbi_token=powerbi_token,
            name=name,
            keep=keep,
        )

    return run


@pytest.fixture(scope="module")
def sales_model():
    return json.loads(FIXTURE.read_text(encoding="utf-8-sig"))


def test_the_sales_fixture_loads_and_its_measures_evaluate(sales_model, validate):
    result = validate(sales_model, "ossie-ci-sales")
    result.raise_for_errors()
    measures = {f.object for f in result.findings if f.kind == "measure"}
    assert measures == {"Sales[Total Sales]", "Sales[Order Count]"}


def test_a_round_tripped_model_still_loads_and_evaluates(sales_model, validate):
    from ossie_microsoft import convert_semantic_model_to_ossie

    exported = convert_ossie_to_semantic_model(convert_semantic_model_to_ossie(sales_model))
    validate(exported, "ossie-ci-roundtrip").raise_for_errors()


def test_the_tpcds_example_loads(validate):
    example = HERE.parents[2] / "examples" / "tpcds_semantic_model.yaml"
    exported = convert_ossie_to_semantic_model(example.read_text(encoding="utf-8"))
    validate(exported, "ossie-ci-tpcds").raise_for_errors()


@pytest.mark.parametrize(
    ("label", "expression"),
    [
        ("unknown column", "SUM('Sales'[NoSuchColumn])"),
        ("unknown function", "TOTALLYFAKE('Sales'[Amount])"),
        ("unbalanced parentheses", "SUM('Sales'[Amount]"),
        ("unqualified column reference", "SUM(Amount)"),
        ("wrong arity", "DATE('Sales'[OrderDate])"),
        ("unknown table", "SUM('NoSuchTable'[Amount])"),
    ],
)
def test_the_engine_rejects_dax_that_offline_tom_accepts(
    sales_model, validate, label, expression
):
    """Every one of these deserializes and validates clean under offline TOM.

    That is the whole reason this layer exists: TOM never parses DAX.
    """
    broken = copy.deepcopy(sales_model)
    sales = next(t for t in broken["model"]["tables"] if t["name"] == "Sales")
    sales.setdefault("measures", []).append({"name": "Broken", "expression": expression})

    result = validate(broken, "ossie-ci-broken")
    assert not result.is_valid, f"the engine accepted {label}: {expression}"


def test_count_rejects_a_boolean_column_but_counta_does_not(
    sales_model, fabric_token, powerbi_token
):
    """Evidence for the COUNT -> COUNTA mapping in the core spec.

    Ossie's COUNT(x) counts non-null values of any type. DAX COUNT refuses a
    boolean column outright, so COUNTA is the faithful translation.
    """
    model = copy.deepcopy(sales_model)
    sales = next(t for t in model["model"]["tables"] if t["name"] == "Sales")
    sales["columns"].append({"name": "Flag", "dataType": "boolean", "sourceColumn": "flag"})

    from ossie_microsoft.engine import build_deployable

    dataset, error = deploy(
        build_deployable(model, "ossie-ci-count"), WORKSPACE, "ossie-ci-count", fabric_token
    )
    assert error is None, error
    try:
        assert refresh(WORKSPACE, dataset, powerbi_token) is None
        _rows, count_error = evaluate(
            WORKSPACE, dataset, powerbi_token, "EVALUATE ROW(\"v\", COUNT('Sales'[Flag]))"
        )
        rows, counta_error = evaluate(
            WORKSPACE, dataset, powerbi_token, "EVALUATE ROW(\"v\", COUNTA('Sales'[Flag]))"
        )
        assert count_error and "Boolean" in count_error
        assert counta_error is None
        assert rows[0]["[v]"] > 0
    finally:
        _request("DELETE", f"{FABRIC_API}/workspaces/{WORKSPACE}/items/{dataset}", fabric_token)
