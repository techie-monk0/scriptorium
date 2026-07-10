"""External-tool integrations — one module per trusted, first-party tool that depends on catalogue
editions. Each defines a single `ExternalToolDependency` impl (its restrictions on catalogue
capabilities); `access_api.tool_policy` registers them. The catalogue core never imports these — it
reaches them only through the `ExternalToolDependency` ABC + the registry. See
docs/access/external_tool_dependency_contract.md and citation_edition_contract_plan.md (D5).
"""
