"""Group-level integrations, reporting, lineage, and approval workflows.

External sources are normalized into ``integration_records`` before they are
shown to a person, a UI, an MCP client, or Manager AI.  No caller in this
module queries a spreadsheet, a weather provider, or Yelp at answer time.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db import group_transaction
from app.groups import GroupUnit
from app.adapters.yelp import YelpAPIError, YelpPlacesClient
from app.domain.market import CompetitorSearch


GOOGLE_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets"
GOOGLE_DRIVE_FILE_URL = "https://www.googleapis.com/drive/v3/files"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
SOURCE_OPEN_METEO = "https://open-meteo.com/en/docs"
SOURCE_YELP_API = "https://docs.developer.yelp.com/docs/fusion-intro"

# Input workbooks are intentionally strict.  The service validates all tabs
# before opening the publish transaction, so partial imports cannot surface.
WORKBOOK_SPECS: dict[str, tuple[str, ...]] = {
    "README_Source": ("section", "description", "source_type"),
    "Import_Manifest": ("dataset", "schema_version", "source_type"),
    "Data_Dictionary": ("dataset", "field_name", "definition"),
    "Units": ("external_id", "unit_code", "display_name", "source_type", "schema_version"),
    "Approval_Policies": ("external_id", "unit_code", "policy", "source_type", "schema_version"),
    "KPI_Targets": ("external_id", "unit_code", "metric", "source_type", "schema_version"),
    "Scenario_Events": ("external_id", "unit_code", "event_date", "source_type", "schema_version"),
    "Sales_Daily_24M": ("external_id", "unit_code", "business_date", "net_sales", "source_type", "schema_version"),
    "Sales_Hourly_90D": ("external_id", "unit_code", "business_date", "hour", "net_sales", "source_type", "schema_version"),
    "Delivery_Daily_24M": ("external_id", "unit_code", "business_date", "net_payout", "source_type", "schema_version"),
    "Delivery_Orders_90D": ("external_id", "unit_code", "order_date", "source_type", "schema_version"),
    "Labor_Roster": ("external_id", "unit_code", "role_name", "source_type", "schema_version"),
    "Labor_Role_Benchmarks": ("external_id", "role_name", "hourly_rate_low", "source_type", "schema_version"),
    "Labor_Daily_24M": ("external_id", "unit_code", "business_date", "labor_cost", "source_type", "schema_version"),
    "Labor_Shifts_8W": ("external_id", "unit_code", "shift_start", "role_name", "source_type", "schema_version"),
    "Inventory_Catalog": ("external_id", "unit_code", "item_name", "source_type", "schema_version"),
    "Inventory_Counts_90D": ("external_id", "unit_code", "count_date", "on_hand", "source_type", "schema_version"),
    "Inventory_Movements_90D": ("external_id", "unit_code", "movement_date", "quantity_delta", "source_type", "schema_version"),
    "Vendors": ("external_id", "unit_code", "vendor_name", "source_type", "schema_version"),
    "Invoices_12M": ("external_id", "unit_code", "invoice_date", "invoice_total", "source_type", "schema_version"),
    "Invoice_Lines_12M": ("external_id", "unit_code", "invoice_external_id", "line_total", "source_type", "schema_version"),
    "Recipes": ("external_id", "unit_code", "recipe_name", "source_type", "schema_version"),
    "Recipe_Lines": ("external_id", "unit_code", "recipe_external_id", "source_type", "schema_version"),
    "Production_Plans_90D": ("external_id", "unit_code", "plan_date", "source_type", "schema_version"),
    "Waste_90D": ("external_id", "unit_code", "waste_date", "source_type", "schema_version"),
    "Tasks_FoodSafety_90D": ("external_id", "unit_code", "task_date", "source_type", "schema_version"),
    "Weather_History_24M": ("external_id", "unit_code", "observed_at", "temperature_f", "source_type", "schema_version"),
    "Weather_Forecast_16D": ("external_id", "unit_code", "forecast_at", "temperature_f", "source_type", "schema_version"),
    "Market_Baseline": ("external_id", "unit_code", "observed_at", "source_type", "schema_version"),
}

NON_RECORD_TABS = {"README_Source", "Import_Manifest", "Data_Dictionary"}
SOURCE_TYPES = {"synthetic_scenario", "public_benchmark", "public_api", "customer_source"}


class IntegrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ValidationIssue:
    sheet_name: str | None
    row_number: int | None
    field_name: str | None
    code: str
    message: str


class WorkbookValidationError(IntegrationError):
    def __init__(self, issues: Iterable[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(f"workbook validation failed with {len(self.issues)} issue(s)")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, default=_json_safe, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _decimal(value: object, *, default: Decimal = Decimal("0")) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid decimal: {value}") from error


def _effective_at(row: Mapping[str, object]) -> datetime | None:
    for field in (
        "business_date", "invoice_date", "order_date", "count_date", "movement_date", "event_date",
        "plan_date", "waste_date", "task_date", "observed_at", "forecast_at", "shift_start",
    ):
        raw = row.get(field)
        if raw in (None, ""):
            continue
        try:
            if isinstance(raw, datetime):
                return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
            if isinstance(raw, date):
                return datetime.combine(raw, datetime.min.time(), tzinfo=timezone.utc)
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                return datetime.combine(date.fromisoformat(str(raw)), datetime.min.time(), tzinfo=timezone.utc)
            except ValueError:
                return None
    return None


def _fernet(settings: Settings) -> Fernet:
    key = settings.integration_token_encryption_key
    if not key:
        raise IntegrationError("Google Sheets integration encryption is not configured")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as error:
        raise IntegrationError("integration token encryption key is invalid") from error


def encrypt_secret(settings: Settings, plaintext: str) -> str:
    return _fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt_secret(settings: Settings, ciphertext: str) -> str:
    try:
        return _fernet(settings).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as error:
        raise IntegrationError("stored integration credential could not be decrypted") from error


def _oauth_ready(settings: Settings) -> None:
    if not settings.google_sheets_enabled:
        raise IntegrationError("Google Sheets OAuth has not been enabled")
    if not all((settings.google_oauth_client_id, settings.google_oauth_client_secret, settings.google_oauth_redirect_uri)):
        raise IntegrationError("Google Sheets OAuth configuration is incomplete")
    _fernet(settings)


def start_google_oauth(engine: Engine, settings: Settings, group_id: UUID, owner_id: UUID) -> str:
    """Create single-use state + PKCE material and return the Google consent URL."""
    _oauth_ready(settings)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    with group_transaction(engine, group_id) as connection:
        connection.execute(
            text(
                """
                INSERT INTO oauth_authorization_states
                  (group_id, owner_id, provider, state_hash, code_verifier_ciphertext, expires_at)
                VALUES (:group_id, :owner_id, 'google_sheets', :state_hash, :verifier, :expires_at)
                """
            ),
            {
                "group_id": str(group_id),
                "owner_id": str(owner_id),
                "state_hash": hashlib.sha256(state.encode()).hexdigest(),
                "verifier": encrypt_secret(settings, verifier),
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
            },
        )
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode({
        'client_id': settings.google_oauth_client_id,
        'redirect_uri': settings.google_oauth_redirect_uri,
        'response_type': 'code',
        'scope': GOOGLE_DRIVE_FILE_SCOPE,
        'access_type': 'offline',
        'include_granted_scopes': 'true',
        'prompt': 'consent',
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    })}"


def finish_google_oauth(engine: Engine, settings: Settings, state: str, code: str, *, transport: httpx.BaseTransport | None = None) -> UUID:
    _oauth_ready(settings)
    if not state or not code:
        raise IntegrationError("missing OAuth state or authorization code")
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT group_id, owner_id, provider, code_verifier_ciphertext FROM consume_oauth_authorization_state(:state_hash)"),
            {"state_hash": state_hash},
        ).mappings().one_or_none()
    if row is None or row["provider"] != "google_sheets":
        raise IntegrationError("OAuth state is invalid or expired")
    verifier = decrypt_secret(settings, str(row["code_verifier_ciphertext"]))
    payload = {
        "code": code,
        "client_id": settings.google_oauth_client_id,
        "client_secret": settings.google_oauth_client_secret,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    if transport is None:
        response = httpx.post(GOOGLE_TOKEN_URL, data=payload, timeout=20)
    else:
        with httpx.Client(transport=transport, timeout=20) as client:
            response = client.post(GOOGLE_TOKEN_URL, data=payload)
    if response.is_error:
        raise IntegrationError("Google declined the authorization exchange")
    try:
        tokens = response.json()
        access_token = str(tokens["access_token"])
        refresh_token = str(tokens.get("refresh_token") or "")
        expires_in = int(tokens.get("expires_in", 3600))
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrationError("Google returned an invalid authorization response") from error

    group_id = UUID(str(row["group_id"]))
    with group_transaction(engine, group_id) as connection:
        connection_id = connection.execute(
            text(
                """
                INSERT INTO integration_connections
                  (group_id, owner_id, provider, status, scopes, access_token_ciphertext,
                   refresh_token_ciphertext, access_token_expires_at)
                VALUES (:group_id, :owner_id, 'google_sheets', 'connected', :scopes, :access, :refresh, :expires)
                ON CONFLICT (group_id, provider) DO UPDATE SET
                  owner_id = EXCLUDED.owner_id, status = 'connected', scopes = EXCLUDED.scopes,
                  access_token_ciphertext = EXCLUDED.access_token_ciphertext,
                  refresh_token_ciphertext = COALESCE(NULLIF(EXCLUDED.refresh_token_ciphertext, ''), integration_connections.refresh_token_ciphertext),
                  access_token_expires_at = EXCLUDED.access_token_expires_at, last_error_code = NULL,
                  updated_at = now()
                RETURNING id
                """
            ),
            {
                "group_id": str(group_id),
                "owner_id": str(row["owner_id"]),
                "scopes": [GOOGLE_DRIVE_FILE_SCOPE],
                "access": encrypt_secret(settings, access_token),
                "refresh": encrypt_secret(settings, refresh_token) if refresh_token else "",
                "expires": datetime.now(timezone.utc) + timedelta(seconds=max(expires_in, 60)),
            },
        ).scalar_one()
    return UUID(str(connection_id))


def list_integrations(engine: Engine, group_id: UUID) -> list[dict[str, object]]:
    with group_transaction(engine, group_id) as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, provider, status, display_name, external_file_id, external_file_name, source_revision,
                       scopes, last_synced_at, last_validated_at, last_error_code, created_at
                FROM integration_connections ORDER BY created_at DESC
                """
            )
        ).mappings().all()
    return [
        {
            "id": str(row["id"]), "provider": str(row["provider"]), "status": str(row["status"]),
            "display_name": row["display_name"], "external_file_id": row["external_file_id"],
            "external_file_name": row["external_file_name"], "source_revision": row["source_revision"],
            "scopes": list(row["scopes"]), "last_synced_at": row["last_synced_at"].isoformat() if row["last_synced_at"] else None,
            "last_validated_at": row["last_validated_at"].isoformat() if row["last_validated_at"] else None,
            "last_error_code": row["last_error_code"], "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]


def select_google_sheet(engine: Engine, group_id: UUID, connection_id: UUID, spreadsheet_id: str, display_name: str | None) -> None:
    if not spreadsheet_id or len(spreadsheet_id) > 256 or not all(c.isalnum() or c in "-_" for c in spreadsheet_id):
        raise IntegrationError("spreadsheet id is invalid")
    with group_transaction(engine, group_id) as connection:
        changed = connection.execute(
            text(
                """
                UPDATE integration_connections
                SET external_file_id = :spreadsheet_id, external_file_name = COALESCE(:display_name, external_file_name),
                    status = 'ready', updated_at = now(), last_error_code = NULL
                WHERE id = :connection_id AND provider = 'google_sheets'
                """
            ),
            {"connection_id": str(connection_id), "spreadsheet_id": spreadsheet_id, "display_name": display_name},
        ).rowcount
    if not changed:
        raise IntegrationError("Google Sheets connection was not found")


def _google_access_token(
    engine: Engine,
    settings: Settings,
    group_id: UUID,
    connection_id: UUID,
    *,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Return a usable access token and rotate it before it expires.

    The refresh token stays encrypted at rest.  Neither token appears in errors,
    import runs, lineage, or logs.
    """
    with group_transaction(engine, group_id) as connection:
        row = connection.execute(
            text(
                """
                SELECT access_token_ciphertext, refresh_token_ciphertext, access_token_expires_at
                FROM integration_connections WHERE id = :id AND provider = 'google_sheets'
                """
            ),
            {"id": str(connection_id)},
        ).mappings().one_or_none()
    if row is None or not row["access_token_ciphertext"]:
        raise IntegrationError("Google Sheets connection credentials are unavailable")
    expires_at = row["access_token_expires_at"]
    if expires_at is None or expires_at > datetime.now(timezone.utc) + timedelta(minutes=2):
        return decrypt_secret(settings, str(row["access_token_ciphertext"]))
    if not row["refresh_token_ciphertext"]:
        raise IntegrationError("Google Sheets access expired; reconnect this file")
    refresh_token = decrypt_secret(settings, str(row["refresh_token_ciphertext"]))
    payload = {
        "client_id": settings.google_oauth_client_id,
        "client_secret": settings.google_oauth_client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if transport is None:
        response = httpx.post(GOOGLE_TOKEN_URL, data=payload, timeout=20)
    else:
        with httpx.Client(transport=transport, timeout=20) as client:
            response = client.post(GOOGLE_TOKEN_URL, data=payload)
    if response.is_error:
        raise IntegrationError("Google Sheets access could not be refreshed; reconnect this file")
    try:
        body = response.json()
        access_token = str(body["access_token"])
        expires_in = int(body.get("expires_in", 3600))
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrationError("Google returned an invalid refresh response") from error
    with group_transaction(engine, group_id) as connection:
        connection.execute(
            text(
                """
                UPDATE integration_connections
                SET access_token_ciphertext = :access, access_token_expires_at = :expires,
                    status = 'ready', last_error_code = NULL, updated_at = now()
                WHERE id = :id
                """
            ),
            {
                "id": str(connection_id), "access": encrypt_secret(settings, access_token),
                "expires": datetime.now(timezone.utc) + timedelta(seconds=max(expires_in, 60)),
            },
        )
    return access_token


def _cell_value(cell: Mapping[str, object]) -> object:
    entered = cell.get("userEnteredValue")
    if isinstance(entered, Mapping):
        if "formulaValue" in entered:
            return {"formula": str(entered["formulaValue"])}
        for key in ("stringValue", "numberValue", "boolValue"):
            if key in entered:
                return entered[key]
    effective = cell.get("effectiveValue")
    if isinstance(effective, Mapping):
        for key in ("stringValue", "numberValue", "boolValue"):
            if key in effective:
                return effective[key]
    return ""


def fetch_google_workbook(engine: Engine, settings: Settings, group_id: UUID, connection_id: UUID, *, transport: httpx.BaseTransport | None = None) -> tuple[dict[str, list[dict[str, object]]], str, str, str]:
    """Read a selected Sheet in one API request and return header-keyed rows.

    The raw Sheet response is kept in memory only.  It is neither logged nor
    persisted: normalized records plus lineage are the source of truth.
    """
    _oauth_ready(settings)
    with group_transaction(engine, group_id) as connection:
        row = connection.execute(
            text(
                """
                SELECT external_file_id, external_file_name
                FROM integration_connections WHERE id = :id AND provider = 'google_sheets'
                """
            ),
            {"id": str(connection_id)},
        ).mappings().one_or_none()
    if row is None or not row["external_file_id"]:
        raise IntegrationError("select a Google Sheet before syncing")
    token = _google_access_token(engine, settings, group_id, connection_id, transport=transport)
    url = f"{GOOGLE_SHEETS_URL}/{row['external_file_id']}"
    params = {"includeGridData": "true", "fields": "spreadsheetId,properties(title),sheets(properties(title),data(rowData(values(userEnteredValue,effectiveValue))))"}
    if transport is None:
        response = httpx.get(url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    else:
        with httpx.Client(transport=transport, timeout=30) as client:
            response = client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
    if response.is_error:
        raise IntegrationError("Google Sheets could not be read; check file access and connection status")
    body = response.json()
    revision = _sha256(body)
    file_name = str(row["external_file_name"] or body.get("properties", {}).get("title") or row["external_file_id"])
    # Drive's monotonically advancing version is preferred as an idempotency key.
    # If Drive metadata is temporarily unavailable, the content hash is still safe.
    drive_url = f"{GOOGLE_DRIVE_FILE_URL}/{row['external_file_id']}"
    try:
        if transport is None:
            drive_response = httpx.get(drive_url, params={"fields": "id,name,version,modifiedTime"}, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        else:
            with httpx.Client(transport=transport, timeout=15) as client:
                drive_response = client.get(drive_url, params={"fields": "id,name,version,modifiedTime"}, headers={"Authorization": f"Bearer {token}"})
        if not drive_response.is_error:
            drive_body = drive_response.json()
            revision = f"drive:{drive_body.get('version', revision)}"
            file_name = str(drive_body.get("name") or file_name)
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    workbook: dict[str, list[dict[str, object]]] = {}
    for sheet in body.get("sheets", []):
        properties = sheet.get("properties", {})
        title = properties.get("title")
        grids = sheet.get("data", [])
        if not isinstance(title, str) or not grids:
            continue
        row_data = grids[0].get("rowData", [])
        raw_rows = [[_cell_value(cell) for cell in row.get("values", [])] for row in row_data]
        if not raw_rows:
            workbook[title] = []
            continue
        headers = [str(value).strip() for value in raw_rows[0]]
        workbook[title] = [
            {headers[index]: value for index, value in enumerate(values) if index < len(headers) and headers[index]}
            for values in raw_rows[1:]
            if any(value not in (None, "") for value in values)
        ]
    return workbook, str(body.get("spreadsheetId", row["external_file_id"])), revision, file_name


def validate_workbook(workbook: Mapping[str, list[Mapping[str, object]]], units: Iterable[GroupUnit]) -> None:
    issues: list[ValidationIssue] = []
    known_units = {unit.unit_code for unit in units}
    missing = [sheet for sheet in WORKBOOK_SPECS if sheet not in workbook]
    issues.extend(ValidationIssue(sheet, None, None, "missing_sheet", f"required sheet {sheet} is missing") for sheet in missing)
    seen_ids: set[tuple[str, str]] = set()
    invoice_totals: dict[str, Decimal] = {}
    invoice_lines: Counter[str] = Counter()
    for sheet_name, required in WORKBOOK_SPECS.items():
        rows = workbook.get(sheet_name, [])
        if not isinstance(rows, list):
            issues.append(ValidationIssue(sheet_name, None, None, "invalid_sheet", "sheet must contain tabular rows"))
            continue
        for row_index, raw_row in enumerate(rows, start=2):
            row = dict(raw_row)
            missing_fields = [field for field in required if row.get(field) in (None, "")]
            for field in missing_fields:
                issues.append(ValidationIssue(sheet_name, row_index, field, "missing_field", f"{field} is required"))
            for field, value in row.items():
                if isinstance(value, Mapping) and "formula" in value:
                    issues.append(ValidationIssue(sheet_name, row_index, field, "formula_not_allowed", "raw import sheets cannot contain formulas"))
            source_type = str(row.get("source_type", ""))
            if source_type and source_type not in SOURCE_TYPES:
                issues.append(ValidationIssue(sheet_name, row_index, "source_type", "invalid_source_type", "source_type is not recognized"))
            unit_code = row.get("unit_code")
            if unit_code and str(unit_code) not in known_units:
                issues.append(ValidationIssue(sheet_name, row_index, "unit_code", "unknown_unit", "unit_code is not in this group"))
            external_id = row.get("external_id")
            if sheet_name not in NON_RECORD_TABS and external_id:
                identity = (sheet_name, str(external_id))
                if identity in seen_ids:
                    issues.append(ValidationIssue(sheet_name, row_index, "external_id", "duplicate_external_id", "external_id must be unique inside its sheet"))
                seen_ids.add(identity)
            try:
                if sheet_name == "Invoices_12M" and external_id:
                    invoice_totals[str(external_id)] = _decimal(row.get("invoice_total"))
                if sheet_name == "Invoice_Lines_12M" and row.get("invoice_external_id"):
                    invoice_lines[str(row["invoice_external_id"])] += _decimal(row.get("line_total"))
            except ValueError:
                issues.append(ValidationIssue(sheet_name, row_index, "line_total", "invalid_number", "financial amount must be numeric"))
    for invoice_id, line_total in invoice_lines.items():
        invoice_total = invoice_totals.get(invoice_id)
        if invoice_total is None:
            issues.append(ValidationIssue("Invoice_Lines_12M", None, "invoice_external_id", "missing_invoice", f"invoice {invoice_id} does not exist"))
        elif abs(invoice_total - line_total) > Decimal("0.01"):
            issues.append(ValidationIssue("Invoice_Lines_12M", None, "line_total", "invoice_total_mismatch", f"invoice {invoice_id} total does not equal its lines"))
    if issues:
        raise WorkbookValidationError(issues)


def publish_workbook(
    engine: Engine,
    *,
    group_id: UUID,
    connection_id: UUID,
    workbook: Mapping[str, list[Mapping[str, object]]],
    units: Iterable[GroupUnit],
    schema_version: str = "manager_group_os.v1",
    source_revision: str | None = None,
    source_file_name: str | None = None,
    source_url: str | None = None,
) -> dict[str, object]:
    """Validate every tab, then atomically publish normalized records + lineage."""
    unit_list = tuple(units)
    source_hash = _sha256(workbook)
    revision = source_revision or source_hash
    with group_transaction(engine, group_id) as connection:
        existing = connection.execute(
            text("SELECT id, status, record_counts FROM import_runs WHERE connection_id = :connection_id AND source_revision = :revision AND schema_version = :schema_version"),
            {"connection_id": str(connection_id), "revision": revision, "schema_version": schema_version},
        ).mappings().one_or_none()
        if existing is not None:
            return {"id": str(existing["id"]), "status": str(existing["status"]), "record_counts": dict(existing["record_counts"]), "idempotent": True}
        run_id = connection.execute(
            text(
                """
                INSERT INTO import_runs (group_id, connection_id, source_revision, schema_version, source_hash)
                VALUES (:group_id, :connection_id, :revision, :schema_version, :source_hash)
                RETURNING id
                """
            ),
            {"group_id": str(group_id), "connection_id": str(connection_id), "revision": revision, "schema_version": schema_version, "source_hash": source_hash},
        ).scalar_one()
        try:
            validate_workbook(workbook, unit_list)
        except WorkbookValidationError as error:
            for issue in error.issues:
                connection.execute(
                    text("INSERT INTO import_errors (group_id, import_run_id, sheet_name, row_number, field_name, code, message) VALUES (:group_id, :run_id, :sheet, :row, :field, :code, :message)"),
                    {"group_id": str(group_id), "run_id": str(run_id), "sheet": issue.sheet_name, "row": issue.row_number, "field": issue.field_name, "code": issue.code, "message": issue.message},
                )
            connection.execute(text("UPDATE import_runs SET status = 'failed', completed_at = now(), validation_summary = :summary WHERE id = :run_id"), {"run_id": str(run_id), "summary": json.dumps({"errors": len(error.issues)})})
            return {"id": str(run_id), "status": "failed", "errors": len(error.issues), "idempotent": False}

        unit_by_code = {unit.unit_code: unit.tenant_id for unit in unit_list}
        counts: Counter[str] = Counter()
        for sheet_name, rows in workbook.items():
            if sheet_name in NON_RECORD_TABS:
                continue
            for row_number, input_row in enumerate(rows, start=2):
                row = {str(key): _json_safe(value) for key, value in dict(input_row).items()}
                tenant_id = unit_by_code.get(str(row.get("unit_code"))) if row.get("unit_code") else None
                record_id = connection.execute(
                    text(
                        """
                        INSERT INTO integration_records
                          (group_id, tenant_id, connection_id, import_run_id, dataset, external_id, effective_at,
                           source_type, schema_version, workflow_status, payload, payload_hash, source_updated_at)
                        VALUES (:group_id, :tenant_id, :connection_id, :run_id, :dataset, :external_id, :effective_at,
                                :source_type, :schema_version, :workflow_status, CAST(:payload AS jsonb), :payload_hash, :effective_at)
                        ON CONFLICT (connection_id, dataset, external_id) DO UPDATE SET
                          tenant_id = EXCLUDED.tenant_id, import_run_id = EXCLUDED.import_run_id,
                          effective_at = EXCLUDED.effective_at, source_type = EXCLUDED.source_type,
                          schema_version = EXCLUDED.schema_version, payload = EXCLUDED.payload,
                          payload_hash = EXCLUDED.payload_hash, source_updated_at = EXCLUDED.source_updated_at,
                          updated_at = now()
                        RETURNING id
                        """
                    ),
                    {
                        "group_id": str(group_id), "tenant_id": str(tenant_id) if tenant_id else None,
                        "connection_id": str(connection_id), "run_id": str(run_id), "dataset": sheet_name,
                        "external_id": str(row["external_id"]), "effective_at": _effective_at(row),
                        "source_type": str(row["source_type"]), "schema_version": str(row["schema_version"]),
                        "workflow_status": "pending_approval" if sheet_name == "Invoices_12M" else "received",
                        "payload": _canonical_json(row), "payload_hash": _sha256(row),
                    },
                ).scalar_one()
                connection.execute(
                    text(
                        """
                        INSERT INTO record_lineage
                          (group_id, record_id, import_run_id, source_file_id, source_file_name, source_sheet,
                           source_row, source_revision, source_url, source_hash)
                        VALUES (:group_id, :record_id, :run_id, :file_id, :file_name, :sheet, :row, :revision, :url, :hash)
                        ON CONFLICT (record_id, import_run_id) DO NOTHING
                        """
                    ),
                    {"group_id": str(group_id), "record_id": str(record_id), "run_id": str(run_id),
                     "file_id": str(connection_id), "file_name": source_file_name, "sheet": sheet_name,
                     "row": row_number, "revision": revision, "url": source_url, "hash": _sha256(row)},
                )
                counts[sheet_name] += 1
        connection.execute(
            text("UPDATE import_runs SET status = 'published', completed_at = now(), published_at = now(), record_counts = CAST(:counts AS jsonb), validation_summary = CAST(:summary AS jsonb) WHERE id = :run_id"),
            {"run_id": str(run_id), "counts": _canonical_json(dict(counts)), "summary": _canonical_json({"errors": 0, "validated_tabs": len(WORKBOOK_SPECS)})},
        )
        connection.execute(text("UPDATE integration_connections SET status = 'ready', source_revision = :revision, last_synced_at = now(), last_validated_at = now(), last_error_code = NULL, updated_at = now() WHERE id = :connection_id"), {"connection_id": str(connection_id), "revision": revision})
    return {"id": str(run_id), "status": "published", "record_counts": dict(counts), "idempotent": False}


def sync_google_sheet(engine: Engine, settings: Settings, group_id: UUID, connection_id: UUID, units: Iterable[GroupUnit]) -> dict[str, object]:
    workbook, file_id, revision, file_name = fetch_google_workbook(engine, settings, group_id, connection_id)
    return publish_workbook(
        engine, group_id=group_id, connection_id=connection_id, workbook=workbook, units=units,
        source_revision=revision, source_file_name=file_name,
        source_url=f"https://docs.google.com/spreadsheets/d/{file_id}",
    )


def scheduled_google_sheet_sync(engine: Engine, settings: Settings) -> dict[str, int]:
    """Sync every ready Google Sheets connection once; failures stay visible on the connection.

    The job intentionally continues to other groups after a failure.  A failed
    source cannot publish partial data because ``publish_workbook`` validates
    before its publishing phase.
    """
    with engine.begin() as connection:
        connections = connection.execute(
            text(
                """
                SELECT c.id, c.group_id, c.owner_id
                FROM integration_connections c
                WHERE c.provider = 'google_sheets' AND c.status IN ('connected', 'ready')
                  AND c.external_file_id IS NOT NULL
                """
            )
        ).mappings().all()
    result = {"synced": 0, "failed": 0}
    for item in connections:
        group_id = UUID(str(item["group_id"]))
        owner_id = UUID(str(item["owner_id"]))
        from app.groups import require_group_units

        try:
            units = require_group_units(engine, owner_id, group_id)
            sync_google_sheet(engine, settings, group_id, UUID(str(item["id"])), units)
            result["synced"] += 1
        except (IntegrationError, PermissionError):
            with group_transaction(engine, group_id) as connection:
                connection.execute(text("UPDATE integration_connections SET status = 'error', last_error_code = 'scheduled_sync_failed', updated_at = now() WHERE id = :id"), {"id": str(item["id"])})
            result["failed"] += 1
    return result


def _external_connection(engine: Engine, group_id: UUID, owner_id: UUID, provider: str, display_name: str) -> UUID:
    """Get the group-owned connection used for a non-credentialed or optional source."""
    with group_transaction(engine, group_id) as connection:
        return UUID(
            str(
                connection.execute(
                    text(
                        """
                        INSERT INTO integration_connections (group_id, owner_id, provider, status, display_name)
                        VALUES (:group_id, :owner_id, :provider, 'ready', :display_name)
                        ON CONFLICT (group_id, provider) DO UPDATE SET status = 'ready', updated_at = now()
                        RETURNING id
                        """
                    ),
                    {"group_id": str(group_id), "owner_id": str(owner_id), "provider": provider, "display_name": display_name},
                ).scalar_one()
            )
        )


def _publish_external_records(
    engine: Engine,
    *,
    group_id: UUID,
    connection_id: UUID,
    records: Iterable[tuple[str, UUID | None, str, str, Mapping[str, object], datetime | None]],
    schema_version: str,
    source_revision: str,
    source_file_name: str,
    source_url: str,
) -> dict[str, object]:
    """Atomically publish provider records that did not originate in a workbook."""
    rows = tuple(records)
    source_hash = _sha256(rows)
    with group_transaction(engine, group_id) as connection:
        existing = connection.execute(text("SELECT id, status, record_counts FROM import_runs WHERE connection_id = :connection_id AND source_revision = :revision AND schema_version = :schema_version"), {"connection_id": str(connection_id), "revision": source_revision, "schema_version": schema_version}).mappings().one_or_none()
        if existing is not None:
            return {"id": str(existing["id"]), "status": str(existing["status"]), "record_counts": dict(existing["record_counts"]), "idempotent": True}
        run_id = connection.execute(text("INSERT INTO import_runs (group_id, connection_id, source_revision, schema_version, source_hash) VALUES (:group_id, :connection_id, :revision, :schema_version, :source_hash) RETURNING id"), {"group_id": str(group_id), "connection_id": str(connection_id), "revision": source_revision, "schema_version": schema_version, "source_hash": source_hash}).scalar_one()
        counts: Counter[str] = Counter()
        for dataset, tenant_id, external_id, source_type, raw_payload, effective_at in rows:
            payload = {str(key): _json_safe(value) for key, value in raw_payload.items()}
            record_id = connection.execute(
                text(
                    """
                    INSERT INTO integration_records
                      (group_id, tenant_id, connection_id, import_run_id, dataset, external_id, effective_at,
                       source_type, schema_version, payload, payload_hash, source_updated_at)
                    VALUES (:group_id, :tenant_id, :connection_id, :run_id, :dataset, :external_id, :effective_at,
                            :source_type, :schema_version, CAST(:payload AS jsonb), :payload_hash, :effective_at)
                    ON CONFLICT (connection_id, dataset, external_id) DO UPDATE SET
                      tenant_id = EXCLUDED.tenant_id, import_run_id = EXCLUDED.import_run_id,
                      effective_at = EXCLUDED.effective_at, payload = EXCLUDED.payload,
                      payload_hash = EXCLUDED.payload_hash, source_updated_at = EXCLUDED.source_updated_at,
                      updated_at = now()
                    RETURNING id
                    """
                ),
                {"group_id": str(group_id), "tenant_id": str(tenant_id) if tenant_id else None, "connection_id": str(connection_id), "run_id": str(run_id), "dataset": dataset, "external_id": external_id, "effective_at": effective_at, "source_type": source_type, "schema_version": schema_version, "payload": _canonical_json(payload), "payload_hash": _sha256(payload)},
            ).scalar_one()
            connection.execute(text("INSERT INTO record_lineage (group_id, record_id, import_run_id, source_file_id, source_file_name, source_sheet, source_row, source_revision, source_url, source_hash) VALUES (:group_id, :record_id, :run_id, :file_id, :file_name, :sheet, NULL, :revision, :source_url, :hash) ON CONFLICT (record_id, import_run_id) DO NOTHING"), {"group_id": str(group_id), "record_id": str(record_id), "run_id": str(run_id), "file_id": str(connection_id), "file_name": source_file_name, "sheet": dataset, "revision": source_revision, "source_url": source_url, "hash": _sha256(payload)})
            counts[dataset] += 1
        connection.execute(text("UPDATE import_runs SET status = 'published', completed_at = now(), published_at = now(), record_counts = CAST(:counts AS jsonb), validation_summary = CAST(:summary AS jsonb) WHERE id = :run_id"), {"run_id": str(run_id), "counts": _canonical_json(dict(counts)), "summary": _canonical_json({"errors": 0, "provider": source_file_name})})
        connection.execute(text("UPDATE integration_connections SET status = 'ready', source_revision = :revision, last_synced_at = now(), last_validated_at = now(), last_error_code = NULL, updated_at = now() WHERE id = :connection_id"), {"connection_id": str(connection_id), "revision": source_revision})
        connection.execute(text("UPDATE integration_records SET workflow_status = 'received', updated_at = now() WHERE connection_id = :connection_id AND dataset = ANY(:datasets) AND import_run_id <> :run_id"), {"connection_id": str(connection_id), "datasets": list(counts), "run_id": str(run_id)})
    return {"id": str(run_id), "status": "published", "record_counts": dict(counts), "idempotent": False}


def sync_open_meteo_forecast(
    engine: Engine,
    *,
    group_id: UUID,
    owner_id: UUID,
    units: Iterable[GroupUnit],
    transport: httpx.BaseTransport | None = None,
) -> dict[str, object]:
    """Fetch the Open-Meteo forecast for each authorized unit and normalize it.

    This is deliberately a manual API capability for the demonstration.  The
    deployment owner must choose the appropriate Open-Meteo production plan
    before enabling a recurring commercial use case.
    """
    connection_id = _external_connection(engine, group_id, owner_id, "open_meteo", "Open-Meteo forecast")
    captured_at = datetime.now(timezone.utc)
    records: list[tuple[str, UUID | None, str, str, Mapping[str, object], datetime | None]] = []
    for unit in units:
        if unit.latitude is None or unit.longitude is None:
            continue
        params = {
            "latitude": unit.latitude, "longitude": unit.longitude,
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "forecast_days": 16, "timezone": "UTC",
        }
        if transport is None:
            response = httpx.get(OPEN_METEO_FORECAST_URL, params=params, timeout=20)
        else:
            with httpx.Client(transport=transport, timeout=20) as client:
                response = client.get(OPEN_METEO_FORECAST_URL, params=params)
        if response.is_error:
            raise IntegrationError(f"Open-Meteo forecast failed for {unit.unit_code}")
        body = response.json()
        hourly = body.get("hourly", {})
        times = hourly.get("time", [])
        temperatures = hourly.get("temperature_2m", [])
        precipitation = hourly.get("precipitation_probability", [])
        codes = hourly.get("weather_code", [])
        if not isinstance(times, list) or not isinstance(temperatures, list):
            raise IntegrationError("Open-Meteo returned an invalid forecast response")
        for index, raw_time in enumerate(times):
            if index >= len(temperatures):
                break
            observed_at = datetime.fromisoformat(str(raw_time)).replace(tzinfo=timezone.utc)
            records.append(("Weather_Forecast_16D", unit.tenant_id, f"{unit.unit_code}-open-meteo-{observed_at.isoformat()}", "public_api", {"external_id": f"{unit.unit_code}-open-meteo-{observed_at.isoformat()}", "unit_code": unit.unit_code, "forecast_at": observed_at.isoformat(), "temperature_f": temperatures[index], "precipitation_probability": precipitation[index] if index < len(precipitation) else None, "weather_code": codes[index] if index < len(codes) else None, "latitude": unit.latitude, "longitude": unit.longitude, "model": body.get("timezone_abbreviation", "Open-Meteo"), "captured_at": captured_at.isoformat(), "source_url": SOURCE_OPEN_METEO}, observed_at))
    revision = f"open-meteo:{captured_at.strftime('%Y%m%d%H')}"
    return _publish_external_records(engine, group_id=group_id, connection_id=connection_id, records=records, schema_version="open_meteo.v1", source_revision=revision, source_file_name="Open-Meteo Forecast API", source_url=SOURCE_OPEN_METEO)


def sync_yelp_market(
    engine: Engine,
    settings: Settings,
    *,
    group_id: UUID,
    owner_id: UUID,
    units: Iterable[GroupUnit],
    transport: httpx.BaseTransport | None = None,
) -> dict[str, object]:
    """Optionally refresh market observations, retaining the Sheets baseline as fallback."""
    if not settings.yelp_enabled or not settings.yelp_api_key:
        raise IntegrationError("Yelp market sync is not enabled")
    connection_id = _external_connection(engine, group_id, owner_id, "yelp", "Yelp market observations")
    captured_at = datetime.now(timezone.utc)
    records: list[tuple[str, UUID | None, str, str, Mapping[str, object], datetime | None]] = []
    client = YelpPlacesClient(settings.yelp_api_key, transport=transport)
    try:
        for unit in units:
            if unit.latitude is None or unit.longitude is None:
                continue
            businesses = client.search(CompetitorSearch(latitude=Decimal(str(unit.latitude)), longitude=Decimal(str(unit.longitude)), term="steakhouse"), limit=20)
            for business in businesses:
                external_id = f"{unit.unit_code}-yelp-{business.provider_id}"
                records.append(("Market_Baseline", unit.tenant_id, external_id, "public_api", {"external_id": external_id, "unit_code": unit.unit_code, "observed_at": captured_at.isoformat(), "business_name": business.name, "provider_id": business.provider_id, "rating": business.rating, "review_count": business.review_count, "price": business.price, "categories": list(business.categories), "business_url": business.url, "observation_source": "Yelp API", "source_url": SOURCE_YELP_API}, captured_at))
    except YelpAPIError as error:
        raise IntegrationError("Yelp market sync failed; the dated Sheets baseline remains available") from error
    finally:
        client.close()
    revision = f"yelp:{captured_at.strftime('%Y%m%d%H%M')}"
    return _publish_external_records(engine, group_id=group_id, connection_id=connection_id, records=records, schema_version="yelp.v1", source_revision=revision, source_file_name="Yelp Fusion API", source_url=SOURCE_YELP_API)


def _safe_number(value: object) -> str:
    return f"{_decimal(value):.2f}"


def group_overview(engine: Engine, group_id: UUID) -> dict[str, object]:
    """One bounded, source-labelled overview used by the UI, MCP and AI gateway."""
    with group_transaction(engine, group_id) as connection:
        latest = connection.execute(text("SELECT max(effective_at)::date FROM integration_records WHERE dataset = 'Sales_Daily_24M'" )).scalar_one_or_none()
        cutoff = (latest - timedelta(days=27)) if isinstance(latest, date) else date.today() - timedelta(days=27)
        metrics = connection.execute(
            text(
                """
                SELECT gu.unit_code, gu.display_name,
                  COALESCE(sum((r.payload->>'net_sales')::numeric) FILTER (WHERE r.dataset = 'Sales_Daily_24M' AND r.effective_at::date >= :cutoff), 0) AS net_sales,
                  COALESCE(sum((r.payload->>'labor_cost')::numeric) FILTER (WHERE r.dataset = 'Labor_Daily_24M' AND r.effective_at::date >= :cutoff), 0) AS labor_cost,
                  COALESCE(sum((r.payload->>'net_payout')::numeric) FILTER (WHERE r.dataset = 'Delivery_Daily_24M' AND r.effective_at::date >= :cutoff), 0) AS delivery_payout,
                  max(r.ingested_at) AS freshness
                FROM group_units AS gu
                LEFT JOIN integration_records AS r ON r.tenant_id = gu.tenant_id
                WHERE gu.active GROUP BY gu.unit_code, gu.display_name ORDER BY gu.unit_code
                """
            ),
            {"cutoff": cutoff},
        ).mappings().all()
        risk_count = connection.execute(text("SELECT count(*) FROM integration_records WHERE dataset = 'Inventory_Counts_90D' AND COALESCE((payload->>'on_hand')::numeric, 0) < COALESCE((payload->>'par_level')::numeric, 0)" )).scalar_one()
        pending_invoices = connection.execute(text("SELECT count(*) FROM integration_records WHERE dataset = 'Invoices_12M' AND workflow_status = 'pending_approval'" )).scalar_one()
    unit_metrics = []
    for row in metrics:
        net_sales = _decimal(row["net_sales"])
        labor = _decimal(row["labor_cost"])
        unit_metrics.append({
            "unit_code": str(row["unit_code"]), "display_name": str(row["display_name"]),
            "net_sales_28d": _safe_number(net_sales), "labor_cost_28d": _safe_number(labor),
            "labor_percent": _safe_number((labor / net_sales * 100) if net_sales else Decimal("0")),
            "delivery_payout_28d": _safe_number(row["delivery_payout"]),
            "freshness": row["freshness"].isoformat() if row["freshness"] else None,
        })
    return {
        "period": {"start": cutoff.isoformat(), "end": latest.isoformat() if isinstance(latest, date) else None},
        "units": unit_metrics,
        "totals": {
            "net_sales_28d": _safe_number(sum((_decimal(row["net_sales"]) for row in metrics), Decimal("0"))),
            "inventory_risks": int(risk_count), "pending_invoice_approvals": int(pending_invoices),
        },
        "sources": ["Postgres canonical integration records", "source-labelled lineage available per record"],
        "freshness": max((item["freshness"] for item in unit_metrics if item["freshness"]), default=None),
    }


def compare_units(engine: Engine, group_id: UUID) -> dict[str, object]:
    overview = group_overview(engine, group_id)
    return {"period": overview["period"], "units": overview["units"], "calculation": "28-day sums by unit from normalized canonical records", "freshness": overview["freshness"]}


def sales_trend(engine: Engine, group_id: UUID, days: int = 30) -> dict[str, object]:
    bounded_days = min(max(days, 1), 730)
    with group_transaction(engine, group_id) as connection:
        rows = connection.execute(text("SELECT effective_at::date AS business_date, COALESCE(sum((payload->>'net_sales')::numeric), 0) AS net_sales FROM integration_records WHERE dataset = 'Sales_Daily_24M' GROUP BY effective_at::date ORDER BY business_date DESC LIMIT :days"), {"days": bounded_days}).mappings().all()
        freshness = connection.execute(text("SELECT max(ingested_at) FROM integration_records WHERE dataset = 'Sales_Daily_24M'" )).scalar_one()
    return {"period_days": bounded_days, "series": [{"date": row["business_date"].isoformat(), "net_sales": _safe_number(row["net_sales"])} for row in reversed(rows)], "calculation": "sum net_sales by business date", "freshness": freshness.isoformat() if freshness else None, "sources": ["Sales_Daily_24M"]}


def labor_variance(engine: Engine, group_id: UUID) -> dict[str, object]:
    overview = group_overview(engine, group_id)
    return {"period": overview["period"], "units": [{key: row[key] for key in ("unit_code", "display_name", "net_sales_28d", "labor_cost_28d", "labor_percent", "freshness")} for row in overview["units"]], "benchmark_context": "NRA labor-cost context belongs in source lineage; it is not an automatic target.", "calculation": "labor_cost / net_sales over the last available 28 days", "sources": ["Labor_Daily_24M", "Sales_Daily_24M"]}


def inventory_risks(engine: Engine, group_id: UUID, limit: int = 30) -> dict[str, object]:
    with group_transaction(engine, group_id) as connection:
        rows = connection.execute(text("SELECT r.id, gu.unit_code, gu.display_name, r.payload, r.ingested_at FROM integration_records r JOIN group_units gu ON gu.tenant_id = r.tenant_id WHERE r.dataset = 'Inventory_Counts_90D' AND COALESCE((r.payload->>'on_hand')::numeric, 0) < COALESCE((r.payload->>'par_level')::numeric, 0) ORDER BY r.effective_at DESC LIMIT :limit"), {"limit": min(max(limit, 1), 100)}).mappings().all()
    return {"items": [{"record_id": str(row["id"]), "unit_code": str(row["unit_code"]), "unit": str(row["display_name"]), "item_name": row["payload"].get("item_name"), "on_hand": row["payload"].get("on_hand"), "par_level": row["payload"].get("par_level"), "freshness": row["ingested_at"].isoformat()} for row in rows], "calculation": "on_hand below par_level", "sources": ["Inventory_Counts_90D"]}


def invoice_exceptions(engine: Engine, group_id: UUID, limit: int = 30) -> dict[str, object]:
    with group_transaction(engine, group_id) as connection:
        rows = connection.execute(text("SELECT r.id, r.import_run_id, gu.unit_code, gu.display_name, r.workflow_status, r.payload, r.ingested_at FROM integration_records r JOIN group_units gu ON gu.tenant_id = r.tenant_id WHERE r.dataset = 'Invoices_12M' AND r.workflow_status = 'pending_approval' ORDER BY r.effective_at DESC LIMIT :limit"), {"limit": min(max(limit, 1), 100)}).mappings().all()
    return {"invoices": [{"record_id": str(row["id"]), "import_run_id": str(row["import_run_id"]), "unit_code": str(row["unit_code"]), "unit": str(row["display_name"]), "invoice_number": row["payload"].get("invoice_number"), "vendor_name": row["payload"].get("vendor_name"), "invoice_total": row["payload"].get("invoice_total"), "status": str(row["workflow_status"]), "freshness": row["ingested_at"].isoformat()} for row in rows], "calculation": "invoices that were imported and await a human decision", "sources": ["Invoices_12M"]}


def supplier_price_changes(engine: Engine, group_id: UUID, limit: int = 30) -> dict[str, object]:
    with group_transaction(engine, group_id) as connection:
        rows = connection.execute(text("SELECT gu.unit_code, r.payload, r.effective_at, r.ingested_at FROM integration_records r JOIN group_units gu ON gu.tenant_id = r.tenant_id WHERE r.dataset = 'Invoice_Lines_12M' ORDER BY r.effective_at ASC NULLS LAST" )).mappings().all()
    changes: list[dict[str, object]] = []
    prior: dict[tuple[str, str, str], Decimal] = {}
    for row in rows:
        payload = row["payload"]
        item = str(payload.get("item_name") or payload.get("description") or "Unspecified item")
        vendor = str(payload.get("vendor_name") or "Unspecified vendor")
        unit_code = str(row["unit_code"])
        price = _decimal(payload.get("unit_price"))
        key = (unit_code, vendor, item)
        previous = prior.get(key)
        if previous is not None and previous != 0:
            delta = (price - previous) / previous * Decimal("100")
            changes.append({"unit_code": unit_code, "item": item, "vendor_name": vendor, "previous_unit_price": _safe_number(previous), "unit_price": _safe_number(price), "change_percent": _safe_number(delta), "effective_at": row["effective_at"].isoformat() if row["effective_at"] else None, "freshness": row["ingested_at"].isoformat()})
        prior[key] = price
    return {"price_changes": list(reversed(changes[-min(max(limit, 1), 100):])), "calculation": "same unit, vendor, and item: current unit_price vs prior normalized invoice line", "sources": ["Invoice_Lines_12M"]}


def weather_impact(engine: Engine, group_id: UUID) -> dict[str, object]:
    with group_transaction(engine, group_id) as connection:
        rows = connection.execute(text("SELECT gu.unit_code, r.payload, r.effective_at, r.source_type, r.ingested_at FROM integration_records r JOIN group_units gu ON gu.tenant_id = r.tenant_id WHERE r.dataset IN ('Weather_History_24M', 'Weather_Forecast_16D') ORDER BY r.effective_at DESC NULLS LAST LIMIT 48")).mappings().all()
    return {"observations": [{"unit_code": str(row["unit_code"]), "observed_at": row["effective_at"].isoformat() if row["effective_at"] else None, "temperature_f": row["payload"].get("temperature_f"), "precipitation_probability": row["payload"].get("precipitation_probability"), "source_type": str(row["source_type"]), "freshness": row["ingested_at"].isoformat()} for row in rows], "calculation": "weather observations and forecast are not treated as causal claims without an explicit analysis", "sources": ["Weather_History_24M", "Weather_Forecast_16D"]}


def market_snapshot(engine: Engine, group_id: UUID) -> dict[str, object]:
    with group_transaction(engine, group_id) as connection:
        rows = connection.execute(text("SELECT gu.unit_code, r.payload, r.source_type, r.effective_at, r.ingested_at FROM integration_records r LEFT JOIN group_units gu ON gu.tenant_id = r.tenant_id WHERE r.dataset = 'Market_Baseline' ORDER BY r.effective_at DESC NULLS LAST LIMIT 100")).mappings().all()
    return {"observations": [{"unit_code": row["unit_code"], "business_name": row["payload"].get("business_name"), "rating": row["payload"].get("rating"), "review_count": row["payload"].get("review_count"), "observed_at": row["effective_at"].isoformat() if row["effective_at"] else None, "source_type": str(row["source_type"]), "freshness": row["ingested_at"].isoformat()} for row in rows], "calculation": "source-labelled market observations; Sheets baseline is retained when Yelp is unavailable", "sources": ["Market_Baseline"]}


def data_lineage(engine: Engine, group_id: UUID, *, record_id: UUID | None = None, limit: int = 100) -> dict[str, object]:
    query = "SELECT l.record_id, r.dataset, r.external_id, r.source_type, l.source_file_name, l.source_sheet, l.source_row, l.source_revision, l.source_url, l.imported_at FROM record_lineage l JOIN integration_records r ON r.id = l.record_id WHERE l.group_id = :group_id"
    params: dict[str, object] = {"group_id": str(group_id), "limit": min(max(limit, 1), 250)}
    if record_id:
        query += " AND l.record_id = :record_id"
        params["record_id"] = str(record_id)
    query += " ORDER BY l.imported_at DESC LIMIT :limit"
    with group_transaction(engine, group_id) as connection:
        rows = connection.execute(text(query), params).mappings().all()
    return {"records": [{"record_id": str(row["record_id"]), "dataset": str(row["dataset"]), "external_id": str(row["external_id"]), "source_type": str(row["source_type"]), "file_name": row["source_file_name"], "sheet": row["source_sheet"], "row": row["source_row"], "revision": row["source_revision"], "source_url": row["source_url"], "imported_at": row["imported_at"].isoformat()} for row in rows]}


def list_import_runs(engine: Engine, group_id: UUID, connection_id: UUID) -> list[dict[str, object]]:
    with group_transaction(engine, group_id) as connection:
        rows = connection.execute(text("SELECT id, status, source_revision, schema_version, started_at, completed_at, published_at, record_counts, validation_summary FROM import_runs WHERE connection_id = :connection_id ORDER BY started_at DESC LIMIT 30"), {"connection_id": str(connection_id)}).mappings().all()
    return [{"id": str(row["id"]), "status": str(row["status"]), "source_revision": row["source_revision"], "schema_version": str(row["schema_version"]), "started_at": row["started_at"].isoformat(), "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None, "published_at": row["published_at"].isoformat() if row["published_at"] else None, "record_counts": dict(row["record_counts"]), "validation_summary": dict(row["validation_summary"])} for row in rows]


def create_proposal(engine: Engine, group_id: UUID, tenant_id: UUID, kind: str, payload: Mapping[str, object], source_record_id: UUID | None = None) -> dict[str, object]:
    if kind not in {"task", "shift", "production_plan", "inventory_count", "invoice_approval"}:
        raise IntegrationError("proposal type is not supported")
    with group_transaction(engine, group_id) as connection:
        valid_unit = connection.execute(text("SELECT 1 FROM group_units WHERE tenant_id = :tenant_id AND active"), {"tenant_id": str(tenant_id)}).scalar_one_or_none()
        if valid_unit is None:
            raise IntegrationError("proposal must target an active group unit")
        proposal_id = connection.execute(text("INSERT INTO action_proposals (group_id, tenant_id, kind, payload, source_record_id) VALUES (:group_id, :tenant_id, :kind, CAST(:payload AS jsonb), :record_id) RETURNING id, created_at"), {"group_id": str(group_id), "tenant_id": str(tenant_id), "kind": kind, "payload": _canonical_json(dict(payload)), "record_id": str(source_record_id) if source_record_id else None}).mappings().one()
    return {"id": str(proposal_id["id"]), "status": "proposed", "kind": kind, "created_at": proposal_id["created_at"].isoformat()}


def approve_invoice_proposal(engine: Engine, group_id: UUID, proposal_id: UUID, owner_id: UUID, reason: str) -> dict[str, object]:
    if len(reason.strip()) < 3:
        raise IntegrationError("approval reason is required")
    with group_transaction(engine, group_id) as connection:
        proposal = connection.execute(text("SELECT id, tenant_id, kind, status, source_record_id FROM action_proposals WHERE id = :proposal_id"), {"proposal_id": str(proposal_id)}).mappings().one_or_none()
        if proposal is None or proposal["kind"] != "invoice_approval" or proposal["status"] != "proposed" or proposal["source_record_id"] is None:
            raise IntegrationError("a pending invoice approval proposal is required")
        record = connection.execute(text("SELECT id, import_run_id, dataset, workflow_status FROM integration_records WHERE id = :record_id"), {"record_id": str(proposal["source_record_id"])}).mappings().one_or_none()
        if record is None or record["dataset"] != "Invoices_12M" or record["workflow_status"] != "pending_approval":
            raise IntegrationError("source invoice is not awaiting approval")
        connection.execute(text("UPDATE action_proposals SET status = 'approved', resolved_at = now(), resolved_by_owner_id = :owner_id, resolution_reason = :reason WHERE id = :proposal_id"), {"proposal_id": str(proposal_id), "owner_id": str(owner_id), "reason": reason.strip()})
        connection.execute(text("UPDATE integration_records SET workflow_status = 'approved', updated_at = now() WHERE id = :record_id"), {"record_id": str(record["id"])})
        event_id = connection.execute(text("INSERT INTO invoice_approval_events (group_id, tenant_id, proposal_id, source_record_id, import_run_id, owner_id, reason, operational_effect) VALUES (:group_id, :tenant_id, :proposal_id, :record_id, :run_id, :owner_id, :reason, CAST(:effect AS jsonb)) RETURNING id, approved_at"), {"group_id": str(group_id), "tenant_id": str(proposal["tenant_id"]), "proposal_id": str(proposal_id), "record_id": str(record["id"]), "run_id": str(record["import_run_id"]), "owner_id": str(owner_id), "reason": reason.strip(), "effect": _canonical_json({"status": "approved_for_operational_receiving", "payment": "not_initiated"})}).mappings().one()
    return {"id": str(event_id["id"]), "status": "approved", "approved_at": event_id["approved_at"].isoformat(), "operational_effect": "approved for operational receiving; no payment, purchase, or message was sent"}


def create_mcp_token(engine: Engine, group_id: UUID, owner_id: UUID, label: str, expires_at: datetime | None = None) -> dict[str, object]:
    if not label.strip():
        raise IntegrationError("MCP token label is required")
    now = datetime.now(timezone.utc)
    if expires_at is None:
        expires_at = now + timedelta(hours=24)
    elif expires_at.tzinfo is None:
        raise IntegrationError("MCP token expiry must include a timezone")
    else:
        expires_at = expires_at.astimezone(timezone.utc)
    if expires_at <= now or expires_at > now + timedelta(hours=24):
        raise IntegrationError("MCP token expiry must be within the next 24 hours")
    token = f"mgr_mcp_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with group_transaction(engine, group_id) as connection:
        token_id = connection.execute(text("INSERT INTO mcp_access_tokens (group_id, owner_id, label, token_hash, expires_at) VALUES (:group_id, :owner_id, :label, :token_hash, :expires_at) RETURNING id, created_at"), {"group_id": str(group_id), "owner_id": str(owner_id), "label": label.strip(), "token_hash": token_hash, "expires_at": expires_at}).mappings().one()
    return {"id": str(token_id["id"]), "token": token, "label": label.strip(), "created_at": token_id["created_at"].isoformat(), "expires_at": expires_at.isoformat() if expires_at else None}


def resolve_mcp_token(engine: Engine, bearer: str) -> tuple[UUID, UUID] | None:
    digest = hashlib.sha256(bearer.encode()).hexdigest()
    with engine.begin() as connection:
        row = connection.execute(text("SELECT owner_id, group_id FROM resolve_manager_mcp_token(:token_hash)"), {"token_hash": digest}).mappings().one_or_none()
        if row is None:
            return None
        connection.execute(text("SELECT mark_manager_mcp_token_used(:token_hash)"), {"token_hash": digest})
    return UUID(str(row["owner_id"])), UUID(str(row["group_id"]))


def revoke_mcp_token(engine: Engine, group_id: UUID, token_id: UUID) -> bool:
    with group_transaction(engine, group_id) as connection:
        return bool(connection.execute(text("UPDATE mcp_access_tokens SET revoked_at = now() WHERE id = :token_id AND revoked_at IS NULL"), {"token_id": str(token_id)}).rowcount)
