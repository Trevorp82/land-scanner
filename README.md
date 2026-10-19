# Perth land scanner

Runs every ~15 minutes on GitHub Actions: fetches developer price lists (parcelpricelist, Satterley, Spires Baldivis,
Parkland Heights, Woodlands Baldivis, land4sale, WeGotLand Treeby Park), matches lots against private briefs,
sends phone notifications via ntfy.sh for new matches, and publishes a password-protected (StatiCrypt, AES-256) results page on GitHub Pages.

Nothing private lives in this public repo:
- briefs are the `BRIEFS_JSON` Actions secret,
- the ntfy topic is the `NTFY_TOPIC` secret, the site password is the `SITE_PASSWORD` secret,
- scanner state (seen lots) is kept in the Actions cache, not committed,
- CI logs only print per-source health counts.

`heartbeat.txt` is touched once a day so GitHub doesn't disable the schedule after 60 days of inactivity.

Local run: `BRIEFS_JSON="$(cat briefs.json)" python3 scan.py --all` (needs `pdftotext` from poppler-utils and `curl`).
