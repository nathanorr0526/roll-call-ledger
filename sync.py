#!/usr/bin/env python3
"""
Download every House and Senate roll-call vote for a Congress into SQLite.

Sources (all official / public, no API key needed):
  * Members:  https://unitedstates.github.io/congress-legislators/  (bioguide <-> Senate LIS ids)
  * House:    https://clerk.house.gov/evs/{year}/roll{NNN}.xml
  * Senate:   https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_{congress}_{session}.xml
              https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/vote_{congress}_{session}_{NNNNN}.xml

Usage:
  python sync.py                    # current Congress, both chambers, only new votes
  python sync.py --congress 118     # a past Congress
  python sync.py --chamber senate   # one chamber
  python sync.py --refresh 10       # also re-download the 10 most recent votes per session

Standard library only. Safe to run repeatedly (e.g. hourly from cron).
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
# Railway sets RAILWAY_VOLUME_MOUNT_PATH when a storage volume is attached; keep the database there so it survives redeploys.
DB_PATH = os.environ.get("VOTES_DB") or os.path.join(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or HERE, "votes.db")
USER_AGENT = os.environ.get("VOTES_USER_AGENT", "congress-vote-tracker/1.0 (+self-hosted)")
LEGISLATORS_URLS = [
    "https://unitedstates.github.io/congress-legislators/legislators-current.json",
    "https://unitedstates.github.io/congress-legislators/legislators-historical.json",
]
DELAY = float(os.environ.get("VOTES_DELAY", "0.25"))  # seconds between requests, be polite

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
  id        TEXT PRIMARY KEY,         -- bioguide id, or 'lis:S123' if unknown
  lis_id    TEXT,
  name      TEXT NOT NULL,
  last_name TEXT,
  party     TEXT,
  state     TEXT,
  district  TEXT,
  chamber   TEXT                      -- 'house' | 'senate'
);
CREATE INDEX IF NOT EXISTS members_lis ON members(lis_id);

CREATE TABLE IF NOT EXISTS votes (
  id          TEXT PRIMARY KEY,       -- 'h-2025-123' or 's-119-1-45'
  chamber     TEXT NOT NULL,
  congress    INTEGER,
  session     INTEGER,
  number      INTEGER,
  date        TEXT,                   -- ISO yyyy-mm-dd
  question    TEXT,
  description TEXT,
  bill        TEXT,
  result      TEXT,
  source_url  TEXT
);
CREATE INDEX IF NOT EXISTS votes_date ON votes(date DESC);

CREATE TABLE IF NOT EXISTS positions (
  vote_id   TEXT NOT NULL REFERENCES votes(id),
  member_id TEXT NOT NULL,
  raw       TEXT,                     -- exactly as recorded: Yea, Aye, No, Not Voting, Guilty...
  position  TEXT,                     -- normalized: yes | no | present | not_voting | other
  party     TEXT,                     -- party at time of vote
  state     TEXT,
  PRIMARY KEY (vote_id, member_id)
);
CREATE INDEX IF NOT EXISTS positions_member ON positions(member_id);
"""

NORMALIZE = {
    "yea": "yes", "aye": "yes", "yes": "yes", "guilty": "yes",
    "nay": "no", "no": "no", "not guilty": "no",
    "present": "present",
    "not voting": "not_voting",
}


def normalize(raw):
    return NORMALIZE.get((raw or "").strip().lower(), "other")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(url):
    """Return bytes, or None on 404. Raises on other errors after retries."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,application/json,*/*"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            time.sleep(DELAY)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == 2:
                raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(2 * (attempt + 1))


def text(el, path, default=""):
    if el is None:
        return default
    found = el.find(path)
    return (found.text or "").strip() if found is not None and found.text else default


def parse_date(s):
    s = (s or "").strip()
    for fmt in ("%d-%b-%Y", "%B %d, %Y, %I:%M %p", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return s


def looks_like_xml(data):
    return data is not None and data.lstrip()[:200].lower().startswith((b"<?xml", b"<rollcall", b"<roll_call", b"<vote_summary"))


# ---------------------------------------------------------------- members

def load_members(db):
    count = 0
    for url in LEGISLATORS_URLS[:1]:  # current members; historical is large, only pulled for gaps below
        data = fetch(url)
        if not data:
            log("! could not download legislators list:", url)
            continue
        for p in json.loads(data):
            upsert_legislator(db, p)
            count += 1
    db.commit()
    log(f"members: {count} current legislators loaded")


def upsert_legislator(db, p):
    ids, name, term = p.get("id", {}), p.get("name", {}), p["terms"][-1]
    full = name.get("official_full") or f"{name.get('first', '')} {name.get('last', '')}".strip()
    db.execute(
        """INSERT INTO members(id, lis_id, name, last_name, party, state, district, chamber)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET lis_id=excluded.lis_id, name=excluded.name, last_name=excluded.last_name,
             party=excluded.party, state=excluded.state, district=excluded.district, chamber=excluded.chamber""",
        (ids["bioguide"], ids.get("lis"), full, name.get("last"), (term.get("party") or "")[:1],
         term.get("state"), str(term.get("district", "")) if term.get("district") is not None else None,
         "senate" if term.get("type") == "sen" else "house"),
    )


def fill_missing_members(db):
    """Members who appear in votes but not in the current list (resigned, died, lost) -> pull from historical."""
    missing = db.execute("SELECT COUNT(*) FROM positions p LEFT JOIN members m ON m.id = p.member_id WHERE m.id IS NULL OR m.name LIKE '(%'").fetchone()[0]
    if not missing:
        return
    data = fetch(LEGISLATORS_URLS[1])
    if not data:
        return
    for p in json.loads(data):
        bid, lis = p["id"]["bioguide"], p["id"].get("lis")
        if db.execute("SELECT 1 FROM positions WHERE member_id IN (?, ?) LIMIT 1", (bid, f"lis:{lis}")).fetchone():
            exists = db.execute("SELECT 1 FROM members WHERE id = ?", (bid,)).fetchone()
            if not exists:
                upsert_legislator(db, p)
            if lis:
                db.execute("UPDATE positions SET member_id = ? WHERE member_id = ?", (bid, f"lis:{lis}"))
                db.execute("DELETE FROM members WHERE id = ?", (f"lis:{lis}",))
    db.commit()


def ensure_member(db, member_id, name, last_name, party, state, chamber, lis_id=None):
    db.execute(
        "INSERT OR IGNORE INTO members(id, lis_id, name, last_name, party, state, chamber) VALUES(?,?,?,?,?,?,?)",
        (member_id, lis_id, name, last_name, party, state, chamber),
    )


# ---------------------------------------------------------------- house

def parse_house(data, url=""):
    root = ET.fromstring(data)
    meta = root.find("vote-metadata")
    congress = int(text(meta, "congress") or 0)
    session_txt = text(meta, "session")
    session = int("".join(c for c in session_txt if c.isdigit()) or 0)
    number = int(text(meta, "rollcall-num") or 0)
    year = parse_date(text(meta, "action-date"))[:4]
    vote = {
        "id": f"h-{year}-{number}",
        "chamber": "house",
        "congress": congress,
        "session": session,
        "number": number,
        "date": parse_date(text(meta, "action-date")),
        "question": text(meta, "vote-question"),
        "description": text(meta, "vote-desc") or text(meta, "amendment-author"),
        "bill": text(meta, "legis-num"),
        "result": text(meta, "vote-result"),
        "source_url": url,
    }
    positions = []
    for rv in root.iter("recorded-vote"):
        leg = rv.find("legislator")
        if leg is None:
            continue
        positions.append({
            "member_id": leg.get("name-id"),
            "name": leg.get("unaccented-name") or (leg.text or "").strip(),
            "last_name": (leg.text or "").strip(),
            "party": leg.get("party"),
            "state": leg.get("state"),
            "raw": text(rv, "vote"),
        })
    return vote, positions


def sync_house(db, congress, refresh):
    first_year = 1787 + 2 * congress
    for session, year in ((1, first_year), (2, first_year + 1)):
        if year > dt.date.today().year:
            break
        have = {r[0] for r in db.execute("SELECT number FROM votes WHERE chamber='house' AND congress=? AND session=?", (congress, session))}
        top = max(have) if have else 0
        start = max(1, top - refresh + 1) if refresh else top + 1
        n, misses, added = start, 0, 0
        while misses < 3:
            if n in have and n <= top - refresh:
                n += 1
                continue
            url = f"https://clerk.house.gov/evs/{year}/roll{n:03d}.xml"
            data = fetch(url)
            if not looks_like_xml(data):
                misses += 1
                n += 1
                continue
            misses = 0
            vote, positions = parse_house(data, url)
            if vote["congress"] and vote["congress"] != congress:
                break
            store(db, vote, positions)
            added += 1
            if added % 25 == 0:
                log(f"  house {year}: through roll {n}")
            n += 1
        log(f"house {year}: {added} roll calls fetched")


# ---------------------------------------------------------------- senate

def parse_senate(data, url=""):
    root = ET.fromstring(data)
    congress = int(text(root, "congress") or 0)
    session = int(text(root, "session") or 0)
    number = int(text(root, "vote_number") or 0)
    doc = root.find("document")
    bill = ""
    if doc is not None:
        bill = text(doc, "document_name") or " ".join(x for x in (text(doc, "document_type"), text(doc, "document_number")) if x)
    vote = {
        "id": f"s-{congress}-{session}-{number}",
        "chamber": "senate",
        "congress": congress,
        "session": session,
        "number": number,
        "date": parse_date(text(root, "vote_date")),
        "question": text(root, "vote_question_text") or text(root, "question"),
        "description": text(root, "vote_title") or text(root, "vote_document_text"),
        "bill": bill,
        "result": text(root, "vote_result_text") or text(root, "vote_result"),
        "source_url": url,
    }
    positions = []
    for m in root.iter("member"):
        lis = text(m, "lis_member_id")
        positions.append({
            "lis_id": lis,
            "name": f"{text(m, 'first_name')} {text(m, 'last_name')}".strip() or text(m, "member_full"),
            "last_name": text(m, "last_name"),
            "party": text(m, "party"),
            "state": text(m, "state"),
            "raw": text(m, "vote_cast"),
        })
    return vote, positions


def senate_vote_numbers(congress, session):
    url = f"https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_{congress}_{session}.xml"
    data = fetch(url)
    if not looks_like_xml(data):
        return []
    root = ET.fromstring(data)
    return sorted({int(v.text) for v in root.iter("vote_number") if v.text and v.text.strip().isdigit()})


def sync_senate(db, congress, refresh):
    lis_map = {r[0]: r[1] for r in db.execute("SELECT lis_id, id FROM members WHERE lis_id IS NOT NULL")}
    for session in (1, 2):
        try:
            numbers = senate_vote_numbers(congress, session)
        except urllib.error.HTTPError as e:
            log(f"! senate.gov refused the vote list ({e.code}). Try setting VOTES_USER_AGENT to a browser-like string.")
            return
        if not numbers:
            continue
        have = {r[0] for r in db.execute("SELECT number FROM votes WHERE chamber='senate' AND congress=? AND session=?", (congress, session))}
        redo = set(numbers[-refresh:]) if refresh else set()
        todo = [n for n in numbers if n not in have or n in redo]
        for i, n in enumerate(todo, 1):
            url = (f"https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/"
                   f"vote_{congress}_{session}_{n:05d}.xml")
            data = fetch(url)
            if not looks_like_xml(data):
                log(f"  ! senate vote {n} unavailable")
                continue
            vote, positions = parse_senate(data, url)
            for p in positions:
                p["member_id"] = lis_map.get(p["lis_id"]) or f"lis:{p['lis_id']}"
            store(db, vote, positions)
            if i % 25 == 0:
                log(f"  senate {congress}-{session}: {i}/{len(todo)}")
        log(f"senate {congress}-{session}: {len(todo)} roll calls fetched ({len(numbers)} total)")


# ---------------------------------------------------------------- store

def store(db, vote, positions):
    db.execute(
        """INSERT OR REPLACE INTO votes(id, chamber, congress, session, number, date, question, description, bill, result, source_url)
           VALUES(:id, :chamber, :congress, :session, :number, :date, :question, :description, :bill, :result, :source_url)""",
        vote,
    )
    db.execute("DELETE FROM positions WHERE vote_id = ?", (vote["id"],))
    for p in positions:
        if not p.get("member_id"):
            continue
        ensure_member(db, p["member_id"], p["name"], p["last_name"], p["party"], p["state"], vote["chamber"], p.get("lis_id"))
        db.execute(
            "INSERT OR REPLACE INTO positions(vote_id, member_id, raw, position, party, state) VALUES(?,?,?,?,?,?)",
            (vote["id"], p["member_id"], p["raw"], normalize(p["raw"]), p["party"], p["state"]),
        )
    db.commit()


def rebuild_stats(db):
    """How each party's majority voted on each roll call (used for 'votes with party' stats)."""
    # Build into a scratch table, then swap it in one step so the website never sees a missing table.
    db.executescript("""
    DROP TABLE IF EXISTS party_majority_new;
    CREATE TABLE party_majority_new AS
      SELECT vote_id, party,
             SUM(position = 'yes') AS yes, SUM(position = 'no') AS no,
             CASE WHEN SUM(position = 'yes') > SUM(position = 'no') THEN 'yes'
                  WHEN SUM(position = 'no')  > SUM(position = 'yes') THEN 'no' END AS majority
      FROM positions WHERE position IN ('yes', 'no') GROUP BY vote_id, party;
    BEGIN;
    DROP TABLE IF EXISTS party_majority;
    ALTER TABLE party_majority_new RENAME TO party_majority;
    CREATE INDEX IF NOT EXISTS party_majority_idx ON party_majority(vote_id, party);
    COMMIT;
    """)
    db.commit()


def connect(path=DB_PATH):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def current_congress(today=None):
    today = today or dt.date.today()
    year = today.year
    # A new Congress starts Jan 3 of odd years.
    if year % 2 == 1 and (today.month, today.day) < (1, 3):
        year -= 1
    return (year - 1787) // 2


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--congress", type=int, default=current_congress())
    ap.add_argument("--chamber", choices=["house", "senate", "both"], default="both")
    ap.add_argument("--refresh", type=int, default=3, help="re-download the N most recent votes per session (catches corrections)")
    ap.add_argument("--skip-members", action="store_true")
    args = ap.parse_args()

    db = connect()
    log(f"Syncing the {args.congress}th Congress into {DB_PATH}")
    if not args.skip_members:
        load_members(db)
    if args.chamber in ("house", "both"):
        sync_house(db, args.congress, args.refresh)
    if args.chamber in ("senate", "both"):
        sync_senate(db, args.congress, args.refresh)
    fill_missing_members(db)
    rebuild_stats(db)
    total = db.execute("SELECT COUNT(*) FROM votes").fetchone()[0]
    log(f"done. {total} roll calls in database.")


if __name__ == "__main__":
    main()
