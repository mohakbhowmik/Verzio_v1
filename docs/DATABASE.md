# Verzio Database

## Core Business Tables



# Businesses

Purpose
-------
Represents one customer business (tenant).

Every salon, clinic or spa using Verzio gets one row here.

Owner
-----
Platform

Children
--------




# Services

Purpose
-------
Stores all services offered by a business.

Examples

- Haircut
- Beard Trim
- Hair Spa

Owner
-----
Business

Primary Key
-----------
id

Foreign Keys
------------
business_id → businesses.id

Used By
-------
- Booking Engine
- Business Portal
- Reports
- Appointments








## 3. Staff

### Purpose

Stores employees.

### Owner

Business

### Decision

**Deferred for MVP**

Businesses are **not required** to assign appointments to staff.

The table remains in the database but will not be used in V1.

### Status

🟡 Deferred

---








- ## 4. Appointments

### Purpose

Stores every customer booking.

### Owner

Business

### Primary Key

- id

### Foreign Keys

- business_id → businesses.id
- service_id → services.id
- staff_id → optional (ignored in MVP)

### Important Fields

| Field | Purpose |
|--------|---------|
| id | Primary Key |
| business_id | Business Owner |
| service_id | Selected Service |
| customer_phone | Primary Customer Identifier |
| customer_name | Ignored in MVP |
| appointment_time | Booking Time |
| status | Pending / Confirmed |
| created_at | Creation Timestamp |

### Used By

- WhatsApp Booking
- Business Portal
- Reports
- Platform Admin

### Engineering Decision

Customer Phone is the primary identifier.

Customer Name is ignored for MVP because WhatsApp profile names are unreliable.

### Status

✅ Approved







## 5. Subscriptions

### Purpose

Stores the billing plan for each business.

### Owner

Platform

### Primary Key

- id

### Foreign Key

- business_id → businesses.id

### Important Fields

| Field | Purpose |
|--------|---------|
| id | Primary Key |
| business_id | Business Owner |
| plan | Current Subscription Plan |
| status | Active / Trial / Cancelled |
| monthly_amount | Monthly Subscription Fee |
| next_billing_date | Renewal Date |
| notes | Internal Notes |
| updated_at | Last Updated Timestamp |

### Used By

- Billing
- Subscription Management
- Business Activation

### Engineering Decision

A business may have only one active subscription.

Payment history is stored separately in `payment_records`.

### Status

✅ Approved






-## 6. Payment Records

### Purpose

Stores the history of subscription payments made by businesses.

### Owner

Platform

### Primary Key

- id

### Foreign Key

- business_id → businesses.id

### Important Fields

| Field | Purpose |
|--------|---------|
| id | Primary Key |
| business_id | Business Owner |
| amount | Payment Amount |
| currency | Payment Currency |
| paid_on | Payment Date |
| status | Paid / Failed / Pending |
| note | Internal Billing Notes |
| created_at | Record Creation Timestamp |

### Used By

- Billing
- Finance
- Subscription History
- Admin Portal

### Engineering Decision

Payment records are immutable.

Once created, they serve as the financial history of the business.

The current subscription state is maintained in the `subscriptions` table.

### Status

✅ Approved 




Primary Key
-----------
id


- services
- staff
- appointments
- subscriptions
- payment_records

## Platform/System Tables





-## 7. Activity Events

### Purpose

Stores important platform and business events for debugging, monitoring, and auditing.

### Owner

Platform

### Primary Key

- id

### Foreign Key

- business_id → businesses.id

### Important Fields

| Field | Purpose |
|--------|---------|
| id | Primary Key |
| business_id | Business Owner |
| event_type | Type of Event |
| status | Success / Info / Error |
| message | Human-readable Event Description |
| created_at | Event Timestamp |

### Used By

- Debugging
- Monitoring
- Error Investigation
- Platform Administration

### Engineering Decision

Activity events are append-only.

They should never be edited after creation.

They serve as the operational history of the system.

### Status

✅ Approved





- ## 8. User Sessions

### Purpose

Stores the current booking state for each customer while they interact with the WhatsApp booking flow.

### Owner

Platform

### Primary Key

- phone_number

### Foreign Key

- business_id (currently `tenant_id`) → businesses.id

### Important Fields

| Field | Purpose |
|--------|---------|
| phone_number | Customer Identifier |
| business_id | Business Owner |
| step | Current Booking Step |
| selected_service_id | Selected Service |
| selected_date | Selected Appointment Date |
| selected_time | Selected Appointment Time |
| updated_at | Last Interaction Timestamp |

### Used By

- Booking Runtime
- WhatsApp Flow
- Conversation Recovery

### Engineering Decision

User sessions are temporary runtime state.

They are not business records and may be safely updated or deleted as the conversation progresses.

### Status

✅ Approved