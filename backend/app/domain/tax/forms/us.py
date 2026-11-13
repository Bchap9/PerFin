# SPDX-License-Identifier: AGPL-3.0-or-later
"""United States federal information returns (jurisdiction ``"US"``).

Box ids and labels follow the IRS forms (recent revisions). Every box that
identifies a person or an organisation (SSN/TIN/EIN, names, addresses, ZIP
codes, account, control and policy numbers, state employer ids, dates of
birth) is :attr:`BoxKind.IDENTITY` and is discarded by
:func:`~app.domain.tax.forms.base.sanitize_boxes`; the issuer's **name**
alone is kept, separately, as ``tax_form_data.issuer_name``. Dates, codes,
state abbreviations, percentages and check boxes are not amounts and are not
stored either.
"""

from __future__ import annotations

from typing import Final

from app.domain.tax.forms.base import BoxDef, BoxKind, FormDef, StaticFormSet


def amt(box_id: str, label: str) -> BoxDef:
    return BoxDef(box_id, label, BoxKind.AMOUNT)


def txt(box_id: str, label: str) -> BoxDef:
    return BoxDef(box_id, label, BoxKind.TEXT)


def chk(box_id: str, label: str) -> BoxDef:
    return BoxDef(box_id, label, BoxKind.CHECKBOX)


def ident(box_id: str, label: str) -> BoxDef:
    return BoxDef(box_id, label, BoxKind.IDENTITY)


def coded(box_id: str, label: str, codes: str) -> BoxDef:
    return BoxDef(box_id, label, BoxKind.CODED_AMOUNT, frozenset(codes.split()))


FILING_STATUSES: Final = frozenset(
    {
        "single",
        "married_filing_jointly",
        "married_filing_separately",
        "head_of_household",
        "qualifying_surviving_spouse",
    }
)

#: Payer/recipient identity boxes shared by the 1099 family.
_PAYER_RECIPIENT: Final = (
    ident("payer", "Payer's name, street address, city, state, ZIP code and telephone"),
    ident("payer_tin", "Payer's TIN"),
    ident("recipient_tin", "Recipient's TIN"),
    ident("recipient", "Recipient's name"),
    ident("recipient_address", "Recipient's street address, city, state and ZIP code"),
    ident("account", "Account number"),
)

_W2_BOX12_CODES = "A B C D E F G H J K L M N P Q R S T V W Y Z AA BB DD EE FF GG HH II"

W2 = FormDef(
    "W-2",
    "Wage and Tax Statement",
    "employer",
    (
        ident("a", "Employee's social security number"),
        ident("b", "Employer identification number (EIN)"),
        ident("c", "Employer's name, address and ZIP code"),
        ident("d", "Control number"),
        ident("e", "Employee's name"),
        ident("f", "Employee's address and ZIP code"),
        amt("1", "Wages, tips, other compensation"),
        amt("2", "Federal income tax withheld"),
        amt("3", "Social security wages"),
        amt("4", "Social security tax withheld"),
        amt("5", "Medicare wages and tips"),
        amt("6", "Medicare tax withheld"),
        amt("7", "Social security tips"),
        amt("8", "Allocated tips"),
        amt("10", "Dependent care benefits"),
        amt("11", "Nonqualified plans"),
        coded("12", "Codes and amounts (box 12)", _W2_BOX12_CODES),
        chk("13", "Statutory employee / Retirement plan / Third-party sick pay"),
        txt("14", "Other"),
        amt("14t", "Box 14 other: total of the amounts listed"),
        ident("15", "State, employer's state ID number"),
        amt("16", "State wages, tips, etc."),
        amt("17", "State income tax"),
        amt("18", "Local wages, tips, etc."),
        amt("19", "Local income tax"),
        txt("20", "Locality name"),
    ),
    issuer_box="c",
)

F1099_INT = FormDef(
    "1099-INT",
    "Interest Income",
    "payer",
    (
        *_PAYER_RECIPIENT,
        ident("rtn", "Payer's RTN"),
        chk("fatca", "FATCA filing requirement"),
        amt("1", "Interest income"),
        amt("2", "Early withdrawal penalty"),
        amt("3", "Interest on U.S. Savings Bonds and Treasury obligations"),
        amt("4", "Federal income tax withheld"),
        amt("5", "Investment expenses"),
        amt("6", "Foreign tax paid"),
        txt("7", "Foreign country or U.S. possession"),
        amt("8", "Tax-exempt interest"),
        amt("9", "Specified private activity bond interest"),
        amt("10", "Market discount"),
        amt("11", "Bond premium"),
        amt("12", "Bond premium on Treasury obligations"),
        amt("13", "Bond premium on tax-exempt bond"),
        txt("14", "Tax-exempt and tax credit bond CUSIP no."),
        txt("15", "State"),
        ident("16", "State identification no."),
        amt("17", "State tax withheld"),
    ),
)

F1099_DIV = FormDef(
    "1099-DIV",
    "Dividends and Distributions",
    "payer",
    (
        *_PAYER_RECIPIENT,
        amt("1a", "Total ordinary dividends"),
        amt("1b", "Qualified dividends"),
        amt("2a", "Total capital gain distributions"),
        amt("2b", "Unrecaptured Sec. 1250 gain"),
        amt("2c", "Section 1202 gain"),
        amt("2d", "Collectibles (28%) gain"),
        amt("2e", "Section 897 ordinary dividends"),
        amt("2f", "Section 897 capital gain"),
        amt("3", "Nondividend distributions"),
        amt("4", "Federal income tax withheld"),
        amt("5", "Section 199A dividends"),
        amt("6", "Investment expenses"),
        amt("7", "Foreign tax paid"),
        txt("8", "Foreign country or U.S. possession"),
        amt("9", "Cash liquidation distributions"),
        amt("10", "Noncash liquidation distributions"),
        chk("11", "FATCA filing requirement"),
        amt("12", "Exempt-interest dividends"),
        amt("13", "Specified private activity bond interest dividends"),
        txt("14", "State"),
        ident("15", "State identification no."),
        amt("16", "State tax withheld"),
    ),
)

F1099_B = FormDef(
    "1099-B",
    "Proceeds From Broker and Barter Exchange Transactions",
    "payer",
    (
        *_PAYER_RECIPIENT,
        txt("1a", "Description of property"),
        txt("1b", "Date acquired"),
        txt("1c", "Date sold or disposed"),
        amt("1d", "Proceeds"),
        amt("1e", "Cost or other basis"),
        amt("1f", "Accrued market discount"),
        amt("1g", "Wash sale loss disallowed"),
        chk("2", "Short-term / long-term gain or loss; ordinary"),
        chk("3", "Basis reported to IRS / proceeds from collectibles / QOF"),
        amt("4", "Federal income tax withheld"),
        chk("5", "Noncovered security"),
        chk("6", "Reported to IRS: gross proceeds / net proceeds"),
        chk("7", "Loss not allowed based on amount in 1d"),
        amt("8", "Profit or (loss) realized in current year on closed contracts"),
        amt("9", "Unrealized profit or (loss) on open contracts, prior year"),
        amt("10", "Unrealized profit or (loss) on open contracts, current year"),
        amt("11", "Aggregate profit or (loss) on contracts"),
        chk("12", "Basis reported to IRS"),
        amt("13", "Bartering"),
        txt("14", "State name"),
        ident("15", "State identification no."),
        amt("16", "State tax withheld"),
    ),
)

F1099_MISC = FormDef(
    "1099-MISC",
    "Miscellaneous Information",
    "payer",
    (
        *_PAYER_RECIPIENT,
        amt("1", "Rents"),
        amt("2", "Royalties"),
        amt("3", "Other income"),
        amt("4", "Federal income tax withheld"),
        amt("5", "Fishing boat proceeds"),
        amt("6", "Medical and health care payments"),
        chk("7", "Payer made direct sales totaling $5,000 or more"),
        amt("8", "Substitute payments in lieu of dividends or interest"),
        amt("9", "Crop insurance proceeds"),
        amt("10", "Gross proceeds paid to an attorney"),
        amt("11", "Fish purchased for resale"),
        amt("12", "Section 409A deferrals"),
        chk("13", "FATCA filing requirement"),
        amt("14", "Excess golden parachute payments"),
        amt("15", "Nonqualified deferred compensation"),
        amt("16", "State tax withheld"),
        ident("17", "State/Payer's state no."),
        amt("18", "State income"),
    ),
)

F1099_NEC = FormDef(
    "1099-NEC",
    "Nonemployee Compensation",
    "payer",
    (
        *_PAYER_RECIPIENT,
        amt("1", "Nonemployee compensation"),
        chk("2", "Payer made direct sales totaling $5,000 or more"),
        amt("3", "Excess golden parachute payments"),
        amt("4", "Federal income tax withheld"),
        amt("5", "State tax withheld"),
        ident("6", "State/Payer's state no."),
        amt("7", "State income"),
    ),
)

F1099_R = FormDef(
    "1099-R",
    "Distributions From Pensions, Annuities, Retirement or Profit-Sharing Plans, IRAs, "
    "Insurance Contracts, etc.",
    "payer",
    (
        *_PAYER_RECIPIENT,
        amt("1", "Gross distribution"),
        amt("2a", "Taxable amount"),
        chk("2b", "Taxable amount not determined / Total distribution"),
        amt("3", "Capital gain (included in box 2a)"),
        amt("4", "Federal income tax withheld"),
        amt("5", "Employee contributions/Designated Roth contributions or insurance premiums"),
        amt("6", "Net unrealized appreciation in employer's securities"),
        txt("7", "Distribution code(s); IRA/SEP/SIMPLE"),
        amt("8", "Other"),
        txt("9a", "Your percentage of total distribution"),
        amt("9b", "Total employee contributions"),
        amt("10", "Amount allocable to IRR within 5 years"),
        txt("11", "1st year of designated Roth contributions"),
        chk("12", "FATCA filing requirement"),
        txt("13", "Date of payment"),
        amt("14", "State tax withheld"),
        ident("15", "State/Payer's state no."),
        amt("16", "State distribution"),
        amt("17", "Local tax withheld"),
        txt("18", "Name of locality"),
        amt("19", "Local distribution"),
    ),
)

F1099_G = FormDef(
    "1099-G",
    "Certain Government Payments",
    "payer",
    (
        *_PAYER_RECIPIENT,
        amt("1", "Unemployment compensation"),
        amt("2", "State or local income tax refunds, credits, or offsets"),
        txt("3", "Box 2 amount is for tax year"),
        amt("4", "Federal income tax withheld"),
        amt("5", "RTAA payments"),
        amt("6", "Taxable grants"),
        amt("7", "Agriculture payments"),
        chk("8", "Box 2 is trade or business income"),
        amt("9", "Market gain"),
        txt("10a", "State"),
        ident("10b", "State identification no."),
        amt("11", "State income tax withheld"),
    ),
)

F1099_SA = FormDef(
    "1099-SA",
    "Distributions From an HSA, Archer MSA, or Medicare Advantage MSA",
    "payer",
    (
        *_PAYER_RECIPIENT,
        amt("1", "Gross distribution"),
        amt("2", "Earnings on excess contributions"),
        txt("3", "Distribution code"),
        amt("4", "FMV on date of death"),
        chk("5", "HSA / Archer MSA / MA MSA"),
    ),
)

_LENDER_BORROWER: Final = (
    ident("recipient", "Recipient's/Lender's name, address and telephone"),
    ident("recipient_tin", "Recipient's/Lender's TIN"),
    ident("payer_tin", "Payer's/Borrower's TIN"),
    ident("payer", "Payer's/Borrower's name"),
    ident("payer_address", "Payer's/Borrower's street address, city, state and ZIP code"),
    ident("account", "Account number"),
)

F1098 = FormDef(
    "1098",
    "Mortgage Interest Statement",
    "lender",
    (
        *_LENDER_BORROWER,
        amt("1", "Mortgage interest received from payer(s)/borrower(s)"),
        amt("2", "Outstanding mortgage principal"),
        txt("3", "Mortgage origination date"),
        amt("4", "Refund of overpaid interest"),
        amt("5", "Mortgage insurance premiums"),
        amt("6", "Points paid on purchase of principal residence"),
        chk("7", "Address of property securing mortgage is the same as payer's"),
        ident("8", "Address or description of property securing mortgage"),
        txt("9", "Number of properties securing the mortgage"),
        txt("10", "Other"),
        amt("10t", "Box 10 other: amount (e.g. real estate taxes paid)"),
        txt("11", "Mortgage acquisition date"),
    ),
    issuer_box="recipient",
)

F1098_E = FormDef(
    "1098-E",
    "Student Loan Interest Statement",
    "lender",
    (
        *_LENDER_BORROWER,
        amt("1", "Student loan interest received by lender"),
        chk("2", "Box 1 does not include loan origination fees and/or capitalized interest"),
    ),
    issuer_box="recipient",
)

F1098_T = FormDef(
    "1098-T",
    "Tuition Statement",
    "institution",
    (
        ident("filer", "Filer's name, street address, city, state, ZIP code and telephone"),
        ident("filer_ein", "Filer's employer identification no."),
        ident("student_tin", "Student's TIN"),
        ident("student", "Student's name"),
        ident("student_address", "Student's street address, city, state and ZIP code"),
        ident("account", "Service provider/Acct. no."),
        amt("1", "Payments received for qualified tuition and related expenses"),
        amt("4", "Adjustments made for a prior year"),
        amt("5", "Scholarships or grants"),
        amt("6", "Adjustments to scholarships or grants for a prior year"),
        chk("7", "Box 1 includes amounts for an academic period beginning January-March"),
        chk("8", "At least half-time student"),
        chk("9", "Graduate student"),
        amt("10", "Insurance contract reimbursement or refund"),
    ),
    issuer_box="filer",
)

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _f1095a_part3() -> tuple[BoxDef, ...]:
    boxes: list[BoxDef] = []
    columns = (
        ("A", "Monthly enrollment premiums"),
        ("B", "Monthly second lowest cost silver plan (SLCSP) premium"),
        ("C", "Monthly advance payment of premium tax credit"),
    )
    for line, month in enumerate((*_MONTHS, "Annual total"), start=21):
        for column, label in columns:
            boxes.append(amt(f"{line}{column}", f"{label} ({month.title()})"))
    return tuple(boxes)


def _coverage() -> BoxDef:
    """Months anyone in the household was covered: ``cov_<MON>`` (or ``cov_ALL``) = 1."""
    return coded("cov", "Months of coverage (1 per covered month)", "ALL " + " ".join(_MONTHS))


F1095_A = FormDef(
    "1095-A",
    "Health Insurance Marketplace Statement",
    "marketplace",
    (
        txt("1", "Marketplace identifier"),
        ident("2", "Marketplace-assigned policy number"),
        ident("3", "Policy issuer's name"),
        ident("4", "Recipient's name"),
        ident("5", "Recipient's SSN"),
        ident("6", "Recipient's date of birth"),
        ident("7", "Recipient's spouse's name"),
        ident("8", "Recipient's spouse's SSN"),
        ident("9", "Recipient's spouse's date of birth"),
        txt("10", "Policy start date"),
        txt("11", "Policy termination date"),
        ident("12", "Street address"),
        ident("13", "City or town"),
        ident("14", "State or province"),
        ident("15", "Country and ZIP or foreign postal code"),
        ident("covered", "Covered individuals (Part II: names, SSNs, dates of birth)"),
        *_f1095a_part3(),
    ),
    issuer_box="3",
)

F1095_B = FormDef(
    "1095-B",
    "Health Coverage",
    "issuer",
    (
        ident("responsible", "Responsible individual (Part I: name, SSN, DOB, address)"),
        txt("8", "Origin of the health coverage (code)"),
        ident("employer", "Employer sponsored coverage (Part II: name, EIN, address)"),
        ident("issuer", "Issuer or other coverage provider (Part III: name, EIN, address)"),
        ident("covered", "Covered individuals (Part IV: names, SSNs, dates of birth)"),
        chk("months", "Months of coverage"),
        _coverage(),
    ),
)

F1095_C = FormDef(
    "1095-C",
    "Employer-Provided Health Insurance Offer and Coverage",
    "employer",
    (
        ident("employee", "Employee (Part I lines 1-6: name, SSN, address)"),
        ident("employer", "Applicable large employer member (lines 7-13: name, EIN, address)"),
        txt("14", "Offer of coverage (code, per month)"),
        coded(
            "15",
            "Employee required contribution (all 12 months or per month)",
            "ALL " + " ".join(_MONTHS),
        ),
        txt("16", "Section 4980H safe harbor and other relief (code, per month)"),
        ident("17", "ZIP code (per month)"),
        ident("covered", "Covered individuals (Part III: names, SSNs, dates of birth)"),
        _coverage(),
    ),
)

#: No issuer box: the Social Security Administration is the only payer, and
#: boxes 1, 2, 7 and 8 identify the beneficiary.
FSSA_1099 = FormDef(
    "SSA-1099",
    "Social Security Benefit Statement",
    "payer",
    (
        ident("1", "Name"),
        ident("2", "Beneficiary's social security number"),
        amt("3", "Benefits paid"),
        amt("4", "Benefits repaid to SSA"),
        amt("5", "Net benefits"),
        amt("6", "Voluntary federal income tax withheld"),
        ident("7", "Address"),
        ident("8", "Claim number"),
    ),
)

US_FORMS: Final = StaticFormSet.of(
    "US",
    "United States (federal)",
    FILING_STATUSES,
    (
        W2,
        F1099_INT,
        F1099_DIV,
        F1099_B,
        F1099_MISC,
        F1099_NEC,
        F1099_R,
        F1099_G,
        F1099_SA,
        FSSA_1099,
        F1098,
        F1098_E,
        F1098_T,
        F1095_A,
        F1095_B,
        F1095_C,
    ),
)
