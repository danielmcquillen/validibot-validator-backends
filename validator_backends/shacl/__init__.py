"""SHACL validator backend.

Runs SHACL validation (RDF parsing, pyshacl, author-supplied SPARQL) inside an
isolated container so untrusted graphs and SPARQL never execute next to the
Validibot worker's database credentials, identity, or network. The engine code
here is the relocated, Django-free descendant of
``validibot/validations/validators/shacl/``
"""
