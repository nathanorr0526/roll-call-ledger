# Roll Call Ledger

Tracks every recorded (roll-call) vote by every senator and representative in the current Congress, pulled straight from the official sources. Search members, see how each one voted on every roll call, filter for missed votes or votes against their party, open any vote to see the full member-by-member breakdown, and compare how often two members agree.

No API keys. No dependencies beyond Python 3.9+.

## Run it

```bash
python sync.py      # first run downloads everything: ~15–30 min for a full Congress
python server.py    # open http://localhost:8000
```

Later runs of `sync.py` only fetch new votes and take seconds.

## Keep it current

Either let the server sync itself:

```bash
python server.py --sync-hours 6
```

or schedule `sync.py` with cron (every hour on weekdays shown here):

```
0 * * * 1-5  cd /path/to/congress-votes && /usr/bin/python3 sync.py >> sync.log 2>&1
```

## Options

| Command | What it does |
|---|---|
| `python sync.py --congress 118` | Load a past Congress (they can share one database) |
| `python sync.py --chamber senate` | One chamber only |
| `python sync.py --refresh 20` | Re-download the 20 newest votes per session, to pick up clerk corrections |
| `python server.py --host 0.0.0.0 --port 8080` | Serve on your network or a VPS |
| `VOTES_DB=/data/votes.db` | Keep the database somewhere else |

## Where the data comes from

- **House**: Office of the Clerk, `clerk.house.gov/evs/{year}/roll{NNN}.xml`
- **Senate**: Senate LIS, `senate.gov/legislative/LIS/roll_call_votes/…`
- **Member names, seats and ID mapping**: the public-domain `congress-legislators` dataset (`unitedstates.github.io/congress-legislators`)

Every vote page links to its official record.

## How the numbers work

- **Missed**: the share of roll calls where the member is recorded as "Not Voting".
- **Votes with party**: on roll calls where the member voted yes or no, how often they matched the majority of their own party. Independents with no party colleagues show "—".
- **Compare**: only roll calls where both members voted yes or no. Senators and representatives rarely share a roll call, so compare within a chamber.
- Positions are normalized to Yes / No / Present / Missed. The House's "Aye/No" and impeachment "Guilty/Not guilty" map to Yes/No; the original wording is kept and shown on hover. House Speaker elections record candidate names and appear as "Other".

## Troubleshooting

- **Senate downloads return 403**: senate.gov sometimes blocks non-browser clients. Set a browser-like agent and rerun:
  `VOTES_USER_AGENT="Mozilla/5.0 (compatible; personal vote tracker)" python sync.py --chamber senate`
- **A member shows as `lis:S123`**: they left office before the current-members list was fetched. The next `sync.py` run fills them in from the historical list.

## API

The web page is a thin client over a JSON API you can also use directly:

```
GET /api/summary
GET /api/members?chamber=senate&party=R&q=TX
GET /api/members/{bioguide_id}
GET /api/members/{bioguide_id}/votes?position=no&q=appropriations&against_party=1
GET /api/votes?chamber=house&q=NDAA
GET /api/votes/{vote_id}            # e.g. h-2025-12, s-119-1-7
GET /api/compare?a={id}&b={id}
```
