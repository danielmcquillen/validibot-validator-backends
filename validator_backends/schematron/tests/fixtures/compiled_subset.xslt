<?xml version="1.0" encoding="UTF-8"?>
<!--
  Test fixture: a hand-authored stand-in for a COMPILED Schematron pack.

  Real vendored packs are Schematron compiled to XSLT that emits SVRL; this
  stylesheet emits the same SVRL shape directly, small enough to read. It is
  deliberately XSLT 2.0: xs:decimal() constructors and the "ne" value
  comparison fail under any XSLT 1.0 processor, so a passing test PROVES the
  Saxon (2.0) engine ran — the exact coverage ADR-2026-07-01 test-plan item
  7 asks for (layers A/B never touch this runtime).

  VB-CO-15 mirrors the community fixture: total with VAT must equal total
  without VAT plus the VAT amount.
-->
<xsl:stylesheet version="2.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:svrl="http://purl.oclc.org/dsdl/svrl"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:ubl="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
    xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
    xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
    exclude-result-prefixes="xs ubl cac cbc">

  <xsl:output method="xml" indent="yes"/>

  <xsl:template match="/">
    <svrl:schematron-output title="VB compiled subset (test fixture)">
      <xsl:for-each select="/ubl:Invoice/cac:LegalMonetaryTotal">
        <svrl:fired-rule context="/ubl:Invoice/cac:LegalMonetaryTotal"/>
        <xsl:variable name="inclusive"
            select="xs:decimal(cbc:TaxInclusiveAmount)"/>
        <xsl:variable name="expected"
            select="xs:decimal(cbc:TaxExclusiveAmount) + xs:decimal(../cac:TaxTotal/cbc:TaxAmount)"/>
        <xsl:if test="$inclusive ne $expected">
          <svrl:failed-assert id="VB-CO-15" flag="fatal"
              location="/Invoice/LegalMonetaryTotal"
              test="TaxInclusiveAmount = TaxExclusiveAmount + TaxAmount">
            <svrl:text>Invoice total with VAT must equal total without VAT
              plus the total VAT amount.</svrl:text>
          </svrl:failed-assert>
        </xsl:if>
      </xsl:for-each>
    </svrl:schematron-output>
  </xsl:template>

</xsl:stylesheet>
