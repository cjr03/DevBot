"""The ticket popup's data: the Jira issue over REST and its pull request chain over gh. No Claude, so it is quick and free."""
import base64
import hashlib
import io
import json
import pathlib
import re
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import urllib.request

from rich import box
from rich.console import Group
from rich.markup import escape
from rich.padding import Padding
from rich.segment import Segment
from rich.panel import Panel
from rich.style import Style
from rich.text import Text

from devbot import bus, graph, triggers

PR = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
_hits = {}  # ticket key -> PRs its last successful GitHub search found, reused when the search is rate limited
FIELDS = "number,title,body,author,createdAt,state,url,baseRefName,headRefName,mergeable,reviewDecision,commits,comments,reviews,statusCheckRollup"
IMG = re.compile(r"!\[[^\]]*\]\((\S+?)\)|<img[^>]*?src=[\"']([^\"']+)")  # Markdown and HTML images in GitHub text
# github.com/o/r/blob|raw/<ref>/<path> and raw.githubusercontent.com/o/r/<ref>/<path>
# ponytail: ref is taken as one path segment (a SHA or simple branch); a branch name with "/" in it would split wrong
REPO_FILE = re.compile(r"https://(?:github\.com/([\w.-]+)/([\w.-]+)/(?:blob|raw)|raw\.githubusercontent\.com/([\w.-]+)/([\w.-]+))/([^/?#]+)/([^?#]+)")
MAX_COLS, MAX_ROWS = 100, 50  # pictures stay small; "Open full size" shows the real pixels
CELL = 2.0  # a terminal cell's height over its width; ui sets the real one when the terminal reports it
KITTY = None  # set by ui at startup when the terminal draws real images (Ghostty, Kitty): textual_image's TGP renderable
PHOTO_COLS, MAX_PHOTOS, MAX_BYTES = 60, 6, 8_000_000
PASS, FAIL = {"SUCCESS", "NEUTRAL", "SKIPPED"}, {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def _jira_auth():
    e = triggers._env()
    return "Basic " + base64.b64encode(f"{e['JIRA_EMAIL']}:{e['JIRA_API_TOKEN']}".encode()).decode()


def _jira(path, body=None):
    e, auth = triggers._env(), _jira_auth()
    req = urllib.request.Request(f"{e['JIRA_BASE_URL'].rstrip('/')}/rest/api/3/{path}", data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": auth, "Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise OSError(r.stderr.strip() or "gh failed")
    return json.loads(r.stdout) if r.stdout.strip() else None


def adf(node):
    """Plain text from Jira's rich-text JSON (Atlassian Document Format)."""
    if isinstance(node, list):
        return "".join(adf(n) for n in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind in ("mention", "emoji"):
        return node.get("attrs", {}).get("text", "")
    if kind == "hardBreak":
        return "\n"
    inner = adf(node.get("content", []))
    return ("• " if kind == "listItem" else "") + inner + ("\n" if kind in ("paragraph", "heading", "codeBlock") else "")


def issue(key):
    f = _jira(f"issue/{key}?fields=summary,status,assignee,reporter,priority,issuetype,description,comment,updated,attachment")["fields"]
    who = lambda p: (p or {}).get("displayName", "—")
    return {"key": key, "summary": f.get("summary", ""), "status": (f.get("status") or {}).get("name", "?"),
            "type": (f.get("issuetype") or {}).get("name", ""), "priority": (f.get("priority") or {}).get("name", ""),
            "assignee": who(f.get("assignee")), "reporter": who(f.get("reporter")), "updated": f.get("updated", ""),
            "description": adf(f.get("description")).strip(),
            "comments": [{"by": who(c.get("author")), "at": c.get("created", ""), "text": adf(c.get("body")).strip()}
                         for c in (f.get("comment") or {}).get("comments", [])],
            "photos": [(a.get("filename", ""), a["content"]) for a in f.get("attachment") or []
                       if str(a.get("mimeType", "")).startswith("image/") and a.get("content")]}


_pr_keys = set()  # last good GitHub search, kept while the search is rate-limited


def mine():
    """(assigned, worked, blocked) from Jira: {key: (status, summary)} for the open tickets assigned to you and the ones
    you once had that are now done or reassigned, and {key: [open tickets it "is blocked by"]}."""
    blocked = {}
    def q(jql):
        r = _jira("search/jql?" + urllib.parse.urlencode({"jql": jql + " ORDER BY updated DESC", "maxResults": 100, "fields": "summary,status,issuelinks"}))
        for i in r.get("issues", []):
            by = [l["inwardIssue"]["key"] for l in i["fields"].get("issuelinks") or [] if l.get("type", {}).get("inward") == "is blocked by"
                  and "inwardIssue" in l and l["inwardIssue"].get("fields", {}).get("status", {}).get("statusCategory", {}).get("key") != "done"]
            if by:
                blocked[i["key"]] = by
        return {i["key"]: (i["fields"]["status"]["name"], i["fields"]["summary"]) for i in r.get("issues", [])}
    assigned = q("assignee = currentUser() AND statusCategory != Done")
    worked = q("assignee was currentUser() AND NOT (assignee = currentUser() AND statusCategory != Done)")
    global _pr_keys
    try:
        _pr_keys = {k for p in _gh("search", "prs", "--author", "@me", "--limit", "200", "--json", "title") or [] for k in bus.TICKET.findall(p["title"])}
    except OSError:  # GitHub's search rate limit: keep the last keys, so their tickets don't flicker out of the lists
        pass
    prs = _pr_keys
    for k in sorted(prs - assigned.keys() - worked.keys()):  # you opened a PR for it while someone else was the assignee
        try:
            worked |= q(f"key = {k}")
        except OSError:  # a key from another Jira, or one you cannot see
            pass
    return assigned, worked, blocked


def comment_jira(key, text):
    paras = [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in text.splitlines() if line.strip()]
    _jira(f"issue/{key}/comment", {"body": {"type": "doc", "version": 1, "content": paras}})


def _repo(repo):
    """The repo's default branch and its open PRs by number (head and base branch): one listing instead of a call per PR."""
    trunk = _gh("repo", "view", repo, "--json", "defaultBranchRef")["defaultBranchRef"]["name"]
    return trunk, {str(p["number"]): p for p in _gh("pr", "list", "-R", repo, "--json", "number,headRefName,baseRefName", "--limit", "200") or []}


def prs(key):
    """The ticket's PRs (CodeBrain's memory, then a GitHub search for the key), each followed down its chain of base
    branches: a PR into another PR's branch is stacked on it. Top of the stack first. Every gh call that can run at
    once does, so the whole chain takes about two round trips."""
    known = bus.load_memory("CodeBrain").get(key)
    found = PR.findall(str(known.get("pr", "")) if isinstance(known, dict) else "")
    with ThreadPoolExecutor(8) as pool:
        search = pool.submit(_gh, "search", "prs", key, "--json", "url", "--limit", "5")
        repos = {r: pool.submit(_repo, r) for r, _ in found}  # start on the PRs we already know while the search runs
        try:
            _hits[key] = [PR.search(p["url"]).groups() for p in search.result() or []]
        except OSError:  # the search API's tight rate limit: the PRs we already know or found before still show
            if not found and key not in _hits:
                raise
        found += _hits.get(key, [])
        repos.update({r: pool.submit(_repo, r) for r, _ in found if r not in repos})
        order, seen = [], set()
        for repo, num in dict.fromkeys(found):
            trunk, open_ = repos[repo].result()
            while num and (repo, num) not in seen and len(order) < 8:
                seen.add((repo, num))
                order.append((repo, num))
                base = (open_.get(num) or _gh("pr", "view", num, "-R", repo, "--json", "baseRefName"))["baseRefName"]
                num = None if base == trunk else next((n for n, p in open_.items() if p["headRefName"] == base), None)
        views = pool.map(lambda rn: {**_gh("pr", "view", rn[1], "-R", rn[0], "--json", FIELDS), "repo": rn[0], "trunk": repos[rn[0]].result()[0]}, order)
        return list(views)


def comment_pr(pr, text):
    subprocess.run(["gh", "pr", "comment", str(pr["number"]), "-R", pr["repo"], "--body", text], check=True, capture_output=True, timeout=30)


GITHUB_HOSTS = {"github.com", "api.github.com", "user-images.githubusercontent.com", "private-user-images.githubusercontent.com"}


class _KeepCredsHome(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but your token never leaves the host it was meant for (GitHub sends images on to signed S3 links)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.headers.pop("Authorization", None); new.unredirected_hdrs.pop("Authorization", None)
        return new


_OPENER = urllib.request.build_opener(_KeepCredsHome)


class Picture:
    """A photo. In Ghostty or Kitty it is the real image (Kitty graphics protocol); elsewhere half blocks: each cell's ▀
    is the upper pixel, its background the lower. It is redrawn at
    exactly the width it is given, from the full-resolution image with Lanczos filtering, so it is as sharp as a
    terminal grid allows (Apple Terminal has no inline image protocol; "Open full size" shows the real pixels)."""

    def __init__(self, img, cols=None):
        self.img, self.cols, self._drawn = img, cols, {}

    def __rich_console__(self, console, options):
        w = self._width(options.max_width)
        if KITTY:  # real pixels: the terminal holds the image; Textual only sees placeholder characters, so it scrolls
            # ponytail: each width sent stays in the terminal's image memory until DevBot restarts; Ghostty evicts past its quota
            try:  # a very tall photo needs more rows than the protocol can place (ValueError); draw it in half blocks instead
                if ("kitty", w) not in self._drawn:
                    self._drawn[("kitty", w)] = KITTY(self.img, width=w, height="auto")  # rows follow the photo's shape
                segments = list(self._drawn[("kitty", w)].__rich_console__(console, options))
            except Exception:  # noqa: BLE001 — never crash the app over a picture
                segments = None
            if segments is not None:
                yield from segments
                return
        if w not in self._drawn:
            from PIL import Image
            h = max(2, round(self.img.height * w / self.img.width / CELL) * 2)  # two pixels per cell, CELL = its height/width
            px = self.img.resize((w, h), Image.LANCZOS).load()
            hexa = lambda c: "#%02x%02x%02x" % c
            self._drawn[w] = [[Segment("▀", Style(color=hexa(px[x, y]), bgcolor=hexa(px[x, y + 1]))) for x in range(w)]
                              for y in range(0, h, 2)]
        for row in self._drawn[w]:
            yield from row
            yield Segment.line()

    def _width(self, room):
        """Columns to draw at: fits the room, at most MAX_COLS wide and MAX_ROWS tall however tall the photo is."""
        fit_rows = int(MAX_ROWS * CELL * self.img.width / self.img.height)
        return max(1, min(self.cols or room, room, self.img.width, MAX_COLS, fit_rows))

    def __rich_measure__(self, console, options):
        from rich.measure import Measurement
        return Measurement(1, self._width(options.max_width))


def picture(data, cols=None):
    from PIL import Image
    return Picture(Image.open(io.BytesIO(data)).convert("RGB"), cols)


def keep(data, url):
    """Save the original next to the chat photos so "Open full size" can show it in Preview, pixel for pixel. It is
    decoded and saved again as PNG under a fixed name, so a link can never plant a file macOS would run (.command, .app)."""
    from PIL import Image
    from devbot import graph as g
    name = "pr-" + hashlib.sha256(url.encode()).hexdigest()[:32] + ".png"
    g.MEDIA.mkdir(parents=True, exist_ok=True)
    Image.open(io.BytesIO(data)).save(g.MEDIA / name, "PNG")
    return name


def shown(data, name):
    """The drawn photo with its Open full size link."""
    link = [Text.from_markup(f"[@click=app.open_photo('{name}')][{graph.TEAL}]⤢ Open full size[/][/]")] if graph.PHOTO_NAME.fullmatch(name) else []
    return Padding(Group(picture(data), *link), (0, 0, 1, 0))  # only a plain file name ever goes inside the click markup


def photo(url, name=""):
    """Fetch and draw one image; GitHub and Jira need your credentials. A failure is a line saying so, never an error."""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").rstrip(".").lower()
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ValueError("not https")
        repo_file = REPO_FILE.match(url)
        if repo_file:  # a file in a repo: github.com answers 404 to tokens on private repos, the contents API serves it
            o1, r1, o2, r2, ref, path = repo_file.groups()
            owner, repo = o1 or o2, r1 or r2
            r = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{owner}/{repo}/contents/{path}?ref={ref}"],
                               capture_output=True, timeout=30)
            if r.returncode:
                raise OSError(r.stderr.decode(errors="replace").strip()[:80] or "gh api failed")
            return shown(r.stdout[:MAX_BYTES], keep(r.stdout[:MAX_BYTES], url))
        headers = {}
        if host == (urllib.parse.urlsplit(triggers._env().get("JIRA_BASE_URL", "")).hostname or "").lower():
            headers["Authorization"] = _jira_auth()
        elif host in GITHUB_HOSTS:
            token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10).stdout.strip()
            if token:
                headers["Authorization"] = f"token {token}"
        with _OPENER.open(urllib.request.Request(url, headers=headers), timeout=20) as r:
            data = r.read(MAX_BYTES)
            return shown(data, keep(data, url))
    except Exception as e:  # noqa: BLE001 — any bad image (network, auth, format) just shows as unavailable
        return Text.from_markup(f"  [{graph.DIM}]🖼 {escape(name or url.rsplit('/', 1)[-1])}: couldn't show ({escape(str(getattr(e, "code", "")) or type(e).__name__)})[/]")


def _urls(body):
    return [a or b for a, b in IMG.findall(body or "")]


def _photos(body, pics):
    """The images a GitHub body links to, from the batch fetched up front (none until the photos have loaded)."""
    return [pics[u] for u in _urls(body) if u in pics]


def _group(out):
    """Runs of markup lines become one Text; Markdown blocks and photos stand on their own."""
    parts, lines = [], []
    for o in out + [None]:
        if isinstance(o, str):
            lines.append(o)
            continue
        if lines:
            parts.append(Text.from_markup("\n".join(lines)))
            lines = []
        if o is not None:
            parts.append(o)
    return Group(*parts)


def _when(ts):
    return escape(graph.when(ts))


def left(t):
    """Jira side: status line, description, every comment."""
    W, D, T = graph.WHITE, graph.DIM, graph.TEAL
    out = [f"[bold {W}]{escape(t['summary'])}[/]",
           f"[bold {T}]{escape(t['status'])}[/]  [{D}]{escape(t['type'])} · {escape(t['priority'])} · updated {_when(t['updated'])}[/]",
           f"[{D}]assignee[/] [{W}]{escape(t['assignee'])}[/]  [{D}]reporter[/] [{W}]{escape(t['reporter'])}[/]", "",
           f"[{W}]{escape(t['description'] or 'No description.')}[/]", "", f"[bold {T}]Comments ({len(t['comments'])})[/]"]
    out += [f"\n[bold {T}]{escape(c['by'])}[/]  [{D}]{_when(c['at'])}[/]\n[{W}]{escape(c['text'])}[/]" for c in t["comments"]]
    if t.get("photos"):
        out += ["", f"[bold {T}]Photos ({len(t['photos'])})[/]"]
        for name, url in t["photos"][:MAX_PHOTOS]:
            out += [f"  [{D}]{escape(name)}[/]", photo(url, name)]
    return _group(out)


def _result(c):
    return (c.get("conclusion") or c.get("state") or "").upper()


def _check(c):
    r = _result(c)
    icon, tone = ("✓", graph.MINT) if r in PASS else ("✗", graph.CORAL) if r in FAIL else ("◌", graph.AMBER)
    return f"  [{tone}]{icon}[/] [{graph.WHITE}]{escape(c.get('name') or c.get('context') or '?')}[/] [{graph.DIM}]{escape(r.lower() or 'running')}[/]"


def _tone(p):
    """A tile's colour: coral when CI fails or it conflicts, mint when merged or approved and green, amber while CI runs."""
    results = [_result(c) for c in p.get("statusCheckRollup") or []]
    if p.get("mergeable") == "CONFLICTING" or any(r in FAIL for r in results):
        return graph.CORAL
    if p.get("state") == "MERGED" or (p.get("reviewDecision") == "APPROVED" and all(r in PASS for r in results)):
        return graph.MINT
    return graph.AMBER if any(r not in PASS for r in results) else graph.TEAL


def flow(chain):
    """The stack drawn like a git graph: one tile per PR, its commits on a rail, arrows down to the branch it merges into."""
    W, D = graph.WHITE, graph.DIM
    out = []
    for p in chain:
        tone, results = _tone(p), [_result(c) for c in p.get("statusCheckRollup") or []]
        commits = list(reversed(p.get("commits") or []))  # newest on top, like git log
        rail = [f"[{tone}]●[/] [{D}]{c['oid'][:7]}[/] [{W}]{escape(c.get('messageHeadline', ''))}[/]" for c in commits[:8]]
        rail += [f"[{tone}]┆[/] [{D}]+{len(commits) - 8} earlier[/]"] if len(commits) > 8 else []
        body = "\n".join([f"[bold {W}]{escape(p['title'])}[/]",
                           f"[{D}]{escape(p['headRefName'])}[/]",
                           f"[{graph.MINT}]✓{sum(r in PASS for r in results)}[/] [{graph.CORAL}]✗{sum(r in FAIL for r in results)}[/] "
                           f"[{graph.AMBER}]◌{sum(r not in PASS | FAIL for r in results)}[/]  [{D}]{escape(p.get('mergeable') or '?')} · "
                           f"{escape((p.get('reviewDecision') or 'no review').replace('_', ' ').lower())}[/]", *rail])
        text = Text.from_markup(body, overflow="ellipsis")
        text.no_wrap = True  # one line per commit, like git log --oneline
        out.append(Panel(text, title=f"[bold {tone}]#{p['number']} · {escape(p['state'].lower())}[/]",
                         title_align="left", border_style=tone, box=box.ROUNDED, padding=(0, 1)))
        last = p is chain[-1]
        out.append(Text.from_markup(f"  [{D}]│\n  ▼[/] [{D}]into[/] [{W}]{escape(p['baseRefName'])}[/]" + (
            f"\n  [bold {graph.TEAL}]◉[/] [{D}]{escape(p.get('trunk') or p['baseRefName'])}[/]" if last else "")))
    return out


REVIEWED = {"APPROVED": ("approved these changes", "MINT"), "CHANGES_REQUESTED": ("requested changes", "CORAL"),
            "COMMENTED": ("reviewed", "DIM"), "DISMISSED": ("had a review dismissed", "DIM")}


def _comment(who, verb, at, body, pics, tone=None):
    """One GitHub comment box: "who verb · when" in the header, the Markdown and its photos inside."""
    inner = [graph.md(body)] if (body or "").strip() else [Text.from_markup(f"[{graph.DIM}]No description provided.[/]")]
    return Panel(Group(*inner, *_photos(body, pics)), title=f"[bold {graph.WHITE}]{escape(who)}[/] [{graph.DIM}]{verb} · {_when(at)}[/]",
                 title_align="left", border_style=tone or graph.FAINT, box=box.ROUNDED, padding=(0, 1))


def conversation(p, pics):
    """A PR laid out like its GitHub Conversation tab: title, the merge line, the description as the first comment, then
    commits, comments and reviews on one timeline by time, and the checks and merge boxes at the bottom."""
    W, D, T = graph.WHITE, graph.DIM, graph.TEAL
    author = (p.get("author") or {}).get("login", "?")
    commits = p.get("commits") or []
    state = {"OPEN": graph.MINT, "MERGED": graph.VIOLET, "CLOSED": graph.CORAL}.get(p["state"], D)
    out = ["", f"[bold {W}]{escape(p['title'])}[/] [{D}]#{p['number']}[/]",
           f"[bold {state}] {escape(p['state'].title())} [/] [bold {W}]{escape(author)}[/] [{D}]wants to merge {len(commits)} commit"
           f"{'s' * (len(commits) != 1)} into[/] [{T}]{escape(p['baseRefName'])}[/] [{D}]from[/] [{T}]{escape(p['headRefName'])}[/]",
           _comment(author, "commented", p.get("createdAt", ""), p.get("body") or "", pics)]
    events = [(c.get("committedDate") or c.get("authoredDate") or "", 0, c) for c in commits]
    events += [(c.get("createdAt", ""), 1, c) for c in p.get("comments") or []]
    events += [(r.get("submittedAt", ""), 2, r) for r in p.get("reviews") or []]
    for at, kind, e in sorted(events, key=lambda x: x[:2]):
        out.append(f"  [{D}]│[/]")
        if kind == 0:
            who = ", ".join(a.get("login") or a.get("name", "?") for a in e.get("authors") or []) or "?"
            out.append(f"  [{T}]●[/] [{W}]{escape(who)}[/] [{D}]committed[/]  [{W}]{escape(e.get('messageHeadline', ''))}[/]  "
                       f"[{D}]{e['oid'][:7]} · {_when(at)}[/]")
        elif kind == 1:
            out.append(_comment((e.get("author") or {}).get("login", "?"), "commented", at, e.get("body", ""), pics))
        else:
            verb, tone = REVIEWED.get(e.get("state", ""), ("reviewed", "DIM"))
            if not (e.get("body") or "").strip():  # inline-only review: a timeline line, like GitHub, not an empty box
                out.append(f"  [{getattr(graph, tone)}]●[/] [{W}]{escape((e.get('author') or {}).get('login', '?'))}[/] [{D}]{verb} · {_when(at)}[/]")
                continue
            out.append(_comment((e.get("author") or {}).get("login", "?"), verb, at, e.get("body", ""), pics, getattr(graph, tone)))
    checks = p.get("statusCheckRollup") or []
    results = [_result(c) for c in checks]
    bad, ok = sum(r in FAIL for r in results), sum(r in PASS for r in results)
    title, tone = (("Some checks were not successful", graph.CORAL) if bad else ("Some checks haven't completed yet", graph.AMBER)
                   if ok < len(checks) else ("All checks have passed", graph.MINT) if checks else ("No checks ran", D))
    merge = ("This branch has conflicts that must be resolved", graph.CORAL) if p.get("mergeable") == "CONFLICTING" else (
        ("Changes approved", graph.MINT) if p.get("reviewDecision") == "APPROVED" else ("Review required", graph.AMBER)
        if p.get("reviewDecision") == "REVIEW_REQUIRED" else ("This branch has no conflicts with the base branch", graph.MINT)
        if p.get("mergeable") == "MERGEABLE" else ("Checking mergeability…", D))
    out += [f"  [{D}]│[/]", Panel(Group(Text.from_markup(f"[{D}]{bad} failing, {len(checks) - bad - ok} in progress, {ok} successful checks[/]"),
                                         *(Text.from_markup(_check(c)) for c in checks),
                                         Text.from_markup(f"\n[bold {merge[1]}]{'✗' if merge[1] == graph.CORAL else '✓'} {merge[0]}[/]")),
                                   title=f"[bold {tone}]{title}[/]", title_align="left", border_style=tone, box=box.ROUNDED, padding=(0, 1))]
    return out


def right(chain, photos=True):
    """GitHub side: the tile flow of the stack, then each PR as its Conversation tab. Without photos it draws at once;
    with them it first fetches every image in parallel."""
    if not chain:
        return f"[{graph.DIM}]No pull requests found for this ticket.[/]"
    bodies = [p.get("body") for p in chain] + [c.get("body") for p in chain for c in (p.get("comments") or []) + (p.get("reviews") or [])]
    urls = list(dict.fromkeys(u for b in bodies for u in _urls(b)))[:MAX_PHOTOS] if photos else []
    with ThreadPoolExecutor(MAX_PHOTOS) as pool:
        pics = dict(zip(urls, pool.map(photo, urls)))
    out = [*flow(chain)]
    for p in chain:
        out += conversation(p, pics)
    return _group(out)
