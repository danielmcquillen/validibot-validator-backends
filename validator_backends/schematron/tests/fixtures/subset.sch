<?xml version="1.0" encoding="UTF-8"?>
<!--
  Test fixture: a tiny Schematron SOURCE, the shape authors actually upload.

  It is deliberately queryBinding="xslt2" with XPath 2.0 expressions
  (xs:decimal constructors, the "eq" value comparison): SchXslt2
  transpiles it to a stylesheet that no XSLT 1.0 processor accepts, so a
  passing test PROVES the full production path ran — SchXslt2 transpile
  under Saxon, then the compiled rules under Saxon (ADR-2026-07-01 test-plan
  item 7).

  VB-CO-15 mirrors the community fixture: total with VAT must equal total
  without VAT plus the VAT amount.
-->
<schema xmlns="http://purl.oclc.org/dsdl/schematron" queryBinding="xslt2">
  <title>VB subset (test fixture)</title>

  <ns prefix="xs" uri="http://www.w3.org/2001/XMLSchema"/>
  <ns prefix="ubl" uri="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>
  <ns prefix="cac" uri="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"/>
  <ns prefix="cbc" uri="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"/>

  <pattern>
    <rule context="/ubl:Invoice/cac:LegalMonetaryTotal">
      <assert id="VB-CO-15" flag="fatal"
          test="xs:decimal(cbc:TaxInclusiveAmount) eq xs:decimal(cbc:TaxExclusiveAmount) + xs:decimal(../cac:TaxTotal/cbc:TaxAmount)"
        >Invoice total with VAT must equal total without VAT plus the total
        VAT amount.</assert>
    </rule>
  </pattern>
</schema>
