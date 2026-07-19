#!/usr/bin/env python3
"""Download TxLINE historical SSE data and save as JSONL."""
import os, json, sys, urllib.request

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "replay")

def load_env():
    env = {}
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

def download_sse(url, headers, out_path):
    req = urllib.request.Request(url, headers=headers)
    records = []
    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line.startswith("data: "):
                payload = line[6:]
                try:
                    records.append(json.loads(payload))
                except json.JSONDecodeError:
                    pass
    with open(out_path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return records

def main():
    env = load_env()
    jwt = env["TXLINE_JWT"]
    api_token = env["TXLINE_API_TOKEN"]
    base = "https://txline-dev.txodds.com/api"
    headers = {"Authorization": f"Bearer {jwt}", "X-Api-Token": api_token}
    os.makedirs(DATA_DIR, exist_ok=True)

    # Fixtures to try
    fixtures = [18222446, 18257739, 18257865, 18241006, 18237038]

    for fid in fixtures:
        url = f"{base}/scores/historical/{fid}"
        out = os.path.join(DATA_DIR, f"scores_historical_{fid}.jsonl")
        print(f"\nGET {url}")
        try:
            recs = download_sse(url, headers, out)
            print(f"  -> {len(recs)} records saved to {out}")
            if recs:
                seqs = [r.get("Seq", "?") for r in recs]
                print(f"  -> Seq range: {seqs[0]} .. {seqs[-1]}")
                # Also download odds
                odds_url = f"{base}/odds/historical/{fid}"
                odds_out = os.path.join(DATA_DIR, f"odds_historical_{fid}.jsonl")
                print(f"GET {odds_url}")
                try:
                    orecs = download_sse(odds_url, headers, odds_out)
                    print(f"  -> {len(orecs)} odds records saved to {odds_out}")
                    if orecs:
                        oseqs = [r.get("Seq", "?") for r in orecs]
                        print(f"  -> Odds Seq range: {oseqs[0]} .. {oseqs[-1]}")
                except Exception as e:
                    print(f"  -> Odds error: {e}")
                print("\nDONE — real data downloaded.")
                return
        except Exception as e:
            print(f"  -> Error: {e}")

    print("\nCRITICAL: No records from any fixture.")
    sys.exit(1)

if __name__ == "__main__":
    main()
