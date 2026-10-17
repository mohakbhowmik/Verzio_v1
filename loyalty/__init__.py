"""
Verzio Loyalty: a WhatsApp points wallet for cafes and restaurants.

Kept separate from the booking engine on purpose. The only places the rest of
the app touches it are:
- server.py: one check per incoming message (loyalty.bot.handle_message),
  and the routers / table setup at startup.
- the owner portal menu (shows "Loyalty" when it's switched on).

Loyalty is OFF for every business until it's switched on in Admin -> Loyalty.

Files:
  models.py   tables (new tables only; no existing table is changed)
  engine.py   the rules: earn, claim, give, adjust, progress
  bot.py      WhatsApp messages (STAMP code, WALLET, CLAIM, buttons)
  wallet.py   the customer's wallet page (/w/<token>)
  owner.py    owner portal page (/owner/loyalty)
  admin.py    admin on/off switch (/admin/loyalty)
"""
