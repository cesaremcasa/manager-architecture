import json
import hashlib
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db import tenant_transaction
from app.domain.brief import BriefFacts, render_validated_brief
from app.domain.metrics import build_sales_insights, compute_daily_metrics
from app.domain.classification import classify_subject
from app.domain.sales import Sale, aggregate_sales


CLASSIFIER_VERSION = "rules-v1"


def _classification_input_hash(sender: str, subject: str) -> str:
    payload = f"{sender.casefold()}\0{subject}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def classify_inbox_item(engine: Engine, tenant_id: UUID, inbox_item_id: UUID) -> str:
    with tenant_transaction(engine, tenant_id) as connection:
        item = connection.execute(
            text("SELECT sender, subject FROM inbox_items WHERE id = :item_id"),
            {"item_id": str(inbox_item_id)},
        ).mappings().one()
        feedback = connection.execute(
            text("""
                SELECT category
                FROM classification_feedback
                WHERE tenant_id = :tenant_id AND inbox_item_id = :item_id
            """),
            {"tenant_id": str(tenant_id), "item_id": str(inbox_item_id)},
        ).scalar_one_or_none()
        if feedback is not None:
            category = str(feedback)
            confidence = 1.0
            reason = "human feedback"
            source = "human"
        else:
            result = classify_subject(str(item["subject"]), str(item["sender"]))
            category = result.category.value
            confidence = result.confidence
            reason = result.reason
            source = "classifier"
        input_hash = _classification_input_hash(str(item["sender"]), str(item["subject"]))
        connection.execute(
            text(
                """
                INSERT INTO classification_runs
                  (tenant_id, inbox_item_id, classifier_version, category, confidence, reason, source, input_hash)
                VALUES (:tenant_id, :item_id, :version, :category, :confidence, :reason, :source, :input_hash)
                ON CONFLICT (tenant_id, inbox_item_id, classifier_version, source, input_hash)
                WHERE input_hash IS NOT NULL
                DO NOTHING
                """
            ),
            {
                "tenant_id": str(tenant_id),
                "item_id": str(inbox_item_id),
                "version": CLASSIFIER_VERSION,
                "category": category,
                "confidence": confidence,
                "reason": reason,
                "source": source,
                "input_hash": input_hash,
            },
        )
        connection.execute(
            text("""
                UPDATE inbox_items
                SET category = :category, confidence = :confidence, status = 'classified'
                WHERE id = :item_id
            """),
            {"category": category, "confidence": confidence, "item_id": str(inbox_item_id)},
        )
        return category


def reclassify_inbox_item(engine: Engine, tenant_id: UUID, inbox_item_id: UUID) -> str:
    """Run the current classifier again without duplicating an identical run."""
    return classify_inbox_item(engine, tenant_id, inbox_item_id)


def build_daily_brief(engine: Engine, tenant_id: UUID, target_date: date) -> str:
    with tenant_transaction(engine, tenant_id) as connection:
        restaurant = connection.execute(
            text("SELECT timezone FROM restaurants WHERE id = :tenant_id"),
            {"tenant_id": str(tenant_id)},
        ).mappings().one()
        rows = connection.execute(
            text("""
                SELECT occurred_at, gross_sales, discounts, comps, voids, refunds, tax, tips, service_charges
                FROM sales
            """)
        ).mappings().all()
        sales = [
            Sale(
                occurred_at=row["occurred_at"],
                gross_sales=Decimal(row["gross_sales"]),
                discounts=Decimal(row["discounts"]),
                comps=Decimal(row["comps"]),
                voids=Decimal(row["voids"]),
                refunds=Decimal(row["refunds"]),
                tax=Decimal(row["tax"]),
                tips=Decimal(row["tips"]),
                service_charges=Decimal(row["service_charges"]),
            )
            for row in rows
        ]
        timezone = str(restaurant["timezone"])
        comparable = [
            aggregate_sales(sales, target_date - date.resolution * (7 * week), timezone)["net_sales"]
            for week in range(1, 5)
        ]
        metrics = compute_daily_metrics(sales, target_date, timezone, comparable)
        facts = BriefFacts(target_date, metrics.net_sales, metrics.sale_count, metrics.prior_same_weekday_sales)
        rendered = render_validated_brief(facts)
        payload = {
            "business_date": target_date.isoformat(),
            "net_sales": str(metrics.net_sales),
            "sale_count": metrics.sale_count,
            "prior_same_weekday_sales": str(metrics.prior_same_weekday_sales),
            "gross_sales": str(metrics.gross_sales),
            "tax": str(metrics.tax),
            "tips": str(metrics.tips),
            "service_charges": str(metrics.service_charges),
        }
        connection.execute(
            text(
                """
                INSERT INTO daily_briefs (tenant_id, business_date, facts, rendered_text)
                VALUES (:tenant_id, :business_date, CAST(:facts AS jsonb), :rendered_text)
                ON CONFLICT (tenant_id, business_date)
                DO UPDATE SET facts = EXCLUDED.facts, rendered_text = EXCLUDED.rendered_text, created_at = now()
                """
            ),
            {
                "tenant_id": str(tenant_id),
                "business_date": target_date,
                "facts": json.dumps(payload),
                "rendered_text": rendered,
            },
        )
        for insight in build_sales_insights(metrics):
            connection.execute(
                text("""
                    INSERT INTO insights
                      (tenant_id, business_date, kind, severity, source, title, payload)
                    VALUES (:tenant_id, :business_date, :kind, :severity, :source, :title, CAST(:payload AS jsonb))
                    ON CONFLICT (tenant_id, business_date, kind, source)
                    DO UPDATE SET severity = EXCLUDED.severity, title = EXCLUDED.title,
                      payload = EXCLUDED.payload, created_at = now()
                """),
                {
                    "tenant_id": str(tenant_id),
                    "business_date": insight.business_date,
                    "kind": insight.kind,
                    "severity": insight.severity.value,
                    "source": insight.source,
                    "title": insight.title,
                    "payload": json.dumps(insight.as_payload()),
                },
            )
        return rendered
