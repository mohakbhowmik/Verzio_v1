"""
Verzio Tables: table booking on WhatsApp by zone, for restaurants, rooftops and bars.

Kept separate from the booking engine, like Verzio Loyalty. The rest of the app
touches it only in:
- server.py: one check per incoming message (tables.bot.handle_message, before
  loyalty), the routers, tables_guard and the table setup at startup.
- scheduler.py: tables.jobs.run on every tick.
- loyalty/admin.py: the Tables switch and plan in Admin -> Products.

Off for every business until switched on in Admin -> Products.

Files:
  models.py   tables (new tables only, plus owners.role for door-staff logins)
  engine.py   the rules: hours, capacity by tables or guests, bookings, waitlist, results
  bot.py      WhatsApp messages (booking link, reminders, approvals, cancel, STOP)
  page.py     the guest's booking page with the floor plan (/t/<token>)
  owner.py    owner portal (/owner/tables) and tables_guard
  jobs.py     reminders, waitlist offers, review asks, book-again offers, expiry
"""
