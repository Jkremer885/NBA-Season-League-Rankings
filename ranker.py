import datetime
import json
import os
import time
import requests
from bs4 import BeautifulSoup
import pandas as pd


def get_current_season_year():
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.year + 1 if now.month >= 10 else now.year


def scrape_bbr_totals(season_year):
    url = f"https://www.basketball-reference.com/leagues/NBA_{season_year}_totals.html"
    session = requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.basketball-reference.com/",
        "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }

    html = None
    for attempt in range(4):
        try:
            print(f"Fetching {url} (Attempt {attempt + 1})...")
            resp = session.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                html = resp.text
                break
            elif resp.status_code == 429:
                wait_time = 25 * (attempt + 1)
                print(f"Rate limited (429). Sleeping {wait_time}s...")
                time.sleep(wait_time)
            else:
                print(f"Status {resp.status_code}. Sleeping 10s...")
                time.sleep(10)
        except Exception as e:
            print(f"Connection attempt {attempt + 1} failed: {e}")
            time.sleep(10)

    if not html:
        raise RuntimeError(f"Unable to load valid page response for season {season_year}")

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"id": "totals_stats"})
    if not table:
        table = soup.find("table")

    if not table:
        raise ValueError(f"Could not locate stats table in response for season {season_year}")

    df_list = pd.read_html(str(table))
    df = df_list[0]

    # Clean headers & duplicate rows for traded players
    if "Rk" in df.columns:
        df = df[df["Rk"] != "Rk"].copy()
    if "Player" in df.columns:
        df = df.drop_duplicates(subset=["Player"], keep="first").copy()

    stat_cols = [
        "G", "GS", "MP", "FG", "FGA", "3P", "FT", "FTA",
        "TRB", "AST", "STL", "BLK", "TOV", "PTS"
    ]
    for col in stat_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        else:
            df[col] = 0.0

    return df


def calculate_category_rankings(df):
    if len(df) == 0:
        return df

    res = df.copy()
    lg_fg = res["FG"].sum() / max(res["FGA"].sum(), 1)
    lg_ft = res["FT"].sum() / max(res["FTA"].sum(), 1)

    res["FG_IMP"] = res["FG"] - (res["FGA"] * lg_fg)
    res["FT_IMP"] = res["FT"] - (res["FTA"] * lg_ft)

    categories = {
        "FG_IMP": False,
        "FT_IMP": False,
        "3P": False,
        "PTS": False,
        "TRB": False,
        "AST": False,
        "STL": False,
        "BLK": False,
        "TOV": True,
    }

    z_cols = []
    for cat, invert in categories.items():
        std = res[cat].std(ddof=0)
        mean = res[cat].mean()
        z_col = f"z_{cat}"
        z_cols.append(z_col)
        if std == 0 or pd.isna(std):
            res[z_col] = 0.0
        else:
            res[z_col] = (res[cat] - mean) / std
            if invert:
                res[z_col] = -res[z_col]

    res["score"] = res[z_cols].sum(axis=1)
    res["rank"] = res["score"].rank(ascending=False, method="min").astype(int)
    return res.sort_values(by="rank")


def calculate_points_rankings(df):
    if len(df) == 0:
        return df

    res = df.copy()
    res["score"] = (
        (res["PTS"] * 1.0)
        + (res["TRB"] * 1.2)
        + (res["AST"] * 1.5)
        + (res["BLK"] * 3.0)
        + (res["STL"] * 3.0)
        + (res["TOV"] * -1.0)
    )
    res["rank"] = res["score"].rank(ascending=False, method="min").astype(int)
    return res.sort_values(by="rank")


def run_pipeline():
    os.makedirs("data/snapshots", exist_ok=True)
    today_str = datetime.date.today().isoformat()
    season = get_current_season_year()

    try:
        df_current = scrape_bbr_totals(season)
    except Exception as e:
        print(f"Failed loading season {season}: {e}")
        fallback_season = season - 1
        print(f"Falling back to prior season {fallback_season}...")
        df_current = scrape_bbr_totals(fallback_season)
        season = fallback_season

    for col in ["Player", "Team", "Pos"]:
        if col not in df_current.columns:
            df_current[col] = "Unknown"

    snapshot_cols = [
        "Player", "Team", "Pos", "G", "FG", "FGA", "3P", "FT",
        "FTA", "TRB", "AST", "STL", "BLK", "TOV", "PTS"
    ]
    snapshot_data = df_current[snapshot_cols].to_dict(orient="records")
    with open(f"data/snapshots/{today_str}.json", "w") as f:
        json.dump(snapshot_data, f)

    yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    prev_run_file = f"data/rankings_{yesterday_str}.json"
    prev_ranks = {}
    if os.path.exists(prev_run_file):
        try:
            with open(prev_run_file, "r") as pf:
                prev_json = json.load(pf)
                prev_ranks = prev_json.get("systems", {})
        except Exception:
            pass

    output_bundle = {
        "metadata": {"updated_at": today_str, "season": season},
        "systems": {"category": {}, "points": {}},
    }

    numeric_cols = [
        "FG", "FGA", "3P", "FT", "FTA", "TRB", "AST", "STL", "BLK", "TOV", "PTS"
    ]
    windows = [0, 7, 10, 14]

    for days in windows:
        window_label = "season" if days == 0 else f"last_{days}_days"

        if days == 0:
            df_window = df_current.copy()
        else:
            target_date = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
            target_file = f"data/snapshots/{target_date}.json"
            if os.path.exists(target_file):
                with open(target_file, "r") as f:
                    old_data = pd.DataFrame(json.load(f))
                merged = df_current.merge(old_data, on="Player", suffixes=("", "_old"))
                df_window = df_current[["Player", "Team", "Pos"]].copy()
                for c in numeric_cols:
                    df_window[c] = (merged[c] - merged.get(f"{c}_old", 0)).clip(lower=0)
            else:
                df_window = df_current.copy()

        ranked_cat = calculate_category_rankings(df_window)
        ranked_pts = calculate_points_rankings(df_window)

        for sys_key, ranked_df in [("category", ranked_cat), ("points", ranked_pts)]:
            prev_window_ranks = {
                p["Player"]: p["rank"]
                for p in prev_ranks.get(sys_key, {}).get(window_label, [])
            }

            ranked_df["rank_change"] = 0
            if prev_window_ranks:
                ranked_df["rank_change"] = ranked_df["Player"].apply(
                    lambda p: prev_window_ranks.get(
                        p,
                        ranked_df.loc[ranked_df["Player"] == p, "rank"].values[0]
                    ) - ranked_df.loc[ranked_df["Player"] == p, "rank"].values[0]
                )

            export_cols = [
                "rank", "rank_change", "Player", "Team", "Pos", "score",
                "PTS", "TRB", "AST", "STL", "BLK", "3P", "TOV",
                "FG", "FGA", "FT", "FTA"
            ]
            output_bundle["systems"][sys_key][window_label] = ranked_df[export_cols].to_dict(orient="records")

    with open("data/latest_rankings.json", "w") as f:
        json.dump(output_bundle, f, indent=2)

    with open(f"data/rankings_{today_str}.json", "w") as f:
        json.dump(output_bundle, f)

    print("Pipeline run complete.")


if __name__ == "__main__":
    run_pipeline()
