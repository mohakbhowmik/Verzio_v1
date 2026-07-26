# Verzio Studio — AI Context

## Purpose

This document is the authoritative context for AI assistants working on Verzio Studio.

Read this file completely before making architectural or implementation suggestions.

When this document conflicts with assumptions, this document wins.

---

# Prime Directive

The current objective is **not** to build the perfect software.

The objective is to ship an MVP that businesses are willing to pay ₹1,000–₹2,000 per month for as quickly as reasonably possible.

Every engineering decision should maximize the probability of reaching paying customers.

Avoid premature optimization.

Avoid unnecessary abstractions.

Avoid rewriting working code.

Avoid large refactors.

Ship first.

Improve later.

---

# Product

Verzio Studio is a multi-tenant SaaS platform for appointment-based businesses.

Target customers include:

- Salons
- Barbers
- Clinics
- Spas
- Beauty Studios

Customers book appointments through WhatsApp.

Business owners approve and manage bookings.

Owners can configure services, availability and booking behavior.

Reports and operational automation are part of the product.

---

# Three Stakeholders

Every decision should optimize for these three stakeholders.

## 1. Business Owner

The paying customer.

Objective:

Deliver enough value that paying ₹1,000–₹2,000/month feels justified.

Examples:

- Easy appointment management
- Control over bookings
- Professional experience
- Automation
- Time savings

---

## 2. Customer

Booking should be effortless.

No app.

No account.

No learning curve.

Simply message on WhatsApp.

---

## 3. Verzio Operator

The platform should remain manageable by one or two people.

Future growth should not require proportional increases in support staff.

Operational simplicity is a product feature.

---

# Engineering Priorities

Highest priority:

1. Reach paying customers.
2. Deliver obvious customer value.
3. Reliability.
4. Simplicity.
5. Maintainability.
6. Code elegance.

Never reverse this order unless explicitly instructed.

---

# Development Workflow

Always inspect code before suggesting modifications.

Never assume file contents.

Always identify:

- file
- function
- exact location
- exact code to replace
- replacement code
- reason

Make one atomic change.

Test.

Only then continue.

Never modify multiple files simultaneously unless requested.

---

# Coding Philosophy

Preserve existing architecture.

Do not rewrite working systems.

Prefer incremental improvements.

Do not introduce unnecessary dependencies.

Consistency is preferred over perfection.

---

# Architecture

FastAPI

↓

Booking Runtime

↓

Booking Engine

↓

Database

Booking Runtime controls conversation flow.

Booking Engine contains business rules.

Database is the source of truth.

Business logic should not live inside templates.

---

# Multi-Tenant Rule

Every business is isolated.

Never introduce cross-tenant queries.

Never assume only one business exists.

Tenant isolation is mandatory.

---

# Current Goal

Finish Version 1 MVP.

After MVP:

Desktop polish

↓

Responsive UI

↓

Deployment

↓

Pilot customers

---

# Founder Vision

Verzio Studio is intended to become a sustainable SaaS business that can be operated efficiently by a very small team.

Its purpose is to generate recurring revenue that funds progressively larger software ventures.

Verzio is the first company, not the final destination.

Future ambitions are intentionally larger than Verzio itself, but they must never distract from shipping the MVP.

---

# Working Style

The founder prefers:

- One milestone at a time
- One file at a time
- One change at a time

Avoid overwhelming responses.

Be implementation-focused.

Recommend the fastest reasonable path to a paying customer.