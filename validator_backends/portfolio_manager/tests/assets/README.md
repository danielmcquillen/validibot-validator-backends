# Portfolio Manager compatibility fixtures

These files are anonymized derivatives of public Portfolio Manager fixtures in
the [SEED Platform repository](https://github.com/SEED-platform/seed). They are
test data, not examples of real buildings and not current EPA certification
artifacts.

## Pinned upstream revision

- Repository: `https://github.com/SEED-platform/seed`
- Commit: `47202f575889f875f78a141ac588b8a7aa7e01eb`
- Retrieved: 2026-07-24
- Upstream license: `UPSTREAM-LICENSE.txt` in this directory

| Local derivative | Upstream source | Upstream SHA-256 |
| --- | --- | --- |
| `portfolio-manager-custom-report-anonymized.xlsx` | `seed/lib/mcm/tests/test_data/test_espm.xlsx` | `cf4ea03c44b0b00529c601804d85f43a7d33a600179219758826f86a557cf9b7` |
| `portfolio-manager-custom-report-single-anonymized.xlsx` | `seed/lib/mcm/tests/test_data/test_espm.xlsx` | `cf4ea03c44b0b00529c601804d85f43a7d33a600179219758826f86a557cf9b7` |
| `portfolio-manager-custom-report-anonymized.xls` | `seed/lib/mcm/tests/test_data/test_espm.xls` | `8238da3c602173819d32bd5e084736115073384100ab4926bacb976ad63469ae` |
| `portfolio-manager-custom-report-single-anonymized.xls` | `seed/lib/mcm/tests/test_data/test_espm.xls` | `8238da3c602173819d32bd5e084736115073384100ab4926bacb976ad63469ae` |
| `portfolio-manager-data-request-anonymized.xlsx` | `seed/tests/data/example-data-request-response.xlsx` | `0f85d4a20f42ad92404309fc0f8a8048767ff18a45e4fa5669dc5618daadcb84` |
| `portfolio-manager-full-property-anonymized.xlsx` | `seed/tests/data/portfolio-manager-single-22482007.xlsx` | `89c8bd1f96e1b75f64a57f593da4e44619b8a9497be83d33b0fee32fc55a7eed` |
| `portfolio-manager-custom-report-anonymized.xml` | `seed/tests/data/portfolio-manager-report.xml` | `170260b31c860cb8d73ff9f99165b7a41f88991a47e1de42aaab17994c353402` |
| `portfolio-manager-custom-report-single-anonymized.xml` | `seed/tests/data/portfolio-manager-report.xml` | `170260b31c860cb8d73ff9f99165b7a41f88991a47e1de42aaab17994c353402` |

## Anonymization and transformation

The derivatives retain report headings, worksheet organization, metric names,
cell types, XML hierarchy, missing-value markers, and representative carrier
formats. All property names, addresses, Portfolio Manager IDs, custom IDs,
meter IDs, consumption IDs, contact details, report identifiers, portfolio
names, dates, and non-empty free-text values were replaced with deterministic
synthetic values.

The XLSX files were imported and re-exported after cell replacement. That
process removed source document properties and author metadata. The public
legacy XLS source was converted to a temporary workbook, sanitized through the
same cell-replacement process, and re-encoded using the Excel 97 BIFF8 format.
This produces a genuine OLE2/BIFF carrier while preventing source OLE metadata
from surviving. The XML derivative retains three properties in both
the `informationAndMetrics` and `monthlyUsage` sections. Its single-property
variant retains one property in each section.

Post-transformation checks inspect all worksheets, hidden workbook content,
OOXML package strings, OLE-visible strings, XML values, document properties,
formulas, external links, and source-specific identifiers. Controlled
vocabulary such as units, property types, `Yes`, `No`, `Not Available`, and
`Not Applicable` is intentionally preserved because it is non-identifying and
part of the format contract.

The files remain derivative works under the bundled SEED license. Their names
do not imply endorsement by SEED, the U.S. Department of Energy, EPA, or any
other upstream contributor.

Current report downloads generated from EPA's Portfolio Manager test
environment are deliberately deferred. These fixtures provide public
compatibility coverage, not proof against a particular live EPA release.
