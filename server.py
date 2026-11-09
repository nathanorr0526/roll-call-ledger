#!/usr/bin/env python3
"""
Serve the vote tracker: a JSON API over votes.db plus the web page in ./static.

  python server.py                 # http://localhost:8000
  python server.py --port 8080 --host 0.0.0.0
  python server.py --sync-hours 6  # also run sync.py in the background every 6 hours

API
  GET /api/summary
  GET /api/members?q=&chamber=house|senate&party=D|R|I
  GET /api/members/<id>
  GET /api/members/<id>/votes?position=&q=&limit=&offset=
  GET /api/votes?chamber=&q=&limit=&offset=
  GET /api/votes/<id>
  GET /api/compare?a=<id>&b=<id>
  GET /api/topics                         policy areas with counts
  GET /api/topic?q=guns&member=<id>       bills on a topic, their votes, and (optionally) one member's positions
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sync  # noqa: E402

STATIC = os.path.join(HERE, "static")


def db():
    conn = sqlite3.connect(sync.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def ensure_ready():
    conn = sync.connect()
    has = conn.execute("SELECT 1 FROM sqlite_master WHERE name='party_majority'").fetchone()
    if not has:
        sync.rebuild_stats(conn)
    conn.close()


# ---------------------------------------------------------------- queries

def summary(c):
    out = {}
    for ch in ("house", "senate"):
        r = c.execute("SELECT COUNT(*) n, MIN(date) first, MAX(date) last FROM votes WHERE chamber=?", (ch,)).fetchone()
        out[ch] = dict(r)
    out["congress"] = c.execute("SELECT MAX(congress) FROM votes").fetchone()[0]
    return out


def member_list(c, q):
    where, args = ["1=1"], []
    if q.get("chamber"):
        where.append("m.chamber = ?"); args.append(q["chamber"])
    if q.get("party"):
        where.append("m.party = ?"); args.append(q["party"])
    if q.get("q"):
        where.append("(m.name LIKE ? OR m.state LIKE ?)"); args += [f"%{q['q']}%", q["q"].upper()]
    return rows(c.execute(f"""
        SELECT m.id, m.name, m.last_name, m.party, m.state, m.district, m.chamber,
               COUNT(p.vote_id) AS votes,
               SUM(p.position = 'not_voting') AS missed
        FROM members m JOIN positions p ON p.member_id = m.id
        WHERE {' AND '.join(where)}
        GROUP BY m.id ORDER BY m.state, m.chamber DESC, m.last_name""", args))


def member_detail(c, mid):
    m = c.execute("SELECT * FROM members WHERE id = ?", (mid,)).fetchone()
    if not m:
        return None
    m = dict(m)
    counts = {r["position"]: r["n"] for r in c.execute(
        "SELECT position, COUNT(*) n FROM positions WHERE member_id = ? GROUP BY position", (mid,))}
    total = sum(counts.values())
    unity = c.execute("""
        SELECT COUNT(*) n, SUM(p.position = pm.majority) agree
        FROM positions p JOIN party_majority pm ON pm.vote_id = p.vote_id AND pm.party = p.party
        WHERE p.member_id = ? AND p.position IN ('yes','no') AND pm.majority IS NOT NULL
          AND pm.yes + pm.no > 1""", (mid,)).fetchone()
    m["stats"] = {
        "total": total,
        "counts": counts,
        "missed_pct": round(100 * counts.get("not_voting", 0) / total, 1) if total else None,
        "party_unity_pct": round(100 * unity["agree"] / unity["n"], 1) if unity["n"] else None,
        "party_unity_n": unity["n"],
    }
    return m


def member_votes(c, mid, q):
    limit = min(int(q.get("limit", 50)), 500)
    offset = int(q.get("offset", 0))
    where, args = ["p.member_id = ?"], [mid]
    if q.get("position"):
        where.append("p.position = ?"); args.append(q["position"])
    if q.get("q"):
        where.append("(v.question LIKE ? OR v.description LIKE ? OR v.bill LIKE ?)"); args += [f"%{q['q']}%"] * 3
    if q.get("against_party"):
        where.append("pm.majority IS NOT NULL AND p.position IN ('yes','no') AND p.position <> pm.majority")
    return rows(c.execute(f"""
        SELECT v.id, v.date, v.chamber, v.number, v.question, v.description, v.bill, v.result,
               p.raw, p.position,
               CASE WHEN pm.majority IS NULL OR p.position NOT IN ('yes','no') THEN NULL
                    ELSE p.position = pm.majority END AS with_party
        FROM positions p JOIN votes v ON v.id = p.vote_id
        LEFT JOIN party_majority pm ON pm.vote_id = p.vote_id AND pm.party = p.party
        WHERE {' AND '.join(where)}
        ORDER BY v.date DESC, v.number DESC LIMIT ? OFFSET ?""", args + [limit, offset]))


def vote_list(c, q):
    limit = min(int(q.get("limit", 50)), 500)
    offset = int(q.get("offset", 0))
    where, args = ["1=1"], []
    if q.get("chamber"):
        where.append("v.chamber = ?"); args.append(q["chamber"])
    if q.get("q"):
        where.append("(v.question LIKE ? OR v.description LIKE ? OR v.bill LIKE ?)"); args += [f"%{q['q']}%"] * 3
    return rows(c.execute(f"""
        SELECT v.*,
          (SELECT COUNT(*) FROM positions p WHERE p.vote_id = v.id AND p.position = 'yes') AS yes,
          (SELECT COUNT(*) FROM positions p WHERE p.vote_id = v.id AND p.position = 'no')  AS no,
          (SELECT COUNT(*) FROM positions p WHERE p.vote_id = v.id AND p.position NOT IN ('yes','no')) AS other
        FROM votes v WHERE {' AND '.join(where)}
        ORDER BY v.date DESC, v.number DESC LIMIT ? OFFSET ?""", args + [limit, offset]))


def vote_detail(c, vid):
    v = c.execute("SELECT * FROM votes WHERE id = ?", (vid,)).fetchone()
    if not v:
        return None
    v = dict(v)
    v["positions"] = rows(c.execute("""
        SELECT p.member_id, m.name, p.party, p.state, m.district, p.raw, p.position
        FROM positions p LEFT JOIN members m ON m.id = p.member_id
        WHERE p.vote_id = ? ORDER BY p.state, m.last_name""", (vid,)))
    v["by_party"] = rows(c.execute("""
        SELECT party, SUM(position='yes') yes, SUM(position='no') no,
               SUM(position NOT IN ('yes','no')) other
        FROM positions WHERE vote_id = ? GROUP BY party ORDER BY party""", (vid,)))
    return v


def compare(c, a, b):
    r = c.execute("""
        SELECT COUNT(*) n, SUM(pa.position = pb.position) agree
        FROM positions pa JOIN positions pb ON pb.vote_id = pa.vote_id
        WHERE pa.member_id = ? AND pb.member_id = ?
          AND pa.position IN ('yes','no') AND pb.position IN ('yes','no')""", (a, b)).fetchone()
    recent = rows(c.execute("""
        SELECT v.id, v.date, v.question, v.description, v.bill, pa.raw AS a_raw, pb.raw AS b_raw,
               pa.position AS a_pos, pb.position AS b_pos
        FROM positions pa JOIN positions pb ON pb.vote_id = pa.vote_id JOIN votes v ON v.id = pa.vote_id
        WHERE pa.member_id = ? AND pb.member_id = ?
          AND pa.position IN ('yes','no') AND pb.position IN ('yes','no') AND pa.position <> pb.position
        ORDER BY v.date DESC LIMIT 25""", (a, b)))
    return {
        "a": member_detail(c, a), "b": member_detail(c, b),
        "shared": r["n"], "agree": r["agree"] or 0,
        "agree_pct": round(100 * (r["agree"] or 0) / r["n"], 1) if r["n"] else None,
        "disagreements": recent,
    }


# ---------------------------------------------------------------- topics

# Everyday words -> the official Congress.gov terms they should also match.
SYNONYMS = {
    "gun": ["firearms"], "guns": ["firearms"], "gun control": ["firearms"], "second amendment": ["firearms"],
    "healthcare": ["health"], "health care": ["health"], "healthcare reform": ["health", "health care coverage and access"],
    "medicare": ["medicare"], "medicaid": ["medicaid"], "obamacare": ["health care coverage", "health insurance"],
    "abortion": ["abortion"], "immigration": ["immigration"], "border": ["border security", "immigration"],
    "climate": ["climate change", "environmental protection"], "environment": ["environmental protection"],
    "taxes": ["taxation"], "tax": ["taxation"], "military": ["armed forces", "defense"], "defense": ["armed forces and national security"],
    "veterans": ["veterans"], "education": ["education"], "energy": ["energy"], "police": ["law enforcement"],
    "crime": ["crime and law enforcement"], "budget": ["economics and public finance", "budget"],
    "spending": ["appropriations", "economics and public finance"], "farm": ["agriculture"], "farming": ["agriculture"],
    "social security": ["social security"], "voting": ["elections"], "elections": ["elections"],
    "housing": ["housing and community development"], "trade": ["foreign trade"], "ukraine": ["ukraine"],
    "israel": ["israel"], "china": ["china"], "ai": ["artificial intelligence"], "privacy": ["right of privacy"],
}


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def topic_terms(q):
    q = q.strip().lower()
    terms = {q} if len(q) >= 3 or q not in SYNONYMS else set()
    for key, extra in SYNONYMS.items():
        if key == q or (len(key) > 3 and key in q):
            terms.update(extra)
    return sorted(terms)


def topics(c):
    areas = rows(c.execute("""
        SELECT b.policy_area AS name, COUNT(DISTINCT b.congress || b.type || b.number) AS bills, COUNT(v.id) AS votes
        FROM bills b JOIN votes v ON v.congress = b.congress AND v.bill_type = b.type AND v.bill_number = b.number
        WHERE COALESCE(b.policy_area, '') <> '' GROUP BY b.policy_area ORDER BY votes DESC"""))
    tagged = c.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
    return {"areas": areas, "tagged_bills": tagged}


BILL_SLUG = {"hr": "house-bill", "s": "senate-bill", "hres": "house-resolution", "sres": "senate-resolution",
             "hjres": "house-joint-resolution", "sjres": "senate-joint-resolution",
             "hconres": "house-concurrent-resolution", "sconres": "senate-concurrent-resolution"}
BILL_LABEL = {"hr": "H.R.", "s": "S.", "hres": "H.Res.", "sres": "S.Res.", "hjres": "H.J.Res.", "sjres": "S.J.Res.",
              "hconres": "H.Con.Res.", "sconres": "S.Con.Res."}


def topic_search(c, q):
    where, args = [], []
    if q.get("area"):
        where.append("b.policy_area = ?"); args.append(q["area"])
    if q.get("q"):
        ors = []
        for t in topic_terms(q["q"]):
            ors.append("(LOWER(b.policy_area) LIKE ? OR LOWER(b.subjects) LIKE ? OR LOWER(b.title) LIKE ? OR LOWER(v.description) LIKE ?)")
            args += [f"%{t}%"] * 4
        where.append("(" + " OR ".join(ors) + ")")
    if not where:
        return {"bills": [], "terms": []}
    member = q.get("member") or ""
    found = rows(c.execute(f"""
        SELECT v.id, v.date, v.chamber, v.number, v.question, v.description, v.result,
               v.congress, v.bill_type, v.bill_number, v.bill,
               b.title, b.policy_area, b.subjects,
               (SELECT COUNT(*) FROM positions p WHERE p.vote_id = v.id AND p.position = 'yes') AS yes,
               (SELECT COUNT(*) FROM positions p WHERE p.vote_id = v.id AND p.position = 'no')  AS no,
               mp.position AS member_position, mp.raw AS member_raw
        FROM votes v
        LEFT JOIN bills b ON b.congress = v.congress AND b.type = v.bill_type AND b.number = v.bill_number
        LEFT JOIN positions mp ON mp.vote_id = v.id AND mp.member_id = ?
        WHERE v.bill_type IS NOT NULL AND {' AND '.join(where)}
        ORDER BY v.date DESC, v.number DESC LIMIT 600""", [member] + args))
    bills, order = {}, []
    for r in found:
        key = f"{r['congress']}-{r['bill_type']}-{r['bill_number']}"
        if key not in bills:
            order.append(key)
            bills[key] = {
                "key": key, "label": f"{BILL_LABEL.get(r['bill_type'], r['bill_type'])} {r['bill_number']}",
                "title": r["title"] or r["description"] or r["bill"], "policy_area": r["policy_area"],
                "subjects": [x for x in (r["subjects"] or "").split("|") if x][:12],
                "url": f"https://www.congress.gov/bill/{ordinal(r['congress'])}-congress/{BILL_SLUG.get(r['bill_type'], '')}/{r['bill_number']}",
                "votes": [],
            }
        bills[key]["votes"].append({k: r[k] for k in ("id", "date", "chamber", "number", "question", "result",
                                                      "yes", "no", "member_position", "member_raw")})
    return {"bills": [bills[k] for k in order], "terms": topic_terms(q.get("q", "")) if q.get("q") else []}


# ---------------------------------------------------------------- http

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return super().do_GET()
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = [unquote(p) for p in url.path.split("/")[2:] if p]
        c = db()
        try:
            if parts == ["summary"]:
                return self.send_json(summary(c))
            if parts == ["members"]:
                return self.send_json(member_list(c, q))
            if len(parts) == 2 and parts[0] == "members":
                m = member_detail(c, parts[1])
                return self.send_json(m) if m else self.send_json({"error": "Member not found"}, 404)
            if len(parts) == 3 and parts[0] == "members" and parts[2] == "votes":
                return self.send_json(member_votes(c, parts[1], q))
            if parts == ["votes"]:
                return self.send_json(vote_list(c, q))
            if len(parts) == 2 and parts[0] == "votes":
                v = vote_detail(c, parts[1])
                return self.send_json(v) if v else self.send_json({"error": "Vote not found"}, 404)
            if parts == ["topics"]:
                return self.send_json(topics(c))
            if parts == ["topic"]:
                return self.send_json(topic_search(c, q))
            if parts == ["compare"] and q.get("a") and q.get("b"):
                return self.send_json(compare(c, q["a"], q["b"]))
            return self.send_json({"error": "Unknown endpoint"}, 404)
        except (ValueError, sqlite3.Error) as e:
            return self.send_json({"error": str(e)}, 400)
        finally:
            c.close()


def background_sync(hours):
    while True:
        print(f"[sync] starting", flush=True)
        subprocess.run([sys.executable, os.path.join(HERE, "sync.py")])
        time.sleep(hours * 3600)


def main():
    ap = argparse.ArgumentParser()
    # On a hosting service (which sets PORT), listen publicly; on your own computer, only locally.
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--sync-hours", type=float, default=float(os.environ.get("SYNC_HOURS", 0)),
                    help="run sync.py every N hours in the background")
    args = ap.parse_args()
    ensure_ready()
    if args.sync_hours:
        threading.Thread(target=background_sync, args=(args.sync_hours,), daemon=True).start()
    print(f"Vote tracker running at http://{args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
