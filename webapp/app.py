"""
webapp/app — Flask GUI for Company OS

Run:
  python company-os/webapp/app.py
  → http://localhost:5050

Routes:
  GET  /                          → company picker / dashboard
  GET  /c/<company>/              → dashboard for a single company
  GET  /c/<company>/departments   → dept list
  GET  /c/<company>/departments/<name>  → dept detail
  GET  /c/<company>/board         → board profiles + meeting list
  GET  /c/<company>/board/meetings/<filename> → meeting view
  GET  /c/<company>/sessions      → session list
  GET  /c/<company>/sessions/<id> → session files
  GET  /c/<company>/decisions     → decisions list
  GET  /c/<company>/artifacts     → demo-artifacts browser
  GET  /c/<company>/view?path=... → markdown file viewer (sandboxed)
  GET  /c/<company>/run           → action launcher
  POST /c/<company>/run/dispatch  → start a manager dispatch (background job)
  POST /c/<company>/run/board     → start a board deliberation (background job)
  POST /c/<company>/run/full-demo → start the full demo run (background job)
  GET  /c/<company>/jobs          → job list page
  GET  /c/<company>/jobs/<id>     → job detail (auto-refreshes if running)
  GET  /api/jobs/<id>             → JSON job status (for polling)
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

# UTF-8 fix for Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Make sibling modules (core/, comprehensive_demo) importable
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent  # company-os/
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from core.env import load_env  # noqa: E402
load_env()

from flask import (  # noqa: E402
    Flask, abort, jsonify, redirect, render_template, request, url_for
)
from urllib.parse import urlparse
from markdown_it import MarkdownIt  # noqa: E402

from core.governance import retrolog_dispatch  # noqa: E402

from webapp.services import (  # noqa: E402
    JOB_REGISTRY,
    cost_log_reader,
    discover_companies,
    list_board_meetings,
    list_board_profiles,
    list_decisions,
    list_demo_artifacts,
    list_dept_summaries,
    list_sessions,
    load_company_safe,
    load_departments_safe,
    read_artifact_safe,
    read_company_summary,
    read_dept_detail,
    run_board_action,
    run_dispatch_action,
    run_full_demo_action,
)


# ---------------------------------------------------------------------------
# Markdown rendering (Phase 3 — markdown-it-py replaces the hand-rolled
# renderer). CommonMark-compliant, actively maintained, escapes attribute
# values properly (fixes CRIT-5 — the hand-rolled renderer left `"` and
# `'` unescaped in link URLs, allowing `[x](" onclick="alert(1))` to break
# out of the href attribute).
# ---------------------------------------------------------------------------

# Schemes that must never appear in rendered hrefs. Case-insensitive match
# after stripping whitespace and null bytes that can be used to bypass
# naive prefix checks (e.g. "java\x00script:"). markdown-it-py's default
# validator is close to this but does not cover `blob:` or the whitespace-
# prefix bypass, so we plug our own in.
_DANGEROUS_HREF_RE = re.compile(
    r"^[\x00-\x20]*(?:javascript|vbscript|data|blob|file):", re.IGNORECASE
)


def _validate_link(url: str) -> bool:
    """Return True if markdown-it-py should render the link, False to drop it.

    Matches the protection of the pre-Phase-3 hand-rolled `_safe_href()`:
    blocks javascript/vbscript/data/blob/file, plus any whitespace-or-null-
    byte-prefixed variant. When False is returned, markdown-it-py renders
    the link text as plain text without the anchor wrapper — strictly safer
    than the prior `href="#"` fallback.
    """
    return _DANGEROUS_HREF_RE.match(url) is None


def _render_fence(tokens, idx, options, env):
    """Custom fence renderer that preserves the `pre.code` CSS selector used
    throughout the existing templates. markdown-it-py's default emits
    `<pre><code class="language-X">`; the app's CSS targets `pre.code`.
    """
    token = tokens[idx]
    content = (
        token.content
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f'<pre class="code"><code>{content}</code></pre>\n'


_MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "breaks": False})
_MD.validateLink = _validate_link
_MD.renderer.rules["fence"] = _render_fence
_MD.renderer.rules["code_block"] = _render_fence


def render_markdown(text: str) -> str:
    """Render a markdown string to HTML.

    Uses markdown-it-py (CommonMark) with html=false so raw HTML is escaped,
    a custom link validator that blocks dangerous URL schemes, and a custom
    fence renderer that keeps the `pre.code` class used by `style.css`.
    Attribute values are properly escaped by the library, which is the
    CRIT-5 fix.
    """
    if not text:
        return ""
    return _MD.render(text)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
def _safe_back_url(url: str, slug: str) -> str:
    """Validate a caller-supplied 'back' URL.

    Only allow relative paths that start with '/' and contain no scheme,
    preventing open-redirect attacks and javascript: pseudo-URLs.
    Falls back to the company dashboard if the supplied value is rejected.
    """
    fallback = url_for("company_dashboard", slug=slug)
    if not url:
        return fallback
    parsed = urlparse(url)
    # Reject anything with a scheme (http, https, javascript, …) or a netloc
    # (//evil.com).  Only plain relative paths (/c/...) are allowed.
    if parsed.scheme or parsed.netloc:
        return fallback
    # Must start with '/' to be a root-relative internal path
    if not url.startswith("/"):
        return fallback
    return url


app = Flask(__name__, template_folder=str(_THIS_DIR / "templates"), static_folder=str(_THIS_DIR / "static"))
app.jinja_env.filters["markdown"] = render_markdown


@app.before_request
def _configure_cost_log_per_request():
    """Point the global cost-log at the company implied by the URL, so
    every LLM call fired during this request attributes spend to the
    right company. Cheap: O(1) string set."""
    from core.llm_client import set_cost_log_path
    slug = request.view_args.get("slug") if request.view_args else None
    if not slug:
        return
    try:
        from core.env import get_vault_dir
        company_dir = (get_vault_dir() / slug).resolve()
        set_cost_log_path(company_dir / "cost-log.jsonl")
    except Exception:
        # Never break a request over cost-log wiring.
        pass


def _company_or_404(slug: str):
    """Resolve a company slug (folder name) to (CompanyConfig, summary, depts).

    Hardens the slug against path-traversal attacks. An attacker could pass
    slugs like `..`, `../..`, absolute paths, or Windows drive-letter
    prefixes to escape the vault root. Every candidate path is resolved
    and then asserted to live strictly beneath the resolved vault root;
    anything else 404s without revealing details.
    """
    from core.env import get_vault_dir
    if not slug or any(ch in slug for ch in ("/", "\\", "\x00")) or slug in ("..", "."):
        abort(404, "Company not found")
    vault_root = get_vault_dir().resolve()
    company_dir = (vault_root / slug).resolve()
    try:
        company_dir.relative_to(vault_root)
    except ValueError:
        abort(404, "Company not found")
    if company_dir == vault_root:
        abort(404, "Company not found")
    company = load_company_safe(str(company_dir))
    if company is None:
        abort(404, f"Company '{slug}' not found or unreadable")
    departments = load_departments_safe(company)
    return company, departments


# ---------------------------------------------------------------------------
# Top-level / company picker
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    companies = discover_companies()
    return render_template("index.html", companies=companies)


# ---------------------------------------------------------------------------
# Per-company dashboard
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/")
def company_dashboard(slug: str):
    from core.cost_summary import compute_spend, format_usd
    company, departments = _company_or_404(slug)
    summary = read_company_summary(company)
    depts = list_dept_summaries(company, departments)
    artifacts = list_demo_artifacts(company)
    meetings = list_board_meetings(company)[:5]
    decisions = list_decisions(company)[:5]
    sessions = list_sessions(company)[:5]
    spend = compute_spend(company.company_dir)
    spend_view = {
        "log_exists": spend.log_exists,
        "today_cost": format_usd(spend.today.cost_usd),
        "today_calls": spend.today.calls,
        "month_cost": format_usd(spend.month.cost_usd),
        "month_calls": spend.month.calls,
        "lifetime_cost": format_usd(spend.lifetime.cost_usd),
        "lifetime_calls": spend.lifetime.calls,
        "last_call_at": spend.last_call_at,
        "top_tags_today": sorted(
            [
                {"tag": tag, "cost": format_usd(bucket.cost_usd), "calls": bucket.calls}
                for tag, bucket in spend.by_tag_today.items()
            ],
            key=lambda r: r["calls"], reverse=True,
        )[:5],
    }
    return render_template(
        "dashboard.html",
        slug=slug,
        company=summary,
        departments=depts,
        artifacts=artifacts,
        recent_meetings=meetings,
        recent_decisions=decisions,
        recent_sessions=sessions,
        spend=spend_view,
    )


# ---------------------------------------------------------------------------
# Company context editor (edit config.json + optional top-level markdown)
# ---------------------------------------------------------------------------
_EDITABLE_MARKDOWN_FILES = [
    ("context.md", "Context", "Freeform narrative about who this company is, what it does, and the moment it's in. Injected into agent prompts as shared background."),
    ("domain.md", "Domain", "The industry domain the company operates in. Regulatory, operational, and cultural facts that every agent should know."),
    ("priorities.md", "Priorities (long form)", "Human-editable narrative behind the structured priorities list. Use this when a priority needs more nuance than a single line."),
    ("founder_profile.md", "Founder profile", "Background, experience, working style. Injected so agents can calibrate their output to how the founder thinks."),
]


def _read_markdown_if_exists(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except OSError:
        return ""


def _parse_lines_field(raw: str) -> list[str]:
    """Split a textarea's contents into a list of trimmed non-empty lines."""
    return [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]


@app.route("/c/<slug>/edit", methods=["GET"])
def company_edit(slug: str):
    """Render the company-context editor. Covers the structured
    config.json fields plus the four top-level markdown files that
    managers read as shared context."""
    company, _ = _company_or_404(slug)
    cfg = dict(company.raw_config)
    delegation = dict(cfg.get("delegation", {}) or {})
    md_blocks = []
    for filename, label, hint in _EDITABLE_MARKDOWN_FILES:
        md_blocks.append({
            "filename": filename,
            "label": label,
            "hint": hint,
            "content": _read_markdown_if_exists(company.company_dir / filename),
        })
    notice = request.args.get("ok", "").strip()
    err = request.args.get("err", "").strip()
    return render_template(
        "company_edit.html",
        slug=slug,
        cfg=cfg,
        delegation=delegation,
        md_blocks=md_blocks,
        notice=notice,
        err=err,
    )


@app.route("/c/<slug>/edit", methods=["POST"])
def company_edit_save(slug: str):
    """Persist edits to config.json and any changed markdown files.
    Minimal validation: company_name and company_id must stay non-empty.
    Lists come in as textareas (one item per line). Booleans come from
    HTML form checkbox presence.
    """
    company, _ = _company_or_404(slug)
    form = request.form
    cfg = dict(company.raw_config)

    # Scalar string fields
    for key in (
        "company_name", "company_id", "industry", "industry_note",
        "business_model", "revenue_status", "geography", "team_size",
    ):
        if key in form:
            cfg[key] = form.get(key, "").strip()

    if not cfg.get("company_name") or not cfg.get("company_id"):
        return redirect(url_for(
            "company_edit", slug=slug,
            err="company_name and company_id are required.",
        ))

    # Integer fields
    try:
        cfg["session_retention_days"] = int(form.get(
            "session_retention_days", cfg.get("session_retention_days", 90),
        ) or 90)
    except ValueError:
        pass

    # List fields rendered as one-per-line textareas
    list_fields = (
        "active_departments",
        "regulatory_context",
        "priorities",
        "settled_convictions",
        "hard_constraints",
    )
    for key in list_fields:
        if key in form:
            cfg[key] = _parse_lines_field(form.get(key, ""))

    # Delegation object: nested numeric thresholds + booleans
    delegation = dict(cfg.get("delegation", {}) or {})
    for numeric_key in (
        "spend_auto_threshold", "spend_report_threshold", "spend_gate_threshold",
    ):
        if f"delegation__{numeric_key}" in form:
            try:
                delegation[numeric_key] = int(form.get(f"delegation__{numeric_key}", "0") or "0")
            except ValueError:
                pass
    for bool_key in (
        "content_publish_requires_approval", "vendor_commit_requires_approval",
    ):
        delegation[bool_key] = form.get(f"delegation__{bool_key}") == "on"
    cfg["delegation"] = delegation

    # Persist config.json
    config_path = company.company_dir / "config.json"
    try:
        config_path.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return redirect(url_for(
            "company_edit", slug=slug,
            err=f"Could not save config.json: {exc}",
        ))

    # Persist markdown blocks (only if the form actually posted them)
    saved_md: list[str] = []
    for filename, _label, _hint in _EDITABLE_MARKDOWN_FILES:
        field = f"md__{filename}"
        if field not in form:
            continue
        body = form.get(field, "")
        target = company.company_dir / filename
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            saved_md.append(filename)
        except OSError as exc:
            return redirect(url_for(
                "company_edit", slug=slug,
                err=f"Could not save {filename}: {exc}",
            ))

    msg = "Company context saved."
    if saved_md:
        msg += f" Updated {len(saved_md)} markdown file(s)."
    return redirect(url_for("company_edit", slug=slug, ok=msg))


# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/departments")
def departments_page(slug: str):
    company, departments = _company_or_404(slug)
    summary = read_company_summary(company)
    depts = list_dept_summaries(company, departments)
    return render_template("departments.html", slug=slug, company=summary, departments=depts)


@app.route("/c/<slug>/departments/<dept_name>")
def department_detail(slug: str, dept_name: str):
    company, departments = _company_or_404(slug)
    detail = read_dept_detail(company, departments, dept_name)
    if detail is None:
        abort(404, f"Department '{dept_name}' not found")
    summary = read_company_summary(company)
    return render_template(
        "department_detail.html",
        slug=slug,
        company=summary,
        dept=detail,
    )


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/board")
def board_page(slug: str):
    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    profiles = list_board_profiles(company)
    meetings = list_board_meetings(company)
    return render_template(
        "board.html",
        slug=slug,
        company=summary,
        profiles=profiles,
        meetings=meetings,
    )


@app.route("/c/<slug>/board/meetings/<path:filename>")
def board_meeting(slug: str, filename: str):
    company, _ = _company_or_404(slug)
    rel = f"board/meetings/{filename}"
    artifact = read_artifact_safe(company, rel)
    if artifact is None:
        abort(404, f"Meeting '{filename}' not found")
    summary = read_company_summary(company)
    return render_template(
        "artifact_view.html",
        slug=slug,
        company=summary,
        artifact=artifact,
        page_title=f"Board Meeting — {filename}",
        back_url=url_for("board_page", slug=slug),
        back_label="← Back to Board",
    )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/sessions")
def sessions_page(slug: str):
    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    sessions = list_sessions(company)
    return render_template("sessions.html", slug=slug, company=summary, sessions=sessions)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/decisions")
def decisions_page(slug: str):
    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    decisions = list_decisions(company)
    return render_template("decisions.html", slug=slug, company=summary, decisions=decisions)


# ---------------------------------------------------------------------------
# Demo artifacts
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/artifacts")
def artifacts_page(slug: str):
    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    artifacts = list_demo_artifacts(company)
    return render_template(
        "artifacts.html",
        slug=slug,
        company=summary,
        artifacts=artifacts,
    )


# ---------------------------------------------------------------------------
# Office — radial org chart + agent chat handoff (Phase 14 UI)
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/office")
def office_page(slug: str):
    """Interactive organizational chart: Orchestrator → Managers →
    Specialists. Each node is clickable; double-click opens a direct
    chat with that agent (using the existing /run/dispatch backend for
    managers and specialists).

    This is the 'scenario entry point' UI — the fastest way to pick a
    target agent and test a brief against it.
    """
    company, departments = _company_or_404(slug)
    summary = read_company_summary(company)
    depts = list_dept_summaries(company, departments)
    # Build a compact graph structure the template renders with SVG.
    org_graph = {
        "orchestrator": {
            "id": "orchestrator",
            "label": "Orchestrator",
            "role": "Founder-facing",
        },
        "managers": [
            {
                "id": d["name"],
                "label": d["display_name"] or d["name"],
                "specialist_count": d.get("specialist_count", 0),
                "onboarded": d.get("onboarded", False),
                "specialists": _list_specialists_for_dept(company, departments, d["name"]),
            }
            for d in depts
        ],
        "board": [
            {"id": p["role"], "label": p["role"].title()}
            for p in list_board_profiles(company)
        ],
    }
    return render_template(
        "office.html",
        slug=slug,
        company=summary,
        org=org_graph,
    )


def _list_specialists_for_dept(company, departments, dept_name: str) -> list[dict]:
    """Light version of read_dept_detail — just specialist name + label.
    SpecialistConfig exposes `name`, not `id` — the loader uses the
    filename-derived name as the stable dispatch key."""
    for d in departments:
        if d.name == dept_name:
            return [{"id": s.name, "label": s.name} for s in d.specialists]
    return []


# ---------------------------------------------------------------------------
# Awareness — ambient note browser (Phase 14 §10.2)
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/awareness")
def awareness_page(slug: str):
    """Browse active ambient awareness notes for this company.
    Read-only surface — note creation happens via the specialist/manager
    dispatch path."""
    from core.primitives.awareness import iter_notes, iter_active_notes

    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    all_notes = list(iter_notes(company.company_dir))
    active = list(iter_active_notes(company.company_dir))
    return render_template(
        "awareness.html",
        slug=slug,
        company=summary,
        all_notes=all_notes,
        active_notes=active,
    )


# ---------------------------------------------------------------------------
# Scenario runner — structured experimental dispatch form (Phase 14)
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/scenario", methods=["GET"])
def scenario_page(slug: str):
    """Scenario runner — the primary experimental UI for Phase 14
    dogfood. Pre-populated department briefs, ambient-awareness preview
    so the founder can see what context will be injected, and a single
    'Run scenario' button that kicks off a dispatch.
    """
    from core.primitives.awareness import iter_active_notes

    company, departments = _company_or_404(slug)
    summary = read_company_summary(company)
    depts = list_dept_summaries(company, departments)
    active_notes = list(iter_active_notes(company.company_dir))
    # Scenario templates — named briefs per department, so the founder
    # doesn't have to write one from scratch.
    scenarios = _default_scenarios(depts)
    return render_template(
        "scenario.html",
        slug=slug,
        company=summary,
        departments=depts,
        active_notes=active_notes,
        scenarios=scenarios,
    )


@app.route("/c/<slug>/run/scenario-ab", methods=["POST"])
def run_scenario_ab(slug: str):
    """Fire the same brief twice as concurrent runs with a shared
    pair_id. Caller rates them side-by-side at /ledger/compare/<pair_id>.
    """
    from core.scenario_ledger import start_pair, persist_run, complete_run
    from dataclasses import replace

    company, _ = _company_or_404(slug)
    dept_name = request.form.get("dept", "").strip()
    brief = request.form.get("brief", "").strip()
    scenario_name = request.form.get("scenario_name", "").strip() or f"adhoc--{dept_name}"
    if not dept_name or not brief:
        abort(400, "dept and brief required")

    run_a, run_b, pair_id = start_pair(
        dept=dept_name, scenario_name=scenario_name, brief=brief,
    )

    def _make_target(run_ref):
        def _target(j):
            result = run_dispatch_action(
                j, str(company.company_dir), dept_name, brief,
            )
            try:
                final = (result or {}).get("final_text") or ""
                complete_run(
                    company.company_dir, run_ref.id,
                    outcome_summary=final[:800],
                    full_output=final,
                )
            except Exception:  # pragma: no cover
                pass
            return result
        return _target

    job_a = JOB_REGISTRY.submit(
        kind="dispatch",
        label=f"A/B a: {scenario_name[:30]} → {dept_name}",
        company_dir=str(company.company_dir),
        target=_make_target(run_a),
    )
    job_b = JOB_REGISTRY.submit(
        kind="dispatch",
        label=f"A/B b: {scenario_name[:30]} → {dept_name}",
        company_dir=str(company.company_dir),
        target=_make_target(run_b),
    )
    try:
        persist_run(company.company_dir, replace(run_a, job_id=job_a.id))
        persist_run(company.company_dir, replace(run_b, job_id=job_b.id))
    except Exception:  # pragma: no cover
        pass

    return redirect(url_for("ledger_compare", slug=slug, pair_id=pair_id))


@app.route("/c/<slug>/ledger/compare/<pair_id>")
def ledger_compare(slug: str, pair_id: str):
    """Side-by-side comparison view for an A/B pair. Renders as soon
    as both runs are at least started; shows job status if either is
    still running."""
    from core.scenario_ledger import runs_by_pair

    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    pairs = runs_by_pair(company.company_dir)
    runs = pairs.get(pair_id, [])
    if not runs:
        abort(404, "pair not found")
    # Arrange slots deterministically
    run_a = next((r for r in runs if r.pair_slot == "a"), None)
    run_b = next((r for r in runs if r.pair_slot == "b"), None)
    # Pull job status for any still-running jobs
    job_a = JOB_REGISTRY.get(run_a.job_id) if run_a and run_a.job_id else None
    job_b = JOB_REGISTRY.get(run_b.job_id) if run_b and run_b.job_id else None
    return render_template(
        "compare.html",
        slug=slug,
        company=summary,
        pair_id=pair_id,
        run_a=run_a,
        run_b=run_b,
        job_a=job_a.to_dict() if job_a else None,
        job_b=job_b.to_dict() if job_b else None,
    )


@app.route("/c/<slug>/ledger/compare/<pair_id>/pick", methods=["POST"])
def ledger_compare_pick(slug: str, pair_id: str):
    from core.scenario_ledger import record_pair_verdict

    company, _ = _company_or_404(slug)
    winner = request.form.get("winner", "").strip().lower()
    notes = request.form.get("notes", "").strip()
    if winner not in {"a", "b", "tie"}:
        abort(400, "winner must be 'a', 'b', or 'tie'")
    updated = record_pair_verdict(
        company.company_dir, pair_id, winner=winner, notes=notes,
    )
    if not updated:
        abort(404, "pair not found")
    return redirect(url_for("ledger_page", slug=slug))


@app.route("/c/<slug>/ledger/pairs")
def ledger_pairs(slug: str):
    """Browse all A/B pairs, newest first. Pending pairs (unjudged)
    are pinned at top."""
    from core.scenario_ledger import runs_by_pair

    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    pairs = runs_by_pair(company.company_dir)
    # Build sortable list: (is_pending, started_at) → want pending first, newest first within group
    items = []
    for pid, rs in pairs.items():
        if not rs:
            continue
        rs_sorted = sorted(rs, key=lambda r: r.pair_slot)
        pending = any(not r.pair_verdict for r in rs_sorted)
        started = rs_sorted[0].started_at
        items.append({
            "pair_id": pid,
            "runs": rs_sorted,
            "pending": pending,
            "started_at": started,
        })
    items.sort(key=lambda it: (not it["pending"], it["started_at"]), reverse=True)
    # pending=True should come first; reverse=True makes True > False, so that works
    return render_template(
        "pairs.html",
        slug=slug,
        company=summary,
        items=items,
    )


@app.route("/c/<slug>/run/scenario-batch", methods=["POST"])
def run_scenario_batch(slug: str):
    """Fire every seeded scenario (or a filtered subset) as concurrent
    background jobs. Each run becomes its own ScenarioLedger entry +
    job. Returns a single HTML index listing the job IDs.

    Form params:
      only_depts — optional comma-sep filter (e.g. "marketing,finance")
    """
    from core.scenario_ledger import start_run, persist_run, complete_run
    from dataclasses import replace

    company, departments = _company_or_404(slug)
    depts = list_dept_summaries(company, departments)
    only_raw = request.form.get("only_depts", "").strip()
    only = {d.strip() for d in only_raw.split(",") if d.strip()} if only_raw else None
    type_filter = request.form.get("only_type", "").strip().lower() or None
    scenarios = _default_scenarios(depts)

    submitted: list[dict] = []
    for group in scenarios:
        if only and group["dept"] not in only:
            continue
        for b in group["briefs"]:
            if type_filter and b.get("scenario_type") != type_filter:
                continue
            brief = b["brief"]
            name = b["name"]
            dept_name = group["dept"]

            try:
                run = start_run(dept=dept_name, scenario_name=name, brief=brief)
            except Exception:  # pragma: no cover
                run = None

            def _make_target(run_ref, dept_bind, brief_bind):
                def _target(j):
                    result = run_dispatch_action(
                        j, str(company.company_dir), dept_bind, brief_bind,
                    )
                    if run_ref is not None:
                        try:
                            final = (result or {}).get("final_text") or ""
                            complete_run(
                                company.company_dir,
                                run_ref.id,
                                outcome_summary=final[:800],
                                full_output=final,
                            )
                        except Exception:  # pragma: no cover
                            pass
                    return result
                return _target

            job = JOB_REGISTRY.submit(
                kind="dispatch",
                label=f"batch: {name} → {dept_name}"[:80],
                company_dir=str(company.company_dir),
                target=_make_target(run, dept_name, brief),
            )
            if run is not None:
                try:
                    persist_run(company.company_dir, replace(run, job_id=job.id))
                except Exception:  # pragma: no cover
                    pass
            submitted.append({
                "job_id": job.id,
                "dept": dept_name,
                "scenario": name,
            })

    if not submitted:
        abort(400, "no scenarios matched the filter")

    summary = read_company_summary(company)
    return render_template(
        "batch_submitted.html",
        slug=slug,
        company=summary,
        submitted=submitted,
    )


def _default_scenarios(depts: list[dict]) -> list[dict]:
    """Portfolio-backed scenarios — every dept has at least one of each
    of the five scenario types (convergence / creativity / constraint /
    calibration / coordination). Live definitions in
    core/scenario_portfolio.py so the portfolio can evolve without
    touching webapp code."""
    from core.scenario_portfolio import as_webapp_groups
    return as_webapp_groups(depts)


# ---------------------------------------------------------------------------
# Chat / conversation threads (Phase 14 — founder ↔ manager conversations)
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/chat")
def chat_index(slug: str):
    """Browse all conversation threads. Open threads first, closed
    threads after. Grouped visually by purpose."""
    from core.conversation import list_threads

    company, departments = _company_or_404(slug)
    summary = read_company_summary(company)
    depts = list_dept_summaries(company, departments)
    threads = list_threads(company.company_dir)
    return render_template(
        "chat_index.html",
        slug=slug,
        company=summary,
        departments=depts,
        threads=threads,
    )


@app.route("/c/<slug>/chat/new", methods=["POST"])
def chat_new(slug: str):
    """Start a fresh ad-hoc chat thread. target_agent can be
    'orchestrator' or 'manager:<dept>'."""
    from core.conversation import start_thread, persist_thread

    company, _ = _company_or_404(slug)
    target = request.form.get("target", "").strip()
    opener = request.form.get("opener", "").strip()
    if not target:
        abort(400, "target required")
    title = request.form.get("title", "").strip() or f"chat with {target}"
    thread = start_thread(
        target_agent=target,
        purpose="chat",
        title=title,
    )
    persist_thread(company.company_dir, thread)
    # If the founder provided an opener, post it immediately.
    if opener:
        from core.conversation import send_and_reply
        send_and_reply(company.company_dir, thread.id, opener)
    return redirect(url_for("chat_detail", slug=slug, thread_id=thread.id))


@app.route("/c/<slug>/chat/<thread_id>")
def chat_detail(slug: str, thread_id: str):
    from core.conversation import load_thread

    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    thread = load_thread(company.company_dir, thread_id)
    if thread is None:
        abort(404, "thread not found")
    return render_template(
        "chat_detail.html",
        slug=slug,
        company=summary,
        thread=thread,
    )


@app.route("/c/<slug>/chat/<thread_id>/send", methods=["POST"])
def chat_send(slug: str, thread_id: str):
    """Append a user message, fire the agent reply, return to the
    thread. Uses background job so UI stays responsive on slow replies."""
    from core.conversation import (
        load_thread, append_message, send_and_reply,
    )

    company, _ = _company_or_404(slug)
    content = request.form.get("content", "").strip()
    if not content:
        abort(400, "message content required")

    # Append the user message synchronously so it appears immediately
    append_message(company.company_dir, thread_id, role="user", content=content)

    # Fire the reply as a background job
    def _target(j):
        thread = load_thread(company.company_dir, thread_id)
        if thread is None:
            return {"ok": False, "reason": "thread not found"}
        # The user message is already appended; we need the agent
        # reply. send_and_reply appends ANOTHER user message if we
        # pass the content in — so instead, pop the last user msg
        # from load_thread, skip the user-append step, and just call
        # the LLM + append_assistant.
        from core.conversation import _system_prompt_for_thread, _messages_for_llm
        from core.llm_client import single_turn
        sys_prompt = _system_prompt_for_thread(thread, company.company_dir)
        msgs = _messages_for_llm(thread)
        response = single_turn(
            messages=msgs,
            model="claude-haiku-4-5-20251001",
            cost_tag=f"chat:{thread.purpose}:{thread.target_agent}",
            system=sys_prompt,
            max_tokens=1400,
        )
        if response.error:
            reply = f"ERROR: {response.error}"
        else:
            reply = (response.text or "").strip() or "(empty response)"
        append_message(
            company.company_dir, thread_id,
            role="assistant", content=reply, job_id=j.id,
            token_usage=response.usage or {},
        )
        return {"ok": True, "reply_preview": reply[:200]}

    job = JOB_REGISTRY.submit(
        kind="chat",
        label=f"chat reply · {thread_id[:8]}",
        company_dir=str(company.company_dir),
        target=_target,
    )
    return redirect(url_for("chat_detail", slug=slug, thread_id=thread_id))


@app.route("/c/<slug>/chat/<thread_id>/close", methods=["POST"])
def chat_close(slug: str, thread_id: str):
    from core.conversation import close_thread

    company, _ = _company_or_404(slug)
    close_thread(company.company_dir, thread_id)
    return redirect(url_for("chat_detail", slug=slug, thread_id=thread_id))


@app.route("/c/<slug>/chat/<thread_id>/reopen", methods=["POST"])
def chat_reopen(slug: str, thread_id: str):
    """Re-open a previously closed thread so the founder can keep asking
    follow-up questions after a synthesis has already run. The summary_path
    stays attached so the prior artifact link is preserved."""
    from core.conversation import load_thread, persist_thread
    from dataclasses import replace

    company, _ = _company_or_404(slug)
    thread = load_thread(company.company_dir, thread_id)
    if thread is None:
        abort(404, f"Thread '{thread_id}' not found")
    if thread.status != "open":
        persist_thread(company.company_dir, replace(thread, status="open"))
    return redirect(url_for("chat_detail", slug=slug, thread_id=thread_id))


# ---------------------------------------------------------------------------
# Orchestrator onboarding
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/onboarding/orchestrator")
def onboarding_orchestrator(slug: str):
    import datetime

    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)

    charter_path = company.company_dir / "orchestrator-charter.md"
    charter_exists = charter_path.exists()
    charter_preview = ""
    charter_modified = ""
    if charter_exists:
        text = charter_path.read_text(encoding="utf-8")
        charter_preview = text[:2000] + ("…" if len(text) > 2000 else "")
        mtime = charter_path.stat().st_mtime
        charter_modified = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")

    # Pre-fill sensible defaults so the form isn't blank
    prefill = {
        "q_direct": "Status questions, recalling past decisions, explaining what the system is doing, routing questions.",
        "q_never": "Writing copy, financial calculations, regulatory research, designing products, writing editorial content.",
        "q_board": "Major strategic tradeoffs where two defensible paths exist and I need to make a call. Capital decisions over $5,000. Operating model selection.",
        "q_comms": "Short declarative statements. Conclusions first. No preamble. No 'Certainly!' or hedging bullet lists.",
        "q_priority": "",
        "q_escalation": "Blocker: anything that stops a top-priority item from moving forward. FYI: everything else — batch it in the digest.",
        "q_other": "",
    }

    return render_template(
        "onboarding_orchestrator.html",
        slug=slug,
        company=summary,
        charter_exists=charter_exists,
        charter_preview=charter_preview,
        charter_modified=charter_modified,
        prefill=prefill,
    )


@app.route("/c/<slug>/onboarding/orchestrator/run", methods=["POST"])
def onboarding_orchestrator_run(slug: str):
    """Generate orchestrator-charter.md from founder interview answers."""
    company, _ = _company_or_404(slug)

    answers = {k: request.form.get(k, "").strip() for k in [
        "q_direct", "q_never", "q_board", "q_comms",
        "q_priority", "q_escalation", "q_other",
    ]}

    company_name = company.name
    charter_path = company.company_dir / "orchestrator-charter.md"

    # Load department list for context
    departments = load_departments_safe(company)
    dept_names = [d.name for d in departments] if departments else []

    def _target(j):
        from core.llm_client import single_turn

        dept_list = ", ".join(dept_names) if dept_names else "none configured"
        prompt = f"""You are generating an orchestrator-charter.md for {company_name}.

The orchestrator is the top-level AI agent the founder talks to. It coordinates
department managers and the board of advisors. The charter defines its exact scope —
what it delegates vs. answers directly, when to escalate, and how to communicate.

Active departments: {dept_list}

The founder answered these calibration questions:

1. What should the orchestrator answer directly (no dispatch)?
{answers['q_direct']}

2. What must it NEVER do itself — always delegate?
{answers['q_never']}

3. When should the board be convened vs. a department dispatch?
{answers['q_board']}

4. Communication style preferences:
{answers['q_comms']}

5. Current highest-priority focus area:
{answers['q_priority']}

6. What distinguishes a blocker from an FYI?
{answers['q_escalation']}

7. Anything else:
{answers['q_other'] or '(none)'}

Write a complete orchestrator-charter.md. Structure it with these sections:
- What You Are (1 short paragraph — coordination layer, not a worker)
- Routing Decision Tree (3-4 if/then rules, concrete and testable)
- Delegation Scope (one entry per active department: what trigger phrases route there, what task types belong there)
- What You Can Answer Directly (short bullet list)
- When to Convene the Board (do/don't rules)
- Founder-Specific Calibration (communication style, attention model, escalation rules from answers above)
- Hard Stops (the never-do list as hard rules)

Be specific and concrete. Avoid abstract platitudes. Each rule should be testable —
a junior agent reading it should know exactly what to do with any given request.
Write in second person ("You dispatch...", "You do not...").
"""

        j.log.append("Generating orchestrator charter from interview answers…")
        response = single_turn(
            messages=[{"role": "user", "content": prompt}],
            model="claude-sonnet-4-6",
            cost_tag="onboarding:orchestrator:charter",
            max_tokens=3000,
        )
        if response.error:
            j.log.append(f"Error: {response.error}")
            return {"ok": False, "error": response.error}

        charter_text = (response.text or "").strip()
        charter_path.write_text(charter_text, encoding="utf-8")
        j.log.append(f"Charter written to {charter_path.name} ({len(charter_text)} chars).")
        return {"ok": True, "charter_length": len(charter_text)}

    job = JOB_REGISTRY.submit(
        kind="onboarding",
        label="Orchestrator charter generation",
        company_dir=str(company.company_dir),
        target=_target,
    )
    return redirect(url_for("job_detail", slug=slug, job_id=job.id))


# ---------------------------------------------------------------------------
# Department onboarding (Phase 14 refinement — 5-phase lifecycle)
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/onboarding")
def onboarding_dashboard(slug: str):
    """Matrix view — every dept × current phase. Primary entry to the
    onboarding workflow."""
    from core.dept_onboarding import (
        OnboardingPhase, list_all_states, overall_progress,
    )

    company, departments = _company_or_404(slug)
    summary = read_company_summary(company)
    depts = list_dept_summaries(company, departments)
    dept_names = [d["name"] for d in depts]
    states = list_all_states(company.company_dir, dept_names)
    progress = overall_progress(states)
    # Pair each state with its dept summary for convenient template access
    rows = []
    for d, s in zip(depts, states):
        rows.append({
            "dept": d,
            "state": s,
            "dept_label": d.get("display_name") or d["name"],
        })
    charter_exists = (company.company_dir / "orchestrator-charter.md").exists()
    from core.scope_coordination import (
        load_state as load_coord_state,
        department_ready_for_coordination,
        STATUS_NOT_READY, STATUS_READY,
    )
    coord_state = load_coord_state(company.company_dir)
    ready_count = sum(1 for s in states if department_ready_for_coordination(s))
    total_depts = len(states)
    all_ready = total_depts > 0 and ready_count == total_depts
    # Surface "ready" state only when no run has ever happened and all depts qualify.
    if coord_state.status == STATUS_NOT_READY and all_ready:
        from dataclasses import replace as _replace
        from core.scope_coordination import persist_state as _persist_coord
        coord_state = _replace(coord_state, status=STATUS_READY)
        _persist_coord(company.company_dir, coord_state)
    coord_summary = {
        "status": coord_state.status,
        "ready_count": ready_count,
        "total_depts": total_depts,
        "all_ready": all_ready,
        "scope_map_path": coord_state.scope_map_path,
        "error": coord_state.error,
        "notes": coord_state.notes,
        "job_id": coord_state.job_id,
    }
    return render_template(
        "onboarding_dashboard.html",
        slug=slug,
        company=summary,
        rows=rows,
        progress=progress,
        phases=[p.value for p in OnboardingPhase],
        charter_exists=charter_exists,
        coord=coord_summary,
    )


@app.route("/c/<slug>/onboarding/<dept>")
def onboarding_dept(slug: str, dept: str):
    """Per-department onboarding detail + next-action buttons."""
    from core.dept_onboarding import ensure_state, OnboardingPhase

    company, departments = _company_or_404(slug)
    summary = read_company_summary(company)
    depts = list_dept_summaries(company, departments)
    dept_summary = next((d for d in depts if d["name"] == dept), None)
    if dept_summary is None:
        abort(404, f"Department '{dept}' not found")
    state = ensure_state(company.company_dir, dept)
    # Load the current phase artifact content if present, for inline preview
    artifact_preview = ""
    artifact_full_path = ""
    if state.current_artifact and state.current_artifact.path:
        rel = state.current_artifact.path
        art = read_artifact_safe(company, rel)
        if art:
            # read_artifact_safe returns the file body under key "text".
            # Key "body" was an earlier convention that no longer exists,
            # and reading it returned "" which kept the "synthesizing"
            # UI state stuck forever on finished phases.
            artifact_preview = art.get("text", "")[:3000]
            artifact_full_path = rel
    # Staffing phase needs the roster for per-row rendering.
    roster = None
    subagent_previews: dict[str, str] = {}
    if state.phase == OnboardingPhase.STAFFING.value:
        from core.dept_roster import load_roster
        from core.conversation import load_thread as _load_thread_s
        roster = load_roster(company.company_dir, dept)
        if roster is not None:
            for entry in roster.entries:
                for c in entry.candidates:
                    thread_id = Path(c.thread_path).stem if c.thread_path else ""
                    if not thread_id:
                        continue
                    t = _load_thread_s(company.company_dir, thread_id)
                    if t is None:
                        continue
                    arrival_text = ""
                    for m in t.messages:
                        if m.role == "assistant" and (m.content or "").strip():
                            arrival_text = (m.content or "").strip()
                            break
                    if arrival_text:
                        subagent_previews[c.candidate_id] = arrival_text[:1400]

    # Scope_calibration phase may use the candidate-slate flow (3 parallel
    # hires). Load the slate if present and, for each candidate, snip
    # the arrival-note text so the template can show a preview card.
    candidate_slate = None
    candidate_previews: dict[str, str] = {}
    if state.phase == OnboardingPhase.SCOPE_CALIBRATION.value:
        from core.dept_candidates import load_slate
        from core.conversation import load_thread as _load_thread
        candidate_slate = load_slate(company.company_dir, dept)
        if candidate_slate is not None:
            for c in candidate_slate.candidates:
                thread_id = Path(c.thread_path).stem if c.thread_path else ""
                if not thread_id:
                    continue
                t = _load_thread(company.company_dir, thread_id)
                if t is None:
                    continue
                arrival_text = ""
                for m in t.messages:
                    if m.role == "assistant" and (m.content or "").strip():
                        arrival_text = (m.content or "").strip()
                        break
                if arrival_text:
                    candidate_previews[c.candidate_id] = arrival_text[:1400]

    # Stalled-job detection: if the current artifact points at a job_id
    # that is no longer tracked (webapp restart) or that errored, the
    # founder needs a way out. Surface a retry affordance via
    # `job_status` instead of leaving them on a forever-spinning page.
    job_status = "none"
    if state.current_artifact and state.current_artifact.job_id:
        job = JOB_REGISTRY.get(state.current_artifact.job_id)
        if job is None:
            job_status = "lost"
        elif job.status == "running":
            job_status = "running"
        elif job.status == "error":
            job_status = "error"
        elif job.status == "done":
            job_status = "done"
    # A job is "stalled" when we have a job_id but no live/completed job
    # AND no output file on disk yet.
    job_stalled = (
        state.current_artifact
        and state.current_artifact.job_id
        and state.current_artifact.signoff == "none"
        and not artifact_preview
        and job_status in {"lost", "error"}
    )

    # Phase navigation (Next / Back). Index of current phase in the
    # canonical order. "pending" and "complete" are terminal states,
    # not phases in the lifecycle proper (7 real phases total).
    phase_order = [
        p.value for p in OnboardingPhase
        if p.value not in {"pending", "complete"}
    ]
    current_phase = state.phase if state.phase not in {"pending", "complete"} else "scope_calibration"
    try:
        phase_index = phase_order.index(current_phase)
    except ValueError:
        phase_index = 0
    prev_phase = phase_order[phase_index - 1] if phase_index > 0 else ""
    next_phase = phase_order[phase_index + 1] if phase_index + 1 < len(phase_order) else ""
    phase_nav = {
        "current": current_phase,
        "current_label": _phase_label(current_phase),
        "index": phase_index + 1,           # 1-based for display
        "total": len(phase_order),
        "prev": prev_phase,
        "prev_label": _phase_label(prev_phase),
        "next": next_phase,
        "next_label": _phase_label(next_phase),
    }

    return render_template(
        "onboarding_dept.html",
        slug=slug,
        company=summary,
        dept=dept_summary,
        state=state,
        phases=[p.value for p in OnboardingPhase],
        artifact_preview=artifact_preview,
        artifact_full_path=artifact_full_path,
        roster=roster,
        job_status=job_status,
        job_stalled=job_stalled,
        phase_nav=phase_nav,
        candidate_slate=candidate_slate,
        candidate_previews=candidate_previews,
        subagent_previews=subagent_previews,
    )


_PHASE_LABELS = {
    "scope_calibration": "Arrival",
    "domain_research": "Research",
    "founder_interview": "Interview",
    "kb_ingestion": "KB ingest",
    "integrations": "Integrate",
    "charter": "Charter",
    "staffing": "Staffing",
    "complete": "Complete",
}


def _phase_label(phase_value: str) -> str:
    return _PHASE_LABELS.get(phase_value, phase_value.replace("_", " ").title())


@app.route("/c/<slug>/onboarding/<dept>/retry-phase", methods=["POST"])
@retrolog_dispatch("retry_phase", agent_resolver=lambda kw: f"manager:{kw['dept']}")
def onboarding_retry_phase(slug: str, dept: str):
    """Recovery action for a stalled or errored phase. Drops the most
    recent artifact for the current phase (the one pointing at a dead
    job), then 307-forwards to the phase's start route so the founder
    can re-fire without losing their place in the lifecycle.

    Useful when a webapp restart killed a long-running job, or when an
    LLM error left the phase with no output file."""
    from dataclasses import replace
    from core.dept_onboarding import (
        OnboardingPhase, ensure_state, persist_state,
    )

    company, _ = _company_or_404(slug)
    state = ensure_state(company.company_dir, dept)
    # Drop the tail artifact for the current phase so begin_phase can
    # re-initialize cleanly.
    new_artifacts = list(state.artifacts)
    while new_artifacts and new_artifacts[-1].phase == state.phase:
        new_artifacts.pop()
    persist_state(company.company_dir, replace(
        state, artifacts=tuple(new_artifacts),
    ))

    # Forward to the right start route based on phase.
    route_by_phase = {
        "scope_calibration": "onboarding_start_scope_calibration",
        "domain_research": "onboarding_start_domain_research",
        "founder_interview": "onboarding_start_interview",
    }
    endpoint = route_by_phase.get(state.phase)
    if endpoint is None:
        # No start route for this phase: land back on the dept page so
        # the founder can take the appropriate manual next step.
        return redirect(url_for("onboarding_dept", slug=slug, dept=dept))
    return redirect(url_for(endpoint, slug=slug, dept=dept), code=307)


@app.route("/c/<slug>/onboarding/<dept>/rerun-scope-calibration", methods=["POST"])
@retrolog_dispatch("rerun_scope_calibration", agent_resolver=lambda kw: f"manager:{kw['dept']}")
def onboarding_rerun_scope_calibration(slug: str, dept: str):
    """Reset the department back to SCOPE_CALIBRATION phase so the
    founder can re-run the interview.

    - Previous artifacts stay in state.artifacts history (audit trail).
    - Previous skill-scope.md (if any) stays on disk as-is — the new
      interview will overwrite when it synthesizes.
    - Completed_phases is left alone, but SCOPE_CALIBRATION is removed
      from completed_phases so the new run can be signed off fresh.

    Use cases: founder realized the first calibration was too narrow /
    wrong / conducted before they knew what they wanted; new secondary
    expertise is needed; department role shifted over time.
    """
    from core.dept_onboarding import (
        OnboardingPhase, ensure_state, persist_state, reset_to_phase,
    )
    from dataclasses import replace

    company, _ = _company_or_404(slug)
    state = ensure_state(company.company_dir, dept)

    # Remove SCOPE_CALIBRATION from completed_phases so the sign-off
    # UI shows it as pending again.
    completed = tuple(
        p for p in state.completed_phases
        if p != OnboardingPhase.SCOPE_CALIBRATION.value
    )
    skipped = tuple(
        p for p in state.skipped_phases
        if p != OnboardingPhase.SCOPE_CALIBRATION.value
    )
    reset_state = replace(
        state,
        phase=OnboardingPhase.SCOPE_CALIBRATION.value,
        completed_phases=completed,
        skipped_phases=skipped,
    )
    persist_state(company.company_dir, reset_state)
    # Delete any existing candidate slate so the next start dispatches
    # a fresh three candidates instead of adding onto a stale slate.
    from core.dept_candidates import delete_slate
    delete_slate(company.company_dir, dept)
    # Kick off a fresh slate immediately. 307 preserves the POST method
    # so the target route (POST-only) accepts the forwarded request.
    return redirect(url_for(
        "onboarding_start_scope_calibration", slug=slug, dept=dept,
    ), code=307)


@app.route("/c/<slug>/onboarding/<dept>/start-scope-calibration", methods=["POST"])
@retrolog_dispatch("scope_calibration_start", agent_resolver=lambda kw: f"manager:{kw['dept']}")
def onboarding_start_scope_calibration(slug: str, dept: str):
    """Phase 1 arrival. Dispatches THREE candidate managers in parallel,
    each with its own independently sampled personality seeds and
    serendipity picks. The founder reviews all three side by side on the
    onboarding dept page and selects one. Output (after selection +
    synthesis) lands at `<dept>/skill-scope.md`."""
    import datetime
    from dataclasses import replace as _replace
    from core.dept_onboarding import (
        OnboardingPhase, begin_phase, ensure_state, attach_artifact,
        render_scope_calibration_prompt,
    )
    from core.dept_candidates import (
        CandidateSlate, Candidate, persist_slate, new_candidate_id,
        candidates_path, DEFAULT_SLATE_SIZE, STATUS_DRAFTING, STATUS_READY,
    )
    from core.conversation import start_thread, persist_thread

    company, departments = _company_or_404(slug)
    depts = list_dept_summaries(company, departments)
    dept_summary = next((d for d in depts if d["name"] == dept), None)
    if dept_summary is None:
        abort(404, f"Department '{dept}' not found")

    now_iso = datetime.datetime.utcnow().isoformat() + "Z"
    slate_rel = candidates_path(company.company_dir, dept).relative_to(
        company.company_dir
    ).as_posix()

    # Register the slate as the dept's current artifact so the onboarding
    # page flips into candidate-review mode.
    begin_phase(
        company.company_dir, dept, OnboardingPhase.SCOPE_CALIBRATION,
        artifact_path=slate_rel,
    )

    labels = ["Candidate A", "Candidate B", "Candidate C"]
    candidates: list[Candidate] = []

    for i in range(DEFAULT_SLATE_SIZE):
        label = labels[i] if i < len(labels) else f"Candidate {i + 1}"
        # Each candidate gets its OWN prompt with independently sampled
        # personality seeds. This is what makes the three demonstrably
        # different from each other.
        opening_prompt = render_scope_calibration_prompt(
            dept=dept,
            dept_label=dept_summary.get("display_name") or dept,
            company_name=company.name,
            industry=company.industry or "",
        )
        thread = start_thread(
            target_agent=f"manager:{dept}",
            purpose="scope_calibration",
            seed_system=opening_prompt,
            dept=dept,
            onboarding_phase=OnboardingPhase.SCOPE_CALIBRATION.value,
            title=f"{dept} hire: {label}",
        )
        persist_thread(company.company_dir, thread)

        cand = Candidate(
            candidate_id=new_candidate_id(),
            label=label,
            thread_path=f"conversations/{thread.id}.json",
            status=STATUS_DRAFTING,
            created_at=now_iso,
        )
        candidates.append(cand)

        # Dispatch each arrival-note job independently. Haiku is fast
        # and cheap, so three in parallel is well under one Sonnet call.
        def _make_target(thread_id: str, cand_id: str, cand_label: str):
            def _target(j):
                from core.conversation import (
                    load_thread, _system_prompt_for_thread, append_message,
                )
                from core.dept_candidates import (
                    load_slate as _ls, persist_slate as _ps,
                    upsert_candidate as _uc, STATUS_READY as _READY,
                )
                from core.llm_client import single_turn
                from dataclasses import replace as _r

                t = load_thread(company.company_dir, thread_id)
                if t is None:
                    return {"ok": False}
                sys_prompt = _system_prompt_for_thread(t, company.company_dir)
                opener_instruction = {
                    "role": "user",
                    "content": (
                        "Write your arrival note now, in FIRST PERSON, "
                        "following the structure in your system prompt "
                        "exactly. You ARE the hire. Open with something "
                        "like 'Hi, I'm your new manager.' Do NOT write a "
                        "welcome letter from the company's perspective. "
                        "One message, 320-550 words. Lean into the "
                        "personality seeds you were given."
                    ),
                }
                resp = single_turn(
                    messages=[opener_instruction],
                    model="claude-haiku-4-5-20251001",
                    cost_tag=f"scope-calibration:candidate:{dept}:{cand_label.replace(' ', '_')}",
                    system=sys_prompt,
                    max_tokens=900,
                )
                reply = resp.text if not resp.error else f"ERROR: {resp.error}"
                append_message(
                    company.company_dir, thread_id,
                    role="assistant", content=(reply or "").strip(),
                    job_id=j.id, token_usage=resp.usage or {},
                )
                # Mark this candidate ready in the slate.
                slate_now = _ls(company.company_dir, dept)
                if slate_now is not None:
                    current = slate_now.find(cand_id)
                    if current is not None:
                        updated = _r(current, status=_READY, job_id=j.id)
                        slate_now = _uc(slate_now, updated)
                        slate_now = _r(
                            slate_now,
                            last_updated_at=datetime.datetime.utcnow().isoformat() + "Z",
                        )
                        _ps(company.company_dir, slate_now)
                return {"ok": not resp.error}
            return _target

        job = JOB_REGISTRY.submit(
            kind="scope-calibration-candidate",
            label=f"{dept} hire: {label}",
            company_dir=str(company.company_dir),
            target=_make_target(thread.id, cand.candidate_id, label),
        )
        candidates[-1] = _replace(cand, job_id=job.id)

    slate = CandidateSlate(
        dept=dept,
        created_at=now_iso,
        last_updated_at=now_iso,
        candidates=tuple(candidates),
    )
    persist_slate(company.company_dir, slate)

    # Point the phase artifact's job_id at the FIRST candidate so the
    # onboarding dept template's generic "job running" indicator lights
    # up. The slate view replaces the single-thread UI entirely, but
    # the refresh trigger still needs a job_id to key off of.
    attach_artifact(
        company.company_dir, dept, OnboardingPhase.SCOPE_CALIBRATION,
        artifact_path=slate_rel, job_id=candidates[0].job_id,
    )

    return redirect(url_for("onboarding_dept", slug=slug, dept=dept))


@app.route("/c/<slug>/onboarding/<dept>/select-candidate/<candidate_id>", methods=["POST"])
@retrolog_dispatch("select_candidate", agent_resolver=lambda kw: f"manager:{kw['dept']}")
def onboarding_select_candidate(slug: str, dept: str, candidate_id: str):
    """Founder picked this candidate off the slate. Mark it selected,
    mark the other candidates discarded (and close their threads so
    they don't linger as open conversations), then forward to
    finish-scope-calibration 307-style with the selected candidate's
    thread set as the active artifact."""
    import datetime
    from dataclasses import replace
    from core.dept_onboarding import (
        OnboardingPhase, ensure_state, attach_artifact,
    )
    from core.dept_candidates import (
        load_slate, persist_slate, upsert_candidate,
        STATUS_SELECTED, STATUS_DISCARDED,
    )
    from core.conversation import close_thread

    company, _ = _company_or_404(slug)
    slate = load_slate(company.company_dir, dept)
    if slate is None:
        abort(404, "No candidate slate on file")
    chosen = slate.find(candidate_id)
    if chosen is None:
        abort(404, f"Candidate '{candidate_id}' not found")

    now_iso = datetime.datetime.utcnow().isoformat() + "Z"

    # Mark statuses. Close the losing threads so they don't read as
    # "still open, founder just forgot about us."
    new_slate = slate
    for c in slate.candidates:
        if c.candidate_id == candidate_id:
            new_slate = upsert_candidate(
                new_slate, replace(c, status=STATUS_SELECTED),
            )
        else:
            new_slate = upsert_candidate(
                new_slate, replace(c, status=STATUS_DISCARDED),
            )
            losing_thread_id = Path(c.thread_path).stem
            close_thread(
                company.company_dir, losing_thread_id,
                summary_path="(discarded candidate)",
            )
    new_slate = replace(
        new_slate,
        selected_candidate_id=candidate_id,
        last_updated_at=now_iso,
    )
    persist_slate(company.company_dir, new_slate)

    # Flip the dept's current artifact from the slate file to the
    # selected candidate's thread so finish-scope-calibration (which
    # looks for a conversations/xxx.json artifact) finds it.
    attach_artifact(
        company.company_dir, dept, OnboardingPhase.SCOPE_CALIBRATION,
        artifact_path=chosen.thread_path,
    )

    return redirect(url_for(
        "onboarding_finish_scope_calibration", slug=slug, dept=dept,
    ), code=307)


@app.route("/c/<slug>/onboarding/<dept>/finish-scope-calibration", methods=["POST"])
@retrolog_dispatch("scope_calibration_finish", agent_resolver=lambda kw: f"manager:{kw['dept']}")
def onboarding_finish_scope_calibration(slug: str, dept: str):
    """Close the scope-calibration thread + synthesize into
    `<dept>/skill-scope.md`. Phase 1 then awaits founder sign-off."""
    from core.dept_onboarding import (
        OnboardingPhase, ensure_state, attach_artifact, skill_scope_path,
        SCOPE_SYNTHESIS_PROMPT,
    )
    from core.conversation import close_thread, load_thread, _system_prompt_for_thread, _messages_for_llm

    company, _ = _company_or_404(slug)
    state = ensure_state(company.company_dir, dept)
    thread_artifact = None
    for a in reversed(state.artifacts):
        if a.phase == OnboardingPhase.SCOPE_CALIBRATION.value and a.path.startswith("conversations/"):
            thread_artifact = a
            break
    if thread_artifact is None:
        abort(400, "no active scope-calibration thread found")
    thread_id = Path(thread_artifact.path).stem

    scope_rel = skill_scope_path(company.company_dir, dept).relative_to(
        company.company_dir
    ).as_posix()

    def _target(j):
        from core.llm_client import single_turn
        thread = load_thread(company.company_dir, thread_id)
        if thread is None:
            return {"ok": False, "reason": "thread not found"}
        sys_prompt = _system_prompt_for_thread(thread, company.company_dir)
        transcript = _messages_for_llm(thread)
        transcript.append({"role": "user", "content": SCOPE_SYNTHESIS_PROMPT})
        resp = single_turn(
            messages=transcript,
            model="claude-sonnet-4-6",
            cost_tag=f"scope-calibration:synth:{dept}",
            system=sys_prompt,
            max_tokens=2500,
        )
        body = resp.text if not resp.error else f"# skill-scope.md (synthesis failed)\n\nError: {resp.error}\n"
        target = (company.company_dir / scope_rel).resolve()
        try:
            target.relative_to(company.company_dir.resolve())
        except ValueError:
            return {"ok": False, "reason": "path escape"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body or "", encoding="utf-8")
        close_thread(company.company_dir, thread_id, summary_path=scope_rel)
        attach_artifact(
            company.company_dir, dept,
            OnboardingPhase.SCOPE_CALIBRATION,
            artifact_path=scope_rel, job_id=j.id,
        )
        return {"ok": True, "written": scope_rel}

    job = JOB_REGISTRY.submit(
        kind="scope-synth",
        label=f"{dept} scope synthesis",
        company_dir=str(company.company_dir),
        target=_target,
    )
    # Attach the eventual output path with the synth job_id NOW so the
    # onboarding dept page can render a "synthesizing" state and
    # auto-refresh, instead of leaving the founder on the arrival-note
    # page wondering whether the click did anything.
    attach_artifact(
        company.company_dir, dept,
        OnboardingPhase.SCOPE_CALIBRATION,
        artifact_path=scope_rel, job_id=job.id,
    )

    return redirect(url_for("onboarding_dept", slug=slug, dept=dept))


@app.route("/c/<slug>/onboarding/<dept>/start-domain-research", methods=["POST"])
@retrolog_dispatch("domain_research_start", agent_resolver=lambda kw: f"manager:{kw['dept']}")
def onboarding_start_domain_research(slug: str, dept: str):
    """Phase 2 — domain research. Consumes `<dept>/skill-scope.md`
    produced during Phase 1 scope-calibration; does NOT assume an
    industry vertical."""
    from core.dept_onboarding import (
        OnboardingPhase, begin_phase, attach_artifact, domain_brief_path,
        skill_scope_path, render_domain_research_brief,
    )

    company, departments = _company_or_404(slug)
    depts = list_dept_summaries(company, departments)
    dept_summary = next((d for d in depts if d["name"] == dept), None)
    if dept_summary is None:
        abort(404, f"Department '{dept}' not found")

    # Load the calibrated scope if it exists. If missing, the brief
    # template has a graceful-degrade path.
    scope_content = ""
    scope_file = skill_scope_path(company.company_dir, dept)
    if scope_file.exists():
        try:
            scope_content = scope_file.read_text(encoding="utf-8")
        except OSError:
            scope_content = ""

    research_brief = render_domain_research_brief(
        dept=dept,
        dept_label=dept_summary.get("display_name") or dept,
        company_name=company.name,
        industry=company.industry or "",
        skill_scope_content=scope_content,
    )

    # Instruct the manager to write the output to a specific path
    target_path = domain_brief_path(company.company_dir, dept).relative_to(company.company_dir).as_posix()
    full_prompt = (
        f"{research_brief}\n\n"
        f"When complete, write the final brief to `{target_path}` and "
        f"return a 3-line summary plus the path you wrote."
    )

    # Mark the phase as started (artifact path filled in; signoff still NONE)
    begin_phase(
        company.company_dir, dept, OnboardingPhase.DOMAIN_RESEARCH,
        artifact_path=target_path,
    )

    def _target(j):
        result = run_dispatch_action(
            j, str(company.company_dir), dept, full_prompt,
        )
        try:
            # Update the artifact with the job id so we can link back
            attach_artifact(
                company.company_dir, dept,
                OnboardingPhase.DOMAIN_RESEARCH,
                artifact_path=target_path,
                job_id=j.id,
            )
        except Exception:  # pragma: no cover
            pass
        return result

    job = JOB_REGISTRY.submit(
        kind="onboarding",
        label=f"{dept} domain research",
        company_dir=str(company.company_dir),
        target=_target,
    )
    attach_artifact(
        company.company_dir, dept,
        OnboardingPhase.DOMAIN_RESEARCH,
        artifact_path=target_path,
        job_id=job.id,
    )
    return redirect(url_for("onboarding_dept", slug=slug, dept=dept))


@app.route("/c/<slug>/onboarding/<dept>/start-interview", methods=["POST"])
@retrolog_dispatch("founder_interview_start", agent_resolver=lambda kw: f"manager:{kw['dept']}")
def onboarding_start_interview(slug: str, dept: str):
    """Phase 2 — kick off the founder interview as a conversation thread.
    Loads the domain brief as context; manager opens with the first
    question. Founder replies via the chat UI. When they're done,
    `/onboarding/<dept>/finish-interview` synthesizes the thread into
    founder-brief.md."""
    from core.dept_onboarding import (
        OnboardingPhase, begin_phase, ensure_state, attach_artifact,
        domain_brief_path, founder_brief_path,
    )
    from core.conversation import start_thread, persist_thread, send_and_reply

    company, departments = _company_or_404(slug)
    depts = list_dept_summaries(company, departments)
    dept_summary = next((d for d in depts if d["name"] == dept), None)
    if dept_summary is None:
        abort(404, f"Department '{dept}' not found")
    state = ensure_state(company.company_dir, dept)

    domain_brief_rel = domain_brief_path(company.company_dir, dept).relative_to(
        company.company_dir
    ).as_posix()
    founder_brief_rel = founder_brief_path(company.company_dir, dept).relative_to(
        company.company_dir
    ).as_posix()

    # Create the interview thread with the domain brief loaded as context.
    thread = start_thread(
        target_agent=f"manager:{dept}",
        purpose="founder_interview",
        context_refs=(domain_brief_rel,),
        dept=dept,
        onboarding_phase=OnboardingPhase.FOUNDER_INTERVIEW.value,
        title=f"{dept} founder interview",
    )
    persist_thread(company.company_dir, thread)

    # Mark phase as started; attach the thread path as its artifact.
    thread_artifact = f"conversations/{thread.id}.json"
    begin_phase(
        company.company_dir, dept, OnboardingPhase.FOUNDER_INTERVIEW,
        artifact_path=thread_artifact,
    )

    # Fire the opening question as a background job so the page loads fast.
    def _target(j):
        from core.conversation import (
            load_thread, _system_prompt_for_thread, _messages_for_llm,
            append_message,
        )
        from core.llm_client import single_turn

        t = load_thread(company.company_dir, thread.id)
        if t is None:
            return {"ok": False}
        sys = _system_prompt_for_thread(t, company.company_dir)
        # Prompt the manager to open with its first interview question.
        opener_instruction = {
            "role": "user",
            "content": (
                "Start the interview by introducing yourself in one sentence "
                "as the manager, then ask your FIRST question from your "
                "domain brief's 'Key questions to ask the founder' section. "
                "Just one question. Wait for the answer before asking more."
            ),
        }
        resp = single_turn(
            messages=[opener_instruction],
            model="claude-haiku-4-5-20251001",
            cost_tag=f"interview:opener:{dept}",
            system=sys,
            max_tokens=600,
        )
        reply = resp.text if (not resp.error) else f"ERROR: {resp.error}"
        append_message(
            company.company_dir, thread.id,
            role="assistant", content=(reply or "").strip(),
            job_id=j.id, token_usage=resp.usage or {},
        )
        return {"ok": True}

    JOB_REGISTRY.submit(
        kind="interview-opener",
        label=f"{dept} interview opening",
        company_dir=str(company.company_dir),
        target=_target,
    )

    return redirect(url_for("chat_detail", slug=slug, thread_id=thread.id))


@app.route("/c/<slug>/onboarding/<dept>/finish-interview", methods=["POST"])
@retrolog_dispatch("founder_interview_finish", agent_resolver=lambda kw: f"manager:{kw['dept']}")
def onboarding_finish_interview(slug: str, dept: str):
    """Close the in-flight interview thread, synthesize into
    founder-brief.md, mark Phase 2 awaiting sign-off."""
    from core.dept_onboarding import (
        OnboardingPhase, ensure_state, attach_artifact,
        founder_brief_path,
    )
    from core.conversation import synthesize_interview

    company, _ = _company_or_404(slug)
    state = ensure_state(company.company_dir, dept)
    # Find the most-recent FOUNDER_INTERVIEW artifact (the thread path).
    thread_artifact = None
    for a in reversed(state.artifacts):
        if a.phase == OnboardingPhase.FOUNDER_INTERVIEW.value and a.path.startswith("conversations/"):
            thread_artifact = a
            break
    if thread_artifact is None:
        abort(400, "no active interview thread found")
    thread_id = Path(thread_artifact.path).stem

    brief_rel = founder_brief_path(company.company_dir, dept).relative_to(
        company.company_dir
    ).as_posix()

    def _target(j):
        try:
            _, written = synthesize_interview(
                company.company_dir, thread_id,
                output_path=brief_rel,
            )
            # Attach the synthesized brief as the new artifact so sign-off
            # shows a file instead of the thread json.
            attach_artifact(
                company.company_dir, dept,
                OnboardingPhase.FOUNDER_INTERVIEW,
                artifact_path=written or brief_rel,
                job_id=j.id,
            )
            return {"ok": True, "written": written}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    JOB_REGISTRY.submit(
        kind="interview-synth",
        label=f"{dept} interview synthesis",
        company_dir=str(company.company_dir),
        target=_target,
    )

    return redirect(url_for("onboarding_dept", slug=slug, dept=dept))


# ---------------------------------------------------------------------------
# Stack review — post-onboarding Board-led synthesis
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/stack-review")
def stack_review_index(slug: str):
    from core.dept_stack_review import list_reviews, all_departments_complete

    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    reviews = list_reviews(company.company_dir)
    ready_to_run = all_departments_complete(
        company.company_dir, company.active_departments,
    )
    return render_template(
        "stack_review_list.html",
        slug=slug,
        company=summary,
        reviews=reviews,
        ready_to_run=ready_to_run,
    )


def _run_stack_review_job(j, company_dir: str):
    """Background-job target: assemble corpus → convene Board → parse
    → persist. This is the blocking heavy lift; the webapp route
    returns the job id immediately."""
    from pathlib import Path as _Path
    from core.company import load_company
    from core.managers.loader import load_departments
    from core.meeting import run_cross_agent_meeting
    from core.dept_stack_review import (
        SYNTHESIZER_CLOSING_PROMPT, STACK_REVIEW_PARTICIPANTS,
        StackReview, build_review_id, load_review_corpus,
        parse_review, persist_review, render_dossier,
    )
    from datetime import datetime as _dt, timezone as _tz

    j.append_log("Loading company + departments...")
    company = load_company(company_dir)
    departments = load_departments(company)

    j.append_log("Assembling review corpus (onboarding artifacts + config)...")
    corpus = load_review_corpus(
        company.company_dir,
        company_name=company.name,
        industry=company.industry or "",
        active_departments=company.active_departments,
        priorities=company.priorities,
        settled_convictions=company.raw_config.get("settled_convictions", []),
        hard_constraints=company.raw_config.get("hard_constraints", []),
    )
    dossier = render_dossier(corpus)

    # The meeting topic is a single string; the dossier goes into the
    # topic. Each Board voice gets to see it as their topic context.
    # The last participant is prompted with SYNTHESIZER_CLOSING_PROMPT
    # via a wrapper; simplest approach: append the closing instruction
    # to the topic so whoever speaks last is told to synthesize.
    topic = (
        dossier
        + "\n\n---\n\nCLOSING PARTICIPANT — follow these instructions verbatim for your response:\n\n"
        + SYNTHESIZER_CLOSING_PROMPT
    )

    # Write transcript to a stack-review-specific session dir so it
    # doesn't collide with a regular board meeting.
    session_dir = company.company_dir / "decisions" / "stack-reviews" / "_transcripts"
    session_dir.mkdir(parents=True, exist_ok=True)

    j.append_log(f"Convening {len(STACK_REVIEW_PARTICIPANTS)} board voices...")
    transcript = run_cross_agent_meeting(
        company=company,
        departments=departments,
        participants=list(STACK_REVIEW_PARTICIPANTS),
        topic=topic,
        session_dir=session_dir,
    )

    # The final statement is the synthesizer's output.
    synthesizer_text = ""
    if transcript.statements:
        synthesizer_text = transcript.statements[-1].content

    j.append_log("Parsing synthesizer output...")
    gaps, exec_summary, proposals = parse_review(synthesizer_text)

    review_id = build_review_id()
    transcript_rel = (session_dir / "cross-meeting.md").relative_to(
        company.company_dir,
    ).as_posix()
    review = StackReview(
        id=review_id,
        created_at=_dt.now(_tz.utc).isoformat(),
        corpus_summary={
            "dept_count": len(corpus.active_departments),
            "departments": list(corpus.active_departments),
            "has_orchestrator_charter": bool(corpus.orchestrator_charter),
        },
        gaps=gaps,
        proposals=proposals,
        board_transcript_path=transcript_rel,
        notes=exec_summary,
    )
    md_path, json_path = persist_review(company.company_dir, review, synthesizer_text)
    j.append_log(f"Review persisted → {md_path.name}")
    return {
        "ok": True,
        "review_id": review_id,
        "proposal_count": len(proposals),
        "md_path": str(md_path.relative_to(company.company_dir).as_posix()),
    }


@app.route("/c/<slug>/stack-review/run", methods=["POST"])
def stack_review_run(slug: str):
    company, _ = _company_or_404(slug)
    label = "Stack review (board synthesis)"
    job = JOB_REGISTRY.submit(
        kind="stack-review",
        label=label,
        company_dir=str(company.company_dir),
        target=lambda j: _run_stack_review_job(j, str(company.company_dir)),
    )
    return redirect(url_for("job_detail", slug=slug, job_id=job.id))


@app.route("/c/<slug>/stack-review/<review_id>")
def stack_review_detail(slug: str, review_id: str):
    from core.dept_stack_review import load_review

    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    review = load_review(company.company_dir, review_id)
    if review is None:
        abort(404, f"Review '{review_id}' not found")
    return render_template(
        "stack_review_detail.html",
        slug=slug,
        company=summary,
        review=review,
    )


@app.route("/c/<slug>/stack-review/<review_id>/proposal/<proposal_id>/<action>", methods=["POST"])
def stack_review_action(slug: str, review_id: str, proposal_id: str, action: str):
    """Accept / reject / defer a proposal. For ACCEPT on NEW_DEPARTMENT
    or ORCHESTRATOR_AMENDMENT, also applies the side effect
    (dormant-dept stub creation, charter amendment)."""
    from core.dept_stack_review import (
        ProposalStatus, ProposalKind, load_review, mark_proposal_status,
    )

    company, _ = _company_or_404(slug)
    action = action.lower().strip()
    if action not in {"accept", "reject", "defer"}:
        abort(400, "action must be accept | reject | defer")

    review = load_review(company.company_dir, review_id)
    if review is None:
        abort(404, "review not found")
    proposal = next((p for p in review.proposals if p.id == proposal_id), None)
    if proposal is None:
        abort(404, "proposal not found")

    notes = request.form.get("notes", "").strip()
    status_map = {
        "accept": ProposalStatus.ACCEPTED,
        "reject": ProposalStatus.REJECTED,
        "defer": ProposalStatus.DEFERRED,
    }
    status = status_map[action]

    # Side effects on accept for actionable kinds
    extra_notes = []
    if status is ProposalStatus.ACCEPTED:
        if proposal.kind == ProposalKind.NEW_DEPARTMENT.value and proposal.proposed_dept_name:
            try:
                _create_dormant_dept_stub(
                    company, proposal.proposed_dept_name,
                    owns=proposal.proposed_dept_owns,
                    never=proposal.proposed_dept_never,
                    source_review=review_id,
                )
                extra_notes.append(f"Dormant dept stub created: {proposal.proposed_dept_name}")
                # Also auto-fire the hire letter so it's pre-drafted
                # by the time the founder navigates to the new dept.
                try:
                    _fire_scope_calibration_opener(
                        company, proposal.proposed_dept_name,
                    )
                    extra_notes.append("Hire letter dispatch queued")
                except Exception as exc:  # pragma: no cover
                    extra_notes.append(f"Hire letter dispatch FAILED: {exc}")
            except Exception as exc:  # pragma: no cover
                extra_notes.append(f"Dept stub creation FAILED: {exc}")
        if proposal.kind == ProposalKind.ORCHESTRATOR_AMENDMENT.value and proposal.orchestrator_delta:
            try:
                _append_orchestrator_amendment(
                    company, proposal.orchestrator_delta,
                    source_review=review_id, source_proposal=proposal_id,
                )
                extra_notes.append("Orchestrator charter amended")
            except Exception as exc:  # pragma: no cover
                extra_notes.append(f"Orchestrator amendment FAILED: {exc}")

    combined_notes = notes
    if extra_notes:
        combined_notes = (notes + "\n" if notes else "") + " / ".join(extra_notes)

    mark_proposal_status(
        company.company_dir, review_id, proposal_id,
        status=status, notes=combined_notes,
    )
    return redirect(url_for("stack_review_detail", slug=slug, review_id=review_id))


def _create_dormant_dept_stub(
    company, dept_name: str,
    *, owns: tuple[str, ...], never: tuple[str, ...], source_review: str,
) -> None:
    """Implement a NEW_DEPARTMENT proposal: append the dept to
    config.active_departments + create a minimal department folder
    with an onboarding state initialized to PENDING. The founder then
    runs the normal onboarding flow (hire → research → interview → etc.)
    to bring it online."""
    import json as _json
    from core.dept_onboarding import ensure_state

    # Append to config.json's active_departments (the canonical list)
    config_path = company.company_dir / "config.json"
    if not config_path.exists():
        raise RuntimeError("config.json missing")
    cfg = _json.loads(config_path.read_text(encoding="utf-8"))
    active = list(cfg.get("active_departments", []))
    if dept_name not in active:
        active.append(dept_name)
        cfg["active_departments"] = active
        config_path.write_text(
            _json.dumps(cfg, indent=2, sort_keys=False),
            encoding="utf-8",
        )

    # Create a minimal dept folder with a scope-matrix stub so the
    # onboarding flow has somewhere to land.
    dept_dir = company.company_dir / dept_name
    dept_dir.mkdir(parents=True, exist_ok=True)
    stub = dept_dir / "department-stub.md"
    if not stub.exists():
        lines = [
            f"# {dept_name} (dormant department stub)",
            "",
            f"Created from stack-review proposal `{source_review}`.",
            "",
            "## Proposed scope (OWNS)",
        ]
        for t in owns:
            lines.append(f"- {t}")
        lines.append("")
        lines.append("## Proposed scope (NEVER)")
        for t in never:
            lines.append(f"- {t}")
        lines.append("")
        lines.append(
            "_Run the Onboarding flow to bring this dept online — "
            "Phase 1 (the hire) will begin when you click through to /onboarding/"
            + dept_name + "._"
        )
        stub.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Initialize onboarding state so the dashboard shows it as PENDING
    ensure_state(company.company_dir, dept_name)


def _fire_scope_calibration_opener(company, dept_name: str):
    """Shared helper — create the scope-calibration thread + begin
    the SCOPE_CALIBRATION phase + queue the opener dispatch as a
    background job. Used by both the standard onboarding-start route
    and the stack-review accept flow (so newly-proposed dormant depts
    arrive with a pre-drafted hire letter)."""
    from core.dept_onboarding import (
        OnboardingPhase, begin_phase, ensure_state,
        render_scope_calibration_prompt,
    )
    from core.conversation import (
        start_thread, persist_thread, load_thread,
        _system_prompt_for_thread, append_message,
    )
    from core.llm_client import single_turn

    # Resolve the dept's display label from departments list; fall
    # back to the slug.
    from webapp.services import list_dept_summaries, load_departments_safe
    departments = load_departments_safe(company)
    depts = list_dept_summaries(company, departments)
    dept_summary = next((d for d in depts if d["name"] == dept_name), None)
    dept_label = (dept_summary or {}).get("display_name") or dept_name

    opening_prompt = render_scope_calibration_prompt(
        dept=dept_name, dept_label=dept_label,
        company_name=company.name, industry=company.industry or "",
    )

    thread = start_thread(
        target_agent=f"manager:{dept_name}",
        purpose="scope_calibration",
        seed_system=opening_prompt,
        dept=dept_name,
        onboarding_phase=OnboardingPhase.SCOPE_CALIBRATION.value,
        title=f"{dept_name} scope calibration",
    )
    persist_thread(company.company_dir, thread)

    ensure_state(company.company_dir, dept_name)
    begin_phase(
        company.company_dir, dept_name, OnboardingPhase.SCOPE_CALIBRATION,
        artifact_path=f"conversations/{thread.id}.json",
    )

    def _target(j):
        t = load_thread(company.company_dir, thread.id)
        if t is None:
            return {"ok": False}
        sys_prompt = _system_prompt_for_thread(t, company.company_dir)
        resp = single_turn(
            messages=[{
                "role": "user",
                "content": (
                    "Write your hire letter now, following the structure in "
                    "your system prompt exactly (primary, secondary, vignette, "
                    "posture note). One message, 250-450 words. Do NOT ask "
                    "the founder to calibrate your secondary — you are "
                    "declaring it, not proposing it for approval."
                ),
            }],
            model="claude-haiku-4-5-20251001",
            cost_tag=f"scope-calibration:opener:{dept_name}",
            system=sys_prompt, max_tokens=900,
        )
        reply = resp.text if not resp.error else f"ERROR: {resp.error}"
        append_message(
            company.company_dir, thread.id,
            role="assistant", content=(reply or "").strip(),
            job_id=j.id, token_usage=resp.usage or {},
        )
        return {"ok": True, "thread_id": thread.id}

    JOB_REGISTRY.submit(
        kind="scope-calibration-opener",
        label=f"{dept_name} hire letter",
        company_dir=str(company.company_dir),
        target=_target,
    )
    return thread.id


def _append_orchestrator_amendment(
    company, delta: str, *, source_review: str, source_proposal: str,
) -> None:
    """Append a dated amendment block to orchestrator-charter.md.
    Does NOT rewrite the charter — additive change log so the
    amendment history is readable."""
    from datetime import datetime as _dt, timezone as _tz

    charter = company.company_dir / "orchestrator-charter.md"
    ts = _dt.now(_tz.utc).strftime("%Y-%m-%d")
    amendment_block = (
        "\n\n---\n\n"
        f"## Amendment — {ts}\n\n"
        f"_Source: stack-review `{source_review}`, proposal `{source_proposal}`._\n\n"
        f"{delta.strip()}\n"
    )
    if charter.exists():
        existing = charter.read_text(encoding="utf-8")
        charter.write_text(existing.rstrip() + amendment_block, encoding="utf-8")
    else:
        # No charter yet — create a minimal one with the amendment
        # embedded. Founder can run /onboarding/orchestrator/run later
        # for a proper one.
        header = (
            "# Orchestrator charter (stub)\n\n"
            "This charter was auto-created from a stack-review amendment. "
            "Run the Orchestrator interview at `/onboarding/orchestrator` for a proper charter.\n"
        )
        charter.write_text(header + amendment_block, encoding="utf-8")


@app.route("/c/<slug>/onboarding/<dept>/signoff", methods=["POST"])
def onboarding_signoff(slug: str, dept: str):
    """Record founder sign-off on the current phase. Form params:
    status=approved|rejected|skipped, rating=-2..+2, notes=free text."""
    from core.dept_onboarding import (
        signoff_phase, SignoffStatus, OnboardingPhase, ensure_state,
    )

    company, _ = _company_or_404(slug)
    state = ensure_state(company.company_dir, dept)
    try:
        phase = OnboardingPhase(state.phase)
    except ValueError:
        abort(400, f"unknown phase {state.phase!r}")

    raw_status = request.form.get("status", "").strip().lower()
    try:
        status = SignoffStatus(raw_status)
    except ValueError:
        abort(400, "status must be approved|rejected|skipped")
    if status not in {SignoffStatus.APPROVED, SignoffStatus.REJECTED, SignoffStatus.SKIPPED}:
        abort(400, "invalid status")
    rating_raw = request.form.get("rating", "").strip()
    rating = None
    if rating_raw:
        try:
            rating = int(rating_raw)
        except ValueError:
            abort(400, "rating must be an int in [-2, 2]")
    notes = request.form.get("notes", "").strip()
    signoff_phase(
        company.company_dir, dept, phase,
        status=status, rating=rating, notes=notes,
    )

    # Auto-trigger the stack review when the LAST active dept just
    # landed on COMPLETE. Propose-only, runs in the background.
    # Guard: only fire if no stack review is already in-flight or
    # already written for today (to avoid repeated firings).
    try:
        from core.dept_stack_review import (
            all_departments_complete, review_json_path, build_review_id,
        )
        if all_departments_complete(company.company_dir, company.active_departments):
            today_path = review_json_path(company.company_dir, build_review_id())
            already_have_one_today = today_path.exists()
            in_flight = any(
                j.kind == "stack-review" and j.status == "running"
                for j in JOB_REGISTRY.list_jobs(str(company.company_dir))
            )
            if not already_have_one_today and not in_flight:
                JOB_REGISTRY.submit(
                    kind="stack-review",
                    label="Auto stack review (all depts complete)",
                    company_dir=str(company.company_dir),
                    target=lambda j: _run_stack_review_job(j, str(company.company_dir)),
                )
    except Exception:  # pragma: no cover — auto-trigger is best-effort
        pass

    return redirect(url_for("onboarding_dept", slug=slug, dept=dept))


# ---------------------------------------------------------------------------
# STAFFING phase: manager proposes roster, founder approves row-by-row,
# each approved row fires a sub-agent hire using the manager hire mechanic.
# ---------------------------------------------------------------------------

def _read_text_if_exists(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except Exception:
        return ""


def _extract_first_json_object(raw: str) -> dict:
    """Pull the first balanced {...} block out of raw text and parse it.
    The manager sometimes wraps JSON in prose or a fence despite the
    prompt; this is a minimal tolerant parser so we don't fail the whole
    phase on a stray prefix."""
    import json as _json
    s = raw.strip()
    # strip code fence
    if s.startswith("```"):
        s = s.split("```", 2)[-1]
        if s.startswith("json"):
            s = s[4:]
    # find first { and walk braces
    start = s.find("{")
    if start < 0:
        raise ValueError("no JSON object found in manager output")
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(s[start:], start=start):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return _json.loads(s[start:i + 1])
    raise ValueError("unbalanced JSON object in manager output")


@app.route("/c/<slug>/onboarding/<dept>/start-staffing", methods=["POST"])
@retrolog_dispatch("staffing_start", agent_resolver=lambda kw: f"manager:{kw['dept']}")
def onboarding_start_staffing(slug: str, dept: str):
    """Dispatch the manager to propose the departmental roster from
    scratch. Writes to <dept>/roster.json when the job finishes."""
    import datetime
    from core.dept_onboarding import (
        OnboardingPhase, begin_phase, ensure_state, attach_artifact,
        charter_path, founder_brief_path, skill_scope_path,
    )
    from core.dept_roster import (
        DepartmentRoster, RosterEntry, persist_roster, slugify_role,
        render_roster_proposal_prompt, VALID_CRITICALITY, CRITICALITY_CORE,
    )
    from core.llm_client import single_turn

    company, departments = _company_or_404(slug)
    depts = list_dept_summaries(company, departments)
    dept_summary = next((d for d in depts if d["name"] == dept), None)
    if dept_summary is None:
        abort(404, f"Department '{dept}' not found")
    state = ensure_state(company.company_dir, dept)
    if state.phase != OnboardingPhase.STAFFING.value:
        abort(400, f"Department '{dept}' is not in the staffing phase (currently {state.phase!r})")

    # Load the inputs the manager will need.
    charter_content = _read_text_if_exists(charter_path(company.company_dir, dept))
    founder_brief_content = _read_text_if_exists(founder_brief_path(company.company_dir, dept))
    skill_scope_content = _read_text_if_exists(skill_scope_path(company.company_dir, dept))

    # Best-effort extract of the manager's declared secondary from their
    # own skill-scope.md so the prompt can anchor adjacency guidance.
    manager_secondary = ""
    for line in skill_scope_content.splitlines():
        stripped = line.strip()
        if stripped.startswith("1.") and "**" in stripped:
            manager_secondary = stripped.split("**")[1] if stripped.count("**") >= 2 else ""
            break

    prompt = render_roster_proposal_prompt(
        dept=dept,
        dept_label=dept_summary.get("display_name") or dept,
        company_name=company.name,
        industry=company.industry or "",
        manager_secondary=manager_secondary,
        charter_content=charter_content,
        founder_brief_content=founder_brief_content,
        skill_scope_content=skill_scope_content,
    )

    roster_rel = (company.company_dir / dept / "roster.json").relative_to(
        company.company_dir
    ).as_posix()

    begin_phase(
        company.company_dir, dept, OnboardingPhase.STAFFING,
        artifact_path=roster_rel,
    )

    def _target(j):
        resp = single_turn(
            messages=[{"role": "user", "content": prompt}],
            model="claude-sonnet-4-6",
            cost_tag=f"staffing:roster-propose:{dept}",
            system=(
                "You are the department manager producing a JSON roster. "
                "Return only the JSON object, no prose."
            ),
            max_tokens=3000,
        )
        if resp.error:
            # Persist an empty roster with the error as the note.
            empty = DepartmentRoster(
                dept=dept,
                proposed_at=datetime.datetime.utcnow().isoformat() + "Z",
                last_updated_at=datetime.datetime.utcnow().isoformat() + "Z",
                entries=(),
                notes=f"LLM error: {resp.error}",
            )
            persist_roster(company.company_dir, empty)
            return {"ok": False, "error": resp.error}

        try:
            parsed = _extract_first_json_object(resp.text or "")
        except Exception as exc:
            persist_roster(company.company_dir, DepartmentRoster(
                dept=dept,
                proposed_at=datetime.datetime.utcnow().isoformat() + "Z",
                last_updated_at=datetime.datetime.utcnow().isoformat() + "Z",
                entries=(),
                notes=f"roster parse failed: {exc}; raw: {(resp.text or '')[:400]}",
            ))
            return {"ok": False, "error": f"parse: {exc}"}

        entries: list[RosterEntry] = []
        seen_slugs: set[str] = set()
        for row in parsed.get("entries", []) or []:
            display_name = (row.get("display_name") or "").strip()
            slug_raw = (row.get("role_slug") or display_name or "role").strip()
            slug = slugify_role(slug_raw)
            base_slug = slug
            n = 2
            while slug in seen_slugs:
                slug = f"{base_slug}-{n}"
                n += 1
            seen_slugs.add(slug)
            crit = (row.get("criticality") or CRITICALITY_CORE).strip().lower()
            if crit not in VALID_CRITICALITY:
                crit = CRITICALITY_CORE
            entries.append(RosterEntry(
                role_slug=slug,
                display_name=display_name or slug,
                primary_description=(row.get("primary_description") or "").strip(),
                criticality=crit,
                suggested_adjacency=(row.get("suggested_adjacency") or "").strip(),
            ))

        persisted = DepartmentRoster(
            dept=dept,
            proposed_at=datetime.datetime.utcnow().isoformat() + "Z",
            last_updated_at=datetime.datetime.utcnow().isoformat() + "Z",
            entries=tuple(entries),
            notes=(parsed.get("notes") or "").strip(),
        )
        persist_roster(company.company_dir, persisted)
        return {"ok": True, "role_count": len(entries)}

    job = JOB_REGISTRY.submit(
        kind="staffing-roster-propose",
        label=f"{dept} roster proposal",
        company_dir=str(company.company_dir),
        target=_target,
    )

    # Update the phase artifact with the new job_id.
    attach_artifact(
        company.company_dir, dept, OnboardingPhase.STAFFING,
        artifact_path=roster_rel, job_id=job.id,
    )

    return redirect(url_for("onboarding_dept", slug=slug, dept=dept))


def _all_nonterminal_resolved(roster) -> bool:
    """True if every row is in a terminal state (hired, rejected, skipped)
    so the staffing phase can be finished."""
    from core.dept_roster import (
        ROLE_STATUS_HIRED, ROLE_STATUS_REJECTED, ROLE_STATUS_SKIPPED,
    )
    terminal = {ROLE_STATUS_HIRED, ROLE_STATUS_REJECTED, ROLE_STATUS_SKIPPED}
    return all(e.status in terminal for e in roster.entries)


def _existing_secondaries_for(company_dir: Path, dept: str) -> tuple[str, ...]:
    """Collect secondaries already occupied in this department's web:
    the manager's own, plus any sub-agent that has declared one (in
    awaiting or hired status)."""
    from core.dept_onboarding import skill_scope_path
    from core.dept_roster import load_roster
    taken: list[str] = []
    mgr_ss = _read_text_if_exists(skill_scope_path(company_dir, dept))
    for line in mgr_ss.splitlines():
        stripped = line.strip()
        if stripped.startswith("1.") and stripped.count("**") >= 2:
            taken.append(stripped.split("**")[1])
            break
    roster = load_roster(company_dir, dept)
    if roster:
        taken.extend([s for s in roster.declared_secondaries if s])
    # dedupe preserving order
    seen = set()
    out = []
    for s in taken:
        key = s.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(s.strip())
    return tuple(out)


def _parse_declared_secondary_from_reply(reply: str) -> str:
    """Best-effort extraction of the declared-secondary field name from
    an arrival-note reply. Returns '' if nothing plausible was found."""
    for line in (reply or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(("**my secondary", "## my secondary", "2.")) and stripped.count("**") >= 2:
            return stripped.split("**")[1].strip().rstrip(":")
    for line in (reply or "").splitlines():
        if line.count("**") >= 2:
            candidate = line.split("**")[1].strip()
            if candidate and len(candidate) < 120:
                return candidate
    return ""


@app.route("/c/<slug>/onboarding/<dept>/roster/<role_slug>/approve", methods=["POST"])
@retrolog_dispatch("roster_approve", agent_resolver=lambda kw: f"subagent:{kw['dept']}:{kw['role_slug']}")
def onboarding_roster_approve(slug: str, dept: str, role_slug: str):
    """Approve a proposed row AND dispatch THREE sub-agent candidates in
    parallel. Each candidate draws its own personality seeds and will
    declare its own secondary. The founder then picks one on the
    roster detail page once the arrivals land."""
    import datetime
    from dataclasses import replace
    from core.conversation import start_thread, persist_thread, append_message
    from core.dept_roster import (
        load_roster, persist_roster, upsert_entry,
        ROLE_STATUS_PROPOSED, ROLE_STATUS_HIRING,
        render_subagent_arrival_prompt,
        SubagentCandidate, new_subagent_candidate_id,
        CANDIDATE_STATUS_DRAFTING, CANDIDATE_STATUS_READY,
    )
    from core.llm_client import single_turn

    company, departments = _company_or_404(slug)
    depts = list_dept_summaries(company, departments)
    dept_summary = next((d for d in depts if d["name"] == dept), None)
    if dept_summary is None:
        abort(404, f"Department '{dept}' not found")

    roster = load_roster(company.company_dir, dept)
    if roster is None:
        abort(404, "No roster proposed yet")
    entry = roster.find(role_slug)
    if entry is None:
        abort(404, f"Role '{role_slug}' not found in roster")
    if entry.status not in {ROLE_STATUS_PROPOSED, "rejected"}:
        # Idempotent: if already hiring/awaiting/hired, do nothing
        return redirect(url_for("onboarding_dept", slug=slug, dept=dept))

    existing = _existing_secondaries_for(company.company_dir, dept)
    now_iso = datetime.datetime.utcnow().isoformat() + "Z"
    labels = ["Candidate A", "Candidate B", "Candidate C"]
    candidates: list[SubagentCandidate] = []
    dispatch_payloads: list[tuple[str, str, str, str, str]] = []

    for i in range(3):
        label = labels[i] if i < len(labels) else f"Candidate {i + 1}"
        prompt = render_subagent_arrival_prompt(
            dept=dept,
            dept_label=dept_summary.get("display_name") or dept,
            company_name=company.name,
            industry=company.industry or "",
            role_slug=entry.role_slug,
            role_display_name=entry.display_name,
            primary_description=entry.primary_description,
            suggested_adjacency=entry.suggested_adjacency,
            existing_secondaries=existing,
        )
        thread = start_thread(
            target_agent=f"subagent:{dept}:{entry.role_slug}",
            purpose="subagent_arrival",
            seed_system=prompt,
            dept=dept,
            onboarding_phase="staffing",
            title=f"{entry.display_name} hire: {label} ({dept})",
        )
        persist_thread(company.company_dir, thread)
        cand = SubagentCandidate(
            candidate_id=new_subagent_candidate_id(),
            label=label,
            thread_path=f"conversations/{thread.id}.json",
            status=CANDIDATE_STATUS_DRAFTING,
            created_at=now_iso,
        )
        candidates.append(cand)
        dispatch_payloads.append((
            thread.id, cand.candidate_id, label, prompt, entry.role_slug,
        ))

    # Persist roster with the slate before dispatching (UI can render
    # "drafting" state immediately without waiting for any job).
    new_entry = replace(
        entry,
        status=ROLE_STATUS_HIRING,
        candidates=tuple(candidates),
        selected_candidate_id="",
        # Clear the single-candidate fields so they don't confuse the UI.
        arrival_thread_path="",
        declared_secondary="",
    )
    roster = upsert_entry(roster, new_entry)
    roster = replace(roster, last_updated_at=now_iso)
    persist_roster(company.company_dir, roster)

    # Dispatch each arrival-note job.
    for thread_id, cand_id, cand_label, prompt_text, role_slug_captured in dispatch_payloads:
        def _make_target(tid: str, cid: str, clabel: str, sys_prompt: str, r_slug: str):
            def _target(j):
                from core.dept_roster import (
                    load_roster as _lr, upsert_entry as _ue, persist_roster as _pr,
                )
                from dataclasses import replace as _replace
                opener = {
                    "role": "user",
                    "content": (
                        "Write your arrival note now, following the system prompt exactly. "
                        "First person. 320 to 550 words. Declare a specific secondary field "
                        "that is adjacent to one already in the department and not a "
                        "duplicate of any existing one. Lean into the personality seeds "
                        "you were given; the note must read as a specific person."
                    ),
                }
                resp = single_turn(
                    messages=[opener],
                    model="claude-haiku-4-5-20251001",
                    cost_tag=f"staffing:subagent-candidate:{dept}:{r_slug}:{clabel.replace(' ', '_')}",
                    system=sys_prompt,
                    max_tokens=900,
                )
                reply = resp.text if not resp.error else f"ERROR: {resp.error}"
                append_message(
                    company.company_dir, tid,
                    role="assistant", content=(reply or "").strip(),
                    job_id=j.id, token_usage=resp.usage or {},
                )
                declared = _parse_declared_secondary_from_reply(reply)
                # Flip this candidate to READY in the roster.
                roster_now = _lr(company.company_dir, dept)
                if roster_now is not None:
                    row = roster_now.find(r_slug)
                    if row is not None:
                        new_cands = []
                        for cc in row.candidates:
                            if cc.candidate_id == cid:
                                new_cands.append(_replace(
                                    cc,
                                    status=CANDIDATE_STATUS_READY,
                                    declared_secondary=declared,
                                    job_id=j.id,
                                ))
                            else:
                                new_cands.append(cc)
                        updated_row = _replace(row, candidates=tuple(new_cands))
                        roster_now = _ue(roster_now, updated_row)
                        _pr(company.company_dir, roster_now)
                return {"ok": not resp.error}
            return _target

        job = JOB_REGISTRY.submit(
            kind="staffing-subagent-candidate",
            label=f"{entry.display_name} hire: {cand_label} ({dept})",
            company_dir=str(company.company_dir),
            target=_make_target(thread_id, cand_id, cand_label, prompt_text, role_slug_captured),
        )
        # Record the job_id on the candidate for the UI.
        roster_for_jobid = load_roster(company.company_dir, dept)
        if roster_for_jobid is not None:
            row = roster_for_jobid.find(role_slug)
            if row is not None:
                new_cands = []
                for cc in row.candidates:
                    if cc.candidate_id == cand_id:
                        new_cands.append(replace(cc, job_id=job.id))
                    else:
                        new_cands.append(cc)
                roster_for_jobid = upsert_entry(
                    roster_for_jobid, replace(row, candidates=tuple(new_cands)),
                )
                persist_roster(company.company_dir, roster_for_jobid)

    return redirect(url_for("onboarding_dept", slug=slug, dept=dept))


@app.route(
    "/c/<slug>/onboarding/<dept>/roster/<role_slug>/select-candidate/<candidate_id>",
    methods=["POST"],
)
@retrolog_dispatch("roster_select_candidate", agent_resolver=lambda kw: f"subagent:{kw['dept']}:{kw['role_slug']}")
def onboarding_roster_select_candidate(slug: str, dept: str, role_slug: str, candidate_id: str):
    """Founder picked this sub-agent candidate off the role's slate.
    Mark it selected, others discarded, close losing threads, pin the
    selected candidate's thread as the row's arrival_thread_path so the
    existing hire synthesis path finds it, and move the row to
    awaiting-signoff."""
    import datetime
    from dataclasses import replace
    from core.conversation import close_thread
    from core.dept_roster import (
        load_roster, persist_roster, upsert_entry,
        ROLE_STATUS_AWAITING,
        CANDIDATE_STATUS_SELECTED, CANDIDATE_STATUS_DISCARDED,
    )

    company, _ = _company_or_404(slug)
    roster = load_roster(company.company_dir, dept)
    if roster is None:
        abort(404, "No roster on file")
    entry = roster.find(role_slug)
    if entry is None:
        abort(404, f"Role '{role_slug}' not found")
    chosen = entry.find_candidate(candidate_id)
    if chosen is None:
        abort(404, f"Candidate '{candidate_id}' not found for role '{role_slug}'")

    new_cands = []
    for c in entry.candidates:
        if c.candidate_id == candidate_id:
            new_cands.append(replace(c, status=CANDIDATE_STATUS_SELECTED))
        else:
            new_cands.append(replace(c, status=CANDIDATE_STATUS_DISCARDED))
            # Close the losing thread so it doesn't linger as open.
            losing_id = Path(c.thread_path).stem
            if losing_id:
                close_thread(
                    company.company_dir, losing_id,
                    summary_path="(discarded candidate)",
                )
    updated_entry = replace(
        entry,
        candidates=tuple(new_cands),
        selected_candidate_id=candidate_id,
        status=ROLE_STATUS_AWAITING,
        arrival_thread_path=chosen.thread_path,
        declared_secondary=chosen.declared_secondary,
    )
    roster = upsert_entry(roster, updated_entry)
    roster = replace(roster, last_updated_at=datetime.datetime.utcnow().isoformat() + "Z")
    persist_roster(company.company_dir, roster)
    return redirect(url_for("onboarding_dept", slug=slug, dept=dept))


@app.route("/c/<slug>/onboarding/<dept>/roster/<role_slug>/reject", methods=["POST"])
@retrolog_dispatch("roster_reject", agent_resolver=lambda kw: f"subagent:{kw['dept']}:{kw['role_slug']}")
def onboarding_roster_reject(slug: str, dept: str, role_slug: str):
    """Mark a proposed row as rejected. Keeps it in history; will not hire."""
    import datetime
    from dataclasses import replace
    from core.dept_roster import (
        load_roster, persist_roster, upsert_entry, ROLE_STATUS_REJECTED,
    )
    company, _ = _company_or_404(slug)
    roster = load_roster(company.company_dir, dept)
    if roster is None:
        abort(404, "No roster proposed yet")
    entry = roster.find(role_slug)
    if entry is None:
        abort(404)
    updated = replace(entry, status=ROLE_STATUS_REJECTED)
    roster = upsert_entry(roster, updated)
    roster = replace(roster, last_updated_at=datetime.datetime.utcnow().isoformat() + "Z")
    persist_roster(company.company_dir, roster)
    return redirect(url_for("onboarding_dept", slug=slug, dept=dept))


@app.route("/c/<slug>/onboarding/<dept>/roster/<role_slug>/skip", methods=["POST"])
@retrolog_dispatch("roster_skip", agent_resolver=lambda kw: f"subagent:{kw['dept']}:{kw['role_slug']}")
def onboarding_roster_skip(slug: str, dept: str, role_slug: str):
    import datetime
    from dataclasses import replace
    from core.dept_roster import (
        load_roster, persist_roster, upsert_entry, ROLE_STATUS_SKIPPED,
    )
    company, _ = _company_or_404(slug)
    roster = load_roster(company.company_dir, dept)
    if roster is None:
        abort(404)
    entry = roster.find(role_slug)
    if entry is None:
        abort(404)
    updated = replace(entry, status=ROLE_STATUS_SKIPPED)
    roster = upsert_entry(roster, updated)
    roster = replace(roster, last_updated_at=datetime.datetime.utcnow().isoformat() + "Z")
    persist_roster(company.company_dir, roster)
    return redirect(url_for("onboarding_dept", slug=slug, dept=dept))


@app.route("/c/<slug>/onboarding/<dept>/roster/<role_slug>/reroll", methods=["POST"])
@retrolog_dispatch("roster_reroll", agent_resolver=lambda kw: f"subagent:{kw['dept']}:{kw['role_slug']}")
def onboarding_roster_reroll(slug: str, dept: str, role_slug: str):
    """Discard the existing candidate slate for a sub-agent role and
    dispatch a fresh set of three. Used when none of the three candidates
    felt right, or when the declared secondaries collided with existing
    department secondaries in a way the founder didn't expect."""
    import datetime
    from dataclasses import replace
    from core.dept_roster import (
        load_roster, persist_roster, upsert_entry, ROLE_STATUS_PROPOSED,
    )
    company, _ = _company_or_404(slug)
    roster = load_roster(company.company_dir, dept)
    if roster is None:
        abort(404)
    entry = roster.find(role_slug)
    if entry is None:
        abort(404)
    cleared = replace(
        entry,
        status=ROLE_STATUS_PROPOSED,
        declared_secondary="",
        arrival_thread_path="",
        job_id="",
        candidates=(),
        selected_candidate_id="",
    )
    roster = upsert_entry(roster, cleared)
    roster = replace(roster, last_updated_at=datetime.datetime.utcnow().isoformat() + "Z")
    persist_roster(company.company_dir, roster)
    # Re-approve to fire a new three-candidate slate.
    return redirect(url_for(
        "onboarding_roster_approve", slug=slug, dept=dept, role_slug=role_slug,
    ), code=307)


@app.route("/c/<slug>/onboarding/<dept>/roster/<role_slug>/hire", methods=["POST"])
@retrolog_dispatch("roster_hire", agent_resolver=lambda kw: f"subagent:{kw['dept']}:{kw['role_slug']}")
def onboarding_roster_hire(slug: str, dept: str, role_slug: str):
    """Final hire: take the awaiting arrival note, synthesize the
    sub-agent's skill-scope.md, close the thread, mark HIRED."""
    import datetime
    from dataclasses import replace
    from core.conversation import (
        load_thread, synthesize_interview, SYNTHESIS_PROMPT,
    )
    from core.dept_onboarding import subagent_skill_scope_path
    from core.dept_roster import (
        load_roster, persist_roster, upsert_entry,
        ROLE_STATUS_AWAITING, ROLE_STATUS_HIRED,
        SUBAGENT_SKILL_SCOPE_SYNTHESIS_PROMPT,
    )
    company, _ = _company_or_404(slug)
    roster = load_roster(company.company_dir, dept)
    if roster is None:
        abort(404)
    entry = roster.find(role_slug)
    if entry is None:
        abort(404)
    if entry.status != ROLE_STATUS_AWAITING:
        abort(400, f"Role '{role_slug}' is not awaiting hire (status={entry.status})")
    if not entry.arrival_thread_path:
        abort(400, "Role has no arrival thread on file")

    thread_id = entry.arrival_thread_path.split("/")[-1].replace(".json", "")
    thread = load_thread(company.company_dir, thread_id)
    if thread is None:
        abort(404, "Arrival thread not found on disk")

    output_rel = subagent_skill_scope_path(
        company.company_dir, dept, role_slug,
    ).relative_to(company.company_dir).as_posix()
    try:
        synthesize_interview(
            company_dir=company.company_dir,
            thread_id=thread_id,
            output_path=output_rel,
            prompt_override=SUBAGENT_SKILL_SCOPE_SYNTHESIS_PROMPT,
        )
    except TypeError:
        # Older synthesize_interview signature without override: fall
        # back to the default SYNTHESIS_PROMPT behavior. Not ideal but
        # keeps the hire progressable.
        synthesize_interview(
            company_dir=company.company_dir,
            thread_id=thread_id,
            output_path=output_rel,
        )
    updated = replace(
        entry,
        status=ROLE_STATUS_HIRED,
        skill_scope_path=output_rel,
    )
    roster = upsert_entry(roster, updated)
    roster = replace(roster, last_updated_at=datetime.datetime.utcnow().isoformat() + "Z")
    persist_roster(company.company_dir, roster)
    return redirect(url_for("onboarding_dept", slug=slug, dept=dept))


@app.route("/c/<slug>/onboarding/<dept>/finish-staffing", methods=["POST"])
@retrolog_dispatch("staffing_finish", agent_resolver=lambda kw: f"manager:{kw['dept']}")
def onboarding_finish_staffing(slug: str, dept: str):
    """Close out the staffing phase. Requires every roster row to be
    in a terminal state (hired, rejected, or skipped)."""
    from core.dept_onboarding import (
        OnboardingPhase, signoff_phase, SignoffStatus, attach_artifact,
    )
    from core.dept_roster import load_roster

    company, _ = _company_or_404(slug)
    roster = load_roster(company.company_dir, dept)
    if roster is None:
        abort(400, "No roster on file yet")
    if not _all_nonterminal_resolved(roster):
        abort(
            400,
            "Cannot finish staffing: some roster rows are still in progress. "
            "Accept, reject, or skip each row first.",
        )
    # Attach the final roster as the phase artifact (idempotent).
    roster_rel = (company.company_dir / dept / "roster.json").relative_to(
        company.company_dir
    ).as_posix()
    attach_artifact(
        company.company_dir, dept, OnboardingPhase.STAFFING,
        artifact_path=roster_rel,
    )
    signoff_phase(
        company.company_dir, dept, OnboardingPhase.STAFFING,
        status=SignoffStatus.APPROVED,
    )
    return redirect(url_for("onboarding_dept", slug=slug, dept=dept))


# ---------------------------------------------------------------------------
# Governance (Phase 1): trust observability + decisions audit log.
# Read-only. Shows per-agent trust scores computed from existing rating
# sources, plus a paginated audit trail of founder-initiated dispatches
# retro-logged by the @retrolog_dispatch decorator.
# ---------------------------------------------------------------------------

@app.route("/c/<slug>/governance")
def governance_page(slug: str):
    import datetime as _datetime
    from core.governance import (
        aggregate_trust, last_successful_retrolog_write,
    )
    from core.governance.trust import is_dormant as _is_dormant
    from core.governance.storage import open_db, recent_decisions

    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)

    # Compute fresh trust snapshots for every agent with at least one
    # rating sample. Phase 1 recomputes on every load (documented
    # limitation); Phase 2 will add an invalidation-based cache.
    snapshots = aggregate_trust(company.company_dir)
    now = _datetime.datetime.now(tz=_datetime.timezone.utc)
    trust_rows = []
    for agent_id, snap in snapshots.items():
        trust_rows.append({
            "agent_id": agent_id,
            "display_name": _humanize_agent_id(agent_id),
            "score": snap.score,
            "sample_count": snap.sample_count,
            "last_sample_at": snap.last_sample_at,
            "dormant": _is_dormant(snap, now=now),
            "breakdown": snap.breakdown,
        })
    trust_rows.sort(key=lambda r: r["score"], reverse=True)

    # Recent decisions pagination.
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        offset = 0
    source_filter = request.args.get("source", "human").strip().lower()
    if source_filter not in {"human", "agent", "all"}:
        source_filter = "human"

    try:
        conn = open_db(company.company_dir)
        try:
            decisions = recent_decisions(
                conn,
                source=None if source_filter == "all" else source_filter,
                limit=50,
                offset=offset,
            )
        finally:
            conn.close()
    except Exception:
        decisions = []

    last_retrolog = last_successful_retrolog_write(company.company_dir)

    return render_template(
        "governance_trust.html",
        slug=slug,
        company=summary,
        trust_rows=trust_rows,
        decisions=decisions,
        source_filter=source_filter,
        offset=offset,
        last_retrolog=last_retrolog,
        total_agents=len(trust_rows),
        total_samples=sum(r["sample_count"] for r in trust_rows),
        oldest_sample_at=min(
            (r["last_sample_at"] for r in trust_rows if r["last_sample_at"]),
            default=None,
        ),
    )


def _humanize_agent_id(agent_id: str) -> str:
    """Turn 'manager:marketing' into 'Marketing manager', etc."""
    if agent_id.startswith("manager:"):
        dept = agent_id.split(":", 1)[1].replace("-", " ")
        return f"{dept.title()} manager"
    if agent_id.startswith("subagent:"):
        parts = agent_id.split(":")
        if len(parts) >= 3:
            dept = parts[1].replace("-", " ")
            role = parts[2].replace("-", " ")
            return f"{role.title()} ({dept.title()})"
    return agent_id


# ---------------------------------------------------------------------------
# Scope coordination: company-level round. Fires when every department has
# completed its founder interview. Managers collectively produce a scope
# map and per-dept scope-of-work.md. Founder ratifies.
# ---------------------------------------------------------------------------

@app.route("/c/<slug>/coordination")
def coordination_page(slug: str):
    """Show the scope-coordination status for the company, including
    per-department readiness, the run button when all depts are ready,
    and the scope map plus approve/reject form after the run completes."""
    from core.dept_onboarding import (
        OnboardingPhase, ensure_state, list_all_states,
    )
    from core.scope_coordination import (
        load_state, department_ready_for_coordination,
        scope_map_md_path,
    )

    company, departments = _company_or_404(slug)
    summary = read_company_summary(company)
    depts = list_dept_summaries(company, departments)
    dept_names = [d["name"] for d in depts]
    states = list_all_states(company.company_dir, dept_names)
    coord_state = load_state(company.company_dir)

    # Per-dept readiness for the template.
    rows = []
    for d, s in zip(depts, states):
        rows.append({
            "dept": d,
            "state": s,
            "dept_label": d.get("display_name") or d["name"],
            "ready": department_ready_for_coordination(s),
        })
    all_ready = rows and all(r["ready"] for r in rows)

    # Preview of the drafted scope map.
    scope_map_body = ""
    if coord_state.scope_map_path:
        p = (company.company_dir / coord_state.scope_map_path).resolve()
        try:
            p.relative_to(company.company_dir.resolve())
        except ValueError:
            p = None
        if p and p.exists():
            scope_map_body = p.read_text(encoding="utf-8")

    return render_template(
        "coordination.html",
        slug=slug,
        company=summary,
        rows=rows,
        all_ready=bool(all_ready),
        coord=coord_state,
        scope_map_body=scope_map_body,
    )


def _gather_coordination_inputs(company, depts, states):
    """Assemble the per-dept context blocks that feed the coordination
    prompt. Pulls skill-scope, domain-brief, and founder-brief content
    for each active department. Missing files become placeholders so
    the prompt is never malformed."""
    from core.dept_onboarding import (
        skill_scope_path, domain_brief_path, founder_brief_path,
    )

    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except Exception:
            return ""

    blocks: list[str] = []
    for d, s in zip(depts, states):
        dept = d["name"]
        label = d.get("display_name") or dept
        ss = _read(skill_scope_path(company.company_dir, dept))
        db = _read(domain_brief_path(company.company_dir, dept))
        fb = _read(founder_brief_path(company.company_dir, dept))
        blocks.append(
            f"### {label} (slug: `{dept}`)\n\n"
            f"#### Manager skill-scope.md\n\n{ss or '(missing)'}\n\n"
            f"#### Domain brief\n\n{db or '(missing)'}\n\n"
            f"#### Founder brief\n\n{fb or '(missing)'}\n\n"
        )
    return "\n".join(blocks)


@app.route("/c/<slug>/coordination/run", methods=["POST"])
@retrolog_dispatch("coordination_run")
def coordination_run(slug: str):
    """Dispatch the coordination job. Requires every active department
    to have completed its founder interview."""
    import datetime
    from dataclasses import replace
    from core.dept_onboarding import list_all_states
    from core.scope_coordination import (
        load_state, persist_state, all_departments_ready,
        render_coordination_prompt, scope_map_md_path, scope_map_json_path,
        render_scope_of_work_md, scope_of_work_path, coordination_dir,
        STATUS_RUNNING, STATUS_AWAITING_SIGNOFF, STATUS_READY,
    )
    from core.llm_client import single_turn

    company, departments = _company_or_404(slug)
    depts = list_dept_summaries(company, departments)
    dept_names = [d["name"] for d in depts]
    states = list_all_states(company.company_dir, dept_names)
    if not all_departments_ready(states):
        abort(400, "Coordination cannot start: not every department has completed its founder interview.")

    coord = load_state(company.company_dir)
    if coord.status == STATUS_RUNNING:
        # Idempotent: don't double-dispatch.
        return redirect(url_for("coordination_page", slug=slug))

    coordination_dir(company.company_dir).mkdir(parents=True, exist_ok=True)
    # Load shared company-level context.
    orchestrator_charter = ""
    try:
        charter_p = company.company_dir / "orchestrator-charter.md"
        if charter_p.exists():
            orchestrator_charter = charter_p.read_text(encoding="utf-8")
    except Exception:
        pass
    company_priorities = ""
    try:
        pri_p = company.company_dir / "priorities.md"
        if pri_p.exists():
            company_priorities = pri_p.read_text(encoding="utf-8")
    except Exception:
        pass

    dept_context_blocks = _gather_coordination_inputs(company, depts, states)
    prompt = render_coordination_prompt(
        company_name=company.name,
        industry=company.industry or "",
        orchestrator_charter=orchestrator_charter,
        company_priorities=company_priorities,
        dept_context_blocks=dept_context_blocks,
    )

    now_iso = datetime.datetime.utcnow().isoformat() + "Z"
    persist_state(company.company_dir, replace(
        coord,
        status=STATUS_RUNNING,
        started_at=now_iso,
        error="",
    ))

    def _target(j):
        resp = single_turn(
            messages=[{"role": "user", "content": prompt}],
            model="claude-sonnet-4-6",
            cost_tag="coordination:scope-map",
            system=(
                "You are the company-wide scope coordinator. "
                "Return only the JSON object specified, no prose."
            ),
            max_tokens=4500,
        )
        if resp.error:
            persist_state(company.company_dir, replace(
                load_state(company.company_dir),
                status="rejected",
                error=resp.error,
                completed_at=datetime.datetime.utcnow().isoformat() + "Z",
            ))
            return {"ok": False, "error": resp.error}

        try:
            parsed = _extract_first_json_object(resp.text or "")
        except Exception as exc:
            persist_state(company.company_dir, replace(
                load_state(company.company_dir),
                status="rejected",
                error=f"parse: {exc}",
                completed_at=datetime.datetime.utcnow().isoformat() + "Z",
            ))
            return {"ok": False, "error": f"parse: {exc}"}

        # Persist the raw structured JSON.
        json_p = scope_map_json_path(company.company_dir)
        json_p.write_text(json.dumps(parsed, indent=2), encoding="utf-8")

        # Render a human-readable scope map markdown.
        md_lines: list[str] = []
        md_lines.append(f"# Scope map for {company.name}")
        md_lines.append("")
        md_lines.append("_Drafted by the coordination round. Awaiting founder ratification. "
                        "Approving will write one `scope-of-work.md` per department._")
        md_lines.append("")
        cov = (parsed.get("coverage_summary") or "").strip()
        if cov:
            md_lines.append("## Coverage summary")
            md_lines.append("")
            md_lines.append(cov)
            md_lines.append("")
        gaps = parsed.get("gaps") or []
        if gaps:
            md_lines.append("## Gaps flagged")
            md_lines.append("")
            for g in gaps:
                md_lines.append(
                    f"- **{g.get('territory', '(unnamed)')}** "
                    f"(proposed owner: `{g.get('proposed_owner', '?')}`): {g.get('reason', '')}"
                )
            md_lines.append("")
        over = parsed.get("overlaps_resolved") or []
        if over:
            md_lines.append("## Overlaps resolved")
            md_lines.append("")
            for o in over:
                others = ", ".join(o.get("other_depts", []) or [])
                md_lines.append(
                    f"- **{o.get('territory', '(unnamed)')}** -> owner `{o.get('owning_dept', '?')}`, "
                    f"others touching ({others}): {o.get('resolution', '')}"
                )
            md_lines.append("")
        handoffs = parsed.get("handoffs") or []
        if handoffs:
            md_lines.append("## Handoffs")
            md_lines.append("")
            for h in handoffs:
                md_lines.append(
                    f"- `{h.get('from_dept', '?')}` -> `{h.get('to_dept', '?')}` "
                    f"when {h.get('trigger', '?')}: {h.get('artifact', '')}"
                )
            md_lines.append("")
        md_lines.append("## Per-department scope")
        md_lines.append("")
        for block in parsed.get("departments", []) or []:
            dept_slug = block.get("dept", "?")
            md_lines.append(f"### `{dept_slug}`")
            md_lines.append("")
            owns = block.get("owns", []) or []
            if owns:
                md_lines.append("**Owns:**")
                for o in owns:
                    md_lines.append(f"- {o}")
                md_lines.append("")
            not_owns = block.get("does_not_own", []) or []
            if not_owns:
                md_lines.append("**Does not own:**")
                for n in not_owns:
                    md_lines.append(f"- {n}")
                md_lines.append("")
            summary = (block.get("summary") or "").strip()
            if summary:
                md_lines.append(summary)
                md_lines.append("")

        md_body = "\n".join(md_lines) + "\n"
        md_p = scope_map_md_path(company.company_dir)
        md_p.write_text(md_body, encoding="utf-8")

        persist_state(company.company_dir, replace(
            load_state(company.company_dir),
            status=STATUS_AWAITING_SIGNOFF,
            completed_at=datetime.datetime.utcnow().isoformat() + "Z",
            scope_map_path=md_p.relative_to(company.company_dir).as_posix(),
            scope_map_json_path=json_p.relative_to(company.company_dir).as_posix(),
            job_id=j.id,
            error="",
        ))
        return {"ok": True}

    job = JOB_REGISTRY.submit(
        kind="coordination-draft",
        label="Scope coordination",
        company_dir=str(company.company_dir),
        target=_target,
    )
    persist_state(company.company_dir, replace(
        load_state(company.company_dir),
        job_id=job.id,
    ))
    return redirect(url_for("coordination_page", slug=slug))


@app.route("/c/<slug>/coordination/approve", methods=["POST"])
@retrolog_dispatch("coordination_approve")
def coordination_approve(slug: str):
    """Ratify the scope map: write per-dept scope-of-work.md files and
    mark coordination approved. Unblocks charter for every department."""
    import datetime
    from dataclasses import replace
    from core.scope_coordination import (
        load_state, persist_state, scope_map_json_path, scope_of_work_path,
        render_scope_of_work_md, STATUS_AWAITING_SIGNOFF, STATUS_APPROVED,
    )
    company, _ = _company_or_404(slug)
    coord = load_state(company.company_dir)
    if coord.status != STATUS_AWAITING_SIGNOFF:
        abort(400, f"Coordination is not awaiting signoff (status={coord.status!r}).")
    json_p = scope_map_json_path(company.company_dir)
    if not json_p.exists():
        abort(400, "Structured scope-map JSON is missing; re-run coordination.")
    parsed = json.loads(json_p.read_text(encoding="utf-8"))
    coverage = (parsed.get("coverage_summary") or "").strip()
    written: list[str] = []
    for block in parsed.get("departments", []) or []:
        dept = (block.get("dept") or "").strip()
        if not dept:
            continue
        body = render_scope_of_work_md(block, coverage)
        p = scope_of_work_path(company.company_dir, dept)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        written.append(p.relative_to(company.company_dir).as_posix())
    notes = request.form.get("notes", "").strip()
    persist_state(company.company_dir, replace(
        coord,
        status=STATUS_APPROVED,
        approved_at=datetime.datetime.utcnow().isoformat() + "Z",
        notes=notes or coord.notes,
    ))
    return redirect(url_for("coordination_page", slug=slug))


@app.route("/c/<slug>/coordination/reject", methods=["POST"])
@retrolog_dispatch("coordination_reject")
def coordination_reject(slug: str):
    """Reject the current draft and put the coordination state back to
    ready so the founder can kick off a fresh run."""
    from dataclasses import replace
    from core.scope_coordination import (
        load_state, persist_state, STATUS_READY,
    )
    company, _ = _company_or_404(slug)
    coord = load_state(company.company_dir)
    notes = request.form.get("notes", "").strip()
    persist_state(company.company_dir, replace(
        coord,
        status=STATUS_READY,
        notes=notes or coord.notes,
    ))
    return redirect(url_for("coordination_page", slug=slug))


# ---------------------------------------------------------------------------
# Scenario ledger — list + rate (Phase 14)
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/ledger")
def ledger_page(slug: str):
    """Browse every scenario run with its rating status. Primary
    empirical-data surface — this is what the newsletter pipeline
    reads from."""
    from core.scenario_ledger import iter_runs_reverse, rating_summary, load_runs

    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    runs = list(iter_runs_reverse(company.company_dir))
    stats = rating_summary(load_runs(company.company_dir))
    return render_template(
        "ledger.html",
        slug=slug,
        company=summary,
        runs=runs,
        stats=stats,
    )


@app.route("/c/<slug>/ledger/export.md")
def ledger_export_md(slug: str):
    """Markdown digest for the newsletter pipeline. Agents or editors
    can consume this endpoint directly to produce newsletter content."""
    from core.scenario_ledger import load_runs, render_newsletter_digest
    from flask import Response

    company, _ = _company_or_404(slug)
    only_rated = request.args.get("include_unrated") != "1"
    runs = load_runs(company.company_dir)
    md = render_newsletter_digest(runs, only_rated=only_rated)
    return Response(md, mimetype="text/markdown; charset=utf-8")


@app.route("/c/<slug>/ledger/export.json")
def ledger_export_json(slug: str):
    """Machine-readable export of the ledger. Consumed by the newsletter
    agent + any future analytics."""
    from core.scenario_ledger import load_runs, rating_summary
    from dataclasses import asdict

    company, _ = _company_or_404(slug)
    runs = load_runs(company.company_dir)
    return jsonify({
        "company": read_company_summary(company)["name"],
        "runs": [asdict(r) for r in runs],
        "summary": rating_summary(runs),
    })


@app.route("/c/<slug>/ledger/<run_id>/translate", methods=["POST"])
def ledger_translate(slug: str, run_id: str):
    """Run the operator-translation pass (Haiku) over a scenario and
    persist the plain-English summary + action items + flags.

    Launches as a background job so the UI stays responsive. If the
    run has no full_output yet (job still running, or pre-Phase-14
    run), we queue the translate call anyway — it will use the
    outcome_summary preview as the source text."""
    from core.scenario_ledger import translate_run

    company, _ = _company_or_404(slug)

    def _target(j):
        j.log.append(f"Translating run {run_id}...")
        updated = translate_run(company.company_dir, run_id)
        if updated is None:
            j.log.append("no run found")
            return {"ok": False, "reason": "not found"}
        j.log.append("translation persisted")
        return {
            "ok": True,
            "run_id": run_id,
            "summary": updated.plain_summary,
            "action_items": list(updated.action_items),
            "flags": list(updated.flags),
        }

    job = JOB_REGISTRY.submit(
        kind="translate",
        label=f"translate {run_id[:8]}",
        company_dir=str(company.company_dir),
        target=_target,
    )
    return redirect(url_for("job_detail", slug=slug, job_id=job.id))


@app.route("/c/<slug>/ledger/translate-all", methods=["POST"])
def ledger_translate_all(slug: str):
    """Back-fill translations for every untranslated run in the ledger.
    Fires one translate job per run — they queue against the standard
    JobRegistry concurrency cap."""
    from core.scenario_ledger import load_runs, translate_run

    company, _ = _company_or_404(slug)
    runs = load_runs(company.company_dir)
    pending = [r for r in runs if not r.plain_summary and (r.full_output or r.outcome_summary)]
    if not pending:
        return redirect(url_for("ledger_page", slug=slug))

    def _make_target(rid):
        def _target(j):
            j.log.append(f"translating {rid}")
            updated = translate_run(company.company_dir, rid)
            if updated is None:
                j.log.append("not found")
                return {"ok": False}
            return {"ok": True, "summary": updated.plain_summary[:200]}
        return _target

    submitted = []
    for r in pending:
        job = JOB_REGISTRY.submit(
            kind="translate",
            label=f"translate {r.scenario_name[:30]}",
            company_dir=str(company.company_dir),
            target=_make_target(r.id),
        )
        submitted.append(job.id)

    return redirect(url_for("jobs_page", slug=slug))


@app.route("/c/<slug>/ledger/<run_id>/rate", methods=["POST"])
def ledger_rate(slug: str, run_id: str):
    from core.scenario_ledger import rate_run

    company, _ = _company_or_404(slug)
    raw_rating = request.form.get("rating", "").strip()
    notes = request.form.get("notes", "").strip()
    try:
        rating = int(raw_rating)
    except ValueError:
        abort(400, "rating must be an int in [-2, 2]")
    if rating < -2 or rating > 2:
        abort(400, "rating must be in [-2, 2]")
    if rate_run(company.company_dir, run_id, rating=rating, notes=notes) is None:
        abort(404, "scenario run not found")
    return redirect(url_for("ledger_page", slug=slug))


# ---------------------------------------------------------------------------
# Founder-authored awareness note (Phase 14)
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/awareness/new", methods=["POST"])
def awareness_new(slug: str):
    """Founder writes an awareness note directly from the UI. Same
    validation pipeline as agent-written notes (quality gate + evidence
    verification) — the founder is subject to the same rails."""
    from core.primitives.awareness import build_note, write_note

    company, _ = _company_or_404(slug)
    subject = request.form.get("subject", "").strip()
    observation = request.form.get("observation", "").strip()
    evidence_raw = request.form.get("evidence_refs", "").strip()
    observer = request.form.get("observer", "founder").strip() or "founder"
    if not subject or not observation or not evidence_raw:
        abort(400, "subject, observation, and evidence_refs required")
    evidence_refs = tuple(
        r.strip() for r in evidence_raw.splitlines() if r.strip()
    )
    note = build_note(
        observer=observer,
        subject=subject,
        observation=observation,
        evidence_refs=evidence_refs,
    )
    try:
        write_note(note, company.company_dir)
    except ValueError as exc:
        abort(400, f"awareness note rejected: {exc}")
    return redirect(url_for("awareness_page", slug=slug))


# ---------------------------------------------------------------------------
# Cost dashboard (chunk 1a.9)
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/costs")
def costs_page(slug: str):
    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    costs = cost_log_reader(company)
    return render_template(
        "costs.html",
        slug=slug,
        company=summary,
        costs=costs,
    )


# ---------------------------------------------------------------------------
# Generic markdown viewer (sandboxed)
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/view")
def view_artifact(slug: str):
    company, _ = _company_or_404(slug)
    rel = request.args.get("path", "").strip()
    if not rel:
        abort(400, "missing ?path=...")
    artifact = read_artifact_safe(company, rel)
    if artifact is None:
        abort(404, "artifact not found or outside sandbox")
    summary = read_company_summary(company)
    back = _safe_back_url(
        request.args.get("back", ""),
        slug,
    )
    return render_template(
        "artifact_view.html",
        slug=slug,
        company=summary,
        artifact=artifact,
        page_title=artifact["name"],
        back_url=back,
        back_label="← Back",
    )


# ---------------------------------------------------------------------------
# Run actions (background jobs)
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/run", methods=["GET"])
def run_page(slug: str):
    company, departments = _company_or_404(slug)
    summary = read_company_summary(company)
    depts = list_dept_summaries(company, departments)
    return render_template(
        "run.html",
        slug=slug,
        company=summary,
        departments=depts,
    )


@app.route("/c/<slug>/run/dispatch", methods=["POST"])
def run_dispatch(slug: str):
    company, _ = _company_or_404(slug)
    dept_name = request.form.get("dept", "").strip()
    brief = request.form.get("brief", "").strip()
    scenario_name = request.form.get("scenario_name", "").strip()
    if not dept_name or not brief:
        abort(400, "dept and brief required")

    # Phase 14 — scenario ledger capture. Every /run/dispatch is a
    # scenario unless the caller explicitly flags otherwise. Ledger
    # creation is best-effort; a failure here must not block dispatch.
    from core.scenario_ledger import start_run, persist_run, complete_run

    try:
        run = start_run(
            dept=dept_name,
            scenario_name=scenario_name or f"adhoc--{dept_name}",
            brief=brief,
            job_id="",  # filled in after submit
        )
    except Exception:  # pragma: no cover
        run = None

    def _target(j):
        result = run_dispatch_action(j, str(company.company_dir), dept_name, brief)
        # On completion, flag the ledger entry with outcome summary
        # AND the full synthesis (for the translation layer + newsletter).
        if run is not None:
            try:
                final = (result or {}).get("final_text") or ""
                complete_run(
                    company.company_dir,
                    run.id,
                    outcome_summary=final[:800],
                    full_output=final,
                )
            except Exception:  # pragma: no cover
                pass
        return result

    job = JOB_REGISTRY.submit(
        kind="dispatch",
        label=(f"{scenario_name or 'dispatch'} → {dept_name}")[:80],
        company_dir=str(company.company_dir),
        target=_target,
    )

    if run is not None:
        # Re-persist with the real job_id so the ledger links to the job.
        from dataclasses import replace
        try:
            run_with_job = replace(run, job_id=job.id)
            persist_run(company.company_dir, run_with_job)
        except Exception:  # pragma: no cover
            pass
    return redirect(url_for("job_detail", slug=slug, job_id=job.id))


@app.route("/c/<slug>/run/board", methods=["POST"])
def run_board(slug: str):
    company, _ = _company_or_404(slug)
    topic = request.form.get("topic", "").strip()
    include_dossier = request.form.get("include_dossier") == "on"
    if not topic:
        abort(400, "topic required")
    label = f"Board: {topic[:40]}{'...' if len(topic) > 40 else ''}"
    job = JOB_REGISTRY.submit(
        kind="board",
        label=label,
        company_dir=str(company.company_dir),
        target=lambda j: run_board_action(j, str(company.company_dir), topic, include_dossier),
    )
    return redirect(url_for("job_detail", slug=slug, job_id=job.id))


@app.route("/c/<slug>/run/full-demo", methods=["POST"])
def run_full_demo(slug: str):
    company, _ = _company_or_404(slug)
    only_raw = request.form.get("only_depts", "").strip()
    only = [d.strip() for d in only_raw.split(",") if d.strip()] if only_raw else None
    force = request.form.get("force") == "on"
    skip_synthesis = request.form.get("skip_synthesis") == "on"
    skip_board = request.form.get("skip_board") == "on"
    label = "Full demo run"
    if only:
        label += f" ({','.join(only)})"
    job = JOB_REGISTRY.submit(
        kind="full_demo",
        label=label,
        company_dir=str(company.company_dir),
        target=lambda j: run_full_demo_action(j, str(company.company_dir), only, force, skip_synthesis, skip_board),
    )
    return redirect(url_for("job_detail", slug=slug, job_id=job.id))


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/jobs")
def jobs_page(slug: str):
    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    jobs = JOB_REGISTRY.list_jobs(str(company.company_dir))
    return render_template("jobs.html", slug=slug, company=summary, jobs=[j.to_dict() for j in jobs])


@app.route("/c/<slug>/jobs/<job_id>")
def job_detail(slug: str, job_id: str):
    company, _ = _company_or_404(slug)
    summary = read_company_summary(company)
    job = JOB_REGISTRY.get(job_id)
    if job is None:
        abort(404, "job not found")
    return render_template("job_detail.html", slug=slug, company=summary, job=job.to_dict())


@app.route("/api/jobs/<job_id>")
def api_job(job_id: str):
    job = JOB_REGISTRY.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(job.to_dict())


@app.route("/api/c/<slug>/threads")
def api_threads(slug: str):
    """Lightweight thread list for the inbox drawer."""
    from core.conversation import list_threads

    company, _ = _company_or_404(slug)
    threads = list_threads(company.company_dir)
    result = []
    for t in threads:
        last_msg = next(
            (m for m in reversed(t.messages) if m.role != "system"), None
        )
        result.append({
            "id": t.id,
            "title": t.title,
            "target_agent": t.target_agent,
            "purpose": t.purpose,
            "is_open": t.is_open,
            "turn_count": t.turn_count,
            "created_at": t.created_at[:19].replace("T", " ") if t.created_at else "",
            "preview": (last_msg.content[:90] + "…" if last_msg and len(last_msg.content) > 90 else (last_msg.content if last_msg else "")),
            "last_role": last_msg.role if last_msg else "",
        })
    return jsonify(result)


@app.route("/c/<slug>/inbox/quick-send", methods=["POST"])
def inbox_quick_send(slug: str):
    """Send a quick message — appends to the most recent open orchestrator
    thread, or creates one. Redirects to the thread detail page."""
    from core.conversation import (
        list_threads, start_thread, persist_thread,
        load_thread, append_message,
        _system_prompt_for_thread, _messages_for_llm,
    )

    company, _ = _company_or_404(slug)
    content = request.form.get("content", "").strip()
    if not content:
        abort(400, "content required")

    threads = list_threads(company.company_dir)
    orch = next(
        (t for t in threads if t.target_agent == "orchestrator" and t.is_open),
        None,
    )
    if orch is None:
        orch = start_thread(
            target_agent="orchestrator",
            purpose="chat",
            title="Chat with Orchestrator",
        )
        persist_thread(company.company_dir, orch)

    append_message(company.company_dir, orch.id, role="user", content=content)

    thread_id = orch.id

    def _target(j):
        from core.llm_client import single_turn

        t = load_thread(company.company_dir, thread_id)
        if t is None:
            return {"ok": False}
        sys_p = _system_prompt_for_thread(t, company.company_dir)
        msgs = _messages_for_llm(t)
        response = single_turn(
            messages=msgs,
            model="claude-haiku-4-5-20251001",
            cost_tag="chat:inbox:orchestrator",
            system=sys_p,
            max_tokens=1400,
        )
        reply = (response.text or "").strip() or "(no response)"
        append_message(
            company.company_dir, thread_id,
            role="assistant", content=reply,
            job_id=j.id, token_usage=response.usage or {},
        )
        return {"ok": True}

    JOB_REGISTRY.submit(
        kind="chat",
        label=f"inbox → orchestrator · {thread_id[:8]}",
        company_dir=str(company.company_dir),
        target=_target,
    )
    return redirect(url_for("chat_detail", slug=slug, thread_id=thread_id))


# ---------------------------------------------------------------------------
# Knowledge & Brand — inventory + file upload
# ---------------------------------------------------------------------------
@app.route("/c/<slug>/knowledge")
def knowledge_page(slug: str):
    from core.brand_db.store import iter_voice_entries, iter_image_entries
    import datetime

    company, departments = _company_or_404(slug)
    summary = read_company_summary(company)
    depts = list_dept_summaries(company, departments)

    voice_entries = list(iter_voice_entries(company.company_dir))
    image_entries = list(iter_image_entries(company.company_dir))

    # Collect KB source docs from every department's knowledge-base/source/ folder
    kb_docs = []
    for d in departments:
        source_dir = company.company_dir / d.name / "knowledge-base" / "source"
        if not source_dir.exists():
            continue
        for p in sorted(source_dir.iterdir()):
            if p.suffix.lower() not in {".md", ".txt"} or not p.is_file():
                continue
            stat = p.stat()
            kb_docs.append({
                "name": p.name,
                "dept": d.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })

    flash_ok = request.args.get("ok", "")
    flash_err = request.args.get("err", "")
    return render_template(
        "knowledge.html",
        slug=slug,
        company=summary,
        departments=depts,
        voice_entries=voice_entries,
        image_entries=image_entries,
        kb_docs=kb_docs,
        flash_ok=flash_ok,
        flash_err=flash_err,
    )


@app.route("/c/<slug>/knowledge/brand-image/<path:filename>")
def knowledge_serve_image(slug: str, filename: str):
    """Serve a brand image file directly — sandboxed to brand-db/images/."""
    import mimetypes
    from flask import Response

    company, _ = _company_or_404(slug)
    images_dir = (company.company_dir / "brand-db" / "images").resolve()
    target = (images_dir / filename).resolve()
    # Sandbox check: must stay inside brand-db/images/
    try:
        target.relative_to(images_dir)
    except ValueError:
        abort(400, "path outside sandbox")
    if not target.is_file():
        abort(404, "image not found")
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return Response(target.read_bytes(), mimetype=mime)


@app.route("/c/<slug>/knowledge/upload/brand-voice", methods=["POST"])
def knowledge_upload_voice(slug: str):
    """Save an uploaded .md file as a brand voice entry."""
    import datetime, re as _re

    company, _ = _company_or_404(slug)
    f = request.files.get("file")
    if not f or not f.filename:
        return redirect(url_for("knowledge_page", slug=slug, err="No file selected."))
    if not f.filename.lower().endswith((".md", ".txt")):
        return redirect(url_for("knowledge_page", slug=slug, err="Only .md and .txt files are accepted."))

    verdict = request.form.get("verdict", "").strip()
    if verdict not in {"gold", "acceptable", "reference", "anti-exemplar"}:
        return redirect(url_for("knowledge_page", slug=slug, err="Invalid verdict."))
    tags_raw = request.form.get("tags", "").strip()
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    description = request.form.get("description", "").strip()

    # Sanitise filename
    safe_name = _re.sub(r"[^\w.\-]", "_", Path(f.filename).name)
    if not safe_name.endswith(".md"):
        safe_name = Path(safe_name).stem + ".md"

    dest_dir = company.company_dir / "brand-db" / "voice"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name

    now = datetime.datetime.utcnow().isoformat() + "+00:00"
    tags_yaml = "\n- ".join([""] + tags) if tags else "[]"
    body = f.read().decode("utf-8", errors="replace")
    frontmatter = (
        f"---\n"
        f"added_at: {now}\n"
        f"verdict: {verdict}\n"
        f"tags:{tags_yaml if tags else ' []'}\n"
        f"description: \"{description.replace(chr(34), chr(39))}\"\n"
        f"---\n\n"
    )
    dest.write_text(frontmatter + body, encoding="utf-8")
    return redirect(url_for("knowledge_page", slug=slug, ok=f"Brand voice doc '{safe_name}' uploaded."))


@app.route("/c/<slug>/knowledge/upload/brand-image", methods=["POST"])
def knowledge_upload_image(slug: str):
    """Save an uploaded image + write its sidecar YAML."""
    import datetime, re as _re

    company, _ = _company_or_404(slug)
    f = request.files.get("file")
    if not f or not f.filename:
        return redirect(url_for("knowledge_page", slug=slug, err="No file selected."))

    allowed_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif"}
    ext = Path(f.filename).suffix.lower()
    if ext not in allowed_exts:
        return redirect(url_for("knowledge_page", slug=slug, err=f"File type '{ext}' not allowed. Use jpg/png/gif/webp/svg."))

    verdict = request.form.get("verdict", "").strip()
    if verdict not in {"gold", "acceptable", "reference", "anti-exemplar"}:
        return redirect(url_for("knowledge_page", slug=slug, err="Invalid verdict."))
    tags_raw = request.form.get("tags", "").strip()
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    description = request.form.get("description", "").strip()

    safe_name = _re.sub(r"[^\w.\-]", "_", Path(f.filename).name)
    dest_dir = company.company_dir / "brand-db" / "images"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name

    f.save(str(dest))

    now = datetime.datetime.utcnow().isoformat() + "+00:00"
    tags_list = "\n  - ".join([""] + tags) if tags else " []"
    sidecar_content = (
        f"added_at: {now}\n"
        f"verdict: {verdict}\n"
        f"tags:{tags_list if tags else ' []'}\n"
        f"description: \"{description.replace(chr(34), chr(39))}\"\n"
    )
    (dest_dir / (safe_name + ".yaml")).write_text(sidecar_content, encoding="utf-8")
    return redirect(url_for("knowledge_page", slug=slug, ok=f"Brand image '{safe_name}' uploaded."))


@app.route("/c/<slug>/knowledge/upload/kb-doc", methods=["POST"])
def knowledge_upload_kb(slug: str):
    """Save an uploaded .md/.txt file to a department KB source folder and re-ingest."""
    import re as _re

    company, departments = _company_or_404(slug)
    f = request.files.get("file")
    if not f or not f.filename:
        return redirect(url_for("knowledge_page", slug=slug, err="No file selected."))
    if not f.filename.lower().endswith((".md", ".txt")):
        return redirect(url_for("knowledge_page", slug=slug, err="Only .md and .txt files are accepted for KB docs."))

    dept_name = request.form.get("dept", "").strip()
    dept_names = {d.name for d in departments}
    if dept_name not in dept_names:
        return redirect(url_for("knowledge_page", slug=slug, err=f"Unknown department '{dept_name}'."))

    safe_name = _re.sub(r"[^\w.\-]", "_", Path(f.filename).name)
    source_dir = company.company_dir / dept_name / "knowledge-base" / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    dest = source_dir / safe_name
    dest.write_bytes(f.read())

    # Re-ingest the department KB so the new doc is immediately retrievable
    try:
        from core.kb.ingest import ingest_all
        ingest_all(company.company_dir / dept_name)
        ingest_note = " Ingestion complete."
    except Exception as exc:
        ingest_note = f" (Ingest warning: {exc})"

    return redirect(url_for("knowledge_page", slug=slug, ok=f"KB doc '{safe_name}' added to {dept_name}.{ingest_note}"))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    """Simple liveness probe — does NOT exercise company routes. Use
    for load-balancer / is-the-port-open checks only."""
    return {"ok": True, "companies": len(discover_companies())}


@app.route("/healthz/deep")
def healthz_deep():
    """Deep healthcheck — exercises company config loading, dept
    enumeration, state-store reads, and template rendering. Returns
    200 with JSON status if everything works end-to-end; 503 with
    error details otherwise.

    A watchdog polling THIS endpoint would have caught the 22-hour
    state rot that /healthz missed. Every subsystem that must work
    for the UI to be usable gets touched here.
    """
    import time as _t
    t0 = _t.time()
    checks: list[dict] = []
    overall_ok = True

    def _check(name, fn):
        nonlocal overall_ok
        started = _t.time()
        try:
            result = fn()
            elapsed_ms = int((_t.time() - started) * 1000)
            checks.append({"check": name, "ok": True, "elapsed_ms": elapsed_ms, "result": result})
        except Exception as exc:
            overall_ok = False
            elapsed_ms = int((_t.time() - started) * 1000)
            checks.append({
                "check": name, "ok": False, "elapsed_ms": elapsed_ms,
                "error": f"{type(exc).__name__}: {exc}",
            })

    # Discovery
    _check("discover_companies", lambda: len(discover_companies()))

    # For each company, load config + depts + render company summary
    companies = discover_companies()
    for c in companies[:3]:  # cap at 3 for speed
        company_dir = c.get("path") or c.get("company_dir")
        if not company_dir:
            continue
        slug = Path(company_dir).name

        def _load_and_summarize(company_dir=company_dir):
            comp = load_company_safe(str(company_dir))
            if comp is None:
                raise RuntimeError("load_company_safe returned None")
            depts = load_departments_safe(comp)
            summary = read_company_summary(comp)
            return {"depts": len(depts), "name": summary.get("name", "")}

        _check(f"load_company:{slug}", _load_and_summarize)

    # Job registry responsive
    _check("job_registry", lambda: {"jobs": len(JOB_REGISTRY.list_jobs(company_dir="") if hasattr(JOB_REGISTRY, "list_jobs") else [])})

    total_ms = int((_t.time() - t0) * 1000)
    payload = {
        "ok": overall_ok,
        "total_ms": total_ms,
        "checks": checks,
    }
    return jsonify(payload), (200 if overall_ok else 503)


# ---------------------------------------------------------------------------
# Custom error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(err):
    return render_template("error.html", code=404, message=str(err)), 404


@app.errorhandler(400)
def bad_request(err):
    return render_template("error.html", code=400, message=str(err)), 400


@app.errorhandler(500)
def internal_error(err):
    # Log the full traceback to disk before the generic 500 page renders.
    # The previous 22-hour outage gave us no trace of WHY company-scoped
    # routes started failing because stderr was lost. This writes
    # tracebacks to <vault>/webapp-errors.log via the rotating handler
    # configured in main().
    import traceback as _tb
    app.logger.error(
        "500 on %s:\n%s",
        request.path if request else "<no request>",
        _tb.format_exc(),
    )
    return render_template("error.html", code=500, message=str(err)), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _configure_logging() -> None:
    """Rotating file logger — writes tracebacks and access logs to
    `webapp-errors.log` next to the company-os directory. Rotates at
    5 MB, keeps 5 backups. Writes to stderr too so `python app.py` still
    shows output in the terminal.
    """
    import logging
    from logging.handlers import RotatingFileHandler

    log_dir = _PROJECT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "webapp.log"

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        str(log_path), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    # Wire into Flask's logger + the werkzeug access logger
    for name in ("", "werkzeug", "waitress"):
        logger = logging.getLogger(name)
        logger.addHandler(file_handler)
        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)
    app.logger.setLevel(logging.INFO)
    app.logger.info("webapp logger configured → %s", log_path)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Company OS — Web GUI")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--prod", action="store_true",
        help="Run under Waitress (production WSGI) instead of Flask's dev server. "
             "Required for long-running deployments — Flask's dev server is known "
             "to leak state after ~24h on Windows.",
    )
    parser.add_argument(
        "--threads", type=int, default=8,
        help="Waitress thread pool size (prod mode only). Default 8.",
    )
    args = parser.parse_args()

    _configure_logging()

    from core.env import MissingRequiredEnv, validate_runtime_environment
    try:
        validate_runtime_environment()
    except MissingRequiredEnv as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    print("=" * 60)
    print(f"  Company OS — Web GUI")
    print(f"  http://{args.host}:{args.port}")
    print(f"  Mode: {'production (Waitress)' if args.prod else 'development (Flask)'}")
    print(f"  Logs: {_PROJECT_DIR / 'logs' / 'webapp.log'}")
    print("=" * 60)

    if args.prod:
        try:
            from waitress import serve
        except ImportError:
            print("ERROR: waitress is not installed. `pip install waitress`.")
            sys.exit(2)
        serve(
            app,
            host=args.host,
            port=args.port,
            threads=args.threads,
            channel_timeout=120,    # drop stuck connections after 2 min
            cleanup_interval=30,    # sweep closed connections every 30s
            ident="company-os-webapp",
        )
    else:
        app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
