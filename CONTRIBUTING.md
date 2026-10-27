# Contributing

Thank you for helping. This project publishes regulator and company data that people use to
make decisions, so changes need evidence as well as code.

## Ground rules

- Never commit source data: workbooks, CSV extracts, PDFs, API snapshots or screenshots of
  them. The repository tests fail if you do. Runtime data lives in Unity Catalog Volumes.
- Never commit deployment values: workspace hosts, CLI profiles, warehouse IDs, email
  addresses, service principal IDs or tokens. Pass them with `--var` or `BUNDLE_VAR_*`.
- A source is enabled only when its publisher, landing page, licence, attribution, release
  identity and parser contract are recorded. Being downloadable is not a licence.
- Do not work around a publisher's browser check or access control. Use the manual bootstrap
  route described in [docs/operations.md](docs/operations.md).

## Making a change

1. Read [docs/adding-a-source.md](docs/adding-a-source.md) if you are adding or changing a source,
   metric mapping, company or benchmark question.
2. Make the change with tests. Parser tests generate their own fixtures at run time.
3. Deploy to your own development target and run the validation job:

   ```bash
   cd bundles/data
   databricks bundle deploy -t dev --profile <profile> --var catalog=<your dev catalog> \
     --var workspace_base=<your workspace folder> --var "warehouse_name=<warehouse>" --var company_code=<code>
   databricks bundle run validate_project -t dev --profile <profile> --var catalog=<your dev catalog> \
     --var workspace_base=<your workspace folder> --var "warehouse_name=<warehouse>" --var company_code=<code>
   ```

   Lint, unit, repository and integration tests all run on Databricks serverless compute.
4. If you changed the Genie definition, run the benchmark questions (see
   [docs/operations.md](docs/operations.md#genie-benchmarks)) and include the pass rate in your pull request.
5. Record any source or licensing decision in
   [docs/data-issues-and-decisions.md](docs/data-issues-and-decisions.md) with a new stable ID.

## Style

- Python: `ruff check` and `ruff format` with the settings in `pyproject.toml`.
- Documentation: plain British English, short paragraphs, exact object names, examples.
- Commit messages: say what changed and why.

## Licence

Code contributions are accepted under the Apache License 2.0 (see [LICENSE](LICENSE)). Source
data keeps its publisher's licence; see [docs/source-audit.md](docs/source-audit.md).
