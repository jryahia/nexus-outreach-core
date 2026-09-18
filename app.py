"""NEXUS: Outreach Core - local B2B lead hunting and cold outreach.

Run with:  streamlit run app.py

Design rule for this file: one screen, one decision, one big button.
Anything that blocks runs on a worker thread (see ui/state.py) so the UI never
freezes and STOP is always clickable. All state lives in SQLite (core/vault.py),
so closing the app or losing power costs nothing.
"""

from __future__ import annotations

import io
import time
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_agraph import Config, Edge, Node, agraph

from core import (
    cannon, diagnostics, geo, hunter, network, purifier, templates, vault,
)
from core.config import ENV_PATH, load_config
from ui.state import DONE, ERROR, RUNNING, STOPPED, get_job, reset_job, start_job, stop_job
from ui.theme import (
    ACCENT,
    DANGER,
    MUTED,
    PLOT_SEQUENCE,
    SUCCESS,
    WARNING,
    hud_runtime,
    status_badge,
    system_state,
    zone_state,
    apply_theme,
    check_row,
    header,
    log_panel,
    section,
    wait_banner,
)

HUNT = "hunt"
CAMPAIGN = "campaign"
DIAGNOSTIC = "diagnostic"

st.set_page_config(
    page_title="NEXUS: Outreach Core",
    page_icon=":material/radar:",
    layout="wide",
    initial_sidebar_state="collapsed",
)
apply_theme()
# The runtime half of the theme: the node-network canvas, the cursor
# spotlight and the targeting rings. Mounted once, next to the stylesheet
# it belongs to, and purely decorative - see ui/theme.hud_runtime.
hud_runtime()

cfg = load_config()
vault.init_db()          # idempotent: creates the schema and folds in legacy CSVs
templates.ensure_defaults()

PLOT_LAYOUT = dict(
    template="plotly_dark",
    # Without an explicit height Plotly grows to fill the column and pushes the
    # table below the fold.
    height=300,
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color="#C6CEDC", size=12),
    margin=dict(t=34, b=10, l=10, r=10),
    # No transition. The stable chart key is what stops Streamlit re-mounting
    # the element on every fragment tick, and that is what kills the flicker.
    # A tween on top of a two-second poll is worse than useless: Plotly
    # re-animates each update, and a bar whose value just changed can stay
    # stuck at an intermediate width indefinitely. Measured, not assumed.
    transition=dict(duration=0),
)

# responsive: the charts first mount inside a Streamlit tab that is not the
# visible one, so Plotly lays them out at zero width. Without this the bars
# keep the geometry they were given while hidden - measured, not assumed.
PLOT_CONFIG = {"displayModeBar": False, "responsive": True}

# Amber, not cyan, for Preview: cyan is the system colour of the whole
# surface now, so a cyan slice beside a green Sent slice reads as chrome
# rather than as data.
STATUS_COLORS = {
    cannon.SENT: SUCCESS,
    cannon.PREVIEW: WARNING,
    cannon.FAILED: DANGER,
    cannon.SKIPPED: MUTED,
}


# --------------------------------------------------------------------------
# Shared pieces
# --------------------------------------------------------------------------
@st.cache_data(ttl=5, show_spinner=False)
def vault_stats(_tick: int) -> dict:
    """Counts straight from SQLite. Cached because the fragment ticks at 2s."""
    return vault.stats()


def tick() -> int:
    return int(time.time() // 5)


def status_strip() -> None:
    """One glanceable row: is the tool ready, and what is in the pipeline."""
    counts = vault_stats(tick())
    a, b, c, d = st.columns(4)
    a.metric("Leads in vault", f"{counts['raw']:,}",
             help="Every business ever scraped, stored in data/leads.db.")
    b.metric("Cleaned", f"{counts['clean']:,}")
    c.metric("Mailboxes", cfg.sender_count if cfg.sender_count else "None",
             help="Sends rotate round-robin across these.")
    d.metric("Never contact", f"{counts['blacklist']:,}",
             help="Blacklisted addresses. Already-emailed leads are skipped too.")
    if not cfg.smtp.is_complete:
        st.info(
            "Sending is locked until Zoho credentials are set. "
            "Run the System Diagnostic tab to see exactly what is missing.",
            icon=":material/lock:",
        )
    st.divider()


def countdown_banner(state: dict) -> None:
    """Turn a pending delay into something that visibly ticks.

    Without this a five to twenty-five minute pause looks identical to a hung
    app, which is the single most likely reason someone kills a healthy run.
    """
    phase = state.get("phase")
    if phase not in {"delay", "break"}:
        return
    total = float(state.get("seconds") or 0)
    remaining = max(0.0, float(state.get("until") or 0) - time.time())
    done = 1.0 if total <= 0 else min(1.0, (total - remaining) / total)
    back_at = state.get("back_at", "")

    if phase == "break":
        wait_banner(
            f"Coffee break - resuming in {remaining/60:.0f} min",
            f"Paused on purpose after {cfg.coffee_every} sends so the rhythm stays "
            f"human. Nothing is stuck. Back around {back_at}.",
        )
    else:
        wait_banner(
            f"Next send in {remaining/60:.1f} min",
            f"Randomised gap between emails. Sending resumes around {back_at}.",
        )
    st.progress(done, text=f"{remaining/60:.1f} min left of {total/60:.1f}")


@st.fragment(run_every=1.0)
def live_panel(job_key: str, unit: str) -> None:
    """Polls the worker thread once a second and redraws only this block."""
    job = get_job(job_key)
    snap = job.snapshot()

    # Tells the runtime HUD the engine is firing. Lives here rather than at the
    # top of the script because only this fragment is redrawn while a worker
    # runs - a marker written at script level would go stale the moment the
    # campaign started.
    system_state(snap["status"] == RUNNING)

    countdown_banner(snap.get("state") or {})

    if snap["total"]:
        st.progress(job.fraction, text=f"{snap['current']} / {snap['total']} {unit}")
    else:
        st.progress(0.0, text=snap["message"] or "Working")
    st.caption(snap["message"])
    log_panel(snap["log"])

    if snap["status"] != RUNNING:
        st.rerun()  # leave the fragment, let the full app render the result


def outcome(job_key: str) -> None:
    """Terminal state for a finished job: one clear visual verdict."""
    job = get_job(job_key)
    snap = job.snapshot()
    if snap["status"] == DONE:
        st.success(snap["message"] or "Finished", icon=":material/check_circle:")
    elif snap["status"] == STOPPED:
        st.warning("Stopped by you. Everything already done is saved.",
                   icon=":material/stop_circle:")
    elif snap["status"] == ERROR:
        st.error(snap["error"], icon=":material/error:")
    if snap["log"]:
        with st.expander("Activity log", expanded=False):
            log_panel(snap["log"], limit=400)


def stop_button(job_key: str, label: str) -> None:
    if st.button(label, key=f"stop_{job_key}", type="primary", width="stretch",
                 icon=":material/stop_circle:"):
        stop_job(job_key)
        st.rerun()


def pick_batch(stage: str, key: str) -> str | None:
    """Choose a saved batch. Replaces picking a CSV file off disk."""
    batches = vault.list_batches(stage)
    if not batches:
        st.info(f"No {stage} leads in the vault yet.", icon=":material/database:")
        return None
    labels = {f"{b['batch']}  ({b['leads']} leads)": b["batch"] for b in batches}
    chosen = st.selectbox("Batch", list(labels), key=key)
    return labels[chosen]


# The CRM view. Order is the intended reading order - who they are
# first, how to reach them second, provenance last.
LEAD_COLUMNS = {
    "name": "Name",
    "lead_type": "Type",
    "location": "Location",
    "phone": "Phone",
    "email": "Email",
    "source": "Source",
    "category": "Detail",
    "website": "Website",
    "handle": "Handle",
}

ALL = "All"


def lead_table(rows: list[dict], caption: str = "", height: int = 320,
               key: str | None = None) -> None:
    """One CRM table, used everywhere leads are shown, so they always look alike."""
    if not rows:
        return
    frame = pd.DataFrame(rows, columns=list(LEAD_COLUMNS)).rename(columns=LEAD_COLUMNS)
    if caption:
        st.caption(caption)
    st.dataframe(
        frame,
        key=key,
        width="stretch",
        height=height,
        hide_index=True,
        column_config={
            "Name": st.column_config.TextColumn(width="medium", pinned=True),
            "Type": st.column_config.TextColumn(width="small"),
            "Location": st.column_config.TextColumn(width="small"),
            "Phone": st.column_config.TextColumn(width="medium"),
            "Email": st.column_config.TextColumn(width="medium"),
            "Source": st.column_config.TextColumn(width="medium"),
            "Detail": st.column_config.TextColumn(
                width="medium", help="Business category or bio snippet."),
            # display_text is a regex: the capture group becomes the label.
            # A Reddit permalink reads "reddit.com/r/VideoEditing" and a
            # Disboard invite reads "disboard.org/server/join/123" instead of a
            # uniform "open", so a community row is identifiable at a glance
            # and still one click from the real page.
            "Website": st.column_config.LinkColumn(
                width="large", display_text=r"https?://(?:www\.)?(.+?)/?$"),
            "Handle": st.column_config.TextColumn(width="medium"),
        },
    )


def type_breakdown(rows: list[dict]) -> None:
    """A row of counts per lead type, so the mix is readable without scrolling."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.get("lead_type") or purifier.UNKNOWN_TYPE] = (
            counts.get(row.get("lead_type") or purifier.UNKNOWN_TYPE, 0) + 1)
    if not counts:
        return
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    for col, (label, count) in zip(st.columns(len(ordered)), ordered):
        col.metric(label, count)


# --------------------------------------------------------------------------
# Tab 1 - The Hunter
# --------------------------------------------------------------------------
def tab_hunt() -> None:
    job = get_job(HUNT)

    st.subheader("Find leads")
    source = st.segmented_control(
        "Source", hunter.SOURCES, default=hunter.SOURCES[0],
        key="hunt_source", width="stretch",
    ) or hunter.SOURCES[0]

    if source == "Google Maps":
        left, right = st.columns(2)
        keyword = left.text_input("Business type", value=hunter.DEFAULT_MAPS_KEYWORD)
        location = right.text_input("Location", value=hunter.DEFAULT_MAPS_LOCATION)
        args = {"keyword": keyword, "location": location}
        fn = hunter.scrape_google_maps
        ready = bool(keyword and location)
    elif source in hunter.KEYWORD_SOURCES:
        # Reddit and Discord search a niche, not an account. They return
        # communities rather than people: intelligence for the vault and the
        # CRM, not addresses for a campaign.
        target = st.text_input(
            "Niche or keyword", placeholder="video editing   or   content creators",
            key="hunt_keyword",
        )
        args = {"target": target}
        fn = (hunter.scrape_reddit if source == "Reddit" else hunter.scrape_discord)
        ready = bool(target)
        st.caption(
            "Communities have no public email address. These rows land in the "
            "vault and the CRM as intelligence; they are not emailable leads."
        )
    else:
        target = st.text_input(
            "Hashtag or account", placeholder="#realestateagent  or  @someaccount"
        )
        args = {"target": target}
        fn = hunter.scrape_instagram if source == "Instagram" else hunter.scrape_tiktok
        ready = bool(target)

    slider_col, toggle_col = st.columns([3, 2])
    max_results = slider_col.slider("Stop after", 10, 300, 40, step=10,
                                    help="Leads to collect.")
    skip_known = toggle_col.toggle(
        "Skip targets already in the vault", value=True,
        help="Checks each target against the vault before fetching anything. "
             "Turn off to re-crawl a list you want refreshed.",
    )
    st.write("")

    if job.is_running:
        stop_button(HUNT, "STOP HUNTING")
        live_panel(HUNT, "leads")
        return

    if st.button("START HUNTING", type="primary", width="stretch",
                 disabled=not ready, icon=":material/travel_explore:"):
        start_job(HUNT, fn, cfg=cfg, max_results=max_results,
                  skip_known=skip_known, **args)
        st.rerun()
    if not ready:
        st.caption("Fill the fields above to enable the button.")

    outcome(HUNT)
    rows = job.snapshot()["result"] or []
    if rows:
        type_breakdown(rows)
        lead_table(rows, f"{len(rows)} leads found", key="hunt_results")
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        slug = source.split()[0].lower()
        if st.button("Save to the vault", width="stretch", icon=":material/database:"):
            saved = vault.save_leads(f"{slug}-{stamp}", vault.RAW, rows)
            vault_stats.clear()
            st.success(f"{saved} leads written to {vault.DB_PATH.name}",
                       icon=":material/check_circle:")


# --------------------------------------------------------------------------
# Tab 2 - The Purifier
# --------------------------------------------------------------------------
def tab_purify() -> None:
    st.subheader("Clean the list")
    batch = pick_batch(vault.RAW, "purify_src")
    drop_generic = st.toggle(
        "Drop role addresses (info@, support@, ...)", value=True,
        help="Keeps only named addresses. Role inboxes are often the ones that "
             "actually reply, so turn this off if your reply rate drops.",
    )
    st.write("")

    if st.button("PURIFY BATCH", type="primary", width="stretch",
                 disabled=batch is None, icon=":material/filter_alt:"):
        try:
            rows = vault.load_leads(batch)
            res = purifier.purify(rows, drop_generic=drop_generic)
            clean_batch = f"clean-{batch}"
            vault.save_leads(clean_batch, vault.CLEAN, res.leads)
            vault_stats.clear()
            st.session_state["purify_result"] = res
            st.session_state["purify_batch"] = clean_batch
        except Exception as exc:
            st.error(f"{type(exc).__name__}: {exc}", icon=":material/error:")

    res = st.session_state.get("purify_result")
    if res:
        st.success(
            f"{res.kept} usable leads saved as {st.session_state['purify_batch']}",
            icon=":material/check_circle:",
        )
        cols = st.columns(5)
        stats = [
            ("In", res.total_in), ("Kept", res.kept), ("Duplicates", res.duplicates),
            ("Invalid", res.invalid + res.no_email), ("Role", res.generic),
        ]
        for col, (label, value) in zip(cols, stats):
            col.metric(label, value)
        type_breakdown(res.leads)
        lead_table(res.leads[:200], "First 200 clean leads", key="purify_results")


# --------------------------------------------------------------------------
# Tab 3 - The Cannon
# --------------------------------------------------------------------------
def template_editor(variant: str) -> None:
    try:
        current = templates.load(variant)
        subject, body = current.subject, current.body
    except templates.TemplateError as exc:
        st.warning(str(exc), icon=":material/warning:")
        parsed = templates.parse(templates.DEFAULTS[variant], variant)
        subject, body = parsed.subject, parsed.body

    new_subject = st.text_input("Subject", value=subject, key=f"subject_{variant}")
    new_body = st.text_area("Message", value=body, height=260, key=f"body_{variant}")
    if st.button(f"Save template {variant}", key=f"save_{variant}",
                 width="stretch", icon=":material/save:"):
        path = templates.save(variant, new_subject, new_body)
        st.success(f"Saved {path.name}", icon=":material/check_circle:")


def tab_campaign() -> None:
    job = get_job(CAMPAIGN)
    st.subheader("Send the campaign")

    if job.is_running:
        stop_button(CAMPAIGN, "STOP CAMPAIGN NOW")
        st.caption("Stop takes effect immediately, including mid-delay and mid-break.")
        live_panel(CAMPAIGN, "emails")
        return

    left, right = st.columns([3, 2], gap="large")

    with left:
        batch = pick_batch(vault.CLEAN, "camp_src")
        editor_a, editor_b = st.tabs(["Template A", "Template B"])
        with editor_a:
            template_editor("A")
        with editor_b:
            template_editor("B")
        st.caption(
            "Spintax {a|b|c} picks one at random per email. Placeholders: "
            "[name] [website] [location] [handle] [email]"
        )

    with right:
        section("A/B testing")
        ab_mode = st.toggle(
            "Split A/B evenly", value=True,
            help="Lead 1 gets A, lead 2 gets B, lead 3 gets A, and so on.",
        )
        if not ab_mode:
            st.caption("Off: every lead gets template A.")
        elif cfg.sender_count > 1 and cfg.sender_count % 2 == 0:
            st.warning(
                f"{cfg.sender_count} mailboxes with 2 variants alternate in step, so "
                "each variant would always be sent by the same mailbox. Use an odd "
                "number of mailboxes to keep the test clean.",
                icon=":material/warning:",
            )

        section("Rhythm")
        min_delay = st.number_input("Min gap (minutes)", 1, 60,
                                    max(1, cfg.min_delay // 60))
        max_delay = st.number_input("Max gap (minutes)", 1, 90,
                                    max(1, cfg.max_delay // 60))
        dry_run = st.toggle(
            "Dry run", value=True,
            help="Renders and logs every email without opening a socket. "
                 "Dry runs never blacklist anyone.",
        )

        section("Safety")
        if cfg.sender_count > 1:
            st.caption(f"Rotating {cfg.sender_count} mailboxes: "
                       + ", ".join(s.email for s in cfg.senders))
        elif cfg.sender_count == 1:
            st.caption(f"Single mailbox: {cfg.senders[0].email}")
        else:
            st.caption("No mailbox configured - dry run only.")
        if cfg.coffee_every > 0:
            st.caption(f"Coffee break every {cfg.coffee_every} sends for "
                       f"{cfg.coffee_min // 60}-{cfg.coffee_max // 60} minutes.")
        st.caption(f"Daily cap {cfg.daily_cap} emails per campaign.")
        st.caption("Anyone already emailed, or on the blacklist, is skipped "
                   "automatically.")

    blocked = batch is None or (not dry_run and not cfg.smtp.is_complete)
    if max_delay < min_delay:
        st.error("Max gap must be at least the min gap.", icon=":material/error:")
        blocked = True

    st.write("")
    label = "LAUNCH DRY RUN" if dry_run else "LAUNCH CAMPAIGN"
    if st.button(label, type="primary", width="stretch", disabled=blocked,
                 icon=":material/rocket_launch:"):
        try:
            chosen = templates.load_all(("A", "B") if ab_mode else ("A",))
        except templates.TemplateError as exc:
            st.error(str(exc), icon=":material/error:")
            return
        leads = vault.load_leads(batch)
        start_job(
            CAMPAIGN, cannon.send_campaign, cfg=cfg, leads=leads,
            templates=chosen,
            min_delay=int(min_delay) * 60, max_delay=int(max_delay) * 60,
            dry_run=dry_run, campaign=batch,
        )
        st.rerun()

    if batch is not None and not dry_run and not cfg.smtp.is_complete:
        st.caption("Live sending needs Zoho credentials. See the Diagnostic tab.")

    outcome(CAMPAIGN)
    if job.snapshot()["status"] in (DONE, STOPPED, ERROR):
        if st.button("Clear", width="stretch", icon=":material/refresh:"):
            reset_job(CAMPAIGN)
            st.rerun()


# --------------------------------------------------------------------------
# Tab 4 - The Command Center
# --------------------------------------------------------------------------
STATUS_FILTERS = ("All", cannon.SENT, cannon.FAILED, cannon.SKIPPED, cannon.PREVIEW)

TABLE_COLUMNS = {
    "email": "Email Target",
    "sender": "Sender Used",
    "variant": "Variant",
    "status": "Status",
    "timestamp": "Timestamp",
    "subject": "Subject",
    "detail": "Detail",
}


def outcome_donut(counts: dict[str, int]):
    """Success / failed / skipped at a glance."""
    live = {k: v for k, v in counts.items() if v}
    if not live:
        live = {"Nothing yet": 1}
    fig = px.pie(
        names=list(live), values=list(live.values()), hole=0.62, color=list(live),
        color_discrete_map={**STATUS_COLORS, "Nothing yet": "#2A3242"},
        color_discrete_sequence=PLOT_SEQUENCE,
    )
    fig.update_traces(textposition="inside", textinfo="value",
                      marker=dict(line=dict(color="#0E1117", width=2)),
                      hovertemplate="%{label}: %{value} (%{percent})<extra></extra>")
    fig.update_layout(**PLOT_LAYOUT, showlegend=True,
                      legend=dict(orientation="h", y=-0.08),
                      title=dict(text="Outcome mix", x=0.02, font=dict(size=14)))
    return fig


def timeline_chart(frame: pd.DataFrame):
    """Emails over time, cumulative, so progress always trends upward."""
    marks = frame.dropna(subset=["Timestamp"]).copy() if not frame.empty else frame
    if marks.empty:
        per_minute = pd.DataFrame({"Timestamp": [pd.Timestamp.now()], "Emails": [0]})
    else:
        marks["bucket"] = marks["Timestamp"].dt.floor("min")
        per_minute = (marks.groupby("bucket").size().reset_index(name="count")
                      .sort_values("bucket"))
        per_minute["Emails"] = per_minute["count"].cumsum()
        per_minute = per_minute.rename(columns={"bucket": "Timestamp"})
    fig = px.area(per_minute, x="Timestamp", y="Emails", markers=True)
    fig.update_traces(line=dict(color=ACCENT, width=2.5),
                      fillcolor="rgba(76,141,255,0.16)",
                      marker=dict(size=7, color=ACCENT),
                      hovertemplate="%{y} emails by %{x|%H:%M}<extra></extra>")
    fig.update_layout(**PLOT_LAYOUT, showlegend=False,
                      title=dict(text="Emails over time", x=0.02, font=dict(size=14)),
                      xaxis=dict(gridcolor="#1C2430", title=None),
                      yaxis=dict(gridcolor="#1C2430", title=None, rangemode="tozero"))
    return fig


def ab_chart(rows: list[dict]):
    """Variant A against variant B, broken down by outcome."""
    if not rows:
        rows = [{"variant": v, "status": "No data", "n": 0} for v in ("A", "B")]
    frame = pd.DataFrame(rows).rename(
        columns={"variant": "Variant", "status": "Status", "n": "Emails"})
    # Horizontal. A vertical grouped bar inside a Streamlit tab renders its
    # categories at unequal widths (the figure itself is correct - verified by
    # dumping the trace - so it is a layout quirk of mounting inside a tab).
    # Horizontal bars lay out from the category axis and are not affected, and
    # a two-way comparison reads better this way anyway.
    fig = px.bar(frame, y="Variant", x="Emails", color="Status", barmode="group",
                 orientation="h",
                 color_discrete_map={**STATUS_COLORS, "No data": "#2A3242"},
                 color_discrete_sequence=PLOT_SEQUENCE, text="Emails")
    # Explicit bar width and no transition. Left to itself, Plotly re-tweens the
    # bars on every two-second fragment tick and a bar whose value changed can
    # stay stuck at an intermediate width indefinitely.
    fig.update_traces(textposition="outside", cliponaxis=False,
                      marker=dict(line=dict(color="#0E1117", width=1)),
                      hovertemplate="%{y} %{fullData.name}: %{x}<extra></extra>")
    fig.update_layout(**{**PLOT_LAYOUT, "height": 230},
                      title=dict(text="A/B comparison", x=0.02, font=dict(size=14)),
                      legend=dict(orientation="h", y=-0.22, title=None),
                      bargap=0.45, bargroupgap=0.1,
                      # Emails are whole numbers - no 0.2 ticks.
                      xaxis=dict(gridcolor="#1C2430", title=None, rangemode="tozero",
                                 tickformat="d",
                                 dtick=1 if frame["Emails"].max() <= 8 else None),
                      yaxis=dict(gridcolor="#1C2430", title=None,
                                 categoryorder="category descending"))
    return fig


@st.fragment(run_every=2.0)
def analytics_body() -> None:
    """Live metrics, charts and send table, all read from SQLite.

    Never calls st.rerun(): unlike live_panel this fragment renders whether or
    not a job is running, so an unconditional rerun would loop forever.
    """
    counts = vault.status_counts()
    stats = vault_stats(tick())
    sent = counts.get(cannon.SENT, 0)
    failed = counts.get(cannon.FAILED, 0)
    skipped = counts.get(cannon.SKIPPED, 0)
    previewed = counts.get(cannon.PREVIEW, 0)
    attempted = sent + failed
    rate = f"{sent / attempted * 100:.0f}% delivered" if attempted else None

    a, b, c, d = st.columns(4)
    a.metric("Total Scraped", f"{stats['raw']:,}")
    b.metric("Cleaned Leads", f"{stats['clean']:,}")
    c.metric("Emails Sent", f"{sent:,}",
             help="Live sends only. Dry-run previews are counted separately.")
    d.metric("Emails Failed", f"{failed:,}", delta=rate,
             delta_color="normal" if not failed else "inverse")

    job = get_job(CAMPAIGN)
    if job.is_running:
        snap = job.snapshot()
        countdown_banner(snap.get("state") or {})
        st.progress(job.fraction,
                    text=f"{snap['current']} / {snap['total']} - {snap['message']}")

    events = vault.read_log()
    frame = pd.DataFrame(events, columns=list(TABLE_COLUMNS)).rename(
        columns=TABLE_COLUMNS)
    if not frame.empty:
        frame["Timestamp"] = pd.to_datetime(frame["Timestamp"], errors="coerce")

    st.write("")
    # The chart key carries a signature of the data. Identical data means an
    # identical key, so a fragment tick that changes nothing leaves the charts
    # completely untouched - no re-render, no flash. When the numbers do move,
    # the key changes and Plotly mounts a fresh figure.
    #
    # Re-rendering the same key every two seconds is what caused a bar whose
    # value had just changed to render at a fraction of its proper width and
    # stay that way. Verified against a standalone page with the identical
    # figure config, which renders correctly - the repeated update was the
    # difference, not the styling.
    variants = vault.variant_counts()
    sig = f"{sent}-{failed}-{skipped}-{previewed}"
    ab_sig = "-".join(f"{r['variant']}{r['status']}{r['n']}" for r in variants)

    chart_left, chart_right = st.columns([2, 3], gap="large")
    with chart_left:
        st.plotly_chart(
            outcome_donut({cannon.SENT: sent, cannon.FAILED: failed,
                           cannon.SKIPPED: skipped, cannon.PREVIEW: previewed}),
            key=f"chart_donut_{sig}", width="stretch",
            config=PLOT_CONFIG)
    with chart_right:
        st.plotly_chart(timeline_chart(frame), key=f"chart_timeline_{sig}",
                        width="stretch", config=PLOT_CONFIG)

    st.plotly_chart(ab_chart(variants), key=f"chart_ab_{ab_sig or 'empty'}",
                    width="stretch", config=PLOT_CONFIG)

    st.divider()

    if not events:
        st.info("No sends yet. Launch a dry run from the Campaign tab.",
                icon=":material/inbox:")
        return

    chosen = st.session_state.get("analytics_filter", "All")
    shown = frame if chosen == "All" else frame[frame["Status"] == chosen]
    shown = shown.iloc[::-1].head(500)

    st.dataframe(
        shown,
        key="analytics_table",
        width="stretch",
        height=360,
        hide_index=True,
        column_config={
            "Timestamp": st.column_config.DatetimeColumn(format="DD MMM HH:mm:ss"),
            "Email Target": st.column_config.TextColumn(width="medium"),
            "Sender Used": st.column_config.TextColumn(width="medium"),
            "Variant": st.column_config.TextColumn(width="small"),
            "Status": st.column_config.TextColumn(width="small"),
        },
    )
    st.caption(f"Showing {len(shown):,} of {len(events):,} events, newest first. "
               f"{previewed:,} dry-run previews on record.")


def lead_intelligence() -> None:
    """Every target in the vault, filterable. The CRM view of the whole list.

    Deliberately outside the polling fragment: this is a browse-and-filter
    surface, and widgets inside a run_every fragment lose their value on
    every tick.
    """
    section("Lead intelligence")
    types = [ALL] + vault.distinct_values("lead_type")
    sources = [ALL] + vault.distinct_values("source")

    a, b, c, d = st.columns([2, 2, 2, 3])
    stage = a.selectbox("Stage", [ALL, vault.RAW, vault.CLEAN], key="crm_stage")
    chosen_type = b.selectbox("Type", types, key="crm_type")
    chosen_source = c.selectbox("Source", sources, key="crm_source")
    text = d.text_input("Search", key="crm_text",
                        placeholder="name, email, city, handle")

    rows = vault.search_leads(
        stage=None if stage == ALL else stage,
        lead_type="" if chosen_type == ALL else chosen_type,
        source="" if chosen_source == ALL else chosen_source,
        text=text.strip(),
    )

    # A zone acquired on the radar narrows this grid too, so the CRM and the
    # map are never showing two different lists.
    zone_keys = st.session_state.get(ZONE_CITIES) or set()
    total_before_zone = len(rows)
    rows = rows_in_zone(rows, zone_keys)
    if zone_keys:
        # No Release button here on purpose: a pydeck selection cannot be
        # cleared from Python - Streamlit documents the state as read-only -
        # so a button that claimed to release the zone would be overruled by
        # the chart on the very next rerun. The map itself is the control.
        st.info(
            f"Zone filter active: {len(rows):,} of {total_before_zone:,} targets "
            "inside the area acquired on the radar. Click empty space on the "
            "map in Network & Radar to release it.",
            icon=":material/my_location:",
        )

    if not rows:
        st.info("No leads match those filters.", icon=":material/search_off:")
        return

    type_breakdown(rows)
    lead_table(rows, f"{len(rows):,} targets", height=380, key="crm_table")

    buffer = io.StringIO()
    pd.DataFrame(rows).to_csv(buffer, index=False)
    st.download_button("Export these leads", buffer.getvalue(),
                       file_name="clipagent-leads.csv", mime="text/csv",
                       width="stretch", icon=":material/download:")


def blacklist_manager() -> None:
    section("Never contact")
    entries = vault.list_blacklist()
    left, right = st.columns([3, 2], gap="large")

    with left:
        new_email = st.text_input("Add an address", key="bl_add",
                                  placeholder="someone@agency.com")
        reason = st.text_input("Reason", key="bl_reason", value="manual")
        if st.button("Add to blacklist", width="stretch",
                     disabled=not new_email.strip(), icon=":material/block:"):
            vault.add_to_blacklist(new_email, reason or "manual")
            vault_stats.clear()
            st.success(f"{new_email.strip().lower()} will never be contacted.",
                       icon=":material/check_circle:")
            st.rerun()

    with right:
        if entries:
            st.dataframe(pd.DataFrame(entries), width="stretch", height=200,
                         hide_index=True)
            drop = st.selectbox("Remove", [e["email"] for e in entries], key="bl_drop")
            if st.button("Remove from blacklist", width="stretch",
                         icon=":material/delete:"):
                vault.remove_from_blacklist(drop)
                vault_stats.clear()
                st.rerun()
        else:
            st.caption("Empty. Anyone already emailed is skipped automatically, "
                       "with or without this list.")


def tab_analytics() -> None:
    st.subheader("Command Center")
    # Outside the fragment: a widget inside a run_every fragment loses its
    # value on every tick. Seeded through session_state rather than default=,
    # so a full rerun cannot clobber the current selection.
    st.session_state.setdefault("analytics_filter", "All")
    st.segmented_control("Filter", STATUS_FILTERS, key="analytics_filter",
                         width="stretch")
    analytics_body()

    st.divider()
    lead_intelligence()

    st.divider()
    blacklist_manager()

    st.divider()
    events = vault.read_log()
    if events:
        buffer = io.StringIO()
        pd.DataFrame(events).to_csv(buffer, index=False)
        left, right = st.columns(2)
        left.download_button(
            "Export log to CSV", buffer.getvalue(), file_name="campaign_log.csv",
            mime="text/csv", width="stretch", icon=":material/download:",
        )
        if right.button("Clear log history", width="stretch", icon=":material/delete:"):
            st.session_state["confirm_clear"] = True
        if st.session_state.get("confirm_clear"):
            st.warning(
                "This deletes every recorded send. Addresses already emailed will "
                "stop being skipped, so you could contact them twice.",
                icon=":material/warning:",
            )
            yes, no = st.columns(2)
            if yes.button("Delete the log", type="primary", width="stretch"):
                vault.clear_log()
                vault_stats.clear()
                st.session_state["confirm_clear"] = False
                st.rerun()
            if no.button("Keep it", width="stretch"):
                st.session_state["confirm_clear"] = False
                st.rerun()


# --------------------------------------------------------------------------
# Tab 5 - Network & Radar
# --------------------------------------------------------------------------
RADAR_KEY = "radar"              # the pydeck widget's own state key
ZONE_RADIUS = "zone_radius_km"   # how far the zone reaches from each seed
ZONE_CITIES = "zone_cities"      # normalised city keys currently captured
ZONE_COUNT = "zone_count"        # leads inside the zone, for the HUD
ZONE_SUMMARY = "zone_summary"    # what the radar should say about the zone


@st.cache_data(ttl=10, show_spinner=False)
def mapped_points(_signature: str) -> tuple[list[dict], list[dict], list[str]]:
    """Vault rows plus the city points they map to.

    Cached because three separate surfaces read it on every rerun, and because
    the selection returned by the chart is a list of POSITIONS into this exact
    point order - recomputing it per caller would risk two callers disagreeing
    about what index 3 means.
    """
    rows = vault.search_leads(limit=10000)
    points, unplaced = geo.city_points(rows)
    return rows, points, unplaced


def resolve_zone() -> dict:
    """Read the radar's selection and work out what it captures.

    Called once, before any tab renders. The chart's own widget state is the
    single source of truth: st.pydeck_chart keeps it across reruns and refuses
    to let it be set programmatically, so mirroring it into session state would
    only create a second version that drifts.

    Running before the tabs matters. The CRM grid lives in an earlier tab than
    the radar, so resolving inside the radar would leave the grid a full rerun
    behind the map.
    """
    counts = vault_stats(tick())
    signature = f"{counts['raw']}-{counts['clean']}"
    rows, points, unplaced = mapped_points(signature)

    state = st.session_state.get(RADAR_KEY)
    indices = []
    if state is not None:
        try:
            indices = list(state["selection"]["indices"].get(geo.LAYER_COLUMNS, []))
        except (KeyError, TypeError):
            indices = []

    radius = int(st.session_state.get(ZONE_RADIUS, 0) or 0)
    captured = geo.capture_zone(points, indices, radius)
    keys = zone_city_keys(points, captured)

    # No selection means no zone - every surface shows everything. An empty
    # grid would be the wrong answer to a stray click on open water.
    st.session_state[ZONE_CITIES] = keys
    st.session_state[ZONE_COUNT] = (
        sum(int(points[i]["leads"]) for i in captured) if captured else None
    )
    return {"rows": rows, "points": points, "unplaced": unplaced,
            "seeds": indices, "captured": captured, "keys": keys,
            "radius": radius}


def zone_city_keys(points: list[dict], captured: list[int]) -> set[str]:
    """Normalised city keys for the captured points.

    Leads carry a free-text ``location``; the map carries one point per city.
    Both go through geo.normalise so "Brooklyn, New York" and "new york" land
    on the same key and the filter cannot miss a row on spelling alone.
    """
    return {geo.normalise(points[i]["city"]) for i in captured
            if 0 <= i < len(points)}


def rows_in_zone(rows: list[dict], keys: set[str]) -> list[dict]:
    """The leads whose city falls inside the acquired zone."""
    if not keys:
        return rows
    return [r for r in rows
            if geo.normalise((r.get("location") or "")) in keys]


def radar_map(points: list[dict], unplaced: list[str]) -> None:
    """Cyber-tracking view of where the leads are.

    Columns scale with lead volume, amber arcs route from the busiest city to
    every other one, and the camera sits at a 60-degree tilt. The basemap is
    Carto Dark Matter, which deck.gl serves without a Mapbox token.
    """
    if not points:
        st.info("No leads with a recognised city yet.", icon=":material/public:")
        return

    zone = st.session_state.get("_zone") or {}
    seeds = zone.get("seeds", [])
    captured = zone.get("captured", [])
    radius = zone.get("radius", 0)

    # Layers, camera and basemap all live in core.geo, beside the coordinates
    # that feed them. See geo.deck for the arc routing and the 60-degree tilt.
    #
    # on_select="rerun" makes the chart a widget: one rerun per selection
    # change, which is not a loop. The map is NOT inside a fragment and nothing
    # here calls st.rerun(), so a click costs exactly one pass.
    state = st.pydeck_chart(
        geo.deck(points, seeds, radius),
        height=470,
        selection_mode="multi-object",
        on_select="rerun",
        key="radar",
    )

    # The widget writes its own state; resolve_zone() reads it at the top of the
    # next rerun. Nothing is written back here, so there is no second copy to
    # drift and no rerun triggered from inside the render.
    del state

    # Tells the HUD how many targets are inside the zone. Emitted from here so
    # it lives and dies with the radar itself.
    zone_state(st.session_state.get(ZONE_COUNT))

    if seeds:
        locked = ", ".join(points[i]["city"] for i in captured[:6])
        more = f" +{len(captured) - 6} more" if len(captured) > 6 else ""
        total = sum(int(points[i]["leads"]) for i in captured)
        st.success(
            f"Zone acquired: {len(captured)} of {len(points)} cities, "
            f"{total:,} targets locked. {locked}{more}",
            icon=":material/my_location:",
        )
    else:
        st.caption(
            "Click a column to acquire it. Ctrl-click or Cmd-click to add more. "
            "Widen the zone radius to pull in every city around the ones you "
            "picked. Click empty space to release."
        )

    if unplaced:
        st.caption(
            f"{len(unplaced)} location(s) not on the map: {', '.join(unplaced[:6])}. "
            "Geocoding is offline by design - add coordinates to CITIES in core/geo.py."
        )


def tab_network() -> None:
    st.subheader("Network & Radar")
    st.caption(
        "Every target in the vault, wired to its type and its city. Drag a node "
        "to pull the cluster around; the simulation settles on its own."
    )

    zone = st.session_state.get("_zone") or {}
    rows = zone.get("rows") or []
    if not rows:
        st.info("Nothing in the vault yet. Run a hunt first.",
                icon=":material/database:")
        return

    controls, _ = st.columns([3, 1])
    with controls:
        left, middle, right = st.columns(3)
        max_nodes = left.slider("Max leads drawn", 20, 400,
                                network.DEFAULT_MAX_NODES, step=20,
                                help="A force simulation with several hundred "
                                     "nodes locks up the browser.")
        spring = middle.slider("Spring length", 60, 320, 150, step=10)
        repulsion = right.slider("Repulsion", 4000, 40000, 18000, step=2000)

    st.slider(
        "Zone radius (km)", 0, 2000, key=ZONE_RADIUS, step=50,
        help="0 locks only the cities you click. Above that, the zone pulls in "
             "every city within this distance of any of them.",
    )

    # Everything below this line reads the zone rather than the full vault, so
    # the grid, the graph and the counters can never disagree with the map.
    zone_keys = st.session_state.get(ZONE_CITIES) or set()
    scoped = rows_in_zone(rows, zone_keys)
    graph = network.build(scoped, max_nodes=max_nodes)

    a, b, c, d = st.columns(4)
    a.metric("Targets", f"{graph.total:,}",
             delta=(f"-{len(rows) - len(scoped):,} outside zone"
                    if zone_keys and len(scoped) != len(rows) else None),
             delta_color="off")
    b.metric("Drawn", f"{graph.shown:,}")
    c.metric("Cities", len({(r.get('location') or '').strip()
                            for r in scoped if (r.get('location') or '').strip()}))
    d.metric("Types", len({(r.get('lead_type') or '').strip()
                           for r in scoped if (r.get('lead_type') or '').strip()}))

    graph_col, map_col = st.columns([3, 4], gap="large")

    with graph_col:
        section("Intelligence network")
        # The vis.js canvas fits its view the moment it mounts. Mounted inside
        # a tab that is not yet on screen it measures a zero-width parent and
        # settles on an unusable zoom level. Gating it behind a button means it
        # first mounts on the rerun after the click, when this tab is already
        # the visible one - the tab selection is client side and survives the
        # rerun. Measured; autoResize and a fixed width were not enough.
        st.session_state.setdefault("network_drawn", False)
        if not st.session_state["network_drawn"]:
            if st.button("Draw the network", type="primary", width="stretch",
                         icon=":material/hub:"):
                st.session_state["network_drawn"] = True
                st.rerun()
            st.caption(f"{graph.total:,} targets and {len(graph.nodes)} nodes "
                       "ready to plot.")
        else:
            # Rendered in the plain script body, never inside a run_every
            # fragment: the physics engine restarts from scratch on every
            # re-render, so a polling parent would leave the graph jittering.
            #
            # physics is a NAMED Config parameter, so passing a dict for it
            # binds to that parameter and Config wraps it as
            # {"enabled": <dict>}, which vis.js rejects outright. It is set
            # after construction instead.
            #
            # width must be a NUMBER: Config formats it as f"{width}px", so
            # "100%" becomes the invalid CSS "100%px" and the canvas collapses.
            options = network.physics_options(spring_length=spring,
                                              repulsion=repulsion)
            physics = options.pop("physics")
            config = Config(width=560, height=430, directed=False, **options)
            config.physics = physics
            agraph(
                nodes=[Node(**node) for node in graph.nodes],
                edges=[Edge(**edge) for edge in graph.edges],
                config=config,
            )
            caption = f"{graph.shown:,} of {graph.total:,} targets drawn."
            if graph.hidden:
                caption += (f" {graph.hidden:,} folded into the "
                            f"'+{graph.hidden} more' node - raise the cap.")
            st.caption(caption)
            if st.button("Hide the network", width="stretch",
                         icon=":material/visibility_off:"):
                st.session_state["network_drawn"] = False
                st.rerun()

    with map_col:
        section("Global radar")
        radar_map(zone.get("points") or [], zone.get("unplaced") or [])


# --------------------------------------------------------------------------
# Tab 6 - The Verifier (Setup folded in)
# --------------------------------------------------------------------------
@st.cache_data(ttl=30, show_spinner=False)
def static_prescan(_signature: str) -> diagnostics.ScanResult:
    """The fast half of the scan, cached.

    It runs on every full rerun otherwise - imports, a directory walk and three
    write probes - which shows up as lag on every button press in the app.
    """
    return diagnostics.run_static_checks(cfg)


def render_scan(result: diagnostics.ScanResult) -> None:
    ok, warn, fail = result.tally()
    a, b, c = st.columns(3)
    a.metric("Passed", ok)
    b.metric("Warnings", warn)
    c.metric("Failed", fail)

    if fail:
        st.error(f"{fail} check(s) failed. Fix the red rows below before sending.",
                 icon=":material/error:")
    elif warn:
        st.warning(f"All critical checks passed, {warn} thing(s) worth tightening.",
                   icon=":material/warning:")
    else:
        st.success("Every check passed. The system is ready.",
                   icon=":material/verified:")

    groups: dict[str, list] = {}
    for check in result.checks:
        groups.setdefault(check.group or "Other", []).append(check)

    for name, checks in groups.items():
        failed_here = sum(1 for c in checks if c.status == diagnostics.FAIL)
        section(f"{name}  -  {len(checks)} checks"
                + (f", {failed_here} failed" if failed_here else ""))
        for check in checks:
            check_row(check.name, check.status, check.detail)


def current_settings() -> None:
    """What used to be the Setup tab. Read-only, so it belongs beside the scan."""
    left, right = st.columns(2, gap="large")

    with left:
        section("Current settings")
        rows = [
            ("Config file", ENV_PATH.name if ENV_PATH.exists() else "missing"),
            ("SMTP host", cfg.smtp.host or "-"),
            ("Port", f"{cfg.smtp.port} ({'SSL' if cfg.smtp.use_ssl else 'STARTTLS'})"),
            ("Mailboxes in rotation", str(cfg.sender_count)),
            ("Send gap", f"{cfg.min_delay // 60}-{cfg.max_delay // 60} min"),
            ("Coffee break",
             f"every {cfg.coffee_every} sends, "
             f"{cfg.coffee_min // 60}-{cfg.coffee_max // 60} min"
             if cfg.coffee_every else "off"),
            ("Daily cap", str(cfg.daily_cap)),
            ("Unsubscribe line", cfg.unsubscribe_line or "missing"),
            ("Vault", str(vault.DB_PATH.name)),
        ]
        st.dataframe(pd.DataFrame(rows, columns=["Setting", "Value"]),
                     width="stretch", hide_index=True)

    with right:
        section("Rotation order")
        if cfg.senders:
            st.dataframe(
                pd.DataFrame(
                    [{"#": i, "Mailbox": s.email, "From name": s.from_name or "-"}
                     for i, s in enumerate(cfg.senders, start=1)]
                ),
                width="stretch", hide_index=True,
            )
        else:
            st.info("No mailbox configured yet. Copy .env.example to .env.",
                    icon=":material/mail:")
        missing = cfg.smtp.missing_fields()
        if missing:
            st.warning("Missing in .env: " + ", ".join(missing),
                       icon=":material/warning:")


def tab_diagnostic() -> None:
    job = get_job(DIAGNOSTIC)
    st.subheader("System Diagnostic & Scan")
    st.caption(
        "Config format, Zoho logins, the scraping engine, the vault and folder "
        "permissions. Everything except the mailbox logins runs instantly; testing "
        "mailboxes opens a real connection per account, so it runs in the background."
    )

    current_settings()
    st.divider()

    if job.is_running:
        stop_button(DIAGNOSTIC, "STOP SCAN")
        live_panel(DIAGNOSTIC, "mailboxes")
        return

    left, right = st.columns([3, 2])
    include_smtp = right.toggle("Include mailbox logins", value=True,
                                help="Opens a real SMTP connection per account.")
    if left.button("RUN FULL SYSTEM SCAN", type="primary", width="stretch",
                   icon=":material/health_and_safety:"):
        start_job(DIAGNOSTIC, diagnostics.run_full_scan, cfg=cfg,
                  include_smtp=include_smtp)
        st.rerun()

    snap = job.snapshot()
    if snap["status"] == ERROR:
        st.error(snap["error"], icon=":material/error:")

    result = snap["result"]
    if isinstance(result, diagnostics.ScanResult):
        st.caption("Last full scan, mailbox logins included."
                   if any(c.group == diagnostics.MAILBOXES for c in result.checks)
                   else "Last scan, mailbox logins skipped.")
    else:
        # Nothing has been scanned yet - the fast half still costs nothing,
        # so show it rather than an empty tab.
        result = static_prescan(f"{cfg.sender_count}:{cfg.smtp.email}:{ENV_PATH.exists()}")
        st.caption("Live pre-scan. Press the button above to also test mailbox logins.")

    render_scan(result)


# --------------------------------------------------------------------------
# The badge reports the one fact that decides whether this is a live console
# or a rehearsal: whether a mailbox is actually configured. Always-green would
# be an ornament.
_ready = cfg.smtp.is_complete
status_badge(
    "SYSTEM STATUS: NEXUS OPTIMAL" if _ready else "SYSTEM STATUS: STANDBY - NO MAILBOX",
    ok=_ready,
)
# Resolved before any tab draws, so the radar, the CRM grid and the graph all
# read one answer in the same rerun.
st.session_state["_zone"] = resolve_zone()

header("NEXUS: Outreach Core",
       "Acquire targets, purify the list, run the outreach. Local only.")
status_strip()

t1, t2, t3, t4, t5, t6 = st.tabs(
    ["Hunt", "Purify", "Campaign", "Analytics & Logs", "Network & Radar",
     "System Diagnostic & Scan"]
)
with t1:
    tab_hunt()
with t2:
    tab_purify()
with t3:
    tab_campaign()
with t4:
    tab_analytics()
with t5:
    tab_network()
with t6:
    tab_diagnostic()
