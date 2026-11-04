# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tax vault API (API.md ``/tax-years``, ``/tax-category-map``).

* Tax years: create (checklist learned from the year before), state
  open -> filed -> closed, a closed year frozen, reopening by admins only.
* Checklist: manual items, received with a document the caller can see,
  ``document_id`` hidden from callers who cannot see the document, learning
  again adds nothing twice.
* Roll-up: deductible tagging (category ``tax_treatment`` and split
  ``tax_flag``) summed by category and schedule hint, converted into the
  base currency; a member who cannot see every account gets totals over
  their visible accounts only (``partial``).
* Forms: amounts by box only (identity boxes of the parsed form are gone).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import Document
from app.domain.tax.models import TaxYear
from app.domain.transactions.models import CategoryKind, TaxTreatment
from tests.fixtures.documents import make_document
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, household_audit, problem_type
from tests.fixtures.ledger import (
    LedgerFactories,
    make_fx_rate,
    make_split,
)
from tests.fixtures.tax import (
    make_form_data,
    make_tax_category_map,
    make_tax_year,
    synthetic_w2_boxes,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def local_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={"documents_dir": tmp_path / "documents", "quarantine_dir": tmp_path / "quarantine"}
    )


@pytest.fixture
def actors(local_settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(local_settings, factories) as opened:
        yield opened


def _hh(client: AuthedClient) -> uuid.UUID:
    household_id = client.record.household_id
    assert household_id is not None
    return household_id


def _document(
    factories: Factories,
    settings: Settings,
    household_id: uuid.UUID,
    *,
    account_id: uuid.UUID | None = None,
    uploaded_by: uuid.UUID | None = None,
) -> Document:
    store = LocalFSBlobStore.from_settings(settings)
    with factories.transaction() as db:
        return make_document(
            db,
            store,
            factories.keyring,
            household_id,
            data=b"%PDF-1.4 synthetic",
            mime="application/pdf",
            filename="w2.pdf",
            account_id=account_id,
            uploaded_by=uploaded_by,
        )


def test_tax_years_and_states(actors: Actors) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    member = actors.new(role="member", household=household)
    admin = actors.new(role="admin", household=household)

    created = owner.post(f"{API}/tax-years", json={"year": 2025, "filing_status": "single"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["year"], body["state"], body["jurisdiction"]) == (2025, "open", "US")
    assert body["checklist"] == {"expected": 0, "received": 0, "not_applicable": 0}
    again = owner.post(f"{API}/tax-years", json={"year": 2025})
    assert again.status_code == 409
    assert problem_type(again) == "tax-year-exists"
    bad = member.patch(f"{API}/tax-years/2025", json={"filing_status": "unmarried"})
    assert bad.status_code == 422
    assert problem_type(bad) == "tax-filing-status-unknown"
    unknown = owner.post(f"{API}/tax-years", json={"year": 2026, "jurisdiction": "ZZ"})
    assert unknown.status_code == 422
    assert problem_type(unknown) == "tax-jurisdiction-unknown"

    assert member.patch(f"{API}/tax-years/2025", json={"state": "filed"}).json()["state"] == "filed"
    closed = member.patch(f"{API}/tax-years/2025", json={"state": "closed"})
    assert closed.json()["state"] == "closed"
    frozen = member.patch(f"{API}/tax-years/2025", json={"notes": "late 1099"})
    assert frozen.status_code == 409
    assert problem_type(frozen) == "tax-year-closed"
    reopen = member.patch(f"{API}/tax-years/2025", json={"state": "open"})
    assert reopen.status_code == 403
    reopen = admin.patch(f"{API}/tax-years/2025", json={"state": "open"})
    assert reopen.status_code == 200
    assert reopen.json()["state"] == "open"
    invalid = member.patch(f"{API}/tax-years/2025", json={"state": "open"})
    assert invalid.status_code == 200  # no change is not a transition
    listed = member.get(f"{API}/tax-years").json()["items"]
    assert [y["year"] for y in listed] == [2025]
    assert member.get(f"{API}/tax-years/1999").status_code == 404

    assert admin.delete(f"{API}/tax-years/2025").status_code == 204
    assert owner.get(f"{API}/tax-years/2025").status_code == 404
    actions = [a.action for a in household_audit(actors.factories, _hh(owner), "tax_year.update")]
    assert len(actions) == 4


def test_checklist_is_learned_from_the_year_before(
    actors: Actors, local_settings: Settings, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner)
    assert owner.post(f"{API}/tax-years", json={"year": 2024}).status_code == 201
    items = {}
    for form, issuer in (
        ("W-2", "Fictional Widgets Inc"),
        ("1099-INT", "Sample National Bank"),
        ("1098", "Placeholder Home Lending"),
    ):
        response = owner.post(
            f"{API}/tax-years/2024/checklist", json={"expected_form": form, "issuer": issuer}
        )
        assert response.status_code == 201, response.text
        items[form] = response.json()["id"]
    dup = owner.post(
        f"{API}/tax-years/2024/checklist",
        json={"expected_form": "W-2", "issuer": "fictional widgets, inc"},
    )
    assert dup.status_code == 409
    assert problem_type(dup) == "tax-checklist-item-exists"
    unknown = owner.post(f"{API}/tax-years/2024/checklist", json={"expected_form": "W-99"})
    assert unknown.status_code == 422
    assert problem_type(unknown) == "tax-form-unknown"
    document = _document(actors.factories, local_settings, household_id)
    received = owner.patch(
        f"{API}/tax-years/2024/checklist/{items['W-2']}", json={"document_id": str(document.id)}
    )
    assert received.status_code == 200, received.text
    assert received.json()["status"] == "received"
    assert received.json()["document_id"] == str(document.id)
    na = owner.patch(
        f"{API}/tax-years/2024/checklist/{items['1098']}", json={"status": "not_applicable"}
    )
    assert na.json()["status"] == "not_applicable"
    # A form parsed for 2024 from a payer nobody listed.
    ledger.run(
        make_form_data,
        document,
        form_type="1099-DIV",
        tax_year=2024,
        boxes={"1a": "12.00"},
        issuer_name="Placeholder Brokerage",
    )

    created = owner.post(f"{API}/tax-years", json={"year": 2025})
    assert created.json()["checklist"]["expected"] == 2
    checklist = owner.get(f"{API}/tax-years/2025/checklist").json()
    learned = {(i["expected_form"], i["issuer"], i["origin"]) for i in checklist["items"]}
    assert learned == {
        ("W-2", "Fictional Widgets Inc", "prior_year"),
        ("1099-DIV", "Placeholder Brokerage", "prior_year"),
    }
    assert all(i["status"] == "expected" for i in checklist["items"])
    assert all(i["form_title"] for i in checklist["items"])
    learn = owner.post(f"{API}/tax-years/2025/checklist/learn")
    assert learn.status_code == 200
    assert learn.json() == {"added": 0}
    item = checklist["items"][0]["id"]
    assert owner.delete(f"{API}/tax-years/2025/checklist/{item}").status_code == 204
    assert owner.post(f"{API}/tax-years/2025/checklist/learn").json() == {"added": 1}


def test_checklist_document_follows_document_visibility(
    actors: Actors, local_settings: Settings, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner)
    member = actors.new(role="member", household=owner.record.household)
    hidden = ledger.account(household_id, name="Owner only")
    shown = ledger.account(household_id, name="Shared")
    ledger.grant(household_id, member.user.id, shown, GrantLevel.EDIT)
    secret = _document(actors.factories, local_settings, household_id, account_id=hidden.id)
    ledger.run(make_tax_year, household_id, year=2025)
    item = owner.post(f"{API}/tax-years/2025/checklist", json={"expected_form": "W-2"}).json()
    refused = member.patch(
        f"{API}/tax-years/2025/checklist/{item['id']}", json={"document_id": str(secret.id)}
    )
    assert refused.status_code == 404
    assert problem_type(refused) == "document-not-found"
    linked = owner.patch(
        f"{API}/tax-years/2025/checklist/{item['id']}", json={"document_id": str(secret.id)}
    )
    assert linked.json()["document_id"] == str(secret.id)
    seen = member.get(f"{API}/tax-years/2025/checklist").json()["items"][0]
    assert seen["status"] == "received"
    assert seen["document_id"] is None
    contradiction = owner.patch(
        f"{API}/tax-years/2025/checklist/{item['id']}",
        json={"status": "expected", "document_id": str(secret.id)},
    )
    assert contradiction.status_code == 422
    back = owner.patch(f"{API}/tax-years/2025/checklist/{item['id']}", json={"status": "expected"})
    assert back.json()["document_id"] is None
    assert back.json()["status"] == "expected"


def _money(value: dict[str, Any]) -> Decimal:
    assert value["currency"] == "USD"
    return Decimal(value["amount"])


def test_summary_rolls_up_deductibles_by_category_and_hint(
    actors: Actors, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner)
    member = actors.new(role="member", household=owner.record.household)
    shared = ledger.account(household_id, name="Shared")
    private = ledger.account(household_id, name="Private")
    euro = ledger.account(household_id, name="Euro", currency="EUR")
    ledger.grant(household_id, member.user.id, shared, GrantLevel.VIEW)
    charity = ledger.category(household_id, name="Charity", tax_treatment=TaxTreatment.DEDUCTIBLE)
    medical = ledger.category(household_id, name="Medical", tax_treatment=TaxTreatment.DEDUCTIBLE)
    salary = ledger.category(
        household_id,
        name="Salary",
        kind=CategoryKind.INCOME,
        tax_treatment=TaxTreatment.TAXABLE_INCOME,
    )
    office = ledger.category(household_id, name="Office supplies")
    ledger.run(make_tax_category_map, charity, schedule="Schedule A", line="12")
    ledger.run(make_tax_category_map, medical, schedule="Schedule A", line="1")
    ledger.run(make_tax_category_map, salary, schedule="Form 1040", line="1a")
    day = date(2025, 3, 10)
    ledger.transaction(shared, amount="-100.00", category=charity, posted_date=day)
    ledger.transaction(private, amount="-40.00", category=charity, posted_date=day)
    ledger.transaction(shared, amount="-250.00", category=medical, posted_date=day)
    ledger.transaction(shared, amount="5000.00", category=salary, posted_date=day)
    # Outside the year and an untreated category without a flag: not counted.
    ledger.transaction(shared, amount="-999.00", category=charity, posted_date=date(2024, 12, 31))
    ledger.transaction(shared, amount="-30.00", category=office, posted_date=day)
    # A split transaction: one split flagged deductible in an untreated category.
    split_txn = ledger.transaction(shared, amount="-80.00", posted_date=day)
    ledger.run(make_split, split_txn, amount="-60.00", category=office, tax_flag=True)
    ledger.run(make_split, split_txn, amount="-20.00", category=office)
    # Euro charity at 1.1 USD per EUR on that day.
    ledger.run(make_fx_rate, household_id, base="EUR", quote="USD", rate_date=day, rate="1.1")
    ledger.transaction(euro, amount="-10.00", category=charity, posted_date=day)
    ledger.run(make_tax_year, household_id, year=2025)

    summary = owner.get(f"{API}/tax-years/2025/summary").json()
    assert summary["partial"] is False
    assert summary["unconverted"] == []
    cats = {c["category_name"]: c for c in summary["categories"]}
    assert _money(cats["Charity"]["total"]) == Decimal("-151.00")
    assert (cats["Charity"]["schedule"], cats["Charity"]["line"]) == ("Schedule A", "12")
    assert _money(cats["Medical"]["total"]) == Decimal("-250.00")
    assert _money(cats["Salary"]["total"]) == Decimal("5000.00")
    assert _money(cats["Office supplies"]["total"]) == Decimal("-60.00")
    assert _money(cats["Office supplies"]["flagged"]) == Decimal("-60.00")
    assert cats["Office supplies"]["tax_treatment"] is None
    by_treatment = {k: _money(v) for k, v in summary["by_treatment"].items()}
    assert by_treatment == {
        "deductible": Decimal("-401.00"),
        "taxable_income": Decimal("5000.00"),
        "flagged": Decimal("-60.00"),
    }
    hints = {(h["schedule"], h["line"]): _money(h["total"]) for h in summary["by_hint"]}
    assert hints == {
        ("Schedule A", "12"): Decimal("-151.00"),
        ("Schedule A", "1"): Decimal("-250.00"),
        ("Form 1040", "1a"): Decimal("5000.00"),
        (None, None): Decimal("-60.00"),
    }

    seen = member.get(f"{API}/tax-years/2025/summary").json()
    assert seen["partial"] is True
    member_cats = {c["category_name"]: _money(c["total"]) for c in seen["categories"]}
    assert member_cats == {
        "Charity": Decimal("-100.00"),
        "Medical": Decimal("-250.00"),
        "Salary": Decimal("5000.00"),
        "Office supplies": Decimal("-60.00"),
    }


def test_category_map(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner)
    viewer = actors.new(role="viewer", household=owner.record.household)
    charity = ledger.category(household_id, name="Charity", tax_treatment=TaxTreatment.DEDUCTIBLE)
    put = owner.put(
        f"{API}/tax-category-map/{charity.id}", json={"schedule": "Schedule A", "line": "12"}
    )
    assert put.status_code == 200, put.text
    assert put.json()["tax_treatment"] == "deductible"
    moved = owner.put(f"{API}/tax-category-map/{charity.id}", json={"schedule": "Schedule A"})
    assert moved.json()["line"] is None
    listed = viewer.get(f"{API}/tax-category-map").json()["items"]
    assert [(m["category_name"], m["schedule"]) for m in listed] == [("Charity", "Schedule A")]
    assert viewer.get(f"{API}/tax-category-map?jurisdiction=CA").json()["items"] == []
    assert owner.delete(f"{API}/tax-category-map/{charity.id}").status_code == 204
    gone = owner.delete(f"{API}/tax-category-map/{charity.id}")
    assert gone.status_code == 404
    assert problem_type(gone) == "tax-category-map-not-found"
    foreign = owner.put(f"{API}/tax-category-map/{uuid.uuid4()}", json={"schedule": "Schedule A"})
    assert foreign.status_code == 404
    assert problem_type(foreign) == "category-not-found"


def test_forms_show_amounts_only(
    actors: Actors, local_settings: Settings, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner)
    member = actors.new(role="member", household=owner.record.household)
    hidden = ledger.account(household_id)
    tax_year: TaxYear = ledger.run(make_tax_year, household_id, year=2025)
    shown_doc = _document(
        actors.factories, local_settings, household_id, uploaded_by=member.user.id
    )
    hidden_doc = _document(actors.factories, local_settings, household_id, account_id=hidden.id)
    boxes = synthetic_w2_boxes(seed=3)
    ledger.run(make_form_data, shown_doc, boxes=boxes, tax_year_row=tax_year)
    ledger.run(make_form_data, hidden_doc, boxes=synthetic_w2_boxes(seed=4), tax_year_row=tax_year)
    response = owner.get(f"{API}/tax-years/2025/forms")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
    mine = member.get(f"{API}/tax-years/2025/forms").json()["items"]
    assert [f["document_id"] for f in mine] == [str(shown_doc.id)]
    got = {b["box"]: b["amount"] for b in mine[0]["boxes"]}
    assert got["1"] == boxes["1"]
    assert got["12_D"] == "5000.00"
    assert "a" not in got
    assert "e" not in got
    assert "14" not in got
    text = response.text
    for key in ("a", "b", "c", "d", "e", "f", "15"):
        assert str(boxes[key]) not in text
    summary = owner.get(f"{API}/tax-years/2025/summary").json()
    (w2,) = summary["forms"]
    assert w2["form_type"] == "W-2"
    assert w2["count"] == 2
