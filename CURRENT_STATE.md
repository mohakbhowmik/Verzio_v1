# Current Development State

## Current Objective

Finish Version 1 MVP.

Current optimization:

Ship quickly.

Do not optimize unnecessarily.

---

## Current Milestone

Implement Service Soft Deletion.

Objective:

Deleted services should disappear from:

- Owner dashboard
- Booking runtime

Historical appointments must remain intact.

Delete behavior:

If appointments exist:

→ Soft delete

Otherwise:

→ Hard delete

---

## Progress

Completed

- Authentication
- Booking Engine
- Booking Runtime
- Reports
- Owner Portal
- Service CRUD
- Multi-tenant database
- Added Service.is_deleted
- Updated owner_services.py
- Updated booking_runtime.py

Remaining

- owner_dashboard.py
- delete endpoint
- template updates
- testing

---

## Immediate Next Task

Inspect `owner_dashboard.py`.

Identify every query that displays or counts services.

Update those queries so that deleted services (`Service.is_deleted == False`) are excluded where appropriate.

After completing those changes:

1. Test the owner dashboard.
2. Implement the service delete endpoint.
3. Verify that customers cannot access deleted services.
4. Run end-to-end testing of the booking flow.

Do not begin UI polishing until service soft deletion is fully complete and tested.

---

## Notes

Desktop polish is still ongoing.

Responsive design has not started.

Deployment has not started.

Pilot customers have not been onboarded.