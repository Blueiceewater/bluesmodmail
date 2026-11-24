# ThreadRating

DMs the user a 1-5 rating survey when their thread is closed (not for silent closes),
logs each rating to your Modmail log channel, and stores results in the plugin database.

Commands:
- `?ratings` - average, response count and breakdown (Supporter+)
- `?ratings toggle` - enable/disable surveys (Administrator)

Ratings are automatically deleted after 90 days (change `RETENTION_DAYS` in the code; if you change it later, drop the old `timestamp_1` index in MongoDB first).
