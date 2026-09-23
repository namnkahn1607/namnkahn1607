#!/usr/bin/env python3

import argparse, hashlib, html, json, os, sys, time, urllib.error, urllib.request

ENDPOINT = "https://api.github.com/graphql"
CACHE_DIR = ".cache"
CACHE_TTL_SECONDS = 3600  # 1 hour TTL for local runs


def ensure_dir(directory):
    if not os.path.exists(directory):
        os.makedirs(directory)


def get_cache(key):
    cache_path = os.path.join(CACHE_DIR, f"{key}.json")
    if os.path.exists(cache_path):
        if time.time() - os.path.getmtime(cache_path) < CACHE_TTL_SECONDS:
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                pass  # Fallback to network on corrupted cache
    return None


def set_cache(key, data):
    ensure_dir(CACHE_DIR)
    cache_path = os.path.join(CACHE_DIR, f"{key}.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def request(query, variables, token):
    if not token:
        raise ValueError("Critical: missing GitHub token")

    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    cache_key = hashlib.sha256(payload).hexdigest()

    cached_data = get_cache(cache_key)
    if cached_data:
        print(f"Cache hit for query: {variables}")
        return cached_data

    print(f"Network request for query: {variables}")
    req = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "GitHub-Metrics-Bot",
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            if "errors" in resp_data:
                sys.exit(f"GraphQL Error: {json.dumps(resp_data['errors'], indent=2)}")

            set_cache(cache_key, resp_data["data"])
            return resp_data["data"]

    except urllib.error.HTTPError as e:
        # Not to log the header request (that contains token value).
        sys.exit(f"HTTP Error {e.code}: {e.reason}")  


# Query 1: Fetch 100 Repositories to calculate Language Stats (including direct colors)
LANGUAGES_QUERY = """
query($login: String!) {
  user(login: $login) {
    repositories(ownerAffiliations: OWNER, isFork: false, first: 100, orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges {
            size
            node {
              name
              color
            }
          }
        }
      }
    }
  }
}
"""

# Query 2: Batch fetching all Issue & PR states in ONE request (Solves N+1 Problem)
STATS_QUERY = """
query($openIss: String!, $closedIss: String!, $openPR: String!, $mergedPR: String!, $closedPR: String!) {
  openIssues: search(query: $openIss, type: ISSUE, first: 1) { issueCount }
  closedIssues: search(query: $closedIss, type: ISSUE, first: 1) { issueCount }
  openPRs: search(query: $openPR, type: ISSUE, first: 1) { issueCount }
  mergedPRs: search(query: $mergedPR, type: ISSUE, first: 1) { issueCount }
  closedPRs: search(query: $closedPR, type: ISSUE, first: 1) { issueCount }
}
"""

# Query 3: Fetch most recent activity cleanly
ACTIVITY_QUERY = """
query($searchQuery: String!) {
  search(query: $searchQuery, type: ISSUE, first: 5) {
    nodes {
      ... on Issue {
        title
        number
        repository { nameWithOwner }
        __typename
      }
      ... on PullRequest {
        title
        number
        repository { nameWithOwner }
        __typename
      }
    }
  }
}
"""


def fetch_language_stats(user, token, ignores={"HTML", "CSS"}):
    data = request(LANGUAGES_QUERY, {"login": user}, token)

    totals = {}
    colors = {}
    repos = data["user"]["repositories"]["nodes"]

    for repo in repos:
        for edge in repo["languages"]["edges"]:
            lang_name = edge["node"]["name"]
            if lang_name in ignores:
                continue

            size = edge["size"]
            color = edge["node"]["color"] or "#858585"  # Fallback color

            totals[lang_name] = totals.get(lang_name, 0) + size
            colors[lang_name] = color

    sorted_langs = dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))
    return sorted_langs, colors


def fetch_contribution_stats(user, token):
    variables = {
        "openIss": f"author:{user} type:issue is:open",
        "closedIss": f"author:{user} type:issue is:closed",
        "openPR": f"author:{user} type:pr is:open",
        "mergedPR": f"author:{user} type:pr is:merged",
        "closedPR": f"author:{user} type:pr is:closed -is:merged"
    }
    data = request(STATS_QUERY, variables, token)

    return {
        "issues": {
            "open": data["openIssues"]["issueCount"],
            "closed": data["closedIssues"]["issueCount"]
        },
        "prs": {
            "open": data["openPRs"]["issueCount"],
            "merged": data["mergedPRs"]["issueCount"],
            "closed": data["closedPRs"]["issueCount"]
        }
    }


def fetch_recent_activity(user, token):
    variables = {"searchQuery": f"author:{user} sort:created-desc"}
    data = request(ACTIVITY_QUERY, variables, token)
    return data["search"]["nodes"]


def generate_bar(x, y, w, h, parts):
    total = sum(val for _, val, _ in parts) or 1
    svg_elements = []
    current_x = x
    
    for _, val, color in parts:
        if val == 0: continue
        segment_width = w * val / total
        svg_elements.append(
            f'<rect x="{current_x:.1f}" y="{y}" width="{segment_width:.1f}" height="{h}" rx="4" fill="{color}"/>'
        )
        current_x += segment_width
    return "".join(svg_elements)


def escape_text(s):
    return html.escape(str(s), quote=True)


def render_stats_svg(langs, colors, stats):
    W, H = 600, 390
    top_langs = list(langs.items())[:8]
    lang_total = sum(v for _, v in top_langs) or 1
    
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<style>text { font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }</style>',
        '<rect width="100%" height="100%" fill="#ffffff" rx="8" stroke="none"/>'
    ]
    
    # 1. Languages Section
    svg.append(f'<text x="40" y="45" font-size="18" font-weight="400" fill="#0969da">Most Used Languages</text>')
    
    y_pos = 80
    for i, (lang, n) in enumerate(top_langs):
        dx = 280 if i % 2 != 0 else 0
        dy = 30 if i % 2 != 0 else 0
        pct = (n / lang_total) * 100
        color = colors.get(lang, "#000")
        
        svg.append(f'<circle cx="{50 + dx}" cy="{y_pos - 5}" r="6" fill="{color}"/>')
        svg.append(f'<text x="{65 + dx}" y="{y_pos}" font-size="14" fill="#24292f">{escape_text(lang)}</text>')
        svg.append(f'<text x="{220 + dx}" y="{y_pos}" font-size="14" fill="#57606a" text-anchor="end">{pct:.1f}%</text>')
        
        if i % 2 != 0: y_pos += dy

    # 2. Issues & PRs Section
    issues, prs = stats["issues"], stats["prs"]
    svg.append(f'<text x="40" y="215" font-size="18" font-weight="400" fill="#0969da">Overall Issues and Pull Requests Status</text>')
    
    # Issues
    svg.append(f'<text x="40" y="250" font-size="15" fill="#0969da">Issues</text>')
    svg.append(generate_bar(40, 265, 230, 10, [
        ("open", issues["open"], "#1a7f37"),
        ("closed", issues["closed"], "#8250df"),
    ]))
    svg.append(f'<circle cx="45" cy="295" r="4" fill="#1a7f37"/><text x="55" y="300" font-size="14" fill="#57606a">{issues["open"]} open</text>')
    svg.append(f'<circle cx="150" cy="295" r="4" fill="#8250df"/><text x="160" y="300" font-size="14" fill="#57606a">{issues["closed"]} closed</text>')

    # PRs
    svg.append(f'<text x="320" y="250" font-size="15" fill="#0969da">Pull requests</text>')
    svg.append(generate_bar(320, 265, 230, 10, [
        ("open", prs["open"], "#1a7f37"),
        ("merged", prs["merged"], "#8250df"),
        ("closed", prs["closed"], "#d1242f"),
    ]))
    svg.append(f'<circle cx="325" cy="295" r="4" fill="#1a7f37"/><text x="335" y="300" font-size="14" fill="#57606a">{prs["open"]} open</text>')
    svg.append(f'<circle cx="410" cy="295" r="4" fill="#8250df"/><text x="420" y="300" font-size="14" fill="#57606a">{prs["merged"]} merged</text>')
    svg.append(f'<circle cx="495" cy="295" r="4" fill="#d1242f"/><text x="505" y="300" font-size="14" fill="#57606a">{prs["closed"]} closed</text>')

    svg.append('</svg>')
    return "\n".join(svg)

def render_activity_svg(activities):
    W, H = 600, 390
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<style>',
        '  text { font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }',
        '  .title { font-weight: 400; font-size: 18px; fill: #0969da; }',
        '  .item-title { font-size: 14px; fill: #0969da; }',
        '  .item-meta { font-size: 13px; fill: #57606a; }',
        '  .repo-name { fill: #0969da; }',
        '</style>',
        '<rect width="100%" height="100%" fill="#ffffff" rx="8" stroke="none"/>',
        '<text x="40" y="45" class="title">Recent Activity</text>'
    ]

    if not activities:
        svg.append('<text x="40" y="90" font-size="14" fill="#57606a">No recent public activity found.</text>')
    else:
        y_pos = 90
        for act in activities:
            is_pr = act["__typename"] == "PullRequest"
            
            repo_name = act["repository"]["nameWithOwner"]
            number = act["number"]
            title = act["title"]
            
            icon_path = "M11.75 2.5a.75.75 0 100 1.5.75.75 0 000-1.5zm-2.25.75a2.25 2.25 0 113 2.122V6A2.5 2.5 0 0110 8.5H6a1 1 0 00-1 1v1.128a2.251 2.251 0 11-1.5 0V5.372a2.25 2.25 0 111.5 0v1.836A2.492 2.492 0 016 7h4a1 1 0 001-1v-.628A2.25 2.25 0 019.5 3.25zM4.25 12a.75.75 0 100 1.5.75.75 0 000-1.5zM3.5 3.25a.75.75 0 111.5 0 .75.75 0 01-1.5 0z" if is_pr else "M7.177 3.073L9.573.677A.25.25 0 0110 .854v4.792a.25.25 0 01-.427.177L7.177 3.427a.25.25 0 010-.354zM3.75 2.5a.75.75 0 100 1.5.75.75 0 000-1.5zm-2.25.75a2.25 2.25 0 113 2.122v5.256a2.251 2.251 0 11-1.5 0V5.372A2.25 2.25 0 011.5 3.25zM11 2.5h-1V4h1a1 1 0 011 1v5.628a2.251 2.251 0 101.5 0V5A2.5 2.5 0 0011 2.5zm1 10.25a.75.75 0 111.5 0 .75.75 0 01-1.5 0zM3.75 12a.75.75 0 100 1.5.75.75 0 000-1.5z"
            svg.append(f'<g transform="translate(38, {y_pos - 12})"><path fill="#57606a" fill-rule="evenodd" d="{icon_path}"/></g>')
            
            # Title line: Opened #123 Title (Truncated if too long)
            display_title = escape_text(title)
            if len(display_title) > 55:
                display_title = display_title[:52] + "..."
                
            svg.append(f'<text x="60" y="{y_pos}" class="item-title">Opened #{number} {display_title}</text>')
            
            # Meta line: in repo (repo colored blue)
            svg.append(f'<text x="60" y="{y_pos + 18}" class="item-meta">in <tspan class="repo-name">{escape_text(repo_name)}</tspan></text>')
            
            y_pos += 55

    svg.append('</svg>')
    return "\n".join(svg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-u", "--user", default="namnkahn1607")
    ap.add_argument("-os", "--output-stats", default="stats.svg")
    ap.add_argument("-oa", "--output-activity", default="activity.svg")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("Error: Target environment is missing GITHUB_TOKEN")

    try:
        langs, colors = fetch_language_stats(args.user, token)
        stats = fetch_contribution_stats(args.user, token)
        activities = fetch_recent_activity(args.user, token)

        stats_svg = render_stats_svg(langs, colors, stats)
        with open(args.output_stats, "w", encoding="utf-8") as f:
            f.write(stats_svg)

        activity_svg = render_activity_svg(activities)
        with open(args.output_activity, "w", encoding="utf-8") as f:
            f.write(activity_svg)

        print(f"Successfully generated {args.output_stats} and {args.output_activity}!")

    except Exception as e:
        sys.exit(f"Runtime execution failed: {str(e)}")


if __name__ == "__main__":
    main()
