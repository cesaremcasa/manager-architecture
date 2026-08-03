# Manager

![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-FF4438?style=for-the-badge&logo=redis&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![Status](https://img.shields.io/badge/status-in%20development-E36209?style=for-the-badge)
![License](https://img.shields.io/badge/license-MIT-3C9A5F?style=for-the-badge)

![Manager](https://www.mycelliumlab.com/assets/project-manager-hero.jpg)

**Forward what matters. Know what to do next.**

A restaurant owner opens fifty emails, three POS exports and a stack of invoices to work out how yesterday went. Manager reads all of it and gives back one page: what changed, and what to do about it.

A POS records what happened. Manager explains it.

This repository documents the architecture. The implementation lives in a private repository.

**[Project page on Mycellium Lab](https://www.mycelliumlab.com/manager)**

---

## The problem

Independent restaurant owners are not short on data. They are short on a place where the data means something. Delivery platform reports, POS summaries, supplier invoices, bank notifications and supplier emails all arrive daily, in different formats, in different places, and none of them answer the only question the owner actually has, which is what needs attention today.

Manager is not another POS and does not try to be. It sits above the systems that already record events and turns them into a short, auditable brief plus a list of next actions.

---

## Product principles

These constrain the architecture. They are the reason the system is built the way it is.

**Code calculates. The LLM narrates.**
Every financial value originates in deterministic SQL and Python. The model classifies, extracts and explains. It never produces a number that someone will act on.

**Postgres is truth. Redis is transport.**
Jobs are recoverable and reconciled from durable state, never from a queue that might have dropped something.

**Raw email is retained.**
MIME persistence exists so any derived conclusion can be audited back to its source and reprocessed when parsing improves.

**Tenant boundaries are enforced server side.**
`tenant_id` never comes from the client and never comes from the model.

**Human corrections win.**
Automation is reviewable and never overwrites an explicit human decision.

**Money stays precise.**
Financial calculations use `NUMERIC` and `Decimal`. Business dates use the restaurant's operating-day cutoff, not midnight, because a restaurant's Tuesday ends when the kitchen closes.

---

## Why "code calculates, the LLM narrates" matters

This is the decision the whole system turns on.

It is tempting to let a language model read an invoice and report the total. It reads the invoice correctly almost every time. Almost every time is the problem: the owner has no way to know which time was the exception, and the number is going into a decision about payroll or an order.

So the model is allowed to classify a document, extract candidate fields, and write the sentence that explains a variance. It is not allowed to be the source of the variance. The number comes from a query. If the query and the narration disagree, the query is right.

The same rule as the rest of the lab, applied to a different domain: the model proposes, deterministic code decides.

---

## Architecture

```
Email / POS exports / invoices / delivery reports
-> Ingestion, raw MIME retained, never discarded
-> Classification, the model assigns type and extracts candidate fields
-> Deterministic layer, SQL and Python compute every value
-> Daily brief and next actions, narration on top of computed truth
```

| Component | Technology | Role |
| --- | --- | --- |
| API and domain | FastAPI | Service layer, domain logic, migrations, tests |
| Workers | Background jobs | Ingestion, classification, reconciliation |
| Source of truth | Postgres | Durable state, tenant isolation, financial precision |
| Transport | Redis | Job delivery, never authoritative |
| Frontend | Web dashboard | Operations surface for the owner |
| Runtime | Docker | Local parity with deployment |

Repository layout in the private implementation:

```
apps/web/          Product frontend and dashboard routes
apps/api/          FastAPI service, domain logic, workers, migrations, tests
packages/shared/   Shared contracts and utilities
infra/             Local Postgres, Redis, API, worker, web
docs/              Product, architecture, operations, delivery records
```

---

## Status

The Operations v4 frontend is implemented and documented. The public product entry and the internal dashboard are reachable through Mycellium Lab.

When the API is unavailable, the frontend deliberately shows a clearly labeled demo-data state rather than an empty screen or a silent failure. API, authentication, tenant data and background workers are separate product responsibilities and are not hosted by the Lab frontend.

---

## Why this is public

The implementation is private because it handles real tenant data. The design is public because the interesting part is not the code.

Anyone can wire an LLM to an inbox. The question is what you let it decide. This repository is the answer to that question for a domain where a wrong number costs someone their margin.

Part of [Mycellium Lab](https://www.mycelliumlab.com). See the [Manager project page](https://www.mycelliumlab.com/manager).

## License

MIT. See [LICENSE](LICENSE).

---

**Cesar Augusto** · AI Systems Engineer, Mycellium Lab
[GitHub](https://github.com/cesaremcasa) · [LinkedIn](https://www.linkedin.com/in/cesar-augusto-22943a351/) · [korvo.dev](https://korvo.dev)
