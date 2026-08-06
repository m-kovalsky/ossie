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
"""

import copy
import json
import os
from pathlib import Path

import pytest

from ossie_microsoft import convert_ossie_to_semantic_model, validate_tmsl_on_engine
from ossie_microsoft.engine import (
    FABRIC_API,
    FABRIC_RESOURCE,
    _access_token,
    _request,
    evaluate_dax,
)

WORKSPACE = os.environ.get("OSSIE_MICROSOFT_FABRIC_WORKSPACE")
if not WORKSPACE:
    pytest.skip(
        "set OSSIE_MICROSOFT_FABRIC_WORKSPACE to run live engine validation",
        allow_module_level=True,
    )

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "sales_model.bim"


def make_engine_ready(database: dict) -> dict:
    """Correct the defects that stop ``sales_model.bim`` loading on a real engine.

    The fixture is shaped for converter coverage, not for deployment: it
    deliberately authors a backwards one-to-many relationship so the exporter's
    orientation-normalising logic has something to normalise. Offline TOM
    accepts all of it. A real engine does not, so the fixture is corrected here
    rather than in-place -- editing the file would delete the coverage it exists
    to provide.

    Each edit below is a defect the engine reported and offline TOM missed.
    """
    database = copy.deepcopy(database)
    model = database["model"]
    tables = model["tables"]

    # The "from" end of a relationship must be the many side unless it is
    # one-to-one. The fixture points Calendar[Date](one) -> Sales[OrderDate](many).
    relationships = {r["name"]: r for r in model.get("relationships", [])}
    for relationship in relationships.values():
        if relationship.get("fromCardinality") == "one" and (
            relationship.get("toCardinality") != "one"
        ):
            relationship.update(
                {
                    "fromTable": relationship["toTable"],
                    "fromColumn": relationship["toColumn"],
                    "toTable": relationship["fromTable"],
                    "toColumn": relationship["fromColumn"],
                    "fromCardinality": "many",
                    "toCardinality": "one",
                }
            )

    # A variation must sit on the column its relationship starts from -- the fact
    # column -- and that relationship must point at the table hosting the default
    # hierarchy, whose table must be marked showAsVariationsOnly.
    variations = None
    for table in tables:
        for column in table.get("columns", []):
            if column.get("variations"):
                variations = column.pop("variations")
    if variations:
        variations[0]["isDefault"] = True
        target = variations[0]["defaultHierarchy"]["table"]
        relationship = relationships.get(variations[0]["relationship"])
        if relationship is not None:
            relationship.update(
                {
                    "fromTable": "Sales",
                    "fromColumn": "OrderDate",
                    "toTable": target,
                    "toColumn": "Date",
                    "fromCardinality": "many",
                    "toCardinality": "one",
                }
            )
        for table in tables:
            if table["name"] == "Sales":
                for column in table["columns"]:
                    if column["name"] == "OrderDate":
                        column["variations"] = variations
            if table["name"] == target:
                table["showAsVariationsOnly"] = True

    # A relationship whose endpoints do not resolve is a load failure, not a warning.
    columns = {t["name"]: {c["name"] for c in t.get("columns", [])} for t in tables}
    model["relationships"] = [
        r
        for r in relationships.values()
        if r["fromColumn"] in columns.get(r["fromTable"], ())
        and r["toColumn"] in columns.get(r["toTable"], ())
    ]

    # A hierarchy level without an explicit ordinal defaults to -1 and is rejected.
    for table in tables:
        for hierarchy in table.get("hierarchies", []):
            for ordinal, level in enumerate(hierarchy.get("levels", [])):
                level.setdefault("ordinal", ordinal)

    return database


@pytest.fixture(scope="module")
def raw_sales_model():
    return json.loads(FIXTURE.read_text(encoding="utf-8-sig"))


@pytest.fixture(scope="module")
def sales_model(raw_sales_model):
    return make_engine_ready(raw_sales_model)


def test_the_sales_fixture_loads_and_its_measures_evaluate(sales_model):
    result = validate_tmsl_on_engine(sales_model, name="ossie-ci-sales")
    result.raise_for_errors()
    assert {value.measure for value in result.values} == {"Total Sales", "Order Count"}


def test_the_committed_fixture_is_not_engine_loadable(raw_sales_model):
    """Documents a known, deliberate divergence -- see :func:`make_engine_ready`.

    If this ever starts failing the fixture has been made deployable, and
    ``make_engine_ready`` should shrink to match.
    """
    result = validate_tmsl_on_engine(raw_sales_model, name="ossie-ci-raw")
    assert not result.is_valid
    assert "cardinality" in " ".join(e.message for e in result.errors).lower()


def test_a_round_tripped_model_still_loads_and_evaluates(sales_model):
    from ossie_microsoft import convert_semantic_model_to_ossie

    exported = convert_ossie_to_semantic_model(convert_semantic_model_to_ossie(sales_model))
    validate_tmsl_on_engine(exported, name="ossie-ci-roundtrip").raise_for_errors()


def test_the_tpcds_example_loads():
    example = HERE.parents[2] / "examples" / "tpcds_semantic_model.yaml"
    exported = convert_ossie_to_semantic_model(example.read_text(encoding="utf-8"))
    validate_tmsl_on_engine(exported, name="ossie-ci-tpcds").raise_for_errors()


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
def test_the_engine_rejects_dax_that_offline_tom_accepts(sales_model, label, expression):
    """Every one of these deserializes and validates clean under offline TOM.

    That is the whole reason this layer exists: TOM never parses DAX.
    """
    broken = copy.deepcopy(sales_model)
    sales = next(t for t in broken["model"]["tables"] if t["name"] == "Sales")
    sales.setdefault("measures", []).append({"name": "Broken", "expression": expression})

    result = validate_tmsl_on_engine(broken, name="ossie-ci-broken")
    assert not result.is_valid, f"the engine accepted {label}: {expression}"


def test_count_rejects_a_boolean_column_but_counta_does_not(sales_model):
    """Evidence for the COUNT -> COUNTA mapping in the core spec.

    Ossie's COUNT(x) counts non-null values of any type. DAX COUNT refuses a
    boolean column outright, so COUNTA is the faithful translation.
    """
    model = copy.deepcopy(sales_model)
    sales = next(t for t in model["model"]["tables"] if t["name"] == "Sales")
    sales["columns"].append({"name": "Flag", "dataType": "boolean", "sourceColumn": "flag"})

    result = validate_tmsl_on_engine(model, name="ossie-ci-count", keep=True)
    dataset = result.diagnostics.get("dataset")
    try:
        result.raise_for_errors()
        _rows, count_error = evaluate_dax(
            WORKSPACE, dataset, "EVALUATE ROW(\"v\", COUNT('Sales'[Flag]))"
        )
        rows, counta_error = evaluate_dax(
            WORKSPACE, dataset, "EVALUATE ROW(\"v\", COUNTA('Sales'[Flag]))"
        )
        assert count_error and "Boolean" in count_error
        assert counta_error is None
        assert rows[0]["[v]"] > 0
    finally:
        if dataset:
            _request(
                "DELETE",
                f"{FABRIC_API}/workspaces/{WORKSPACE}/items/{dataset}",
                _access_token(FABRIC_RESOURCE),
            )
