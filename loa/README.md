# LOA

Leave-of-absence system for Modmail staff, using slash commands.

| Command | Who | What |
|---|---|---|
| `/loarequest days reason [start]` | staff | Request LOA (max 7 days, 2 per month) |
| `/myloa` | staff | Usage per month, each LOA's length, and whether you're on LOA |
| `/loalist` | staff | Everyone who is on LOA right now |
| `/loamanage` | management | Approve/deny pending requests (reasoning required) |
| `/loaend [member]` | staff | End your LOA early (management can end anyone's) |
| `/forceloa member days reason [start]` | management | Forced leave, up to 21 days, ignores monthly limits |
| `/loadelete member entry` | management | Permanently delete one specific LOA record |

Staff are DM'd when their LOA starts and when it ends.

Setup (admins): `?loasetstaff @role`, `?loasetmanager @role`, `?loasetchannel #channel`,
then `?loasync`. `?loasettings` shows the current settings.

Records are deleted automatically ~4 months (120 days) after they were created.
