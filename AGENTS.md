# AGENTS Guide for microservicio_CRM_integration

This document orients autonomous coding agents working in this FastAPI microservice. Follow the conventions below to keep delivery predictable and production safe. No Cursor or Copilot rule files exist in this repo, so the guidance here is the authoritative agent playbook.

## 1. Repository At A Glance
- Runtime: Python 3.11+ with FastAPI, httpx, and pydantic v2.
- Entry point: `main.py` (FastAPI app); supporting modules `job_store.py` and `uploader.py`.
- Integration assets: `test_upload.py`, `test_data.csv`, `2.json`, `10000.json`, `test_loop.json`.
- Environment variables: `BACKEND_URL` (required), `API_SECRET_KEY` (optional but enforced when present), `BACKEND_EMAIL` and `BACKEND_PASSWORD` for integration testing.
- Persistent state is in-memory only; restarting the process clears jobs.
- Logging feeds stdout via the Python `logging` module. No external monitoring hooks are wired.

## 2. Workspace Setup
1. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```
2. Optional dev tools:
   ```bash
   pip install pytest ruff mypy
   ```
3. Populate `.env` with valid values before running the API or integration script. Guard secrets via `chmod 600 .env` when on shared hosts.
4. Activate the virtual environment for every shell session before invoking app or tests.

## 3. Running The Service
- Development server with auto-reload:
  ```bash
  uvicorn main:app --host 0.0.0.0 --port 8000 --reload
  ```
- Production-like launch (mirrors README):
  ```bash
  uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
  ```
- Health probe: `curl http://localhost:8000/health` should return `{"status":"ok",...}`.

## 4. Build And Packaging Notes
- No build artifacts are generated; deployment is source based. When containerising, replicate the steps in README (Python 3.11 base, install deps, copy source, start uvicorn).
- Keep the footprint lean: only `main.py`, `job_store.py`, `uploader.py`, and static assets are required at runtime. Exclude test fixtures from production images.
- If packaging to wheel, ensure optional dev dependencies stay outside `requirements.txt` to avoid bloating runtime images.

## 5. Linting And Static Analysis
- Preferred linter: `ruff` (install manually). Run `ruff check .`; add `--fix` for safe auto-corrections.
- Type checking (optional but encouraged): `mypy main.py job_store.py uploader.py`.
- Quick syntax validation without extra tooling: `python -m compileall main.py job_store.py uploader.py`.

## 6. Testing Strategy
- There is no dedicated unit-test suite yet. `test_upload.py` exercises an end-to-end flow against a live service.
- Full integration scenario (requires valid backend credentials): `python test_upload.py --input test_data.csv --page-size 1000 --email "$BACKEND_EMAIL" --password "$BACKEND_PASSWORD"`.
- Smaller payloads or alternative formats: `python test_upload.py --input test_data.csv --page-size 50 --format list --dataset-name demo_dataset --email "$BACKEND_EMAIL" --password "$BACKEND_PASSWORD"`.
- Recommended unit-test harness: `pytest`. Typical commands once installed: `pytest`, `pytest path/to/test_file.py::TestClass::test_case` (single test), `pytest path/to/test_module.py -k "keyword"` (filtered run).
- When adding new tests place them under `tests/` or keep the `test_*.py` naming to stay discoverable. Ensure fixtures avoid hitting live GCS endpoints by mocking `PlatformUploader`.

## 7. Data And Fixtures
- Sample CSV: `test_data.csv` (UTF-8, header + rows). Use for local dry-runs.
- JSON payloads: `2.json`, `10000.json`, `test_loop.json`. They mirror chunked responses from CRM APIs.
- Avoid committing large datasets. Prefer generating fixtures during tests using factories or lightweight JSON templates.
- Do not store secrets in test files; rely on `.env` for credentials.

## 8. API Contract Reminders
- `POST /jobs`: requires `dataset_name`, `email`, `password`, optional `headers`. Response includes `job_id`, `status`.
- `POST /jobs/{id}/chunks`: accepts list of dicts or list of lists. Service deduces headers from dict keys if needed.
- `POST /jobs/{id}/complete`: flushes remaining buffer; expects API key when configured.
- `GET /jobs/{id}/status`: returns progress metrics, byte offsets, errors.
- `DELETE /jobs/{id}`: idempotent cleanup.
- Respect the 256 KiB chunk policy in `uploader.py`; buffer is managed server-side but test clients should still batch realistically.

## 9. Code Style: Imports And Structure
- Order imports: standard library, third-party, local modules. Separate blocks with single blank lines.
- Keep module docstrings (Spanish narrative style) summarising responsibilities at the top.
- Group constants in ALL_CAPS near the top of the file; derive values (like `256 * 1024`) inline for readability.
- FastAPI routers stay in `main.py`; avoid splitting unless routing complexity demands it.

## 10. Code Style: Formatting
- Use Black-style 4-space indentation; maximum line length 100 characters unless readability suffers.
- Prefer f-strings for formatting. Avoid string concatenation except when building long docstrings.
- Keep blank lines around large comment banners (see existing `# ── Section ──` guides) and reuse that motif when adding new sections.
- Encode files as UTF-8 but stick to ASCII characters unless domain-specific strings require accents (existing files already use descriptive Spanish text—preserve accents when quoting user content).

## 11. Code Style: Types And Functions
- Type hints are mandatory for public functions and methods. Use Python 3.11 syntax (`list[str]`, `dict[str, Any]`, `|` unions).
- Return explicit dictionaries from FastAPI handlers; rely on Pydantic models for request validation only.
- Use dataclasses only if state becomes more complex; current approach stores dicts with metadata inside `JobStore`.
- When expanding `JobStore`, maintain thread safety by touching `_jobs` inside `self._lock` only.

## 12. Naming Conventions
- snake_case for functions, variables, and module-level helpers (`_get_job_or_404`).
- PascalCase for classes (`JobStore`, `PlatformUploader`).
- Prefix private helpers with a single underscore within modules.
- Keep API payload keys in lowerCamelCase to match external platform expectations (`dataset_name` accepted client-side but GCS metadata uses `objectName`).

## 13. Error Handling And Validation
- Raise `HTTPException` with accurate status codes for FastAPI endpoints; include concise Spanish detail messages.
- Wrap external calls (`httpx.post`, `httpx.put`) with `try/except` to convert unexpected errors into actionable messages while logging the stack trace.
- Keep error strings free of credentials or personally identifiable data.
- On fatal states set `JobStatus.FAILED` and persist `error` in the job record for diagnostics.

## 14. Logging Practices
- Use the module-level `logger` configured in each file. Do not use bare `print` in production code.
- Logging levels: `info` for lifecycle milestones, `warning` for recoverable anomalies, `error` for failures prior to raising.
- Preserve the structured logging style seen in `test_upload.py` when extending CLI tools: timestamps plus level tags improve trace readability.
- Avoid excessive log volume in chunk loops; summarise stats once per request.

## 15. HTTP And External Calls
- Always pass explicit `timeout` to `httpx` requests (30s or 120s as in current code). Mirror this pattern for new endpoints.
- Reuse `PlatformUploader` for GCS operations; never bypass it with raw httpx calls from other modules.
- When mocking in tests, patch `PlatformUploader` methods (`get_resumable_url`, `init_resumable_session`, `upload_chunk`, `finalize_upload`).
- Respect authentication headers: `App-Identifier` for backend calls, `x-api-key` for FastAPI endpoints.

## 16. Concurrency And State
- `JobStore` uses a threading lock. Hold the lock only for minimal critical sections; avoid long-running operations while the lock is held.
- Do not mutate job dictionaries outside `JobStore` methods; always fetch, operate via provided helpers, then persist via setter.
- When adding async features, consider replacing `threading.Lock` with `asyncio.Lock` and migrating to async-compatible storage.
- Jobs are ephemeral; ensure new background tasks clean up their own state on completion or failure.

## 17. Environment Configuration
- Use `dotenv.load_dotenv()` only in entry points meant for local development. Avoid loading `.env` inside libraries to keep container environments pure.
- Document new environment variables in `README.md` and mirror them here.
- Validate critical env vars at startup; fail fast with clear logs if `BACKEND_URL` is missing.
- Do not commit `.env` or secrets; `.gitignore` already covers these files.

## 18. Comments And Documentation
- Reserve inline comments for non-obvious logic (e.g., GCS chunk size rationale). Avoid restating what the code already says.
- Maintain section banners (`# ── Section ──`) to help future agents scan modules quickly.
- Update `README.md` and `N8N_GUIDE.md` whenever public behaviour changes. Keep AGENTS.md in sync with new workflows.
- Provide docstrings for new classes/functions describing intent, inputs, outputs, and side-effects.

## 19. Dependency Management
- Pin runtime dependencies in `requirements.txt`. Use a separate `requirements-dev.txt` if dev tooling grows.
- Run `pip install -r requirements.txt` after updating pins; ensure the app starts before committing.
- When adding cloud SDKs, prefer lightweight clients over full GCP bundles to keep container images slim.
- Periodically check for fastapi/pydantic/httpx compatibility; update as a set to prevent breaking changes.

## 20. Git And Process Expectations
- Keep branches focused; avoid mixing infrastructure, feature, and formatting changes.
- Run lint and tests locally before opening PRs or handing off to CI.
- Follow conventional commit style in commit messages (`feat:`, `fix:`, `chore:`) unless repo owners specify otherwise.
- Never commit secrets, `.venv`, or large binary files.

## 21. When Extending Functionality
- For new endpoints, add request/response models in `main.py` alongside existing ones.
- Update `JobStore` atomically when adding new fields; provide migration defaults for existing jobs in memory.
- Keep integration clients backward compatible; coordinate schema changes with the upstream backend team.
- Document any new cron or background routines in this guide so agents know how to operate them.

## 22. Hand-off Checklist
- [ ] Virtualenv activated and dependencies installed.
- [ ] `.env` configured with valid backend credentials.
- [ ] `ruff check` and (if applicable) `mypy` pass.
- [ ] `pytest` (or `python test_upload.py` with appropriate args) executes without failures.
- [ ] Logs reviewed for warnings before delivering the branch.

Stay disciplined with these practices to keep the microservice reliable and friendly for both humans and agents.
