# Blacklist

A staff blacklist with three parts.

**The list** (`?blacklistsetlist #channel`): one embed, "🚫 Staff Blacklist", listing
`• username (user id) — added Month D, YYYY`. The bot edits it whenever someone is added or removed
(it continues into extra messages if the list gets very long).

**Requests** (`?blacklistsetrequests #channel`): people post a request in this format:

```
Your username:
Your rank:

Their username:
Their user id:

Blacklist Reason:
Evidence: (must include one screenshot at least)
```

A correctly formatted request is left alone. A wrong one gets an "auto denied" reply listing what's wrong and the
required format, and the message is deleted (its text is copied into the reply). Management and replies are ignored.
A screenshot is an attached image or an image link in the Evidence field. The user ID must be 17-20 digits.

**Decisions** (management):
- `/blacklistapproval request decision reason`: pick the request (or paste its link/ID), Approve or Deny. Reason is
  required when denying. Approving adds the person to the list and announces it under the request.
- `/blacklistremove entry`: removes someone from the list.
- `/staffeligibility user_id`: checks a user ID against the list. Not on it: "Allowed to be hired". On it: "On the blacklist" (with their username and the date they were added).

Management = the LOA plugin's manager role (or Administrator). Managers can't decide a request they submitted or that
is about them, or remove themselves; the LOA plugin's override users can after pressing Override.

The Reaction Check plugin's `/terminate` can add someone to this list directly (blacklist = Yes).

Other admin commands: `?blacklistsettings`, `?blacklistrefresh`.
The bot needs View Channel, Send Messages, Embed Links and Read Message History in both channels,
and Manage Messages in the requests channel (to delete invalid requests).
