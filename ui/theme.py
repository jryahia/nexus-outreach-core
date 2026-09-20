"""Visual layer: NEXUS - a live, holographic command surface.

Framework-level dark mode lives in .streamlit/config.toml. This module layers
the Jarvis treatment on top: a void-black field, cyan holographic edges, a
drifting background grid, a scanline that sweeps each metric card, and the
Streamlit chrome hidden so the page reads as a compiled console rather than a
notebook.

Motion rules, because this dashboard already repaints on its own:

* Every loop animates ``transform`` or ``opacity`` only. Those composite off
  the main thread, so a sweep cannot fight the 1s live panel or the 2s
  analytics fragment. Animating ``box-shadow`` or ``filter`` on a metric card
  would repaint it on every one of those ticks.
* The background layers are two fixed pseudo-elements on ``.stApp``, not an
  ``st.components.v1.html`` iframe. A component iframe renders inline in the
  document flow, so it takes up layout space and cannot paint behind the app.
  Pure CSS also means no extra JavaScript on the websocket.

Three deliberate exceptions to the glass treatment, all measured rather than
assumed:

* ``st.dataframe`` gets a solid surface, not ``backdrop-filter``. The grid is
  virtualised and blurring what sits behind it makes the rows look muddy. It
  still gets the holographic border and glow, which cost nothing.
* ``[data-testid="stStatusWidget"]`` stays visible. Hiding the header removes
  the native running indicator, and that widget is the only remaining signal
  that a background thread is alive on screens with no live panel.
* ``@media (prefers-reduced-motion: reduce)`` kills every loop defined here.
  A permanent scanline on a screen somebody watches for an hour is an
  accessibility problem, not a feature.

No emoji anywhere - UI symbols are Material Symbols icons (":material/name:")
or inline SVG.
"""

from __future__ import annotations

import html

import streamlit as st
import streamlit.components.v1 as components

# Single source of truth for colour. Plotly, the status donut and the network
# graph read these too, so the charts and the chrome cannot drift apart.
# Rebinding a value here restyles the whole app; renaming one breaks app.py.
ACCENT = "#00F3FF"        # glowing cyan - the system colour
ACCENT_DEEP = "#0090A8"   # cyan, dimmed, for gradient ends
VIOLET = "#7A5CFF"        # third stop of the primary-button gradient
SUCCESS = "#00FF9C"       # live sends
DANGER = "#FF2E63"        # failures and alerts
WARNING = "#FFAA00"       # alert amber - dry runs, warnings, waiting states
MUTED = "#6B7A8F"
INK = "#D8F6FF"

VOID = "#020205"          # deep void black - the page floor
ACTIVE = "#39FF88"        # engine firing - only ever shown while a job runs
GHOST_RED = "#9B111E"     # Ghost Protocol - dried blood, not alarm red

GLASS = "rgba(6, 13, 22, 0.62)"
GLASS_STRONG = "rgba(3, 8, 14, 0.94)"
GLASS_EDGE = "rgba(0, 243, 255, 0.20)"
GLASS_EDGE_HOVER = "rgba(0, 243, 255, 0.55)"

# The holographic signature: a thin outer glow plus an inner cyan wash.
HOLO = "0 0 10px rgba(0, 243, 255, 0.2), inset 0 0 20px rgba(0, 243, 255, 0.05)"
HOLO_HOT = "0 0 22px rgba(0, 243, 255, 0.45), inset 0 0 26px rgba(0, 243, 255, 0.10)"
LIFT = "0 12px 26px rgba(0, 0, 0, 0.66)"
EASE = "cubic-bezier(0.25, 0.8, 0.25, 1)"

PLOT_SEQUENCE = [SUCCESS, DANGER, WARNING, ACCENT, MUTED]

# Two faces, each doing one job: Orbitron for the HUD chrome (headings,
# metrics, tabs, buttons) and Share Tech Mono for anything that reads as
# machine output (the log, the data grid, numbers). The @import has to be the
# very first thing in the stylesheet - a CSS parser silently drops an @import
# that appears after any rule.
FONT_IMPORT = ("@import url('https://fonts.googleapis.com/css2?"
               "family=Orbitron:wght@400..900&family=Rajdhani:wght@400;600;700&"
               "family=Share+Tech+Mono&display=swap');")

HUD_FONT = "'Orbitron', 'Rajdhani', sans-serif"
BODY_FONT = "'Rajdhani', 'Segoe UI', sans-serif"
MONO_FONT = "'Share Tech Mono', ui-monospace, Consolas, monospace"

_CSS = f"""
<style>
  {FONT_IMPORT}

  /* ---- Typography: the single biggest change to the feel --------------- */
  /* Set on the root and inherited from there. A blanket ".stApp *" was tried
     first and is wrong: Streamlit paints every Material Symbols icon as a
     ligature inside a span whose font-family it sets through a hashed emotion
     class, so a catch-all override turns each icon into its literal text -
     "lock", "travel_explore" - across the whole app. Measured, not assumed.
     Text elements therefore opt in by name, and a bare "span" is deliberately
     absent from that list. */
  html, body, .stApp {{
      font-family: {BODY_FONT};
  }}
  .stApp p, .stApp label, .stApp li, .stApp td, .stApp th,
  .stApp input, .stApp textarea, .stApp select,
  .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5,
  [data-testid="stMarkdownContainer"], [data-testid="stWidgetLabel"],
  [data-testid="stCaptionContainer"], [data-testid="stTab"] {{
      font-family: inherit;
  }}
  h1, h2, h3, h4,
  .nx-hero, .nx-section, .nx-badge,
  [data-testid="stMetricValue"], [data-testid="stMetricLabel"],
  [data-testid="stTab"], .stButton > button, .stDownloadButton > button {{
      font-family: {HUD_FONT};
  }}
  /* Machine output stays monospaced: the log, the grid and code blocks. The
     grid is not descended into for the same icon reason as above. */
  .nx-log, code, pre, kbd, samp,
  [data-testid="stDataFrame"] {{
      font-family: {MONO_FONT};
  }}
  h1, h2, h3 {{ letter-spacing: 0.04em; }}

  /* ---- Native app feel: no browser chrome, no notebook furniture -------- */
  header[data-testid="stHeader"] {{ display: none; }}
  #MainMenu, footer, [data-testid="stToolbar"],
  [data-testid="stDecoration"] {{ display: none; }}
  /* stStatusWidget deliberately NOT hidden: with the header gone it is the
     only native signal that a worker thread is still running. */

  /* The void lives on the document, NOT on .stApp, and that is the whole
     reason the WebGL core is visible at all. Measured: a fixed z-index:-100
     layer under an opaque .stApp is painted over and never seen, while the
     same layer under a transparent .stApp sits correctly behind every piece
     of UI with the interface still fully readable on top. So .stApp is made
     transparent and the background it used to own is moved up to body. */
  html, body {{
      background:
        radial-gradient(1200px 620px at 12% -8%, rgba(0,243,255,0.10), transparent 60%),
        radial-gradient(1000px 560px at 92% 4%, rgba(255,170,0,0.06), transparent 62%),
        {VOID};
      background-attachment: fixed;
  }}
  .stApp {{ background: transparent; }}

  /* The Three.js core: the deepest layer in the stack. Depth order is
     core (-100) -> node constellation (0) -> interface (1). */
  canvas.nx-core {{
      position: fixed;
      inset: 0;
      z-index: -100;
      pointer-events: none;
      opacity: 0.95;
  }}

  /* ---- Background layer 1: the node network ----------------------------- */
  /* The drifting CSS grid that used to live on .stApp::before was replaced by
     a real canvas, painted by the runtime controller in hud_runtime(). Two
     backgrounds compositing over each other is worse than either alone, so the
     pseudo-element is gone rather than merely hidden.
     The canvas is inserted into the PARENT document, behind everything, and
     never takes a pointer event. */
  canvas.nx-canvas {{
      position: fixed;
      inset: 0;
      z-index: 0;
      pointer-events: none;
      opacity: 0.85;
  }}

  /* The runtime is mounted through a zero-size component iframe. Streamlit
     still emits a wrapper element for it, and an empty iframe in the flow
     leaves a stray line box, so the wrapper is taken out of layout. */
  [data-testid="stCustomComponentV1"],
  iframe[title="streamlit_component_html"] {{
      display: none !important;
  }}

  /* ---- The radar: a framed global tracking pane ------------------------ */
  /* deck.gl renders into a square canvas, so the rounded frame only holds if
     the container clips it - border-radius alone leaves the corners poking
     through. The height comes from st.pydeck_chart(height=600); here is only
     the chrome. The Carto Dark Matter basemap is fetched over the network at
     render time, so behind a blocking proxy the tiles grey out - the frame
     and the data layers still draw. */
  [data-testid="stDeckGlJsonChart"] {{
      width: 100% !important;
      border-radius: 12px;
      border: 1px solid rgba(0, 243, 255, 0.30);
      box-shadow: 0 0 24px rgba(0, 243, 255, 0.12),
                  inset 0 0 40px rgba(0, 243, 255, 0.05);
      overflow: hidden;
      background: rgba(3, 8, 16, 0.85);
  }}
  [data-testid="stDeckGlJsonChart"] canvas,
  [data-testid="stDeckGlJsonChart"] > div {{
      border-radius: 12px;
  }}

  /* ---- Background layer 2: the horizon sweep ---------------------------- */
  .stApp::after {{
      content: "";
      position: fixed;
      left: 0; right: 0; top: 0;
      height: 180px;
      z-index: 0;
      pointer-events: none;
      background: linear-gradient(180deg,
        transparent 0%,
        rgba(0,243,255,0.05) 46%,
        rgba(0,243,255,0.13) 50%,
        rgba(0,243,255,0.05) 54%,
        transparent 100%);
      animation: nx-horizon 9s linear infinite;
      will-change: transform;
  }}
  @keyframes nx-horizon {{
      from {{ transform: translate3d(0, -190px, 0); }}
      to   {{ transform: translate3d(0, 100vh, 0); }}
  }}

  /* The container is a container. It carries no background, no border, no
     frame and no transform.
     Tilting it was a mistake and this is the record of why: a transform on the
     column shrinks every pixel inside it, so the app pulled away from the
     viewport edges and the black beyond it read as margin. The heavy drop
     shadow then framed that gap, and the result was a sunken box rather than
     a floating panel. Depth belongs on the cards, which are small enough to
     float without the whole interface moving with them. */
  .block-container {{
      padding: 2.1rem 2.2rem 4rem 2.2rem;
      max-width: 1320px;
      position: relative;
      z-index: 1;
      background: transparent;
      /* Still the depth field the cards tilt inside - perspective alone moves
         nothing, it only gives the children something to rotate against. */
      perspective: 1400px;
      perspective-origin: 50% 40%;
  }}
  /* Kept from the reverted change: it costs nothing and stops a wide card or
     a long readout from buying a horizontal scrollbar. */
  html, body, .stApp {{ overflow-x: hidden; }}

  /* Nothing sits between the background and the interface now, so text has to
     hold its own against a moving starfield. A tight shadow does that without
     putting a panel back. */
  .block-container h1, .block-container h2, .block-container h3,
  .block-container p, .block-container label, .nx-hero, .nx-section {{
      text-shadow: 0 1px 12px rgba(0, 0, 0, 0.85);
  }}

  /* Holographic panels. The transform itself is written inline by the runtime
     so each card can lean toward the actual cursor, which CSS alone cannot
     express - there is no selector for "how far is the pointer from this
     element's centre". A :hover rule here would win against nothing and lose
     to the inline style, so the CSS only carries the easing and the hint.
     The runtime clears the inline transform on exit, and this transition is
     what makes that return smooth instead of a snap. */
  div[data-testid="stMetric"],
  div[data-testid="metric-container"],
  div[data-testid="stDataFrame"] {{
      transform-style: preserve-3d;
      transition: transform 0.45s {EASE}, border-color 0.3s {EASE};
      will-change: transform;
  }}
  /* While the cursor is over a card the runtime updates the tilt every frame,
     so the easing has to get out of the way or every move would lag behind by
     almost half a second. */
  div[data-testid="stMetric"].nx-tilting,
  div[data-testid="metric-container"].nx-tilting,
  div[data-testid="stDataFrame"].nx-tilting {{
      transition: none;
  }}
  /* This app renders no sidebar today (nothing calls st.sidebar), so this is
     insurance rather than styling: .stApp is transparent now, and a sidebar
     added later would otherwise sit directly on the rotating core with its
     text unreadable. Same glass as the main column. */
  section[data-testid="stSidebar"] {{
      position: relative;
      z-index: 1;
      background: rgba(2, 2, 5, 0.72);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      border-right: 1px solid {GLASS_EDGE};
  }}
  div[data-testid="stVerticalBlock"] > div {{ gap: 0.4rem; }}

  /* ---- Holographic panels ----------------------------------------------- */
  /* The glass. Each card is its own pane with the core visible through it,
     which is what produces depth without anything having to move. */
  div[data-testid="stMetric"],
  div[data-testid="metric-container"],
  div[data-testid="stExpander"],
  div[data-testid="stNotification"],
  div[data-testid="stAlert"],
  .nx-badge,
  [data-testid="stTabs"] [role="tablist"] {{
      background: rgba(0, 15, 30, 0.40);
      backdrop-filter: blur(12px) saturate(120%);
      -webkit-backdrop-filter: blur(12px) saturate(120%);
      border: 1px solid {GLASS_EDGE};
      border-radius: 12px;
      box-shadow: {HOLO};
  }}

  div[data-testid="stMetric"],
  div[data-testid="metric-container"] {{
      padding: 1.15rem 1.25rem 1.0rem 1.25rem;
      transition: transform 0.3s {EASE}, border-color 0.3s {EASE},
                  background-size 0.3s {EASE};
      position: relative;
      overflow: hidden;

      /* HUD targeting brackets: four L-shaped corners, drawn as gradient
         layers on the card itself. ::before is the top hairline and ::after
         is the radar sweep - both already spoken for - so the brackets take
         the background instead of a third pseudo-element.
         Each corner is two thin bars (one horizontal, one vertical) pinned to
         its corner with background-position. */
      background-image:
        /* top-left */
        linear-gradient({ACCENT}, {ACCENT}), linear-gradient({ACCENT}, {ACCENT}),
        /* top-right */
        linear-gradient({ACCENT}, {ACCENT}), linear-gradient({ACCENT}, {ACCENT}),
        /* bottom-left */
        linear-gradient({ACCENT}, {ACCENT}), linear-gradient({ACCENT}, {ACCENT}),
        /* bottom-right */
        linear-gradient({ACCENT}, {ACCENT}), linear-gradient({ACCENT}, {ACCENT});
      background-repeat: no-repeat;
      background-size:
        18px 2px, 2px 18px,
        18px 2px, 2px 18px,
        18px 2px, 2px 18px,
        18px 2px, 2px 18px;
      background-position:
        left 7px top 7px,    left 7px top 7px,
        right 7px top 7px,   right 7px top 7px,
        left 7px bottom 7px, left 7px bottom 7px,
        right 7px bottom 7px, right 7px bottom 7px;
  }}
  /* Lock-on: the brackets grow when the card is targeted. */
  div[data-testid="stMetric"]:hover,
  div[data-testid="metric-container"]:hover {{
      background-size:
        26px 2px, 2px 26px,
        26px 2px, 2px 26px,
        26px 2px, 2px 26px,
        26px 2px, 2px 26px;
  }}
  /* Hairline of cyan along the top edge of every card. */
  div[data-testid="stMetric"]::before,
  div[data-testid="metric-container"]::before {{
      content: "";
      position: absolute; inset: 0 0 auto 0; height: 1px;
      background: linear-gradient(90deg, transparent, {ACCENT}, transparent);
      opacity: 0.55;
  }}
  /* The radar sweep: a narrow cyan band crossing the card, forever. Pure
     transform, so a card that is repainted by a fragment tick mid-sweep does
     not stutter. */
  div[data-testid="stMetric"]::after,
  div[data-testid="metric-container"]::after {{
      content: "";
      position: absolute; top: 0; bottom: 0; left: 0;
      width: 38%;
      background: linear-gradient(90deg,
        transparent, rgba(0,243,255,0.13), rgba(0,243,255,0.02), transparent);
      transform: translate3d(-140%, 0, 0);
      animation: nx-sweep 4.6s ease-in-out infinite;
      pointer-events: none;
      will-change: transform;
  }}
  @keyframes nx-sweep {{
      0%   {{ transform: translate3d(-140%, 0, 0); }}
      60%  {{ transform: translate3d(320%, 0, 0); }}
      100% {{ transform: translate3d(320%, 0, 0); }}
  }}
  div[data-testid="stMetric"]:hover,
  div[data-testid="metric-container"]:hover {{
      transform: translateY(-4px);
      border-color: {GLASS_EDGE_HOVER};
  }}
  /* A <label>, not a <div> - the tag-qualified form matches nothing. */
  [data-testid="stMetricLabel"] {{
      color: {ACCENT}B0;
      font-size: 0.72rem;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      font-weight: 600;
  }}
  div[data-testid="stMetricValue"] {{
      font-size: 2.05rem;
      font-weight: 700;
      line-height: 1.15;
      color: {INK};
      font-variant-numeric: tabular-nums;
      text-shadow: 0 0 18px rgba(0, 243, 255, 0.35);
  }}

  /* ---- Buttons ---------------------------------------------------------- */
  .stButton > button, .stDownloadButton > button {{
      height: 3.4rem;
      font-size: 1.02rem;
      font-weight: 650;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      border-radius: 12px;
      border: 1px solid {GLASS_EDGE};
      background: {GLASS};
      color: {INK};
      box-shadow: {HOLO};
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      transition: transform 0.3s {EASE}, border-color 0.3s {EASE};
  }}
  .stButton > button:hover, .stDownloadButton > button:hover {{
      transform: translateY(-4px);
      border-color: {GLASS_EDGE_HOVER};
  }}

  /* The primary call to action carries a slow, continuously shifting gradient
     so the one button that matters is never mistaken for furniture. */
  .stButton > button[kind="primary"],
  [data-testid="stBaseButton-primary"] {{
      background: linear-gradient(110deg, {ACCENT_DEEP}, {ACCENT}, {VIOLET},
                                  {ACCENT}, {ACCENT_DEEP});
      background-size: 320% 100%;
      animation: nx-flow 9s ease infinite;
      border: 1px solid rgba(0,243,255,0.55);
      color: {VOID};
      font-weight: 750;
      box-shadow: 0 0 22px rgba(0,243,255,0.35);
  }}
  .stButton > button[kind="primary"]:hover,
  [data-testid="stBaseButton-primary"]:hover {{
      transform: translateY(-4px);
      animation-duration: 4s;
  }}
  @keyframes nx-flow {{
      0%   {{ background-position:   0% 50%; }}
      50%  {{ background-position: 100% 50%; }}
      100% {{ background-position:   0% 50%; }}
  }}

  /* ---- Tabs ------------------------------------------------------------- */
  /* Streamlit 1.64 renders tabs through react-aria, not BaseWeb: the old
     div[data-baseweb="tab-list"] selectors match zero elements on this
     version. Verified against the live DOM. */
  [data-testid="stTabs"] [role="tablist"] {{
      gap: 0.25rem;
      background: {GLASS};
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid {GLASS_EDGE};
      border-radius: 12px;
      box-shadow: {HOLO};
      padding: 0.3rem;
  }}
  [data-testid="stTab"] {{
      font-size: 0.92rem;
      letter-spacing: 0.10em;
      text-transform: uppercase;
      padding: 0.58rem 1.0rem;
      border-radius: 9px;
      color: {MUTED};
      transition: background 0.3s {EASE}, color 0.3s {EASE};
  }}
  [data-testid="stTab"]:hover {{ background: rgba(0,243,255,0.07); color: {INK}; }}
  [data-testid="stTab"][aria-selected="true"],
  [data-testid="stTab"][data-selected="true"] {{
      background: rgba(0,243,255,0.14);
      color: {ACCENT};
      box-shadow: inset 0 0 18px rgba(0,243,255,0.12);
      text-shadow: 0 0 12px rgba(0,243,255,0.55);
  }}
  /* The stock underline fights the pill. */
  [data-testid="stTabs"] [role="tablist"] + div {{
      background: transparent;
  }}

  /* ---- Inputs ----------------------------------------------------------- */
  /* Same react-aria migration as the tabs: these are the wrappers Streamlit
     1.64 actually emits. */
  [data-testid="stTextInputRootElement"],
  [data-testid="stTextAreaRootElement"],
  [data-testid="stNumberInputContainer"],
  [data-testid="stSelectbox"] .react-aria-ComboBox > div,
  [data-testid="stSelectbox"] [role="button"] {{
      background: rgba(4, 10, 17, 0.82) !important;
      border: 1px solid {GLASS_EDGE} !important;
      border-radius: 10px !important;
      transition: border-color 0.3s {EASE}, box-shadow 0.3s {EASE};
  }}
  [data-testid="stTextInputRootElement"]:focus-within,
  [data-testid="stTextAreaRootElement"]:focus-within,
  [data-testid="stNumberInputContainer"]:focus-within,
  [data-testid="stSelectbox"] .react-aria-ComboBox > div:focus-within {{
      border-color: {ACCENT} !important;
      box-shadow: {HOLO};
  }}
  [data-testid="stWidgetLabel"] p {{
      color: {MUTED};
      font-size: 0.78rem;
      letter-spacing: 0.10em;
      text-transform: uppercase;
      font-weight: 600;
  }}

  /* ---- Segmented control (source picker) -------------------------------- */
  [data-testid="stButtonGroup"] button[aria-pressed="true"],
  [data-testid="stButtonGroup"] button[data-selected="true"] {{
      background: rgba(0,243,255,0.16) !important;
      color: {ACCENT} !important;
      border-color: {ACCENT}88 !important;
      box-shadow: inset 0 0 18px rgba(0,243,255,0.12);
  }}

  /* ---- Data grid: solid surface, holographic frame ---------------------- */
  /* Border and glow only. The background stays opaque on purpose - the grid
     is virtualised and a blur behind it makes the rows look muddy. */
  div[data-testid="stDataFrame"], div[data-testid="stTable"] {{
      background: rgba(0, 12, 24, 0.82);
      border: 1px solid {GLASS_EDGE};
      border-radius: 12px;
      box-shadow: {HOLO};
      overflow: hidden;
      transition: border-color 0.3s {EASE};
  }}
  div[data-testid="stDataFrame"]:hover {{ border-color: {GLASS_EDGE_HOVER}; }}

  /* Progress bar reads as a status light, not decoration. */
  [data-testid="stProgress"] > div > div > div > div {{
      background-image: linear-gradient(90deg, {ACCENT}, {SUCCESS});
      box-shadow: 0 0 12px rgba(0, 243, 255, 0.55);
  }}

  hr {{ margin: 1.4rem 0; border-color: rgba(0,243,255,0.12); }}
  .stCaption, div[data-testid="stCaptionContainer"] {{ color: {MUTED}; }}

  /* ---- Custom blocks ---------------------------------------------------- */
  .nx-log {{
      background: rgba(2, 7, 12, 0.90);
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      border: 1px solid {GLASS_EDGE};
      border-left: 2px solid {ACCENT};
      border-radius: 12px;
      box-shadow: {HOLO};
      padding: 0.8rem 1rem;
      font-family: {MONO_FONT};
      font-size: 0.82rem;
      line-height: 1.55;
      color: #9FE9F5;
      max-height: 240px;
      overflow-y: auto;
      overscroll-behavior: contain;
      /* Deliberately NOT smooth. The panel is redrawn every second during a
         run, and an animated scroll would still be gliding when the next tick
         replaces the node - the view lags permanently behind the newest line,
         and a programmatic scrollTop read back mid-animation returns a stale
         value, which also makes the pin logic misfire. Instant is correct for
         a terminal. */
      scroll-behavior: auto;
      white-space: pre-wrap;
  }}
  .nx-hero {{
      font-size: 1.8rem; font-weight: 750; margin: 0 0 0.15rem 0;
      letter-spacing: 0.02em;
      color: {INK};
      text-shadow: 0 0 26px rgba(0, 243, 255, 0.45);
  }}
  .nx-hero .nx-mark {{ color: {ACCENT}; }}
  .nx-sub {{
      color: {MUTED}; margin: 0 0 1.25rem 0;
      letter-spacing: 0.04em;
  }}
  .nx-section {{
      color: {ACCENT};
      font-size: 0.72rem;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      font-weight: 700;
      margin: 1.25rem 0 0.5rem 0;
      opacity: 0.85;
  }}

  .nx-check {{
      display: flex; align-items: flex-start; gap: 0.7rem;
      background: {GLASS};
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      border: 1px solid {GLASS_EDGE};
      border-left-width: 3px;
      border-radius: 12px;
      box-shadow: {HOLO};
      padding: 0.62rem 0.9rem;
      margin-bottom: 0.4rem;
      transition: transform 0.3s {EASE}, border-color 0.3s {EASE};
  }}
  .nx-check:hover {{ transform: translateY(-2px); border-color: {GLASS_EDGE_HOVER}; }}
  .nx-check.ok {{ border-left-color: {SUCCESS}; }}
  .nx-check.warn {{ border-left-color: {WARNING}; }}
  .nx-check.fail {{ border-left-color: {DANGER}; }}
  .nx-check .dot {{
      width: 9px; height: 9px; border-radius: 50%;
      margin-top: 0.42rem; flex: 0 0 9px;
      animation: nx-breathe 2.6s ease-in-out infinite;
  }}
  .nx-check.ok .dot {{ background: {SUCCESS}; box-shadow: 0 0 12px {SUCCESS}; }}
  .nx-check.warn .dot {{ background: {WARNING}; box-shadow: 0 0 12px {WARNING}; }}
  .nx-check.fail .dot {{ background: {DANGER}; box-shadow: 0 0 12px {DANGER}; }}
  .nx-check .body {{ flex: 1 1 auto; min-width: 0; }}
  .nx-check .name {{ font-weight: 600; font-size: 0.93rem; color: {INK}; }}
  .nx-check .detail {{
      color: {MUTED}; font-size: 0.82rem; margin-top: 0.12rem;
      word-break: break-word;
  }}

  .nx-wait {{
      position: relative;
      overflow: hidden;
      background: {GLASS};
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid {GLASS_EDGE};
      border-radius: 12px;
      box-shadow: {HOLO_HOT};
      padding: 0.95rem 1.15rem;
      margin-bottom: 0.55rem;
  }}
  /* The waiting banner gets its own sweep - it is the one panel on screen
     when the worker is deliberately idle between sends. */
  .nx-wait::after {{
      content: "";
      position: absolute; top: 0; bottom: 0; left: 0; width: 30%;
      background: linear-gradient(90deg,
        transparent, rgba(255,170,0,0.10), transparent);
      transform: translate3d(-120%, 0, 0);
      animation: nx-sweep 3.4s ease-in-out infinite;
      pointer-events: none;
      will-change: transform;
  }}
  .nx-wait .title {{
      font-weight: 700; font-size: 1.0rem; color: {INK};
      display: flex; align-items: center; gap: 0.55rem;
      position: relative; z-index: 1;
  }}
  .nx-wait .sub {{
      color: {MUTED}; font-size: 0.85rem; margin-top: 0.2rem;
      position: relative; z-index: 1;
  }}
  .nx-pulse {{
      width: 10px; height: 10px; border-radius: 50%; background: {WARNING};
      box-shadow: 0 0 14px {WARNING};
      animation: nx-breathe 1.9s ease-in-out infinite;
  }}
  .nx-pulse.sending {{ background: {SUCCESS}; box-shadow: 0 0 14px {SUCCESS}; }}
  @keyframes nx-breathe {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.35; }}
  }}

  /* ---- Kill-feed terminal ------------------------------------------------ */
  /* Fed by a WebSocket, not by a rerun. It sits above the interface and below
     the boot screen, and never takes a pointer event except on its own
     scrollbar. */
  /* A command rail welded to the bottom edge of the viewport, full width.
     The previous version was a floating box parked over the middle of the
     page, which is exactly where the interface is. */
  /* An inline console. The runtime appends it to .block-container as the last
     element, so it sits in the document flow and pushes nothing aside - the
     previous version was fixed to the corner and sat over the campaign
     textareas, which made the tab unusable while a feed was running.
     Everything is forced because this element has to hold its shape against
     whatever Streamlit's stylesheet does to an unknown id inside its own
     container. */
  #kill-feed {{
      position: relative !important;
      width: 100% !important;
      height: 180px !important;
      background: rgba(0, 15, 30, 0.25) !important;
      backdrop-filter: blur(12px) !important;
      -webkit-backdrop-filter: blur(12px) !important;
      border: 1px solid rgba(0, 243, 255, 0.15) !important;
      border-top: 1px solid rgba(0, 243, 255, 0.4) !important;
      border-radius: 8px !important;
      box-shadow: inset 0 0 20px rgba(0, 243, 255, 0.05) !important;
      padding: 15px !important;
      padding-top: 30px !important;   /* clears the label strip */
      margin-top: 30px !important;
      margin-bottom: 30px !important;
      display: flex !important;
      flex-direction: column-reverse !important;
      overflow-y: hidden !important;
      z-index: 10 !important;

      font-family: {MONO_FONT};
      font-size: 11px;
      line-height: 1.65;
      letter-spacing: 0.04em;
      color: {ACCENT};
      transition: opacity 0.35s {EASE};
      opacity: 0;
  }}
  /* Until the first line lands there is nothing to show, and an empty console
     holding 240px of vertical space at the bottom of every tab is worse than
     no console. It takes no room until it has something to say. */
  #kill-feed:not(.live) {{ display: none !important; }}
  #kill-feed.live {{ opacity: 1; }}

  #kill-feed .hd {{
      position: absolute;
      top: 0; left: 0; right: 0;
      padding: 9px 15px 7px 15px;
      color: {MUTED};
      font-size: 8.5px;
      letter-spacing: 0.24em;
      display: flex;
      justify-content: space-between;
      pointer-events: none;
      border-radius: 8px 8px 0 0;
      /* A fade, so a line scrolling up dissolves under the label rather than
         being cut in half by it. */
      background: linear-gradient(180deg, rgba(0, 15, 30, 0.85) 60%,
                                          rgba(0, 15, 30, 0));
      z-index: 1;
  }}
  #kill-feed .ln {{
      pointer-events: none;
      /* Wraps rather than truncating. At 480px a subject line always ran past
         the edge, and half a line with an ellipsis tells you an email went out
         without telling you to whom. Fewer entries visible, every one of them
         complete. */
      white-space: pre-wrap;
      word-break: break-word;
      margin-bottom: 2px;
      /* The glow is what makes it read as a phosphor terminal rather than
         grey text on a dark bar. */
      text-shadow: 0 0 6px currentColor, 0 0 18px rgba(0, 243, 255, 0.28);
      animation: nx-feed-in 0.3s ease-out;
  }}
  #kill-feed .ln .ts {{ color: {MUTED}; text-shadow: none; }}
  #kill-feed .ln.ok {{ color: {SUCCESS}; }}
  #kill-feed .ln.warn {{ color: {WARNING}; }}
  #kill-feed .ln.fail {{ color: {DANGER}; }}
  #kill-feed .ln.fire {{ color: {ACTIVE}; }}
  /* Lines rise into place, which is the direction the rail reads in. */
  @keyframes nx-feed-in {{
      from {{ opacity: 0; transform: translate3d(0, 10px, 0); }}
      to   {{ opacity: 1; transform: translate3d(0, 0, 0); }}
  }}
  /* The terminal floats clear of the column, so the page needs no allowance
     for it. Hidden on narrow screens, where 450px is no longer "a corner". */
  @media (max-width: 1100px) {{ #kill-feed {{ display: none; }} }}

  /* ---- Voice mute control ------------------------------------------------ */
  /* The one element in the visor that accepts a click. Everything else there
     is pointer-events:none so it can never intercept the interface. */
  .nx-visor .mute {{
      position: absolute;
      right: 18px;
      bottom: 92px;
      pointer-events: auto;
      font-size: 9px;
      letter-spacing: 0.18em;
      padding: 5px 9px;
      color: {ACCENT};
      background: rgba(2, 7, 12, 0.6);
      border: 1px solid rgba(0, 243, 255, 0.28);
      border-radius: 4px;
      user-select: none;
      transition: color 0.2s {EASE}, border-color 0.2s {EASE};
  }}
  .nx-visor .mute:hover {{ border-color: {ACCENT}; }}
  .nx-visor .mute.off {{ color: {MUTED}; border-color: rgba(255,255,255,0.14); }}
  @media (max-width: 1423px) {{ .nx-visor .mute {{ display: none; }} }}

  /* ---- Ghost Protocol: the system goes dark ------------------------------ */
  /* Everything desaturates toward grey and the accents bleed to blood red.
     Driven by a class on <html>, so one toggle repaints the whole surface
     without touching a single component. */
  .nx-ghost {{ --nx-ghost-accent: {GHOST_RED}; }}
  .nx-ghost.nx-ghost .nx-visor,
  .nx-ghost.nx-ghost .nx-visor .readout,
  .nx-ghost.nx-ghost .nx-visor .rail {{ color: {GHOST_RED}; }}
  .nx-ghost.nx-ghost .nx-visor .corner::before {{
      border-color: {GHOST_RED};
      box-shadow: 0 0 12px rgba(155, 17, 30, 0.5);
  }}
  .nx-ghost #kill-feed {{
      color: {GHOST_RED};
      border-color: rgba(155, 17, 30, 0.35);
      border-left-color: {GHOST_RED};
  }}
  .nx-ghost.nx-ghost .nx-badge {{
      color: {GHOST_RED};
      border-color: rgba(155, 17, 30, 0.6);
      background: rgba(155, 17, 30, 0.07);
      box-shadow: 0 0 12px rgba(155, 17, 30, 0.28);
  }}
  .nx-ghost.nx-ghost .nx-badge .beacon {{
      background: {GHOST_RED};
      box-shadow: 0 0 12px {GHOST_RED};
  }}
  /* The interface itself dims and loses its colour. filter on one element is
     a single composited pass, which is why this costs nothing. */
  .nx-ghost .block-container {{
      filter: grayscale(0.72) brightness(0.82);
      border-color: rgba(155, 17, 30, 0.22);
  }}
  .nx-ghost canvas.nx-canvas {{ opacity: 0.32; }}
  .nx-ghost .nx-crt {{
      box-shadow:
        inset 0 0 150px rgba(0, 0, 0, 0.8),
        inset 0 0 30px rgba(155, 17, 30, 0.10);
  }}

  /* ---- Crosshair cursor --------------------------------------------------- */
  /* The reticle and the HUD I-beam, defined once and referenced everywhere.
     Inline SVG data URIs, so they cost no request. Hotspots are dead centre
     (12,12): an offset hotspot makes every click feel like it landed
     somewhere other than where it was aimed. */
  :root {{
      --nx-reticle: url("data:image/svg+xml;utf8,\
<svg xmlns='http://www.w3.org/2000/svg' width='24' height='24'>\
<circle cx='12' cy='12' r='8' fill='none' stroke='%2300f3ff' stroke-width='1' opacity='0.85'/>\
<circle cx='12' cy='12' r='1.6' fill='%2300f3ff'/>\
<path d='M12 0v4M12 20v4M0 12h4M20 12h4' stroke='%2300f3ff' stroke-width='1' opacity='0.7'/>\
</svg>") 12 12, crosshair;
      /* The typing cursor: a cyan bar with serifs, so a text field still reads
         as a text field without handing the native I-beam back. */
      --nx-ibeam: url("data:image/svg+xml;utf8,\
<svg xmlns='http://www.w3.org/2000/svg' width='24' height='24'>\
<path d='M12 4v16' stroke='%2300f3ff' stroke-width='1.4' opacity='0.95'/>\
<path d='M8 4h8M8 20h8' stroke='%2300f3ff' stroke-width='1.2' opacity='0.8'/>\
<circle cx='12' cy='12' r='1' fill='%2300f3ff' opacity='0.9'/>\
</svg>") 12 12, text;
  }}

  /* Everything, in every state. The native pointer was never a flash: it was
     this stylesheet handing control back. Buttons, tabs, sliders and links
     were set to `pointer`, which IS the operating system hand, and html, body
     and the body-level overlays were never claimed at all, so they fell
     through to the arrow. Both are closed here.
     The universal selector is deliberate. A HUD that drops the reticle over
     one unclaimed element breaks the illusion exactly as badly as one that
     drops it everywhere, and new elements arrive with every Streamlit
     release. */
  *, *::before, *::after,
  *:hover, *:active, *:focus, *:focus-visible, *:disabled {{
      cursor: var(--nx-reticle) !important;
  }}

  /* Typing surfaces keep a caret - the HUD one. Listed after the universal
     rule so it wins at equal specificity. */
  /* Named rather than excluded. A blacklist let input[type=range] through and
     put a text caret on the sliders; listing the types that are actually
     typed into cannot drift that way when a new input type appears. */
  input[type="text"], input[type="text"]:hover, input[type="text"]:focus,
  input[type="email"], input[type="search"], input[type="password"],
  input[type="number"], input[type="tel"], input[type="url"],
  input:not([type]), input:not([type]):hover, input:not([type]):focus,
  textarea, textarea:hover, textarea:focus,
  [contenteditable="true"], [contenteditable="true"]:hover,
  [data-testid="stTextInputRootElement"], [data-testid="stTextInputRootElement"] *,
  [data-testid="stTextAreaRootElement"], [data-testid="stTextAreaRootElement"] * {{
      cursor: var(--nx-ibeam) !important;
  }}

  /* Cursors that carry information rather than affordance stay as they are.
     col-resize on a grid edge tells the operator a column can be dragged;
     replacing it with a reticle removes a control, which is a different
     problem from the one being fixed here. */
  [data-testid="stDataFrame"] [class*="resize"],
  [class*="col-resize"], [class*="row-resize"],
  .nx-resize-handle {{
      cursor: col-resize !important;
  }}

  /* ---- Click pulse --------------------------------------------------------- */
  /* A lock-on ring at the point of contact. Pure transform and opacity, so it
     composites and never touches layout, and it removes itself when the
     animation ends rather than accumulating a node per click. */
  .nx-pulse-ring {{
      position: fixed;
      width: 18px;
      height: 18px;
      margin: -9px 0 0 -9px;
      border: 1px solid {ACCENT};
      border-radius: 50%;
      pointer-events: none;
      z-index: 999998;
      opacity: 0.9;
      box-shadow: 0 0 12px rgba(0, 243, 255, 0.55);
      animation: nx-lockpulse 0.4s cubic-bezier(0.2, 0.7, 0.3, 1) forwards;
  }}
  @keyframes nx-lockpulse {{
      from {{ transform: scale(0.35); opacity: 0.95; }}
      to   {{ transform: scale(4.2);  opacity: 0; }}
  }}
  .nx-ghost .nx-pulse-ring {{
      border-color: {GHOST_RED};
      box-shadow: 0 0 12px rgba(155, 17, 30, 0.5);
  }}

  /* ---- CRT curvature: the glass of the helmet ---------------------------- */
  /* Two things at once. The radial gradient darkens the corners the way a
     curved screen falls away from the eye, and the inset shadow fakes the
     bevel where that glass meets its housing. Both sit above the interface and
     neither can ever take a pointer event. */
  /* The visor. Softened: the previous pass darkened the corners to 62% and
     laid a 120px black inset over the whole frame, which dimmed the interface
     it was supposed to sit in front of. A vignette should be felt at the very
     edges and nowhere else. */
  .nx-crt {{
      position: fixed;
      inset: 0;
      z-index: 3;
      pointer-events: none;
      background:
        radial-gradient(135% 135% at 50% 50%,
          transparent 68%, rgba(0, 0, 0, 0.10) 88%, rgba(0, 0, 0, 0.30) 100%);
      box-shadow:
        inset 0 0 90px rgba(0, 0, 0, 0.22),
        inset 0 0 20px rgba(0, 243, 255, 0.04);
  }}
  /* The scanline film, at roughly a third of its old weight. A CRT you notice
     is a CRT that is in the way. */
  .nx-crt::after {{
      content: "";
      position: absolute;
      inset: 0;
      background: repeating-linear-gradient(0deg,
        rgba(0, 0, 0, 0.09) 0 1px, transparent 1px 4px);
      opacity: 0.22;
  }}

  /* ---- Boot sequence ------------------------------------------------------ */
  .nx-boot {{
      position: fixed;
      inset: 0;
      z-index: 9999;
      /* Never takes a click, even mid-animation: a boot screen that swallowed
         the first click of a session would be a bug, not a flourish. */
      pointer-events: none;
      background: {VOID};
      display: flex;
      align-items: center;
      justify-content: center;
      font-family: {MONO_FONT};
      color: {ACCENT};
      transform-origin: 50% 50%;
  }}
  .nx-boot .lines {{
      font-size: 13px;
      line-height: 2.1;
      letter-spacing: 0.22em;
      text-transform: uppercase;
      text-shadow: 0 0 14px rgba(0, 243, 255, 0.55);
      white-space: pre;
      min-width: 32ch;
  }}
  .nx-boot .lines b {{ color: {SUCCESS}; font-weight: 400; }}
  .nx-boot .caret {{
      display: inline-block;
      width: 8px; height: 1em;
      background: {ACCENT};
      vertical-align: text-bottom;
      animation: nx-caret 0.9s steps(1, end) infinite;
  }}
  /* The snap: a fast scale past 1 with the opacity going first, so the panel
     appears to be pulled off the screen rather than faded out behind it. */
  .nx-boot.done {{
      animation: nx-boot-snap 0.55s cubic-bezier(0.6, 0, 0.2, 1) forwards;
  }}
  @keyframes nx-boot-snap {{
      0%   {{ transform: scale(1); opacity: 1; filter: blur(0); }}
      45%  {{ transform: scale(1.04); opacity: 0.85; }}
      100% {{ transform: scale(1.35); opacity: 0; filter: blur(6px); }}
  }}

  /* ---- The visor: peripheral HUD overlays -------------------------------- */
  /* One fixed, non-interactive layer pinned to the viewport edges. It sits
     ABOVE the interface on z, but every part of it is pointer-events:none and
     confined to the margins outside the 1320px column, so it can never take a
     click or cover a control. Below 1500px the side rails are removed
     entirely rather than allowed to crowd the layout. */
  .nx-visor {{
      position: fixed;
      inset: 0;
      z-index: 2;
      pointer-events: none;
      font-family: {MONO_FONT};
      color: {ACCENT};
      text-transform: uppercase;
  }}
  .nx-visor .corner {{
      position: absolute;
      width: 132px; height: 62px;
      opacity: 0.62;
  }}
  .nx-visor .corner::before {{
      content: "";
      position: absolute;
      width: 100%; height: 100%;
      border: 1px solid {ACCENT};
      box-shadow: 0 0 12px rgba(0, 243, 255, 0.35);
  }}
  .nx-visor .tl {{ top: 14px; left: 14px; }}
  .nx-visor .tl::before {{ border-right: 0; border-bottom: 0; }}
  .nx-visor .tr {{ top: 14px; right: 14px; }}
  .nx-visor .tr::before {{ border-left: 0; border-bottom: 0; }}
  .nx-visor .bl {{ bottom: 14px; left: 14px; }}
  .nx-visor .bl::before {{ border-right: 0; border-top: 0; }}
  .nx-visor .br {{ bottom: 14px; right: 14px; }}
  .nx-visor .br::before {{ border-left: 0; border-top: 0; }}

  .nx-visor .readout {{
      position: absolute;
      font-size: 9.5px;
      letter-spacing: 0.16em;
      line-height: 1.5;
      white-space: pre;
      text-shadow: 0 0 10px rgba(0, 243, 255, 0.5);
      animation: nx-flicker 6s steps(1, end) infinite;
  }}
  .nx-visor .tl .readout {{ top: 10px; left: 12px; }}
  .nx-visor .tr .readout {{ top: 10px; right: 12px; text-align: right; }}
  .nx-visor .bl .readout {{ bottom: 10px; left: 12px; }}
  .nx-visor .br .readout {{ bottom: 10px; right: 12px; text-align: right; }}
  .nx-visor .br .readout {{ color: {WARNING}; text-shadow: 0 0 10px rgba(255,170,0,0.5); }}
  /* Opacity only - a flicker that never touches layout. */
  @keyframes nx-flicker {{
      0%, 96%, 100% {{ opacity: 1; }}
      97%           {{ opacity: 0.35; }}
      98%           {{ opacity: 0.85; }}
      99%           {{ opacity: 0.5; }}
  }}

  /* Side rails: scrolling hex in the dead margin outside the column. */
  .nx-visor .rail {{
      position: absolute;
      top: 0; bottom: 0;
      width: 76px;
      overflow: hidden;
      opacity: 0.30;
      font-size: 9px;
      line-height: 1.75;
      letter-spacing: 0.08em;
      color: {ACCENT};
      mask-image: linear-gradient(180deg, transparent, #000 18%, #000 82%, transparent);
      -webkit-mask-image: linear-gradient(180deg, transparent, #000 18%, #000 82%, transparent);
  }}
  .nx-visor .rail.left {{ left: 10px; text-align: left; }}
  .nx-visor .rail.right {{ right: 10px; text-align: right; }}
  .nx-visor .rail .stream {{
      position: absolute;
      top: 0; left: 0; right: 0;
      white-space: pre;
      animation: nx-stream 34s linear infinite;
      will-change: transform;
  }}
  .nx-visor .rail.right .stream {{ animation-duration: 47s; animation-direction: reverse; }}
  @keyframes nx-stream {{
      from {{ transform: translate3d(0, 0, 0); }}
      to   {{ transform: translate3d(0, -50%, 0); }}
  }}
  /* A vertical sweep riding each rail. */
  .nx-visor .rail::after {{
      content: "";
      position: absolute;
      left: 0; right: 0; height: 120px;
      background: linear-gradient(180deg, transparent,
                  rgba(0,243,255,0.22), transparent);
      animation: nx-rail-sweep 7s linear infinite;
      will-change: transform;
  }}
  @keyframes nx-rail-sweep {{
      from {{ transform: translate3d(0, -140px, 0); }}
      to   {{ transform: translate3d(0, 100vh, 0); }}
  }}

  /* Narrow screens, in the order things give way. The thresholds are measured,
     not guessed: the column is 1320px wide and centred, and a full corner needs
     about 146px of margin, so below 1612px a full corner starts sitting on the
     tabs, the slider and the buttons. Verified by hit-testing every corner
     against every real control at six widths.
     A compact bracket still needs ~52px of margin, and the column stops
     leaving that below 1424px, so that is where the corners stand down.
       >= 1612px   full corners with live readouts, rails from 1500px up
       1424-1611px bracket only - the frame survives, the text stands down
       <  1424px   the visor stands down entirely */
  @media (max-width: 1500px) {{
      .nx-visor .rail {{ display: none; }}
  }}
  @media (max-width: 1611px) {{
      .nx-visor .corner {{ width: 46px; height: 34px; }}
      .nx-visor .tl, .nx-visor .bl {{ left: 6px; }}
      .nx-visor .tr, .nx-visor .br {{ right: 6px; }}
      .nx-visor .readout {{ display: none; }}
  }}
  @media (max-width: 1423px) {{
      .nx-visor .corner {{ display: none; }}
  }}

  /* ---- Engine-active overdrive ------------------------------------------ */
  /* The runtime adds .nx-active to <html> while a worker thread is running and
     removes it the moment the job ends. Everything below is a repaint of
     existing pixels - no layout, no new elements - so flipping state mid-run
     costs nothing. The marker is emitted by the live panel, which is a 1s
     fragment, so the class tracks the real job rather than a guess. */
  .nx-active .nx-visor {{ color: {ACTIVE}; }}
  .nx-active .nx-visor .corner::before {{
      border-color: {ACTIVE};
      box-shadow: 0 0 16px rgba(57, 255, 136, 0.55);
  }}
  .nx-active .nx-visor .readout {{
      color: {ACTIVE};
      text-shadow: 0 0 10px rgba(57, 255, 136, 0.55);
  }}
  .nx-active .nx-visor .br .readout {{
      color: {WARNING};
      text-shadow: 0 0 10px rgba(255, 170, 0, 0.5);
  }}
  .nx-active .nx-visor .rail {{ color: {ACTIVE}; opacity: 0.38; }}
  .nx-active .nx-visor .rail::after {{
      background: linear-gradient(180deg, transparent,
                  rgba(57, 255, 136, 0.30), transparent);
      animation-duration: 3.2s;      /* the sweep speeds up while firing */
  }}
  .nx-active .nx-badge {{
      border-color: {ACTIVE}88;
      color: {ACTIVE};
      background: rgba(57, 255, 136, 0.06);
      box-shadow: 0 0 14px rgba(57, 255, 136, 0.30),
                  inset 0 0 22px rgba(57, 255, 136, 0.06);
      animation-duration: 1.4s;      /* pulse quickens */
  }}
  .nx-active .nx-badge .beacon {{
      background: {ACTIVE};
      box-shadow: 0 0 14px {ACTIVE};
      animation-duration: 0.9s;
  }}
  .nx-active div[data-testid="stMetric"]::after,
  .nx-active div[data-testid="metric-container"]::after {{
      animation-duration: 2.0s;      /* card radar sweeps faster */
  }}
  .nx-active .nx-rings::before {{ animation-duration: 3.4s; }}
  .nx-active .nx-rings::after  {{ animation-duration: 5.0s; }}
  /* The state marker itself is data, not decoration. */
  .nx-state {{ display: none; }}

  /* ---- Lock-on: a zone is acquired on the radar -------------------------- */
  /* Amber rather than the firing green: a zone is a selection, not a send.
     The corners blink; nothing else on the page moves, so the blink reads as
     an alert instead of noise. */
  .nx-lock .nx-visor .corner::before {{
      border-color: {WARNING};
      box-shadow: 0 0 18px rgba(255, 170, 0, 0.6);
      animation: nx-lockblink 1.1s steps(1, end) infinite;
  }}
  .nx-lock .nx-visor .readout {{
      color: {WARNING};
      text-shadow: 0 0 10px rgba(255, 170, 0, 0.6);
  }}
  .nx-lock .nx-visor .tl .readout {{
      animation: nx-lockblink 1.1s steps(1, end) infinite;
  }}
  .nx-lock .nx-visor .rail {{ color: {WARNING}; }}
  @keyframes nx-lockblink {{
      0%, 59%   {{ opacity: 1; }}
      60%, 100% {{ opacity: 0.32; }}
  }}

  /* ---- Mouse-tracking spotlight ----------------------------------------- */
  /* --mouse-x / --mouse-y are written to the document root once per animation
     frame by the runtime controller, never on the mousemove event itself:
     setting a custom property invalidates style for every rule that reads it,
     and doing that at mousemove rate on a page with 1s and 2s fragments is
     the one thing guaranteed to make this feel cheap.
     The spotlight is a real child element rather than a pseudo-element or a
     background on the card, because the card's ::before is the hairline, its
     ::after is the radar sweep, and its background-image is the eight corner
     bracket layers. All three were built earlier and all three stay. */
  .nx-spot {{
      position: absolute;
      inset: 0;
      z-index: 0;
      pointer-events: none;
      border-radius: inherit;
      /* --spot-x / --spot-y are the cursor expressed in THIS card's own
         coordinates. A gradient resolves its position against the element's
         own box, so feeding it the viewport-relative --mouse-x lights the
         correct spot only on a card sitting at the top-left of the page and
         drifts further off with every card to the right. Measured: cursor at
         x=900 put the gradient origin 622px outside a 278px-wide card.
         --mouse-x / --mouse-y are still published on the root for anything
         that wants page coordinates. */
      background: radial-gradient(600px circle at var(--spot-x, 50%) var(--spot-y, 50%),
                  rgba(0, 243, 255, 0.10), transparent 40%);
      opacity: var(--spot-a, 0);
      transition: opacity 0.25s {EASE};
  }}

  /* ---- Rotating targeting rings ----------------------------------------- */
  /* Its own element, so it owns its own ::before and ::after outright. */
  .nx-rings {{
      position: absolute;
      top: 50%;
      right: 14px;
      width: 104px;
      height: 104px;
      margin-top: -52px;
      z-index: 0;
      pointer-events: none;
      opacity: 0.5;
  }}
  .nx-rings::before, .nx-rings::after {{
      content: "";
      position: absolute;
      border-radius: 50%;
      border: 1px dashed {ACCENT};
      will-change: transform;
  }}
  .nx-rings::before {{
      inset: 0;
      animation: nx-spin-right 10s linear infinite;
  }}
  .nx-rings::after {{
      inset: 18px;
      border-style: dashed;
      border-color: {WARNING};
      opacity: 0.75;
      animation: nx-spin-left 15s linear infinite;
  }}
  @keyframes nx-spin-right {{
      from {{ transform: rotate(0deg); }}
      to   {{ transform: rotate(360deg); }}
  }}
  @keyframes nx-spin-left {{
      from {{ transform: rotate(0deg); }}
      to   {{ transform: rotate(-360deg); }}
  }}
  /* Card content rides above the spotlight and the rings.
     The :not() pair is load-bearing: both injected layers are themselves
     direct div children of the card, so a bare "> div" rule overrode their
     position:absolute with position:relative - which dropped the rings out of
     their corner and onto the label - and lifted them to the content layer.
     Measured: rings landed at x=177 inside a card starting at x=170. */
  div[data-testid="stMetric"] > div:not(.nx-rings):not(.nx-spot) {{
      position: relative;
      z-index: 1;
  }}

  /* ---- Cyber glitch on hover -------------------------------------------- */
  /* transform + text-shadow only, in a short steps() burst: a chromatic split
     that resolves, not a loop. Nothing here reflows. */
  @keyframes nx-glitch {{
      0%   {{ transform: translate3d(0, 0, 0); text-shadow: none; }}
      20%  {{ transform: translate3d(-2px, 1px, 0);
              text-shadow: 2px 0 {DANGER}, -2px 0 {ACCENT}; }}
      40%  {{ transform: translate3d(2px, -1px, 0);
              text-shadow: -2px 0 {DANGER}, 2px 0 {ACCENT}; }}
      60%  {{ transform: translate3d(-1px, 0, 0);
              text-shadow: 1px 0 {ACCENT}, -1px 0 {DANGER}; }}
      100% {{ transform: translate3d(0, 0, 0); text-shadow: none; }}
  }}
  .nx-hero:hover, .nx-section:hover {{
      animation: nx-glitch 0.28s steps(2, end) 1;
  }}
  .stButton > button:hover, .stDownloadButton > button:hover,
  [data-testid="stTab"]:hover {{
      animation: nx-glitch 0.22s steps(2, end) 1;
  }}

  /* ---- System status badge ---------------------------------------------- */
  /* Reports real state. The tone is passed in by status_badge() from
     cfg.smtp.is_complete - an always-green "OPTIMAL" badge sitting above a
     locked sender would be decoration, not instrumentation. */
  .nx-badge {{
      display: inline-flex; align-items: center; gap: 0.6rem;
      font-size: 0.74rem; font-weight: 700;
      letter-spacing: 0.22em; text-transform: uppercase;
      padding: 0.42rem 1.0rem 0.40rem 0.85rem;
      border-radius: 4px;
      margin-bottom: 0.9rem;
      position: relative; overflow: hidden;
      background: rgba(0, 243, 255, 0.05);
      border: 1px solid {ACCENT}66;
      color: {ACCENT};
      box-shadow: {HOLO};
      animation: nx-badge-pulse 3.2s ease-in-out infinite;
  }}
  .nx-badge.warn {{
      background: rgba(255, 170, 0, 0.06);
      border-color: {WARNING}77;
      color: {WARNING};
      box-shadow: 0 0 10px rgba(255,170,0,0.22),
                  inset 0 0 20px rgba(255,170,0,0.05);
  }}
  .nx-badge .beacon {{
      width: 8px; height: 8px; border-radius: 50%;
      background: {SUCCESS}; box-shadow: 0 0 12px {SUCCESS};
      animation: nx-breathe 1.6s ease-in-out infinite;
  }}
  .nx-badge.warn .beacon {{ background: {WARNING}; box-shadow: 0 0 12px {WARNING}; }}
  /* Opacity only, so the pulse composites and never repaints the bar. */
  @keyframes nx-badge-pulse {{
      0%, 100% {{ opacity: 1; }}
      50%      {{ opacity: 0.74; }}
  }}

  /* ---- Terminal treatment for the activity log -------------------------- */
  /* Not a typewriter: .nx-log is multi-line, scrollable, and redrawn by a
     1s fragment, so a steps() type-on would restart every tick and read as a
     glitch. What sells "booting terminal" without fighting the poll is a
     scanline wash over the panel plus a blinking block cursor on the last
     line - both pure CSS, neither touching layout. */
  .nx-log {{
      position: relative;
      background-image:
        repeating-linear-gradient(0deg,
          rgba(0, 243, 255, 0.045) 0 1px, transparent 1px 3px);
      text-shadow: 0 0 8px rgba(0, 243, 255, 0.30);
  }}
  .nx-log::after {{
      content: "";
      display: inline-block;
      width: 8px; height: 1em;
      margin-left: 2px;
      vertical-align: text-bottom;
      background: {ACCENT};
      box-shadow: 0 0 10px {ACCENT};
      animation: nx-caret 1.05s steps(1, end) infinite;
  }}
  @keyframes nx-caret {{
      0%, 49%   {{ opacity: 1; }}
      50%, 100% {{ opacity: 0; }}
  }}

  /* Respect a reduced-motion preference: kill every loop, keep the layout.
     Every keyframe defined above is named here on purpose. */
  @media (prefers-reduced-motion: reduce) {{
      .stApp::before, .stApp::after,
      div[data-testid="stMetric"]::after,
      div[data-testid="metric-container"]::after,
      .nx-wait::after,
      .stButton > button[kind="primary"],
      [data-testid="stBaseButton-primary"],
      .nx-badge, .nx-badge .beacon, .nx-log::after,
      .nx-lock .nx-visor .corner::before,
      .nx-lock .nx-visor .tl .readout,
      .nx-rings::before, .nx-rings::after,
      .nx-hero:hover, .nx-section:hover,
      .stButton > button:hover, .stDownloadButton > button:hover,
      [data-testid="stTab"]:hover,
      .nx-pulse, .nx-check .dot {{ animation: none; }}
      /* The controller checks the same query and never starts the canvas
         loop, but if it is already running this hides the result. */
      canvas.nx-canvas, canvas.nx-core, .nx-spot {{ display: none; }}
      #kill-feed .ln {{ animation: none; }}
      .nx-pulse-ring {{ display: none; }}
      /* A custom cursor is motion the operator did not ask for. */
      *, *:hover, *:active, *:focus {{ cursor: auto !important; }}
      input, textarea, [contenteditable="true"] {{ cursor: text !important; }}
      .nx-boot {{ display: none; }}
      .nx-crt::after {{ display: none; }}
      div[data-testid="stMetric"], div[data-testid="stDataFrame"] {{
          transform: none !important;
      }}
      .nx-visor .readout, .nx-visor .rail .stream,
      .nx-visor .rail::after {{ animation: none; }}
      .nx-visor .rail {{ display: none; }}
      div[data-testid="stMetric"]:hover,
      div[data-testid="metric-container"]:hover {{ transform: none; }}
      .stApp::after {{ opacity: 0; }}
      * {{ transition-duration: 0.01ms !important; }}
  }}
</style>
"""

# Radar mark used in the header. Inline SVG, so it scales and follows theme.
LOGO_SVG = f"""
<svg width="26" height="26" viewBox="0 0 24 24" fill="none"
     stroke="{ACCENT}" stroke-width="1.8" stroke-linecap="round"
     stroke-linejoin="round"
     style="vertical-align:-4px;margin-right:10px;
            filter:drop-shadow(0 0 6px {ACCENT})">
  <path d="M19.07 4.93A10 10 0 1 1 6.99 3.34"/>
  <path d="M15.54 8.46a5 5 0 1 0-6.07-.86"/>
  <line x1="12" y1="12" x2="19" y2="5"/>
</svg>
"""


def apply_theme() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Runtime HUD controller
# ---------------------------------------------------------------------------
# This is the one place the app reaches outside its own iframe.
#
# st.components.v1.html renders a same-origin srcdoc iframe, so window.parent
# .document is reachable and the script can paint into the real page instead of
# a boxed-off frame. That is an unsupported escape hatch: it works today and a
# future Streamlit release could close it. The failure mode is chosen to be
# boring - every feature here is additive decoration, so if the hatch closes
# the background and the spotlight simply stop appearing and the app keeps
# working. Nothing below touches state, widgets or the websocket.
#
# Four behaviours, one script, one animation frame loop:
#   1. a node-network canvas behind the whole page, reacting to the cursor
#   2. --mouse-x / --mouse-y written to the document root, once per frame
#   3. rings and a spotlight element injected into every metric card
#   4. a MutationObserver that puts 3 back after Streamlit re-mounts the DOM
#
# Streamlit re-runs this component on every full rerun, and this app also has
# fragments redrawing at 1s and 2s. So the script guards on a flag stored on
# the parent window: a second copy returns immediately rather than starting a
# second rAF loop and a second observer.
_HUD_JS = r"""
<script>
(function () {
  var P = window.parent;
  if (!P || !P.document) { return; }          // hatch closed: stay silent

  // A rerun mounts a fresh copy of this script. Rather than bail out and hope
  // the previous instance is still healthy, the old one is torn down first:
  // its animation frame is cancelled, its listeners are dropped and - the part
  // that actually matters - its WebGL context is released. A browser allows
  // only a handful of live contexts, so leaking one per rerun would blank the
  // 3D core after a dozen interactions with nothing in the console about it.
  if (P.__nexusHud && typeof P.__nexusHud.destroy === 'function') {
    try { P.__nexusHud.destroy(); } catch (e) {}
  }

  var D = P.document;
  var reduce = false;
  try {
    reduce = P.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) { reduce = false; }

  var ACCENT = [0, 243, 255];

  /* ---- mouse state. Captured on the event, written on the frame. ------- */
  var mx = -9999, my = -9999, haveMouse = false, mouseDirty = false;
  D.addEventListener('mousemove', function (ev) {
    mx = ev.clientX; my = ev.clientY; haveMouse = true; mouseDirty = true;
  }, { passive: true });
  D.addEventListener('mouseleave', function () {
    haveMouse = false; mouseDirty = true;
  }, { passive: true });

  /* ---- the canvas -------------------------------------------------------- */
  var canvas = null, ctx = null, nodes = [], dpr = 1;

  function sizeCanvas() {
    if (!canvas) { return; }
    dpr = Math.min(P.devicePixelRatio || 1, 2);
    var w = P.innerWidth, h = P.innerHeight;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    if (ctx) { ctx.setTransform(dpr, 0, 0, dpr, 0, 0); }
  }

  function onCanvasResize() { sizeCanvas(); seedNodes(); }

  function seedNodes() {
    // Density by viewport area, hard-capped. Connection search is O(n^2), so
    // the cap is what keeps this off the CPU on a large monitor.
    var w = P.innerWidth, h = P.innerHeight;
    var count = Math.max(28, Math.min(72, Math.round((w * h) / 26000)));
    nodes = [];
    for (var i = 0; i < count; i++) {
      nodes.push({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.22,
        vy: (Math.random() - 0.5) * 0.22,
        r: 1 + Math.random() * 1.4
      });
    }
  }

  function buildCanvas() {
    var host = D.querySelector('.stApp') || D.body;
    if (!host) { return false; }
    canvas = D.createElement('canvas');
    canvas.className = 'nx-canvas';
    canvas.setAttribute('aria-hidden', 'true');
    canvas.dataset.born = String(Date.now());
    host.insertBefore(canvas, host.firstChild);
    ctx = canvas.getContext('2d');
    sizeCanvas();
    seedNodes();
    // Named, not anonymous: destroy() has to be able to take it off again, and
    // an anonymous handler here would leave one dead listener per rerun.
    P.addEventListener('resize', onCanvasResize, { passive: true });
    return true;
  }

  var LINK = 132, LINK2 = LINK * LINK;      // node-to-node reach, squared
  var PULL = 190, PULL2 = PULL * PULL;      // cursor reach, squared

  function drawNetwork() {
    if (!ctx) { return; }
    var w = P.innerWidth, h = P.innerHeight;
    ctx.clearRect(0, 0, w, h);

    var i, j, a, b, dx, dy, d2;
    for (i = 0; i < nodes.length; i++) {
      a = nodes[i];
      a.x += a.vx; a.y += a.vy;
      if (a.x < 0) { a.x = w; } else if (a.x > w) { a.x = 0; }
      if (a.y < 0) { a.y = h; } else if (a.y > h) { a.y = 0; }
    }

    // Each pair tested once, squared distances only - no Math.sqrt in here.
    ctx.lineWidth = 1;
    for (i = 0; i < nodes.length; i++) {
      a = nodes[i];
      for (j = i + 1; j < nodes.length; j++) {
        b = nodes[j];
        dx = a.x - b.x; dy = a.y - b.y;
        d2 = dx * dx + dy * dy;
        if (d2 < LINK2) {
          ctx.strokeStyle = 'rgba(' + ACCENT[0] + ',' + ACCENT[1] + ',' +
                            ACCENT[2] + ',' + (0.16 * (1 - d2 / LINK2)).toFixed(3) + ')';
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }

      // The cursor is just another node to link to, which is what makes the
      // field feel like it reacts rather than merely plays.
      if (haveMouse) {
        dx = a.x - mx; dy = a.y - my;
        d2 = dx * dx + dy * dy;
        if (d2 < PULL2) {
          ctx.strokeStyle = 'rgba(255,170,0,' +
                            (0.30 * (1 - d2 / PULL2)).toFixed(3) + ')';
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(mx, my);
          ctx.stroke();
        }
      }

      ctx.fillStyle = 'rgba(' + ACCENT[0] + ',' + ACCENT[1] + ',' + ACCENT[2] + ',0.55)';
      ctx.beginPath();
      ctx.arc(a.x, a.y, a.r, 0, 6.283185);
      ctx.fill();
    }
  }

  /* ---- audio ------------------------------------------------------------- */
  // Oscillators only - no files, no network, nothing to preload. Every voice
  // is built, scheduled and thrown away, and each one disconnects itself in
  // onended: an AudioNode that is never disconnected keeps its whole graph
  // alive, and at one blip per hover that leak would be measured in thousands.
  //
  // A browser refuses to start an AudioContext until the page has been
  // interacted with, so the context is created lazily on the first real
  // gesture rather than at load. Before that gesture every call here is a
  // silent no-op instead of an exception.
  var audio = { ctx: null, hum: null, enabled: true, lastTick: 0, level: 1 };

  function audioCtx() {
    if (!audio.enabled) { return null; }
    if (!audio.ctx) {
      var AC = P.AudioContext || P.webkitAudioContext;
      if (!AC) { return null; }
      try { audio.ctx = new AC(); } catch (e) { return null; }
    }
    if (audio.ctx.state === 'suspended') { audio.ctx.resume(); }
    return audio.ctx;
  }

  function voice(freq, startAt, duration, peak, type) {
    var ctx = audio.ctx;
    var osc = ctx.createOscillator();
    var gain = ctx.createGain();
    osc.type = type || 'square';
    osc.frequency.setValueAtTime(freq, startAt);
    // A short ramp in and an exponential tail out. A square wave switched on
    // at full gain clicks, and the click is louder than the note.
    gain.gain.setValueAtTime(0.0001, startAt);
    gain.gain.exponentialRampToValueAtTime(peak * audio.level, startAt + 0.006);
    gain.gain.exponentialRampToValueAtTime(0.0001, startAt + duration);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(startAt);
    osc.stop(startAt + duration + 0.02);
    osc.onended = function () {
      try { osc.disconnect(); gain.disconnect(); } catch (e) {}
    };
  }

  // A dry mechanical tick. Throttled: moving across a row of tabs fires
  // mouseover repeatedly, and an un-throttled blip per event is a machine gun.
  function sfxTick() {
    var ctx = audioCtx();
    if (!ctx) { return; }
    var now = ctx.currentTime;
    if (now - audio.lastTick < 0.06) { return; }
    audio.lastTick = now;
    voice(2100, now, 0.028, 0.018, 'square');
  }

  // Two tones a fifth apart, the second landing late enough to read as a
  // confirmation rather than a chord.
  function sfxLock() {
    var ctx = audioCtx();
    if (!ctx) { return; }
    var now = ctx.currentTime;
    voice(880, now, 0.10, 0.055, 'triangle');
    voice(1320, now + 0.075, 0.16, 0.05, 'triangle');
  }

  // The overdrive bed: a low tone under a slower one, breathing through an
  // LFO on the shared gain. Held open until stopped, so it is the one voice
  // that has to be cleaned up by hand.
  function humStart() {
    var ctx = audioCtx();
    if (!ctx || audio.hum) { return; }
    var gain = ctx.createGain();
    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.03 * audio.level,
                                          ctx.currentTime + 0.8);
    var a = ctx.createOscillator();
    a.type = 'sine';
    a.frequency.value = 54;
    var b = ctx.createOscillator();
    b.type = 'sine';
    b.frequency.value = 81.5;      // a fifth up, for a beat rather than a drone
    var lfo = ctx.createOscillator();
    lfo.type = 'sine';
    lfo.frequency.value = 0.7;
    var lfoGain = ctx.createGain();
    lfoGain.gain.value = 0.012;
    lfo.connect(lfoGain);
    lfoGain.connect(gain.gain);
    a.connect(gain);
    b.connect(gain);
    gain.connect(ctx.destination);
    a.start(); b.start(); lfo.start();
    audio.hum = { gain: gain, nodes: [a, b, lfo, lfoGain] };
  }

  function humStop() {
    if (!audio.hum || !audio.ctx) { return; }
    var ctx = audio.ctx;
    var held = audio.hum;
    audio.hum = null;
    try {
      held.gain.gain.cancelScheduledValues(ctx.currentTime);
      held.gain.gain.setValueAtTime(held.gain.gain.value || 0.03, ctx.currentTime);
      held.gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.5);
    } catch (e) {}
    P.setTimeout(function () {
      for (var i = 0; i < held.nodes.length; i++) {
        try { held.nodes[i].stop(); } catch (e) {}
        try { held.nodes[i].disconnect(); } catch (e) {}
      }
      try { held.gain.disconnect(); } catch (e) {}
    }, 620);
  }

  // The unlock gesture. Registered once, removed as soon as it fires.
  function unlockAudio() {
    audioCtx();
    flushVoice();          // speak anything held back before the first gesture
    D.removeEventListener('pointerdown', unlockAudio);
    D.removeEventListener('keydown', unlockAudio);
  }
  D.addEventListener('pointerdown', unlockAudio, { passive: true });
  D.addEventListener('keydown', unlockAudio, { passive: true });

  // What the machine says when an action is launched. Short by design: a line
  // that outlasts the click reads as a delay rather than as confirmation.
  var ACTION_LINES = [
    ["start hunting", "Scanning sectors."],
    ["purify batch", "Purifying the list."],
    ["launch dry run", "Rehearsal engaged. Nothing will be sent."],
    ["launch campaign", "Outreach sequence engaged."],
    ["draw the network", "Plotting the network."],
  ];

  function onActionSpeak(ev) {
    var el = ev.target && ev.target.closest ? ev.target.closest('button') : null;
    if (!el) { return; }
    // A Streamlit button with an icon renders that icon as a Material Symbols
    // ligature inside its own label, so innerText arrives with the icon name
    // on the first line and the caption on the last. Matching the raw string
    // matches the icon name instead of the button.
    var lines = (el.innerText || "").split(String.fromCharCode(10));
    var label = "";
    for (var n = lines.length - 1; n >= 0; n--) {
      if (lines[n].trim()) { label = lines[n].trim().toLowerCase(); break; }
    }
    if (!label) { return; }
    for (var i = 0; i < ACTION_LINES.length; i++) {
      // startsWith, not includes: "launch campaign" would otherwise also match
      // a button that merely mentions it, and the dry-run label starts with
      // the same word as the live one.
      if (label.indexOf(ACTION_LINES[i][0]) === 0) {
        say(ACTION_LINES[i][1], 0.95, 0.5);
        return;
      }
    }
  }

  D.addEventListener('click', onActionSpeak, true);

  // Hover blips, delegated from the document so controls that Streamlit
  // re-mounts on every rerun never need re-binding.
  function onHover(ev) {
    var el = ev.target;
    if (!el || !el.closest) { return; }
    if (el.closest('button, [data-testid="stTab"], [role="tab"], a')) {
      sfxTick();
    }
  }
  D.addEventListener('mouseover', onHover, { passive: true });

  /* ---- the WebGL core ---------------------------------------------------- */
  // Three.js is pulled from the CDN into the PARENT head, once. If the network
  // is not there the promise simply never resolves into a scene: the 2D
  // constellation, the visor and the whole interface carry on unchanged. The
  // 3D layer is the only thing that goes missing.
  var THREE_SRC = 'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js';
  var core = null;   // { renderer, scene, camera, canvas, globe, halo, dust }

  function loadThree(done) {
    if (P.THREE) { done(P.THREE); return; }
    var existing = D.querySelector('script[data-nexus-three]');
    if (existing) {
      existing.addEventListener('load', function () { done(P.THREE); });
      return;
    }
    var tag = D.createElement('script');
    tag.src = THREE_SRC;
    tag.async = true;
    tag.setAttribute('data-nexus-three', '1');
    tag.addEventListener('load', function () { done(P.THREE); });
    tag.addEventListener('error', function () { done(null); });
    (D.head || D.documentElement).appendChild(tag);
  }

  function buildCore(THREE) {
    if (!THREE) { return; }
    var canvas = D.createElement('canvas');
    canvas.className = 'nx-core';
    canvas.setAttribute('aria-hidden', 'true');
    D.body.appendChild(canvas);

    var renderer;
    try {
      renderer = new THREE.WebGLRenderer({
        canvas: canvas, antialias: false, alpha: true,
        powerPreference: 'low-power'
      });
    } catch (e) {
      canvas.remove();
      return;                      // no WebGL on this machine: stay 2D
    }
    renderer.setPixelRatio(Math.min(P.devicePixelRatio || 1, 1.75));
    renderer.setSize(P.innerWidth, P.innerHeight, false);
    renderer.setClearColor(0x000000, 0);

    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(
      52, P.innerWidth / P.innerHeight, 0.1, 100);
    camera.position.set(0, 0, 15.5);

    // The globe: a wireframe sphere, the "core" everything else orbits.
    var globe = new THREE.LineSegments(
      new THREE.WireframeGeometry(new THREE.SphereGeometry(5.6, 26, 18)),
      new THREE.LineBasicMaterial({
        color: 0x00f3ff, transparent: true, opacity: 0.20 })
    );
    scene.add(globe);

    // A tighter amber shell inside it, counter-rotating, so the core reads as
    // two systems turning against each other rather than one spinning ball.
    var halo = new THREE.LineSegments(
      new THREE.WireframeGeometry(new THREE.IcosahedronGeometry(3.9, 1)),
      new THREE.LineBasicMaterial({
        color: 0xffaa00, transparent: true, opacity: 0.26 })
    );
    scene.add(halo);

    // The data stream: points on a wide shell, drifting.
    var COUNT = 1400;
    var positions = new Float32Array(COUNT * 3);
    for (var i = 0; i < COUNT; i++) {
      var r = 8 + Math.random() * 13;
      var theta = Math.random() * Math.PI * 2;
      var phi = Math.acos(2 * Math.random() - 1);
      positions[i * 3]     = r * Math.sin(phi) * Math.cos(theta);
      positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta) * 0.55;
      positions[i * 3 + 2] = r * Math.cos(phi);
    }
    // Spherical coordinates are kept alongside the flat position buffer so
    // the stream can be pushed outward by advancing one number per particle
    // instead of recomputing a direction every frame.
    var radii = new Float32Array(COUNT);
    var thetas = new Float32Array(COUNT);
    var phis = new Float32Array(COUNT);
    for (var j = 0; j < COUNT; j++) {
      var px = positions[j * 3], py = positions[j * 3 + 1] / 0.55,
          pz = positions[j * 3 + 2];
      radii[j] = Math.sqrt(px * px + py * py + pz * pz);
      thetas[j] = Math.atan2(py, px);
      phis[j] = Math.acos(Math.max(-1, Math.min(1, pz / (radii[j] || 1))));
    }

    var dustGeo = new THREE.BufferGeometry();
    dustGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    var dust = new THREE.Points(dustGeo, new THREE.PointsMaterial({
      color: 0x00f3ff, size: 0.075, transparent: true, opacity: 0.55,
      sizeAttenuation: true
    }));
    scene.add(dust);

    core = { renderer: renderer, scene: scene, camera: camera, canvas: canvas,
             globe: globe, halo: halo, dust: dust, THREE: THREE,
             positions: positions, radii: radii, thetas: thetas, phis: phis,
             count: COUNT,
             calm: { globe: new THREE.Color(0x00f3ff),
                     halo: new THREE.Color(0xffaa00),
                     dust: new THREE.Color(0x00f3ff) },
             fire: { globe: new THREE.Color(0xff2e63),
                     halo: new THREE.Color(0xffaa00),
                     dust: new THREE.Color(0xff7a2e) } };
    sizeCore();
  }

  function sizeCore() {
    if (!core) { return; }
    core.camera.aspect = P.innerWidth / P.innerHeight;
    core.camera.updateProjectionMatrix();
    core.renderer.setSize(P.innerWidth, P.innerHeight, false);
  }

  /* ---- engine state ------------------------------------------------------ */
  // Python renders <div class="nx-state" data-state="running"> from inside the
  // live panel, which only exists on screen while a worker thread is alive.
  // Reading the DOM is therefore the honest source of truth: no polling the
  // backend, no guessing, and it self-clears when the fragment stops drawing.
  var active = false;
  var spin = 1;          // eased rotation multiplier, so speed changes glide
  var zoneCount = -1;    // -1 = no zone; 0+ = that many targets locked

  function readState() {
    var marker = D.querySelector('.nx-state[data-state="running"]');
    var now = !!marker;
    if (now !== active) {
      active = now;
      root.classList.toggle('nx-active', active);
      // The overdrive bed follows the engine, not the click that started it.
      if (active) { humStart(); } else { humStop(); }
    }

    // The count is read off the marker's dataset rather than counted in JS:
    // Python already knows how many leads fell inside the zone, and two
    // independent tallies would eventually disagree.
    var zone = D.querySelector('.nx-zone-state[data-count]');
    var next = zone ? parseInt(zone.dataset.count, 10) : -1;
    if (isNaN(next)) { next = -1; }
    if (next !== zoneCount) {
      var wasClear = zoneCount < 0;
      zoneCount = next;
      root.classList.toggle('nx-lock', zoneCount >= 0);
      fpsLast = 0;   // force the readouts to repaint on the next frame
      // Only on acquisition, never on release or on a recount.
      if (wasClear && zoneCount >= 0) {
        sfxLock();
        say(zoneCount + (zoneCount === 1 ? " target acquired."
                                         : " targets acquired."), 0.9, 0.5);
      }
    }
  }

  // Camera target, eased toward the cursor. The core leans, it does not snap.
  var camX = 0, camY = 0;

  var coreLast = 0;
  var CORE_INTERVAL = 33;   // ms: the core draws at ~30fps, the rest at 60
  var burst = 0;            // eased 0..1 outward speed of the data stream

  function renderCore(t) {
    if (!core) { return; }
    // A globe turning this slowly does not need 60fps. Throttling the draw
    // halves the most expensive call in the loop with nothing visible lost.
    if (t - coreLast < CORE_INTERVAL) { return; }
    coreLast = t;
    var tx = 0, ty = 0;
    if (haveMouse) {
      tx = (mx / P.innerWidth - 0.5) * 2.6;
      ty = (my / P.innerHeight - 0.5) * -1.7;
    }
    camX += (tx - camX) * 0.045;
    camY += (ty - camY) * 0.045;
    core.camera.position.x = camX;
    core.camera.position.y = camY;
    core.camera.lookAt(0, 0, 0);

    // Rotation is integrated rather than derived from absolute time: easing a
    // multiplier into t * k would make the whole scene jump backwards the
    // instant the speed changed. Adding a per-frame delta keeps it continuous.
    spin += ((active ? 3.4 : 1) - spin) * 0.03;
    var step = CORE_INTERVAL * spin;

    core.globe.rotation.y += step * 0.00006;
    core.globe.rotation.x = Math.sin(t * 0.00004) * 0.16;
    core.halo.rotation.y -= step * 0.00011;
    core.halo.rotation.z += step * 0.00005;
    core.dust.rotation.y += step * 0.000022;

    // Live fire: the core goes to alert colour and the stream fires outward.
    // Colours are eased rather than switched so the change reads as the system
    // spinning up, and the lerp is cheap enough to run unconditionally.
    var target = active ? core.fire : core.calm;
    core.globe.material.color.lerp(target.globe, 0.05);
    core.halo.material.color.lerp(target.halo, 0.05);
    core.dust.material.color.lerp(target.dust, 0.05);
    core.globe.material.opacity += ((active ? 0.42 : 0.20)
                                    - core.globe.material.opacity) * 0.05;

    if (active || burst > 0.002) {
      // Ease the outward speed in and out so stopping a campaign settles the
      // field instead of freezing it mid-flight.
      burst += ((active ? 1 : 0) - burst) * 0.04;
      var speed = burst * 0.26;
      var pos = core.positions, r = core.radii, th = core.thetas, ph = core.phis;
      for (var i = 0; i < core.count; i++) {
        r[i] += speed;
        if (r[i] > 26) { r[i] = 7.5; }        // respawn at the core
        var sinPhi = Math.sin(ph[i]);
        pos[i * 3]     = r[i] * sinPhi * Math.cos(th[i]);
        pos[i * 3 + 1] = r[i] * sinPhi * Math.sin(th[i]) * 0.55;
        pos[i * 3 + 2] = r[i] * Math.cos(ph[i]);
      }
      core.dust.geometry.attributes.position.needsUpdate = true;
    }

    core.renderer.render(core.scene, core.camera);
  }

  function disposeCore() {
    if (!core) { return; }
    try {
      core.scene.traverse(function (obj) {
        if (obj.geometry) { obj.geometry.dispose(); }
        if (obj.material) { obj.material.dispose(); }
      });
      core.renderer.dispose();
      // The part that actually frees the GPU-side context.
      if (core.renderer.forceContextLoss) { core.renderer.forceContextLoss(); }
      if (core.canvas && core.canvas.parentNode) { core.canvas.remove(); }
    } catch (e) {}
    core = null;
  }

  /* ---- voice ------------------------------------------------------------- */
  // SpeechSynthesis is native, so there is nothing to load and nothing to ship.
  // Two things make it awkward and both are handled here rather than hoped
  // over: the voice list is populated asynchronously on first access, and a
  // browser will not speak before the page has been interacted with.
  // Renamed from `voice` because that is already the oscillator builder
  // above; the collision silently broke every sound effect.
  var speech = { on: true, pick: null, queue: [], ready: false };

  function pickVoice() {
    if (!P.speechSynthesis) { return null; }
    var all = P.speechSynthesis.getVoices() || [];
    if (!all.length) { return null; }
    // Prefer a deeper, more synthetic English voice where one exists, and
    // fall back to whatever the platform offers rather than staying silent.
    var wanted = ["Google UK English Male", "Microsoft George",
                  "Microsoft Guy", "Daniel", "Alex", "Google US English"];
    for (var w = 0; w < wanted.length; w++) {
      for (var i = 0; i < all.length; i++) {
        if (all[i].name.indexOf(wanted[w]) === 0) { return all[i]; }
      }
    }
    for (var j = 0; j < all.length; j++) {
      if (/^en(-|_)/i.test(all[j].lang)) { return all[j]; }
    }
    return all[0];
  }

  function say(text, rate, pitch) {
    if (!speech.on || !P.speechSynthesis || !text) { return; }
    // Before the first gesture the browser silently drops the utterance, so
    // it is held and spoken once the page has been touched.
    if (!speech.ready) {
      speech.queue.push([text, rate, pitch]);
      if (speech.queue.length > 4) { speech.queue.shift(); }
      return;
    }
    try {
      var u = new P.SpeechSynthesisUtterance(text);
      speech.pick = speech.pick || pickVoice();
      if (speech.pick) { u.voice = speech.pick; }
      u.rate = rate || 0.88;      // slower reads as deliberate, not sluggish
      u.pitch = pitch || 0.55;    // low, for the machine register
      u.volume = 0.95;
      P.speechSynthesis.speak(u);
    } catch (e) {}
  }

  function flushVoice() {
    speech.ready = true;
    var held = speech.queue.splice(0, speech.queue.length);
    for (var i = 0; i < held.length; i++) {
      say(held[i][0], held[i][1], held[i][2]);
    }
  }

  if (P.speechSynthesis) {
    // getVoices() is empty on the first call in most browsers.
    P.speechSynthesis.addEventListener('voiceschanged', function () {
      speech.pick = pickVoice();
    });
  }

  function buildMute() {
    if (!visor || visor.querySelector('.mute')) { return; }
    var btn = D.createElement('div');
    btn.className = 'mute';
    btn.setAttribute('role', 'button');
    btn.setAttribute('tabindex', '0');
    btn.textContent = 'VOICE ON';
    function toggle() {
      speech.on = !speech.on;
      btn.textContent = speech.on ? 'VOICE ON' : 'VOICE OFF';
      btn.classList.toggle('off', !speech.on);
      if (!speech.on && P.speechSynthesis) {
        try { P.speechSynthesis.cancel(); } catch (e) {}
      } else {
        say("Voice online.");
      }
      try { P.localStorage.setItem('nexusVoice', speech.on ? '1' : '0'); } catch (e) {}
    }
    btn.addEventListener('click', toggle);
    btn.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggle(); }
    });
    // The choice survives a reload, because being greeted by a voice you
    // muted yesterday is the fastest way to hate a feature.
    try {
      if (P.localStorage.getItem('nexusVoice') === '0') {
        speech.on = false;
        btn.textContent = 'VOICE OFF';
        btn.classList.add('off');
      }
    } catch (e) {}
    visor.appendChild(btn);
  }

  /* ---- click pulse -------------------------------------------------------- */
  // Capture phase, so the ring appears even when a handler below stops the
  // event. Bounded: a rapid clicker could otherwise queue rings faster than
  // they retire, and each one is a composited layer.
  var MAX_RINGS = 6;
  var liveRings = 0;

  function onClickPulse(ev) {
    if (reduce || liveRings >= MAX_RINGS) { return; }
    var ring = D.createElement('div');
    ring.className = 'nx-pulse-ring';
    ring.style.left = ev.clientX + 'px';
    ring.style.top = ev.clientY + 'px';
    liveRings++;
    // Guarded: animationend and the fallback timeout can both fire for the
    // same ring, and a double decrement drifts the counter down until the cap
    // stops capping. Measured as a peak of seven against a limit of six.
    var finished = false;
    var done = function () {
      if (finished) { return; }
      finished = true;
      liveRings--;
      if (ring.parentNode) { ring.remove(); }
    };
    ring.addEventListener('animationend', done, { once: true });
    // A belt for the brace: if the animation never fires - a background tab,
    // a reduced-motion switch mid-flight - the node still goes.
    P.setTimeout(done, 900);
    D.body.appendChild(ring);
  }

  D.addEventListener('click', onClickPulse, true);

  /* ---- kill-feed --------------------------------------------------------- */
  // A WebSocket straight to the engine. Streamlit's rerun cycle is not in the
  // path at all: a line pushed from a campaign worker reaches this terminal in
  // the time it takes a loopback socket to deliver a frame.
  var feedEl = null, feedSock = null, feedRetry = 0, feedTimer = null;
  var FEED_MAX_LINES = 60;

  // The console lives at the end of the Streamlit column rather than on top
  // of it. That means Streamlit owns its parent, and a rerun that rebuilds the
  // column will carry the console out with it - so the node is kept and put
  // back rather than recreated, which is what preserves the scrollback.
  function feedHost() {
    return D.querySelector('.block-container') || D.body;
  }

  function buildFeed() {
    if (!feedEl) {
      feedEl = D.createElement('div');
      feedEl.id = 'kill-feed';
      feedEl.setAttribute('aria-hidden', 'true');
      var head = D.createElement('div');
      head.className = 'hd';
      head.innerHTML = '<span>NEXUS KILL-FEED</span>' +
                       '<span class="st">LINK DOWN</span>';
      feedEl.appendChild(head);
    }
    var host = feedHost();
    // Re-append only when it is genuinely detached or no longer last. Doing it
    // unconditionally would move the node on every observer pass, and moving a
    // node is a mutation, which would call the observer again.
    if (feedEl.parentNode !== host || host.lastElementChild !== feedEl) {
      host.appendChild(feedEl);
    }
  }

  function feedStatus(text) {
    if (!feedEl) { return; }
    var st = feedEl.querySelector('.hd .st');
    if (st) { st.textContent = text; }
  }

  function feedLine(entry) {
    if (!feedEl) { return; }
    var row = D.createElement('div');
    row.className = 'ln ' + (entry.level || 'info');
    row.innerHTML = '<span class="ts">' + entry.t + '</span> ' +
                    String(entry.text).replace(/[<>&]/g, function (c) {
                      return { '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c];
                    });
    // The rail is column-reverse, so the newest line goes in first and the
    // stack grows upward on its own - no scroll position to chase.
    feedEl.insertBefore(row, feedEl.firstChild);
    // Bounded. An all-night campaign would otherwise grow this node forever.
    var lines = feedEl.querySelectorAll('.ln');
    while (lines.length > FEED_MAX_LINES) {
      feedEl.removeChild(lines[lines.length - 1]);
      lines = feedEl.querySelectorAll('.ln');
    }
    feedEl.classList.add('live');
  }

  function connectFeed() {
    var marker = D.querySelector('.nx-link-state[data-port]');
    if (!marker || feedSock) { return; }
    var port = marker.dataset.port, token = marker.dataset.token;
    if (!port || !token) { return; }
    buildFeed();
    try {
      feedSock = new P.WebSocket('ws://127.0.0.1:' + port + '/?token=' +
                                 encodeURIComponent(token));
    } catch (e) { feedSock = null; return; }

    feedSock.onopen = function () {
      feedRetry = 0;
      feedStatus('LINK LIVE');
      feedEl.classList.add('live');
    };
    feedSock.onmessage = function (ev) {
      try { feedLine(JSON.parse(ev.data)); } catch (e) {}
    };
    feedSock.onclose = function () {
      feedSock = null;
      feedStatus('LINK DOWN');
      // Backoff, capped. A tight reconnect loop against a server that is not
      // coming back is worse than no feed at all.
      feedRetry = Math.min(feedRetry + 1, 6);
      feedTimer = P.setTimeout(connectFeed, 800 * feedRetry);
    };
    feedSock.onerror = function () { try { feedSock.close(); } catch (e) {} };
  }

  /* ---- ghost protocol ---------------------------------------------------- */
  var ghost = false;

  function readGhost() {
    var on = !!D.querySelector('.nx-ghost-state');
    if (on === ghost) { return; }
    ghost = on;
    root.classList.toggle('nx-ghost', ghost);
    if (core) {
      // The core loses its colour with everything else. The palette swap is
      // picked up by the easing already running in renderCore.
      core.calm.globe.setHex(ghost ? 0x9b111e : 0x00f3ff);
      core.calm.halo.setHex(ghost ? 0x5a0d14 : 0xffaa00);
      core.calm.dust.setHex(ghost ? 0x6b1520 : 0x00f3ff);
    }
    // Going dark means going quiet: the SFX drop to a whisper rather than off,
    // so the interface still answers but stops announcing itself.
    audio.level = ghost ? 0.25 : 1;
    if (ghost) {
      say("Ghost protocol engaged. Running dark.", 0.82, 0.42);
    } else {
      say("Ghost protocol disengaged.", 0.88, 0.55);
    }
  }

  /* ---- boot sequence ----------------------------------------------------- */
  // Runs once per page load. Streamlit reruns cannot replay it, because a
  // rerun remounts the component and the guard at the top of this file returns
  // before reaching here.
  var BOOT_LINES = [
    "NEXUS OS // KERNEL BOOT",
    "ESTABLISHING SATELLITE UPLINK",
    "ENCRYPTING CONNECTION",
    "READY."
  ];

  var bootEl = null;
  var bootTimers = [];

  function bootSequence() {
    if (reduce) { return; }        // motion is off: skip straight to the app
    bootEl = D.createElement('div');
    bootEl.className = 'nx-boot';
    bootEl.setAttribute('aria-hidden', 'true');
    var lines = D.createElement('div');
    lines.className = 'lines';
    bootEl.appendChild(lines);
    D.body.appendChild(bootEl);

    var shown = [];
    BOOT_LINES.forEach(function (line, i) {
      bootTimers.push(P.setTimeout(function () {
        var done = i === BOOT_LINES.length - 1;
        shown.push((done ? "<b>> " : "> ") + line + (done ? "</b>" : ""));
        lines.innerHTML = shown.join("\n") +
                          (done ? "" : '\n<span class="caret"></span>');
        sfxTick();
      }, 120 + i * 480));
    });

    // Snap away at 2.5s, gone from the DOM once the animation ends so it can
    // never sit invisibly over the interface.
    bootTimers.push(P.setTimeout(function () {
      if (!bootEl) { return; }
      bootEl.classList.add('done');
      bootTimers.push(P.setTimeout(removeBoot, 700));
      // Spoken after the panel snaps away, not over it. If the page has not
      // been touched yet the browser will refuse, so say() holds the line and
      // flushVoice speaks it on the first click instead.
      say("Welcome back, Core Architect. System operational.", 0.86, 0.45);
    }, 2500));
  }

  function removeBoot() {
    if (bootEl && bootEl.parentNode) { bootEl.remove(); }
    bootEl = null;
  }

  function buildCrt() {
    if (D.querySelector('.nx-crt')) { return; }
    var crt = D.createElement('div');
    crt.className = 'nx-crt';
    crt.setAttribute('aria-hidden', 'true');
    D.body.appendChild(crt);
  }

  /* ---- the visor --------------------------------------------------------- */
  var visor = null, readouts = {};

  function hex(n) {
    var out = [], chars = '0123456789ABCDEF';
    for (var i = 0; i < n; i++) {
      out.push(chars[(Math.random() * 16) | 0]);
    }
    return out.join('');
  }

  function hexBlock(rows) {
    var lines = [];
    for (var i = 0; i < rows; i++) {
      lines.push(hex(2) + ' ' + hex(2) + ' ' + hex(4));
    }
    return lines.join('\n');
  }

  function buildVisor() {
    if (D.querySelector('.nx-visor')) { return; }
    visor = D.createElement('div');
    visor.className = 'nx-visor';
    visor.setAttribute('aria-hidden', 'true');

    var spots = ['tl', 'tr', 'bl', 'br'];
    for (var i = 0; i < spots.length; i++) {
      var c = D.createElement('div');
      c.className = 'corner ' + spots[i];
      var r = D.createElement('div');
      r.className = 'readout';
      c.appendChild(r);
      visor.appendChild(c);
      readouts[spots[i]] = r;
    }

    var sides = ['left', 'right'];
    for (var j = 0; j < sides.length; j++) {
      var rail = D.createElement('div');
      rail.className = 'rail ' + sides[j];
      var stream = D.createElement('div');
      stream.className = 'stream';
      // Doubled so the -50% scroll loops seamlessly.
      var block = hexBlock(90);
      stream.textContent = block + '\n' + block;
      rail.appendChild(stream);
      visor.appendChild(rail);
    }

    D.body.appendChild(visor);
  }

  // Real numbers only. Every value below is measured from this page - frame
  // time from the loop, heap from performance.memory where the browser offers
  // it, node count from the constellation, viewport from the window. An
  // invented "MEM.ALLOC: 42%" would look identical and mean nothing.
  var fpsFrames = 0, fpsLast = 0, fpsValue = 0;

  function updateReadouts(t) {
    fpsFrames++;
    if (t - fpsLast >= 1000) {
      fpsValue = Math.round((fpsFrames * 1000) / (t - fpsLast));
      fpsFrames = 0;
      fpsLast = t;

      if (readouts.tl) {
        readouts.tl.textContent = zoneCount >= 0
          ? 'ZONE ACQUIRED\n' + zoneCount + ' TARGETS LOCKED\nFRAME   ' +
            fpsValue + ' FPS'
          : 'SYS.OP  ' + (active ? 'FIRING' : 'NORMAL') +
            '\nRENDER  ' + (core ? 'WEBGL' : '2D') +
            '\nFRAME   ' + fpsValue + ' FPS';
      }
      if (readouts.tr) {
        var mem = 'N/A';
        if (P.performance && P.performance.memory) {
          var m = P.performance.memory;
          mem = Math.round((m.usedJSHeapSize / m.jsHeapSizeLimit) * 100) + '%';
        }
        readouts.tr.textContent =
          'HEAP    ' + mem +
          '\nNODES   ' + nodes.length +
          '\nVIEW    ' + P.innerWidth + 'x' + P.innerHeight;
      }
      if (readouts.bl) {
        readouts.bl.textContent =
          'LINK    ' + (P.navigator && P.navigator.onLine ? 'ONLINE' : 'OFFLINE') +
          '\nCARDS   ' + D.querySelectorAll('[data-testid="stMetric"]').length +
          '\nCURSOR  ' + (haveMouse ? (mx | 0) + ',' + (my | 0) : 'IDLE');
      }
      if (readouts.br) {
        // SECURE reports the two things that actually decide it: whether the
        // operational feed is running over the token-gated loopback socket,
        // and whether Ghost is routing. Anything else here would be a badge
        // that says "secure" because someone typed it.
        var secure = ghost ? 'GHOST' : (feedSock ? 'LINK' : 'LOCAL');
        readouts.br.textContent =
          'NEXUS CORE\nSECURE  ' + secure +
          '\n' + new Date().toTimeString().slice(0, 8);
      }
    }
  }

  /* ---- spotlight bookkeeping -------------------------------------------- */
  // Rects are cached and refreshed on the events that can invalidate them,
  // never read inside the frame loop: getBoundingClientRect() forces layout,
  // and doing that for every card on every frame is exactly the kind of thing
  // that makes an effect like this cost more than it is worth.
  var spotCards = [];
  var rectsDirty = true;

  // Every panel that leans toward the cursor. The metric cards also carry a
  // spotlight layer; the data grids only tilt.
  var tiltCards = [];

  function refreshRects() {
    var cards = D.querySelectorAll('[data-testid="stMetric"]');
    spotCards = [];
    for (var i = 0; i < cards.length; i++) {
      var layer = cards[i].querySelector(':scope > .nx-spot');
      if (!layer) { continue; }
      var r = cards[i].getBoundingClientRect();
      spotCards.push({ el: cards[i], layer: layer, r: r });
    }

    var panels = D.querySelectorAll('[data-testid="stMetric"], '
                                    + '[data-testid="stDataFrame"]');
    tiltCards = [];
    for (var k = 0; k < panels.length; k++) {
      tiltCards.push({ el: panels[k], r: panels[k].getBoundingClientRect(),
                       tilted: false });
    }
    rectsDirty = false;
  }

  var SPOT_MARGIN = 220;   // how far outside a card the glow still reaches

  function updateSpots() {
    if (rectsDirty) { refreshRects(); }
    for (var i = 0; i < spotCards.length; i++) {
      var c = spotCards[i], r = c.r;
      var near = haveMouse &&
                 mx > r.left - SPOT_MARGIN && mx < r.right + SPOT_MARGIN &&
                 my > r.top - SPOT_MARGIN && my < r.bottom + SPOT_MARGIN;
      if (near) {
        c.layer.style.setProperty('--spot-x', (mx - r.left) + 'px');
        c.layer.style.setProperty('--spot-y', (my - r.top) + 'px');
        c.layer.style.setProperty('--spot-a', '1');
      } else {
        // Unconditional. A "skip if already dark" flag was tried and is not
        // worth it: refreshRects() rebuilds this array on every decorate(),
        // so the flag resets constantly and the branch never actually skips.
        // Two property writes on a handful of cards, only on a frame where
        // the cursor moved, is cheaper than the bookkeeping to avoid them.
        c.layer.style.setProperty('--spot-a', '0');
      }
    }
  }

  var TILT_MAX = 7;        // degrees at the far corner
  var TILT_LIFT = 18;      // px toward the viewer

  // Written once per frame from cached rects. A rect read here would force
  // layout on every panel on every mouse move, which is the difference
  // between a tilt that feels physical and one that feels like a stutter.
  function updateTilt() {
    for (var i = 0; i < tiltCards.length; i++) {
      var c = tiltCards[i], r = c.r;
      if (!r.width || !r.height) { continue; }
      var inside = haveMouse && mx >= r.left && mx <= r.right &&
                   my >= r.top && my <= r.bottom;
      if (inside) {
        // -1..1 from the centre, so the card leans away from the cursor on the
        // far edge and toward it on the near one.
        var dx = (mx - (r.left + r.width / 2)) / (r.width / 2);
        var dy = (my - (r.top + r.height / 2)) / (r.height / 2);
        if (!c.tilted) { c.el.classList.add('nx-tilting'); c.tilted = true; }
        c.el.style.transform =
          'perspective(1000px) rotateX(' + (-dy * TILT_MAX).toFixed(2) +
          'deg) rotateY(' + (dx * TILT_MAX).toFixed(2) +
          'deg) translateZ(' + TILT_LIFT + 'px)';
      } else if (c.tilted) {
        // Hand the card back to CSS, which carries the easing that makes the
        // return smooth rather than a snap.
        c.tilted = false;
        c.el.classList.remove('nx-tilting');
        c.el.style.transform = '';
      }
    }
  }

  function clearTilt() {
    for (var i = 0; i < tiltCards.length; i++) {
      tiltCards[i].el.classList.remove('nx-tilting');
      tiltCards[i].el.style.transform = '';
      tiltCards[i].tilted = false;
    }
  }

  P.addEventListener('scroll', function () { rectsDirty = true; }, { passive: true });
  P.addEventListener('resize', function () { rectsDirty = true; }, { passive: true });

  /* ---- per-card decoration ---------------------------------------------- */
  // Idempotent by construction: a card that already carries the marker is
  // skipped, so an observer that fires on every fragment tick cannot stack
  // duplicates.
  function decorate() {
    var cards = D.querySelectorAll('[data-testid="stMetric"]');
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      if (card.querySelector(':scope > .nx-spot') === null) {
        var spot = D.createElement('div');
        spot.className = 'nx-spot';
        card.insertBefore(spot, card.firstChild);
      }
      if (card.querySelector(':scope > .nx-rings') === null) {
        var rings = D.createElement('div');
        rings.className = 'nx-rings';
        card.insertBefore(rings, card.firstChild);
      }
    }
    rectsDirty = true;   // the card set or its geometry just changed

    // If Streamlit ever re-mounts .stApp the canvas goes with it, and the
    // background would vanish for the rest of the session with nothing in the
    // console to say why. Cheap to check, so it is checked.
    if (canvas && !D.body.contains(canvas)) {
      var host = D.querySelector('.stApp') || D.body;
      host.insertBefore(canvas, host.firstChild);
      sizeCanvas();
    }
  }

  // The activity log is redrawn wholesale by a 1s fragment, so each tick hands
  // us a brand-new node scrolled to the top. Pin it to the bottom unless the
  // reader has deliberately scrolled up inside this instance.
  function pinLogs() {
    var logs = D.querySelectorAll('.nx-log');
    for (var i = 0; i < logs.length; i++) {
      var el = logs[i];
      if (!el.dataset.nxPinned) {
        el.dataset.nxPinned = '1';
        el.addEventListener('scroll', function () {
          var gap = this.scrollHeight - this.scrollTop - this.clientHeight;
          this.dataset.nxPinned = gap < 40 ? '1' : '0';
        }, { passive: true });
      }
      if (el.dataset.nxPinned === '1' && el.scrollTop !== el.scrollHeight) {
        el.scrollTop = el.scrollHeight;
      }
    }
  }

  var pending = false;
  function scheduleDecorate() {
    if (pending) { return; }
    pending = true;
    P.requestAnimationFrame(function () {
      pending = false;
      decorate();
      pinLogs();
      readState();
      readGhost();
      connectFeed();
      if (feedEl) { buildFeed(); }   // Streamlit rebuilt the column; re-seat it
    });
  }

  /* ---- one loop for everything ------------------------------------------ */
  var root = D.documentElement;
  var running = true;

  var rafId = 0;

  function frame(t) {
    if (!running) { return; }
    if (mouseDirty) {
      // Written once per frame, never on the event itself.
      root.style.setProperty('--mouse-x', mx + 'px');
      root.style.setProperty('--mouse-y', my + 'px');
      updateSpots();
      updateTilt();
      mouseDirty = false;
    }
    drawNetwork();
    renderCore(t || 0);
    updateReadouts(t || 0);
    rafId = P.requestAnimationFrame(frame);
  }

  var observer = null;

  function onVisibility() {
    if (D.visibilityState === 'hidden') {
      running = false;
      if (rafId) { P.cancelAnimationFrame(rafId); rafId = 0; }
    } else if (!running) {
      running = true;
      rafId = P.requestAnimationFrame(frame);
    }
  }

  function onResize() {
    rectsDirty = true;
    sizeCore();
  }

  function destroy() {
    running = false;
    if (rafId) { P.cancelAnimationFrame(rafId); rafId = 0; }
    if (observer) { try { observer.disconnect(); } catch (e) {} observer = null; }
    D.removeEventListener('visibilitychange', onVisibility);
    P.removeEventListener('resize', onResize);
    P.removeEventListener('resize', onCanvasResize);
    disposeCore();
    // Audio first: a held oscillator survives every DOM teardown and would
    // keep humming over a page that no longer has a HUD.
    humStop();
    if (P.speechSynthesis) { try { P.speechSynthesis.cancel(); } catch (e) {} }
    speech.queue = [];
    if (feedTimer) { P.clearTimeout(feedTimer); feedTimer = null; }
    if (feedSock) {
      // Drop the handler first: onclose would otherwise schedule a reconnect
      // for a HUD that is being torn down.
      feedSock.onclose = null;
      try { feedSock.close(); } catch (e) {}
      feedSock = null;
    }
    if (feedEl && feedEl.parentNode) { feedEl.remove(); }
    feedEl = null;
    root.classList.remove('nx-ghost');
    D.removeEventListener('mouseover', onHover);
    D.removeEventListener('click', onClickPulse, true);
    D.removeEventListener('click', onActionSpeak, true);
    var strays = D.querySelectorAll('.nx-pulse-ring');
    for (var r = 0; r < strays.length; r++) { strays[r].remove(); }
    D.removeEventListener('pointerdown', unlockAudio);
    D.removeEventListener('keydown', unlockAudio);
    if (audio.ctx) {
      try { audio.ctx.close(); } catch (e) {}
      audio.ctx = null;
    }
    for (var b = 0; b < bootTimers.length; b++) { P.clearTimeout(bootTimers[b]); }
    bootTimers = [];
    removeBoot();
    var crt = D.querySelector('.nx-crt');
    if (crt) { crt.remove(); }
    clearTilt();
    root.classList.remove('nx-active');
    root.classList.remove('nx-lock');
    if (visor && visor.parentNode) { visor.remove(); }
    visor = null;
    if (canvas && canvas.parentNode) { canvas.remove(); }
    canvas = null; ctx = null;
    var leftovers = D.querySelectorAll('.nx-spot, .nx-rings');
    for (var i = 0; i < leftovers.length; i++) { leftovers[i].remove(); }
  }

  function start() {
    if (!buildCanvas()) { return; }
    buildCrt();
    bootSequence();
    buildVisor();
    buildMute();
    buildFeed();
    decorate();
    pinLogs();
    readState();
    readGhost();
    connectFeed();
    observer = new P.MutationObserver(scheduleDecorate);
    observer.observe(D.body, { childList: true, subtree: true });

    // A hidden tab should cost nothing - the frame is cancelled, not just
    // skipped, so the 3D core stops drawing entirely.
    D.addEventListener('visibilitychange', onVisibility);
    P.addEventListener('resize', onResize, { passive: true });

    loadThree(function (THREE) {
      if (THREE) { buildCore(THREE); }
    });

    rafId = P.requestAnimationFrame(frame);
    // Small introspection surface: lets the state of the runtime be inspected
    // from the console (or a test) without reaching into the closure.
    P.__nexusHud = {
      destroy: destroy,
      version: 3,
      state: function () {
        return {
          active: active,
          spin: +spin.toFixed(3),
          burst: +burst.toFixed(3),
          core: !!core,
          nodes: nodes.length,
          tiltPanels: tiltCards.length,
          tilted: tiltCards.filter(function (c) { return c.tilted; }).length,
          audio: audio.ctx ? audio.ctx.state : 'not started',
          audioLevel: audio.level,
          humming: !!audio.hum,
          ghost: ghost,
          voice: speech.on ? (speech.ready ? 'ready' : 'waiting for gesture')
                           : 'muted',
          feed: feedSock ? 'live' : 'down',
          feedLines: feedEl ? Math.max(0, feedEl.childNodes.length - 1) : 0,
          booting: !!bootEl,
          globeColor: core ? '#' + core.globe.material.color.getHexString() : null,
          globeY: core ? +core.globe.rotation.y.toFixed(4) : null
        };
      },
      // Lets the operator silence the console without touching the code.
      audio: function (on) {
        audio.enabled = on !== false;
        if (!audio.enabled) { humStop(); }
        return audio.enabled;
      }
    };
  }

  if (reduce) {
    // Motion is off: no canvas, no loop. The cards keep their static styling.
    return;
  }
  if (D.readyState === 'loading') {
    D.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
</script>
"""


def hud_runtime() -> None:
    """Mount the runtime HUD: node canvas, spotlight, rings, live re-attach.

    Rendered through a zero-size component so it has an iframe to run in; the
    wrapper is hidden by _CSS so it takes no space in the layout. Call it once,
    right after apply_theme().
    """
    components.html(_HUD_JS, height=0, width=0)



def header(title: str, subtitle: str) -> None:
    """Page title. A leading word before ':' is picked out in cyan."""
    mark, _, rest = title.partition(":")
    if rest:
        title_html = (f'<span class="nx-mark">{html.escape(mark)}:</span>'
                      f'{html.escape(rest)}')
    else:
        title_html = html.escape(title)
    st.markdown(
        f'<div class="nx-hero">{LOGO_SVG}{title_html}</div>'
        f'<p class="nx-sub">{html.escape(subtitle)}</p>',
        unsafe_allow_html=True,
    )


def system_state(running: bool) -> None:
    """Publish the engine state into the DOM for the runtime HUD to read.

    Rendered from inside the live panel, which is a 1s fragment that only
    exists on screen while a worker thread is alive. That makes the marker
    self-clearing: when the job ends the fragment stops drawing, the node
    disappears, and the HUD drops out of its active skin on the next frame.
    """
    st.markdown(
        f'<div class="nx-state" data-state="{"running" if running else "idle"}"></div>',
        unsafe_allow_html=True,
    )


def link_state(port: int | None, token: str) -> None:
    """Publish the kill-feed endpoint so the runtime can open the socket.

    The token travels through the DOM rather than the page source because it is
    minted per process: hard-coding it would mean it survived a restart, and a
    long-lived token on a loopback port is the thing worth avoiding here.
    """
    if not port or not token:
        return
    st.markdown(
        f'<div class="nx-link-state" data-port="{int(port)}" '
        f'data-token="{html.escape(token)}"></div>',
        unsafe_allow_html=True,
    )


def ghost_state(active: bool, has_proxy: bool = False) -> None:
    """Tell the runtime the system has gone dark, and how dark it really is."""
    if not active:
        return
    st.markdown(
        f'<div class="nx-ghost-state" data-proxy="{"1" if has_proxy else "0"}">'
        f'</div>',
        unsafe_allow_html=True,
    )


def zone_state(count: int | None) -> None:
    """Publish the acquired-zone size for the runtime HUD.

    ``None`` means no zone. Rendered wherever the radar is drawn, so it
    disappears with the tab and the HUD falls back to its normal readout.
    """
    if count is None:
        return
    st.markdown(
        f'<div class="nx-zone-state" data-count="{int(count)}"></div>',
        unsafe_allow_html=True,
    )


def status_badge(label: str, ok: bool = True) -> None:
    """The HUD status strip at the top of the page.

    ``ok`` drives colour and wording, so the badge is an instrument rather than
    an ornament: cyan when the core is ready to send, amber when it is not.
    """
    tone = "nx-badge" if ok else "nx-badge warn"
    st.markdown(
        f'<div class="{tone}"><span class="beacon"></span>'
        f'[ {html.escape(label)} ]</div>',
        unsafe_allow_html=True,
    )


def section(label: str) -> None:
    st.markdown(f'<div class="nx-section">{html.escape(label)}</div>',
                unsafe_allow_html=True)


def log_panel(lines: list[str], limit: int = 120) -> None:
    body = html.escape("\n".join(lines[-limit:]) or "Awaiting telemetry.")
    st.markdown(f'<div class="nx-log">{body}</div>', unsafe_allow_html=True)


def check_row(name: str, status: str, detail: str = "") -> None:
    """One green/amber/red line of the diagnostic checklist."""
    css = status if status in {"ok", "warn", "fail"} else "warn"
    detail_html = (f'<div class="detail">{html.escape(detail)}</div>' if detail else "")
    st.markdown(
        f'<div class="nx-check {css}"><div class="dot"></div><div class="body">'
        f'<div class="name">{html.escape(name)}</div>{detail_html}</div></div>',
        unsafe_allow_html=True,
    )


def wait_banner(title: str, subtitle: str, sending: bool = False) -> None:
    """The 'not frozen, just waiting' banner shown during gaps and breaks."""
    pulse = "nx-pulse sending" if sending else "nx-pulse"
    st.markdown(
        f'<div class="nx-wait"><div class="title"><span class="{pulse}"></span>'
        f'{html.escape(title)}</div>'
        f'<div class="sub">{html.escape(subtitle)}</div></div>',
        unsafe_allow_html=True,
    )
