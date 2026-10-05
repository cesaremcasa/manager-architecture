# Manager

Manager turns restaurant records into auditable operating context. This public
demo runs selected, verbatim Manager domain, tenant-context, authorization, and
read-only MCP code against synthetic rows in local PostgreSQL. It is an
engineering example, not the hosted product or a production deployment.

## Run

Requires Docker and Python 3.12. Start the isolated database and install pinned
demo dependencies:

```sh
docker compose -f demo/compose.yaml up -d --wait
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r demo/requirements.txt
export MANAGER_DEMO_DATABASE_URL='postgresql+psycopg://app_user:demo_password@localhost:55432/manager_demo'
PYTHONPATH=demo/manager_source python demo/run.py
PYTHONPATH=demo/manager_source pytest -q demo/tests
```

The tests use PostgreSQL role `app_user`; they exercise the checked-in RLS
policies, fail-closed contexts, tenant/group boundaries, worker scope, member
and role guards, and actual MCP read calls. Fixtures contain synthetic data.

## Source and license

The selected application files are copied from the private implementation at
`d2de4a2db54da516e82821cfc18a7f1efa1fb012`. File hashes and original paths are
in `demo/SOURCE-MANIFEST.json`. No private history, records, credentials, or
environment files are included. The repository is MIT licensed; see [LICENSE](LICENSE).

## Screenshot

![Manager public homepage](docs/screenshots/manager-public.jpg)

Captured 2026-10-05 from [mycelliumlab.com/manager/access](https://www.mycelliumlab.com/manager/access).
It shows the public homepage and is not evidence of backend behavior; run the
local PostgreSQL demo and tests for that.
