#!/usr/bin/env python3
"""
Auto README generator:
- Input: GitHub repo URL (e.g. https://github.com/owner/repo)
- Uses GitHub API to fetch file list and code snippets
- Sends summary + snippets to Gemini API
- Writes README.md locally

Requirements:
    pip install requests python-dotenv

Environment variables:
    GITHUB_TOKEN (optional, increases API rate limit)
    GOOGLE_API_KEY (required)
    GEMINI_MODEL (optional, default: "gemini-2.0-flash")
"""

import os
import re
import base64
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv
from typing import List, Dict, Tuple

# Load .env file
load_dotenv()

# Env vars
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

HEADERS_GITHUB = {"Accept": "application/vnd.github.v3+json"}
if GITHUB_TOKEN:
    HEADERS_GITHUB["Authorization"] = f"token {GITHUB_TOKEN}"

MAX_SNIPPET_BYTES = 20 * 1024  # 20 KB per file
MAX_TOTAL_BYTES = 40 * 1024    # 40 KB total for prompt
SNIPPET_LINE_LIMIT = 300       # Max 300 lines per file


def parse_github_url(url: str) -> Tuple[str, str, str]:
    """Parse GitHub URL, return (owner, repo, branch)"""
    p = urlparse(url)
    parts = p.path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError("Invalid GitHub repo URL")
    owner, repo = parts[0], parts[1].replace(".git", "")
    branch = "main"
    if len(parts) >= 4 and parts[2] == "tree":
        branch = parts[3]
    return owner, repo, branch


def get_repo_tree(owner: str, repo: str, branch: str = "main") -> List[Dict]:
    """List all files in the repo using GitHub Trees API"""
    # Get branch SHA
    url_ref = f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}"
    r = requests.get(url_ref, headers=HEADERS_GITHUB)
    if r.status_code == 404:
        info = requests.get(f"https://api.github.com/repos/{owner}/{repo}", headers=HEADERS_GITHUB).json()
        branch = info.get("default_branch", "main")
        url_ref = f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}"
        r = requests.get(url_ref, headers=HEADERS_GITHUB)
    r.raise_for_status()
    sha = r.json()["object"]["sha"]

    # Get tree
    url_tree = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{sha}?recursive=1"
    r = requests.get(url_tree, headers=HEADERS_GITHUB)
    r.raise_for_status()
    return r.json().get("tree", [])


def select_relevant_files(tree: List[Dict]) -> List[Dict]:
    """Pick up to 50 relevant code/documentation files"""
    picked = []

    for item in tree:
        if item["type"] != "blob":
            continue
        path = item["path"]

        # Include all Python, Markdown, config, or small text files
        if path.endswith((".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg")):
            picked.append(item)

    # If nothing selected, fallback to all top-level files
    if not picked:
        picked = [t for t in tree if t["type"] == "blob"]

    return picked[:50]  # Limit to 50 files


def fetch_file_snippet(owner: str, repo: str, path: str, branch="main") -> str:
    """Fetch raw file content and trim"""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    r = requests.get(url, headers=HEADERS_GITHUB, params={"ref": branch})
    if r.status_code != 200:
        return ""
    data = r.json()

    if data.get("encoding") == "base64":
        content = base64.b64decode(data["content"]).decode(errors="replace")
    else:
        content = data.get("content", "")

    lines = content.splitlines()[:SNIPPET_LINE_LIMIT]
    snippet = "\n".join(lines)

    if len(snippet.encode("utf-8")) > MAX_SNIPPET_BYTES:
        snippet = snippet.encode("utf-8")[:MAX_SNIPPET_BYTES].decode("utf-8", errors="ignore")
    return snippet


def build_prompt(owner, repo, branch, files_snippets, repo_url):
    """Build structured prompt for Gemini"""
    intro = (
    "You are an expert developer & technical writer. "
    "Analyze this repository deeply and write a precise README. "
    "Do NOT wrap output in ```markdown or triple backticks. "
    "Output clean GitHub-flavored Markdown (GFM) only.\n"
    )
    meta = f"\nRepository: {owner}/{repo}\nURL: {repo_url}\nBranch: {branch}\n\n"
    files_list = "Files included:\n"
    for p, s in files_snippets.items():
        files_list += f"- {p} : {len(s.splitlines())} lines\n"
    files_list += "\n"

    snippets = ""
    total_bytes = 0
    for path, snippet in files_snippets.items():
        size = len(snippet.encode("utf-8"))
        if size == 0:
            continue
        if total_bytes + size > MAX_TOTAL_BYTES:
            break
        snippets += f"---\nFile: {path}\n```\n{snippet}\n```\n"
        total_bytes += size

    instructions = (
        "\nWrite a concise README with:\n"
        "- Short project description\n"
        "- Key features (bullet points)\n"
        "- Installation steps\n"
        "- Usage examples\n"
        "- Tech stack and dependencies\n"
        "- 3 suggestions for improvements\n"
        "Keep it 300-800 words and use Markdown.\n"
    )

    return intro + meta + files_list + snippets + instructions


def call_gemini(prompt: str) -> str:
    """Send prompt to Gemini API"""
    if not GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY is missing!")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GOOGLE_API_KEY}"
    headers = {"Content-Type": "application/json"}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 800},
    }

    r = requests.post(url, headers=headers, json=body)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini API error {r.status_code}: {r.text}")

    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        raise RuntimeError(f"Unexpected Gemini API response: {data}")


def generate_readme_from_repo(repo_url: str, out="README.md"):
    """Main pipeline to generate README"""
    owner, repo, branch = parse_github_url(repo_url)
    print(f"[+] Fetching repo tree for {owner}/{repo} (branch: {branch})...")
    tree = get_repo_tree(owner, repo, branch)
    picked = select_relevant_files(tree)
    print(f"[+] Selected {len(picked)} files for snippet extraction.")

    snippets = {}
    for f in picked:
        path = f["path"]
        try:
            snippets[path] = fetch_file_snippet(owner, repo, path, branch)
        except Exception as e:
            print(f"[-] Failed to fetch {path}: {e}")

    prompt = build_prompt(owner, repo, branch, snippets, repo_url)
    print("[+] Sending prompt to Gemini API...")
    readme = call_gemini(prompt)

    if not readme:
        raise RuntimeError("Gemini returned empty README.")

    with open(out, "w", encoding="utf-8") as f:
        f.write(readme)
    print(f"[+] README saved to {out}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate README from GitHub repo using Gemini API")
    parser.add_argument("repo_url", help="GitHub repository URL")
    parser.add_argument("--out", default="README.md", help="Output README file path")
    args = parser.parse_args()

    generate_readme_from_repo(args.repo_url, args.out)
