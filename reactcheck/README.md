# ReactCheck

Staff must react to tracked messages within 24 hours. At the 24-hour mark, every member with the
staff role (set in the LOA plugin) who hasn't reacted (any emoji) and isn't on LOA gets a strike and a DM.

**Which messages are tracked** (everything else is ignored):
1. A message in a tracked channel (`?reactaddchannel`) that pings the staff role, automatically.
2. Any message a manager right-clicks -> Apps -> **Require reactions** (24 hours start from that moment).
   Managers can undo this with **Cancel reaction check** before the check runs.

**Strikes**
- One strike is removed after each full week with no new strike (oldest first). A new strike restarts the week.
- At 3 strikes the DM warns it's the final strike.
- A strike while holding 3 = disciplinary action: member DM'd, management pinged in the log channel,
  all strikes cleared, and a permanent disciplinary record is kept.
- The message's author, bots, and members with the bypass role (`?reactsetbypass @role`) are never struck.

**4th strike:** management gets a log message with a **Record punishment** button. They enter the punishment, duration and short reasoning; it's saved on the disciplinary record and DM'd to the member.

When the punishment is saved, the log message loses its button and an update with the punishment, duration and reasoning is posted under it.

**Management commands:** `/adddiscipline` (reason, punishment and duration are all required), `/viewdiscipline` (a member's strikes and full disciplinary actions), `/addstrike`, `/removestrike`, `/removedisciplinaryactions`.
**Termination:** `/terminate user reasoning blacklist` (management; asks for confirmation) clears all of that person's disciplinary records and strikes, records the termination, DMs them, and logs it. With blacklist = Yes they're added to the Blacklist plugin's list. Terminated people are skipped by reaction checks and weekly summaries, and the termination shows in `/viewdiscipline`. `/removetermination user` is locked to the LOA plugin's override users: it removes the termination (and the blacklist entry that termination added); the cleared records and strikes are not restored. Nobody can run either command on themselves except the override users, who press Override first.

**Staff commands:** `/mystrikes`, `/mydisciplinary`, `/appeal`.

Managers can't add or remove their own strikes or disciplinary records, or set their own punishment (only the LOA plugin's override users can, after pressing Override).

**Appeals:** staff run `/appeal` on one of their strikes or disciplinary actions with a reason. It's posted in the appeals channel (`?reactsetappeals #channel`) pinging management, with Approve / Deny buttons (reasoning required). Approving removes the strike, or deletes the disciplinary record (strikes that action cleared are not restored). The member is DM'd either way. One appeal per record. Managers can't decide their own appeal (override users can, after pressing Override).

**Weekly summary:** every week the bot DMs each staff member their strikes and disciplinary actions from that week ("None" if there aren't any). Default Mondays 14:00 UTC; change with `?reactsetreport monday 14`. `?reactreportpreview` DMs you a preview.

Requires the LOA plugin (staff role, manager role, and who is on LOA).

Setup (admins): `?reactaddchannel #channel`, `?reactsetlog #channel`, `?reactsetappeals #channel`, `?reactsettings` to verify,
then `?loasync` so `/mystrikes`, `/viewdiscipline` and the right-click options appear.
