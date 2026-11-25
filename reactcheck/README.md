# ReactCheck

Staff must react to tracked messages within 36 hours. At the 36-hour mark, every member with the
staff role (set in the LOA plugin) who hasn't reacted (any emoji) and isn't on LOA gets a strike and a DM.

**Which messages are tracked** (everything else is ignored):
1. A message in a tracked channel (`?reactaddchannel`) that pings the staff role, automatically.
2. Any message a manager right-clicks -> Apps -> **Require reactions** (36 hours start from that moment).
   Managers can undo this with **Cancel reaction check** before the check runs.

**Strikes**
- One strike is removed after each full week with no new strike (oldest first). A new strike restarts the week.
- At 3 strikes the DM warns it's the final strike.
- A strike while holding 3 = disciplinary action: member DM'd, management pinged in the log channel,
  all strikes cleared, and a permanent disciplinary record is kept.
- The message's author and bots are never struck.

Requires the LOA plugin (staff role, manager role, and who is on LOA).

Setup (admins): `?reactaddchannel #channel`, `?reactsetlog #channel`, `?reactsettings` to verify,
then `?loasync` so `/mystrikes`, `/strikeview` and the right-click options appear.
